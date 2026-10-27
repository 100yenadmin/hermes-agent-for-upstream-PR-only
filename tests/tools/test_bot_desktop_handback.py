"""Opt-in handback wakes only the most recent screen-driving conversation, once."""

import asyncio
import contextvars
import json
from types import SimpleNamespace

import pytest

from gateway import run as gateway_run, session_context, wake
from hermes_constants import get_hermes_home
from tools.bot_desktop import lease, runtime
from tools.computer_use import tool


@pytest.fixture
def rig(monkeypatch):
    # The behavioral regressions also run on the base before the helper exists.
    try:
        from tools.bot_desktop import handback
    except ImportError:
        handback = None
    if handback is not None:
        monkeypatch.setattr(handback, "_drivers", {})
        monkeypatch.setattr(handback, "_subscribed", False)
    monkeypatch.setattr(lease, "_listeners", [])
    monkeypatch.setattr(runtime, "ensure_started_for_tool", lambda: None)
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    monkeypatch.setattr(tool, "_new_backend", lambda mode: tool._NoopBackend())
    tool.reset_backend_for_tests()
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.yaml"
    config.write_text(json.dumps({"bot_desktop": {"resume_on_handback": True}}))
    clock = SimpleNamespace(now=100.0)
    if handback is not None:
        monkeypatch.setattr(handback, "monotonic", lambda: clock.now)
    runner = SimpleNamespace(_running=True, _gateway_loop=SimpleNamespace(is_running=lambda: True))
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    scheduled, calls, contexts = [], [], []
    ambient = contextvars.ContextVar("handback_test_ambient", default="empty")
    ambient.set("tool")

    def schedule(coro, loop, **kwargs):
        assert loop is runner._gateway_loop
        contexts.append(ambient.get())
        scheduled.append(coro)

    async def observe(runner, route, text, still_pending, *, producer):
        calls.append(SimpleNamespace(route=route, text=text, pending=still_pending(), producer=producer))

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", schedule)
    monkeypatch.setattr(wake, "wake_route", observe)

    def bind(name="A", **kwargs):
        session_context.set_session_vars(platform="local", chat_id="chat-" + name, chat_type="dm",
            thread_id="thread-" + name, user_id="user-" + name, user_name="name-" + name,
            scope_id="scope-" + name, session_key="route-" + name, session_id="parent-" + name,
            cron_session="", **kwargs)

    async def drain():
        while scheduled:
            await scheduled.pop(0)

    bind()
    yield SimpleNamespace(handback=handback, config=config, runner=runner, clock=clock,
                          scheduled=scheduled, calls=calls, contexts=contexts, bind=bind,
                          drain=lambda: asyncio.run(drain()))
    for coro in scheduled:
        coro.close()
    session_context.clear_session_vars([])
    tool.reset_backend_for_tests()


def _act():
    assert json.loads(tool.handle_computer_use({"action": "list_apps"}))["apps"] == []


def _hand_back():
    lease.acquire("v")
    lease.release("v")


def test_admitted_computer_action_wakes_once_with_captured_route_and_empty_context(rig):
    _act()
    rig.bind("other")  # A later unrelated context cannot change the screen driver's route.
    lease.acquire("v")
    lease.release("stale-viewer")
    lease.release(unless_human=True)
    assert not rig.scheduled
    lease.release("v")
    lease.release("v")
    _hand_back()  # No intervening screen action: the record was already consumed.
    assert len(rig.scheduled) == 1
    assert rig.contexts == ["empty"]
    rig.drain()
    call, = rig.calls
    assert call.route == {"platform": "local", "chat_id": "chat-A", "chat_type": "dm",
                          "thread_id": "thread-A", "user_id": "user-A", "user_name": "name-A",
                          "scope_id": "scope-A", "session_key": "route-A", "parent_session_id": "parent-A"}
    assert call.producer == "bot-screen-handback" and call.pending
    assert call.text == (
        "[Hermes gateway notice, not typed by the user: the person handed the Bot Screen back to you. "
        "Re-capture the screen before acting. If your earlier request in this conversation is not "
        "finished, continue it without asking them to confirm and do not repeat steps already done; "
        "otherwise say briefly that you have control again.]"
    )


@pytest.mark.parametrize("gate", ["off", "unset", "no-action", "cron", "no-key", "no-runner", "stopped", "loop"])
def test_ineligible_actions_never_wake(rig, monkeypatch, gate):
    changes = {
        "off": lambda: rig.config.write_text(json.dumps({"bot_desktop": {"resume_on_handback": False}})),
        "unset": lambda: rig.config.write_text("{}"),
        "no-action": lambda: None,
        "cron": lambda: session_context.set_session_vars(session_key="route-A", cron_session="1"),
        "no-key": session_context.set_session_vars,
        "no-runner": lambda: monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: None),
        "stopped": lambda: setattr(rig.runner, "_running", False),
        "loop": lambda: setattr(rig.runner, "_gateway_loop", None),
    }
    changes[gate]()
    if gate != "no-action":
        _act()
    _hand_back()
    assert not rig.scheduled


def test_last_driver_wins(rig):
    _act()
    rig.bind("B")
    _act()
    _hand_back()
    assert len(rig.scheduled) == 1
    rig.drain()
    assert [call.route["session_key"] for call in rig.calls] == ["route-B"]


@pytest.mark.parametrize("superseded", ["action", "human"])
def test_pending_wake_is_cancelled_when_control_is_used_again(rig, superseded):
    _act()
    _hand_back()
    if superseded == "action":
        _act()
    else:
        lease.acquire("new-viewer")
    rig.drain()
    assert len(rig.calls) == 1 and not rig.calls[0].pending


def test_old_driver_does_not_wake(rig):
    _act()
    rig.clock.now += 60 * 60 + 1
    _hand_back()
    assert not rig.scheduled


@pytest.mark.parametrize("local", [True, False])
def test_browser_fence_records_only_the_local_bot_desktop_driver(rig, local):
    from tools.browser_session import run_fenced

    info = {"session_name": "fixture", "features": {"local": local}}
    assert run_fenced(info, lambda: {"success": True}) == {"success": True}
    _hand_back()
    assert len(rig.scheduled) == int(local)
    rig.drain()
    if local:
        assert rig.calls[0].route["session_key"] == "route-A"


def test_listener_failure_never_escapes_the_lease_writer(rig, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("fixture scheduling unavailable")

    _act()
    monkeypatch.setattr(wake, "wake_route", fail)
    with caplog.at_level("DEBUG", logger="tools.bot_desktop.handback"):
        _hand_back()
    assert lease.get().holder == lease.AGENT
    assert "handback" in caplog.text.lower()
