"""Contract tests for the public plugin subagent lifecycle API."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.subagent_lifecycle import (
    SubagentLaunchRequest,
    SubagentLifecycleError,
    SubagentLifecycleService,
    SubagentState,
    bind_subagent_parent,
    get_active_subagent_parent,
)


class FakeChild:
    def __init__(self, ident="sa-test"):
        self._subagent_id = ident
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self.provider = "test"
        self.model = "test-model"
        self.interrupted = False
        self.interrupt_kind = None
        self.interrupt_message = None
        self.tool_reason = None
        self.steering = []

    def steer(self, text):
        self.steering.append(text)
        return True

    def interrupt(self, _reason):
        self.interrupted = True
        self.interrupt_kind = "soft"

    def hard_interrupt(self, reason, *, tool_reason=None):
        self.interrupted = True
        self.interrupt_kind = "hard"
        self.interrupt_message = reason
        self.tool_reason = tool_reason


@pytest.fixture
def lifecycle(monkeypatch):
    parent = SimpleNamespace(session_id="parent-1", enabled_toolsets=["file"])
    counter = iter(range(1000))

    def build(**_kwargs):
        return FakeChild(f"sa-{next(counter)}")

    def run(_index, _goal, child, _parent):
        for _ in range(20):
            if child.interrupted:
                return {
                    "status": "interrupted",
                    "summary": None,
                    "api_calls": 0,
                    "duration_seconds": 0,
                }
            time.sleep(0.002)
        return {
            "status": "completed",
            "summary": "safe summary",
            "api_calls": 1,
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", build)
    monkeypatch.setattr("tools.delegate_tool._run_single_child", run)
    return SubagentLifecycleService(lambda: parent)






def test_cancel_is_cooperative_and_forged_handle_is_unknown(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="x"))
    assert lifecycle.cancel(handle, reason="test").accepted
    terminal = lifecycle.wait(handle, timeout_seconds=1)
    assert terminal.state is SubagentState.CANCELLED
    forged = handle.__class__(**{**handle.to_dict(), "capability": "forged"})
    assert lifecycle.status(forged).state is SubagentState.UNKNOWN
    assert lifecycle.result(forged).error_classification == "UNKNOWN_HANDLE"
    other_parent = SimpleNamespace(session_id="different-parent")
    other_service = SubagentLifecycleService(lambda: other_parent)
    assert other_service.status(handle).state is SubagentState.UNKNOWN


def test_cancel_uses_explicit_hard_interrupt(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="x"))
    record = lifecycle._record(handle)
    assert record is not None and record.agent is not None

    assert lifecycle.cancel(handle, reason="explicit user cancel").accepted

    assert record.agent.interrupt_kind == "hard"
    assert "explicit user cancel" in record.agent.interrupt_message
    assert record.agent.tool_reason == "subagent cancellation requested"
    lifecycle.wait(handle, timeout_seconds=1)








def test_public_lifecycle_runs_host_aggregation(monkeypatch):
    memory = Mock()
    parent = SimpleNamespace(
        session_id="parent-aggregate",
        enabled_toolsets=["file"],
        _memory_manager=memory,
        _current_turn_id="turn-1",
        session_estimated_cost_usd=1.0,
        session_cost_source="none",
        session_cost_status="unknown",
    )
    child = FakeChild("sa-aggregate")
    child.session_id = "child-session"
    hook = Mock()

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "task_index": 0,
            "status": "completed",
            "summary": "aggregated",
            "api_calls": 1,
            "duration_seconds": 0.25,
            "_child_role": "leaf",
            "_child_cost_usd": 2.5,
        },
    )
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", hook)

    service = SubagentLifecycleService(lambda: parent)
    handle = service.launch(SubagentLaunchRequest(goal="aggregate me"))
    assert service.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED

    memory.on_delegation.assert_called_once_with(
        task="aggregate me", result="aggregated", child_session_id="child-session"
    )
    hook.assert_called_once_with(
        "subagent_stop",
        parent_session_id="parent-aggregate",
        parent_turn_id="turn-1",
        child_session_id="child-session",
        child_role="leaf",
        child_summary="aggregated",
        child_status="completed",
        # Redacted tool history rides the shared finalization pipeline
        # (#62011/#72403); empty here because the fabricated result carries
        # no tool_trace.
        tool_call_history=[],
        duration_ms=250,
    )
    assert parent.session_estimated_cost_usd == 3.5
    assert parent.session_cost_source == "subagent"
    assert parent.session_cost_status == "estimated"




def test_agent_turn_binds_and_clears_lifecycle_parent(monkeypatch):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    observed = []

    def run_conversation(parent, *_args, **_kwargs):
        observed.append(get_active_subagent_parent())
        return {"final_response": "ok"}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", run_conversation)

    assert agent.run_conversation("hello") == {"final_response": "ok"}
    assert observed == [agent]
    assert get_active_subagent_parent() is None


@pytest.fixture
def steer_lifecycle(monkeypatch):
    from agent.subagent_lifecycle import _REGISTRY
    from tools.delegate_tool_registry import _register_subagent, _unregister_subagent

    child = FakeChild("sa-steer-contract")
    started, finish = threading.Event(), threading.Event()
    service = SubagentLifecycleService(lambda: SimpleNamespace(session_id="parent-steer", enabled_toolsets=["file"]))

    def run(_index, _goal, child, _parent):
        _register_subagent({"subagent_id": child._subagent_id, "agent": child})
        try:
            started.set()
            assert finish.wait(5)
            return {"status": "interrupted" if child.interrupted else "completed", "summary": "done"}
        finally:
            _unregister_subagent(child._subagent_id, agent=child)

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr("tools.delegate_tool._run_single_child", run)
    yield service, child, started, finish
    finish.set()
    record = _REGISTRY.records.get(child._subagent_id)
    if record is not None:
        record.future.result(timeout=5)


@pytest.mark.parametrize("wire", [False, True])
def test_steer_queues_to_running_child_and_rejects_invalid_calls(steer_lifecycle, wire):
    from hermes_cli.plugin_host_wire import decode, encode

    service, child, started, finish = steer_lifecycle
    request = SubagentLaunchRequest(goal="review", allowed_toolsets=("file",))
    handle = service.launch(decode(encode(request)) if wire else request)
    assert started.wait(2)
    handle = decode(encode(handle)) if wire else handle
    assert service.status(handle).state is SubagentState.RUNNING
    assert service.reconnect(handle).connected
    assert service.steer(handle, "focus on correctness") is True
    assert child.steering == ["focus on correctness"]
    assert service.steer(handle, "") is False
    assert service.steer(handle, "   ") is False
    other = SubagentLifecycleService(lambda: SimpleNamespace(session_id="foreign"))
    assert other.steer(handle, "foreign") is False
    forged = {**(dict(handle) if wire else handle.to_dict()), "capability": "forged"}
    assert service.steer(forged, "forged") is False
    finish.set()
    assert service.wait(handle, timeout_seconds=2).state is SubagentState.SUCCEEDED
    assert service.result(handle).summary == "done"
    assert service.steer(handle, "too late") is False
    assert child.steering == ["focus on correctness"]


def test_wire_handle_can_cancel_a_running_child(steer_lifecycle):
    from hermes_cli.plugin_host_wire import decode, encode

    service, child, started, finish = steer_lifecycle
    handle = decode(encode(service.launch(SubagentLaunchRequest(goal="cancel"))))
    assert started.wait(2)
    assert service.cancel(handle, reason="cancel wire child").accepted
    assert child.interrupted
    finish.set()
    assert service.wait(handle, timeout_seconds=2).state is SubagentState.CANCELLED


@pytest.mark.parametrize("handle", [{}, {"subagent_id": "unknown"}, 42])
def test_malformed_handles_keep_unknown_results(lifecycle, handle):
    assert lifecycle.status(handle).diagnostic == "UNKNOWN_HANDLE"
    assert lifecycle.wait(handle).diagnostic == "UNKNOWN_HANDLE"
    assert lifecycle.cancel(handle, reason="test").unknown_handle
    assert lifecycle.result(handle).error_classification == "UNKNOWN_HANDLE"
    assert lifecycle.reconnect(handle).diagnostic == "RECONNECT_UNAVAILABLE"
    assert lifecycle.steer(handle, "test") is False


@pytest.mark.parametrize("launch_request, message", [
    ({"goal": "review", "future_field": True}, "future_field"),
    (42, "request must be a SubagentLaunchRequest"),
])
def test_mapping_request_errors_are_explicit(lifecycle, launch_request, message):
    with pytest.raises(SubagentLifecycleError, match=message):
        lifecycle.launch(launch_request)
