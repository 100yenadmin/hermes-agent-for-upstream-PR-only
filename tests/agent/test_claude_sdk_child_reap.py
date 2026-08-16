"""The SDK lane must not strand its Claude CLI child when disconnect() hangs.

`ClaudeAgentSdkSession.close()` bounds `client.disconnect()`. On timeout the
loop thread is stopped immediately afterwards, which kills the SDK transport's
own (shielded) SIGTERM/SIGKILL escalation mid-ladder — so the child survives
until interpreter exit. These tests pin the direct-kill fallback that covers it.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time

import pytest

from agent.transports import claude_agent_sdk_session as M


def _alive(pid: int) -> bool:
    """Live (non-zombie) process check."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            state = next(
                (line.split()[1] for line in fh if line.startswith("State:")), "Z"
            )
    except OSError:
        return False
    return state != "Z"


@pytest.fixture
def child():
    procs = []

    def _spawn(argv):
        proc = subprocess.Popen(argv)
        procs.append(proc)
        return proc

    yield _spawn
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_timeout_budget_exceeds_sdk_close_ladder():
    """The transport's own ladder is 5s lock + 5s grace + 5s TERM + 5s KILL."""
    assert M._SDK_DISCONNECT_TIMEOUT_S > 20.0


def test_cooperative_child_dies_on_sigterm(child):
    proc = child(["sleep", "300"])
    assert _alive(proc.pid)

    M._force_kill_sdk_child(proc.pid)

    proc.wait(timeout=5)
    assert not _alive(proc.pid)


def test_sigterm_ignoring_child_escalates_to_sigkill(child):
    proc = child([
        sys.executable,
        "-c",
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(300)",
    ])
    time.sleep(1.0)  # let the handler install

    started = time.monotonic()
    M._force_kill_sdk_child(proc.pid)
    elapsed = time.monotonic() - started

    proc.wait(timeout=5)
    assert not _alive(proc.pid)
    assert elapsed >= 4.5, "SIGKILL must follow a grace period, not fire immediately"


def test_never_signals_a_process_that_is_not_our_live_child(child):
    assert M._is_own_sdk_child(1) is False  # init — never ours

    proc = child(["sleep", "300"])
    proc.kill()
    proc.wait()
    assert M._is_own_sdk_child(proc.pid) is False  # reaped: pid may be reused

    assert M._force_kill_sdk_child(None) is None


def test_close_reaps_child_when_disconnect_hangs(child, monkeypatch):
    proc = child(["sleep", "300"])

    class _Process:
        pid = proc.pid

    class _Transport:
        _process = _Process()

    class _HangingClient:
        _transport = _Transport()

        async def disconnect(self):
            await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    session = M.ClaudeAgentSdkSession.__new__(M.ClaudeAgentSdkSession)
    session._closed = False
    session._reader_task = None
    session._client = _HangingClient()
    session._loop = loop
    session._loop_thread = thread

    monkeypatch.setattr(M, "_SDK_DISCONNECT_TIMEOUT_S", 1.0)
    assert M._sdk_child_pid(session._client) == proc.pid
    assert _alive(proc.pid)

    session.close()

    proc.wait(timeout=5)
    assert not _alive(proc.pid), "hung disconnect() stranded the CLI child"
    assert session._client is None


def test_child_pid_lookup_is_defensive():
    assert M._sdk_child_pid(object()) is None
    assert M._sdk_child_pid(None) is None
