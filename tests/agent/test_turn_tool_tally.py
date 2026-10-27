"""Cron refusal evidence comes from real turns, including middleware replacements (#135544)."""
from unittest.mock import MagicMock

import pytest

from tests.agent.test_run_agent import _mock_response, _mock_tool_call
from tests.agent.test_turn_user_intervened import _PROBE, _loop_agent, _run


@pytest.mark.parametrize("policy", ["plugin", "middleware", "allowed"])
def test_turn_counts_attempts_and_real_dispatch(monkeypatch, policy):
    agent = _loop_agent()
    dispatch = MagicMock(return_value="probe ok")
    monkeypatch.setattr("model_tools.handle_function_call", dispatch)
    if policy == "plugin":
        monkeypatch.setattr("hermes_cli.plugins._dispatch_pre_tool_call_hooks",
                            lambda *_a, **_k: ("HTTP 401 invalid API key", None))
    elif policy == "middleware":
        from hermes_cli.plugins import get_plugin_manager
        monkeypatch.setattr(get_plugin_manager(), "_middleware",
                            {"tool_request": [], "tool_execution": [lambda **kw: "refused"]})
    replies = MagicMock(side_effect=[
        _mock_response(content=None, finish_reason="tool_calls", tool_calls=[_mock_tool_call(name=_PROBE)]),
        _mock_response(content="done"),
    ])
    result = _run(agent, replies)
    assert result["completed"] is True
    assert result["tool_calls_attempted"] == 1
    assert result["tool_calls_executed"] == (1 if policy == "allowed" else 0)
    assert result["tool_calls_refused_names"] == ([] if policy == "allowed" else [_PROBE])
    assert dispatch.call_count == (1 if policy == "allowed" else 0)
    # Cached agents start each turn clean, including a text-only turn after a refusal.
    next_result = _run(agent, lambda _: _mock_response(content="hello"))
    assert not any(key.startswith("tool_calls_") for key in next_result)


def test_text_only_turn_has_no_tally():
    result = _run(_loop_agent(), lambda _: _mock_response(content="hello"))
    assert result["completed"] is True
    assert not any(key.startswith("tool_calls_") for key in result)
