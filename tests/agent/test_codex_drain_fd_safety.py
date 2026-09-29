"""Regressions for the post-terminal Codex drain closing its stream from a stranger thread.

``run_codex_stream`` drains the SSE stream on a ``codex-post-terminal-drain`` daemon thread after
``final`` is assembled. When the drain budget expired, the owner thread used to ``close()`` the raw
stream and the Relay-managed wrapper while the drain thread was still blocked in ``SSL_read`` — the
FD-recycle race of #29507: ``close()`` releases the FD, the kernel recycles the integer into the next
``open()``, and the drain thread's TLS layer flushes a record into that unrelated file (it clobbered
a SQLite header).

The rules pinned here (object granularity, no network, no real sockets):

* on timeout the owner thread only ``shutdown(SHUT_RDWR)``s the socket, never ``close()``s;
* the drain thread closes raw + managed stream exactly once, after its iteration ends;
* the owner's ``finally`` never closes a handed-off stream; every other path is unchanged.
"""
from __future__ import annotations

import logging
import socket as _socket
import threading
from types import SimpleNamespace

import pytest

import agent.codex_runtime as codex_runtime
from agent import relay_llm

_DRAIN_THREAD = "codex-post-terminal-drain"


class _FakeSocket:
    """Records shutdown/close/settimeout calls with the calling thread's name; touches no FD."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def settimeout(self, _value):
        self.calls.append(("settimeout", threading.current_thread().name))

    def shutdown(self, how):
        assert how == _socket.SHUT_RDWR
        self.calls.append(("shutdown", threading.current_thread().name))

    def close(self):  # pragma: no cover — must never run; asserted below
        self.calls.append(("close", threading.current_thread().name))

    def names(self):
        return [name for name, _thread in self.calls]


class _FakeNetworkStream:
    def __init__(self, sock):
        self._socket = sock

    def get_extra_info(self, key):
        return self._socket if key == "socket" else None


class _CloseRecorder:
    def __init__(self):
        self.closes: list[str] = []
        self.closed = threading.Event()

    def close(self):
        self.closes.append(threading.current_thread().name)
        self.closed.set()


class _HeldOpenRawStream(_CloseRecorder):
    """Yields a completed response, then blocks (like an SSL_read) until ``release`` is set."""

    def __init__(self, release: threading.Event, sock: _FakeSocket | None):
        super().__init__()
        self._release = release
        self.blocked = threading.Event()
        extensions = {"network_stream": _FakeNetworkStream(sock)} if sock is not None else {}
        self.response = SimpleNamespace(extensions=extensions)
        message = SimpleNamespace(type="message", status="completed",
                                  content=[SimpleNamespace(type="output_text", text="All done.")])
        usage = SimpleNamespace(input_tokens=10, output_tokens=6, total_tokens=16)
        self._events = [
            SimpleNamespace(type="response.output_item.done", item=message),
            SimpleNamespace(type="response.completed",
                            response=SimpleNamespace(status="completed", usage=usage, id="resp_drain_fd")),
        ]

    def __iter__(self):
        yield from self._events
        self.blocked.set()
        assert self._release.wait(5.0), "test never released the blocked drain"


class _ManagedWrapper(_CloseRecorder):
    """Stand-in for Relay's ``ManagedLlmStream``: a distinct object wrapping the raw stream."""

    final_response = None
    instances: list["_ManagedWrapper"] = []

    def __init__(self, request, stream_factory, *, on_stream_created=None, **_kwargs):
        super().__init__()
        self.raw = stream_factory(request)
        on_stream_created(self.raw)
        self._iter = iter(self.raw)
        _ManagedWrapper.instances.append(self)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iter)


class _FakeAgent:
    provider = "openai-codex"
    base_url = "https://example.invalid/backend-api/codex"
    model = "gpt-5-codex"
    session_id = "test-session"
    is_subagent = False
    _fallback_index = 0
    show_commentary = False
    interim_assistant_callback = None
    _current_api_request_id = None
    _interrupt_requested = False

    def _fire_stream_delta(self, _text): pass
    def _fire_reasoning_delta(self, *_a): pass
    def _fire_streamed_codex_commentary(self, *_a): pass
    def _touch_activity(self, _label): pass
    def _client_log_context(self): return "provider=test"
    def _abort_request_openai_client(self, *_a, **_k): pass


def _run(monkeypatch, *, budget: float, release: threading.Event, sock: _FakeSocket | None):
    _ManagedWrapper.instances.clear()
    raw = _HeldOpenRawStream(release, sock)
    client = SimpleNamespace(base_url="https://example.invalid/backend-api/codex",
                             responses=SimpleNamespace(create=lambda **_kw: raw))
    monkeypatch.setattr(relay_llm, "stream", _ManagedWrapper)
    monkeypatch.setattr(codex_runtime, "_stream_drain_timeout", lambda: budget)
    final = codex_runtime.run_codex_stream(
        _FakeAgent(), {"model": "gpt-5-codex", "input": [{"role": "user", "content": "Ping"}], "store": False},
        client=client,
    )
    assert final.status == "completed" and final.id == "resp_drain_fd"
    (wrapper,) = _ManagedWrapper.instances
    return raw, wrapper


def _drain_threads():
    return [t for t in threading.enumerate() if t.name == _DRAIN_THREAD]


