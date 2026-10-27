"""``tool_decision``: exactly one structured allow/refuse event per tool call, never raw arguments."""

import hashlib
import json
import logging
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import model_tools
from agent import tool_decision
from agent.inline_tool_executors import emit_terminal_post_tool_call
from tools import approval, approval_prompt
from tools import approval_context

SECRET = "sk-live-do-not-leak-4242"
ARGS = {"path": f"/tmp/{SECRET}.txt", "nested": {"b": 2, "a": [1, SECRET]}}
IDS = dict(task_id="task-1", tool_call_id="call-1", session_id="sess-1", turn_id="turn-1")


def _digest(args):
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.fixture
def hooks(monkeypatch):
    """Capture tool_decision events; ``rec.pre`` is what pre_tool_call subscribers return."""
    from hermes_cli import lifecycle

    rec = SimpleNamespace(decisions=[], pre=[], fail=False)

    def fake_invoke(name, **kwargs):
        if name == "tool_decision":
            if rec.fail:
                raise RuntimeError("subscriber exploded")
            rec.decisions.append(kwargs)
        return list(rec.pre) if name == "pre_tool_call" else []

    monkeypatch.setattr(lifecycle, "invoke_hook", fake_invoke)
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name in ("tool_decision", "pre_tool_call"))
    monkeypatch.setattr(tool_decision, "_pending", OrderedDict())
    monkeypatch.setattr(tool_decision, "_emitted", OrderedDict())
    yield rec
    # Raw arguments never reach a subscriber, on any path.
    assert SECRET not in repr(rec.decisions)


@pytest.fixture
def cli_prompt(monkeypatch):
    """An interactive CLI approver whose answer the test sets; nothing pre-approved, no yolo."""
    answer = SimpleNamespace(choice="deny")
    for module in (approval, approval_context):
        monkeypatch.setattr(module, "get_current_session_key", lambda default="default": "test-session")
        monkeypatch.setattr(module, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "is_approved", lambda sk, pk: False)
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: None, raising=False)
    for module in (approval, approval_prompt):
        monkeypatch.setattr(module, "prompt_dangerous_approval", lambda *a, **k: answer.choice)
    return answer


def _call(name="read_file", args=None, dispatch=None):
    with patch("model_tools.registry.dispatch", side_effect=dispatch or (lambda *a, **k: '{"ok": true}')) as mock:
        result = model_tools.handle_function_call(name, dict(args or ARGS), **IDS)
    return result, mock


def test_plain_allowed_call_emits_one_allowed_event(hooks):
    _call()
    [event] = hooks.decisions
    assert event["verdict"] == "allowed"
    assert event["tool_name"] == "read_file"
    assert event["args_sha256"] == _digest(ARGS)
    assert {k: event[k] for k in IDS} == IDS
    assert event["initiator"] == "session"
    assert "args" not in event


def test_hook_blocked_call_emits_one_blocked_by_hook_event(hooks):
    hooks.pre = [{"action": "block", "message": "not today"}]
    result, dispatch = _call()
    dispatch.assert_not_called()
    assert "not today" in result
    [event] = hooks.decisions
    assert (event["verdict"], event["source"]) == ("blocked_by_hook", "pre_tool_call")


def test_user_denied_approval_emits_one_denied_by_user_event(hooks, cli_prompt):
    hooks.pre = [{"action": "approve", "message": "needs a human"}]
    cli_prompt.choice = "deny"
    result, dispatch = _call()
    dispatch.assert_not_called()
    assert "BLOCKED" in result
    [event] = hooks.decisions
    assert (event["verdict"], event["source"]) == ("denied_by_user", "approval_prompt")


def test_user_approved_call_emits_one_user_approved_event(hooks, cli_prompt):
    hooks.pre = [{"action": "approve", "message": "needs a human"}]
    cli_prompt.choice = "once"
    _, dispatch = _call()
    dispatch.assert_called_once()
    [event] = hooks.decisions
    assert (event["verdict"], event["source"]) == ("user_approved", "approval_prompt")


def test_auto_approved_call_emits_one_auto_approved_event(hooks, cli_prompt, monkeypatch):
    monkeypatch.setattr(approval, "is_approved", lambda sk, pk: True)  # session/permanent allowlist hit
    hooks.pre = [{"action": "approve", "message": "needs a human"}]
    _, dispatch = _call()
    dispatch.assert_called_once()
    [event] = hooks.decisions
    assert (event["verdict"], event["source"]) == ("auto_approved", "allowlist")


def test_deny_rule_inside_the_handler_emits_one_denied_by_rule_event(hooks, monkeypatch):
    monkeypatch.setattr(approval, "_match_user_deny_rule", lambda command: "git push --force*")

    def terminal_handler(name, args, **kwargs):
        verdict = approval.check_all_command_guards(args["command"], "local")
        return json.dumps({"error": verdict["message"]}) if not verdict["approved"] else '{"ok": true}'

    _call("terminal", {"command": f"git push --force {SECRET}"}, dispatch=terminal_handler)
    [event] = hooks.decisions
    assert (event["verdict"], event["source"]) == ("denied_by_rule", "approvals.deny")