def _join_drain_threads():
    for thread in _drain_threads():
        thread.join(5.0)
        assert not thread.is_alive()


@pytest.fixture(autouse=True)
def _no_lingering_drain_threads():
    yield
    _join_drain_threads()


def test_timeout_shuts_socket_down_and_hands_close_to_drain_thread(monkeypatch):
    release, sock = threading.Event(), _FakeSocket()
    raw, wrapper = _run(monkeypatch, budget=0.05, release=release, sock=sock)
    try:
        assert raw.blocked.is_set()
        # The owner thread closed nothing while the iteration is blocked and woke the reader FD-safely.
        assert raw.closes == [] and wrapper.closes == [], (
            "close() from the owner thread while the drain thread iterates is the #29507 FD-recycle race")
        assert sock.names() == ["settimeout", "shutdown"]
        assert {thread for _name, thread in sock.calls} == {threading.current_thread().name}
    finally:
        release.set()
    _join_drain_threads()
    assert raw.closes == [_DRAIN_THREAD]
    assert wrapper.closes == [_DRAIN_THREAD]
    assert "close" not in sock.names()


def test_drain_finishing_within_budget_closes_once_on_owner_thread(monkeypatch):
    release, sock = threading.Event(), _FakeSocket()
    release.set()  # the provider closes right after the terminal event
    raw, wrapper = _run(monkeypatch, budget=5.0, release=release, sock=sock)
    _join_drain_threads()
    assert sock.calls == []
    assert wrapper.closes == [threading.current_thread().name]
    assert raw.closes == []  # unchanged: the managed wrapper owns the raw stream on this path


def test_zero_budget_skips_the_drain_and_closes_on_owner_thread(monkeypatch):
    release, sock = threading.Event(), _FakeSocket()
    started = []
    real_thread = threading.Thread

    def _spy_thread(*args, **kwargs):
        started.append(kwargs.get("name"))
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(codex_runtime.threading, "Thread", _spy_thread)
    try:
        raw, wrapper = _run(monkeypatch, budget=0.0, release=release, sock=sock)
    finally:
        release.set()
    assert _DRAIN_THREAD not in started
    assert sock.calls == []
    assert wrapper.closes == [threading.current_thread().name]
    assert raw.closes == []


def test_no_socket_found_still_hands_off_and_warns(monkeypatch, caplog):
    release = threading.Event()
    with caplog.at_level(logging.WARNING, logger="agent.codex_runtime"):
        raw, wrapper = _run(monkeypatch, budget=0.05, release=release, sock=None)
    try:
        assert raw.closes == [] and wrapper.closes == []
        assert "stream remained open" in caplog.text
        assert "drain thread will close" in caplog.text
    finally:
        release.set()
    _join_drain_threads()
    assert raw.closes == [_DRAIN_THREAD]
    assert wrapper.closes == [_DRAIN_THREAD]


def test_drain_ending_as_timeout_fires_closes_each_stream_exactly_once(monkeypatch):
    budget = 0.02
    for _ in range(50):
        release, sock = threading.Event(), _FakeSocket()
        timer = threading.Timer(budget, release.set)
        timer.start()
        try:
            raw, wrapper = _run(monkeypatch, budget=budget, release=release, sock=sock)
        finally:
            timer.join(5.0)
            release.set()
        _join_drain_threads()
        assert len(wrapper.closes) == 1
        owner = threading.current_thread().name
        # Branch on the socket, not on who closed: a shutdown means the owner handed off, so ONLY the drain
        # thread may close; no shutdown means the drain finished first and ONLY the owner may close.
        if "shutdown" in sock.names():
            assert sock.names() == ["settimeout", "shutdown"]
            assert wrapper.closes == [_DRAIN_THREAD]
            assert raw.closes == [_DRAIN_THREAD]
        else:
            assert wrapper.closes == [owner]
            assert raw.closes == []
        assert "close" not in sock.names()


class _FaithfulWrapper(_ManagedWrapper):
    """Like Relay's real wrapper: ``close()`` closes the raw stream it owns and marks its response closed."""

    def close(self):
        super().close()
        self.raw.close()
        self.raw.response.is_closed = True


def test_wrapper_that_closes_its_raw_stream_is_not_closed_twice(monkeypatch):
    release, sock = threading.Event(), _FakeSocket()
    _ManagedWrapper.instances.clear()
    raw = _HeldOpenRawStream(release, sock)
    raw.response.is_closed = False
    client = SimpleNamespace(base_url="https://example.invalid/backend-api/codex",
                             responses=SimpleNamespace(create=lambda **_kw: raw))
    monkeypatch.setattr(relay_llm, "stream", _FaithfulWrapper)
    monkeypatch.setattr(codex_runtime, "_stream_drain_timeout", lambda: 0.05)
    try:
        final = codex_runtime.run_codex_stream(
            _FakeAgent(), {"model": "gpt-5-codex", "input": [{"role": "user", "content": "Ping"}], "store": False},
            client=client,
        )
        assert final.status == "completed"
        assert raw.closes == []
    finally:
        release.set()
    _join_drain_threads()
    (wrapper,) = _ManagedWrapper.instances
    assert wrapper.closes == [_DRAIN_THREAD]
    # The wrapper closed the raw stream on the drain thread; the drain thread must not close it again.
    assert raw.closes == [_DRAIN_THREAD]
    assert "close" not in sock.names()