def test_executor_owned_terminal_event_is_the_only_one(hooks, monkeypatch):
    """Sequential executor path: the inner post hook is suppressed, the executor emits once, and the
    verdict a gate noted during the suppressed dispatch is still the one reported."""
    monkeypatch.setattr(approval, "_match_user_deny_rule", lambda command: "rm*")

    def terminal_handler(name, args, **kwargs):
        approval.check_all_command_guards(args["command"], "local")
        return '{"error": "blocked"}'

    with model_tools.suppress_post_tool_call_hook():
        _call("terminal", {"command": "rm notes.txt"}, dispatch=terminal_handler)
    assert hooks.decisions == []
    agent = SimpleNamespace(session_id=IDS["session_id"], _current_turn_id=IDS["turn_id"], _current_api_request_id="")
    emit_terminal_post_tool_call(agent, function_name="terminal", function_args={"command": "rm notes.txt"},
                                 result='{"error": "blocked"}', effective_task_id=IDS["task_id"],
                                 tool_call_id=IDS["tool_call_id"])
    [event] = hooks.decisions
    assert event["verdict"] == "denied_by_rule"
    assert tool_decision._pending == {}


def _agent():
    return SimpleNamespace(session_id=IDS["session_id"], _current_turn_id=IDS["turn_id"], _current_api_request_id="")


def _terminal_emit(**outcome):
    emit_terminal_post_tool_call(_agent(), function_name="write_file", function_args={"path": "a"}, result="r",
                                 effective_task_id=IDS["task_id"], tool_call_id=IDS["tool_call_id"], **outcome)


def test_a_second_terminal_emission_for_the_same_call_is_dropped(hooks):
    """A worker finishing after its timeout result was published must not report the call twice."""
    _terminal_emit(status="timeout", error_type="tool_timeout")
    _terminal_emit()
    [event] = hooks.decisions
    assert event["verdict"] == "allowed"


def test_block_inside_a_suppressed_dispatch_reaches_the_outer_emission(hooks):
    """Sequential path: an ACP edit denial inside handle_function_call is reported by the executor's
    single terminal emission, which carries no blocked status of its own."""
    with patch("acp_adapter.edit_approval.maybe_require_edit_approval", return_value='{"error": "rejected"}'), \
            model_tools.suppress_post_tool_call_hook():
        _, dispatch = _call("write_file", {"path": "a", "content": SECRET})
    dispatch.assert_not_called()
    assert hooks.decisions == []
    _terminal_emit()
    [event] = hooks.decisions
    assert (event["verdict"], event["source"]) == ("denied_by_user", "acp_edit_approval")


def test_notes_made_while_deferred_are_dropped(hooks):
    tokens = approval_context.set_current_observability_context(tool_call_id="call-1", session_id="sess-1")
    try:
        with tool_decision.notes_deferred():
            tool_decision.note("denied_by_user", "approval_prompt")
        assert tool_decision._pending == {}
        tool_decision.note("user_approved", "approval_prompt")
    finally:
        approval_context.reset_current_observability_context(tokens)
    assert tool_decision._pending == {("sess-1", "call-1"): ("user_approved", "approval_prompt")}


def test_call_skipped_by_interrupt_emits_no_decision(hooks):
    agent = SimpleNamespace(session_id="sess-1", _current_turn_id="turn-1", _current_api_request_id="")
    emit_terminal_post_tool_call(agent, function_name="read_file", function_args={}, result="skipped",
                                 effective_task_id="task-1", tool_call_id="call-9", status="cancelled",
                                 error_type="user_interrupt")
    assert hooks.decisions == []


def test_failing_subscriber_never_breaks_the_call(hooks, caplog):
    hooks.fail = True
    with caplog.at_level(logging.WARNING, logger="agent.tool_decision"):
        result, dispatch = _call()
    dispatch.assert_called_once()
    assert json.loads(result) == {"ok": True}
    [record] = [r for r in caplog.records if r.name == "agent.tool_decision"]
    assert record.exc_info is not None


def test_no_subscriber_records_nothing(monkeypatch):
    from hermes_cli import lifecycle

    monkeypatch.setattr(lifecycle, "has_hook", lambda name: False)
    monkeypatch.setattr(tool_decision, "_pending", OrderedDict())
    tokens = approval_context.set_current_observability_context(tool_call_id="call-1", session_id="sess-1")
    try:
        tool_decision.note("denied_by_user", "approval_prompt")
    finally:
        approval_context.reset_current_observability_context(tokens)
    assert tool_decision._pending == {}


def test_a_refusal_outranks_an_earlier_approval_for_the_same_call():
    merged = tool_decision._merge(("auto_approved", "allowlist"), ("denied_by_user", "approval_prompt"))
    assert merged == ("denied_by_user", "approval_prompt")
    assert tool_decision._merge(merged, ("user_approved", "approval_prompt")) == merged
    assert tool_decision._merge(("auto_approved", "allowlist"), ("user_approved", "approval_prompt"))[0] == "user_approved"
