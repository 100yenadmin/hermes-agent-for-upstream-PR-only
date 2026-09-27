"""Regression coverage for persist overrides on repaired user rows (#124731)."""

from types import SimpleNamespace

from agent.agent_runtime_helpers import _merge_consecutive_users
from agent.turn_context import reanchor_current_turn_user_idx
from agent.turn_finalizer import finalize_turn
from hermes_state import SessionDB
from run_agent import AIAgent


RESTORED = "please deploy the restored build"
USER_API = "[03:00] can you run the smoke tests"
USER_CLEAN = "can you run the smoke tests"


def _merged_messages():
    messages = [
        {"role": "assistant", "content": "Earlier reply."},
        {"role": "user", "content": RESTORED},
        {"role": "user", "content": USER_API},
    ]
    messages, repairs = _merge_consecutive_users(messages)
    assert repairs == 1
    return messages, reanchor_current_turn_user_idx(messages, USER_API)


class _FinalizerAgent:
    def __init__(self):
        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = self.session_output_tokens = 0
        self.session_cache_read_tokens = self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = self.session_prompt_tokens = 0
        self.session_completion_tokens = self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = True
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self._persist_user_message_idx = None
        self._persist_user_message_override = None
        self._persist_user_message_timestamp = None

    def _handle_max_iterations(self, *_args):
        raise AssertionError("not expected")

    def _noop(self, *_args, **_kwargs):
        pass

    _emit_status = _safe_print = _save_trajectory = _cleanup_task_resources = _noop
    _drop_trailing_empty_response_scaffolding = _persist_session = _noop

    def _apply_persist_user_message_override(self, messages):
        AIAgent._apply_persist_user_message_override(self, messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    clear_interrupt = _sync_external_memory_for_turn = _noop


def test_finalize_turn_override_preserves_restored_user_prefix(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    messages, current_idx = _merged_messages()
    agent = _FinalizerAgent()
    agent._persist_user_message_idx = current_idx
    agent._persist_user_message_override = USER_CLEAN

    result = finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message=USER_API,
        original_user_message=USER_CLEAN,
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    assert result["messages"][current_idx]["content"] == f"{RESTORED}\n\n{USER_CLEAN}"


def test_replay_heal_override_preserves_restored_user_prefix(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "sess-replay-heal"
    db.create_session(session_id=session_id, source="cli")
    try:
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            session_id=session_id,
        )
        agent._session_db_created = True
        messages, current_idx = _merged_messages()
        agent._persist_user_message_idx = current_idx
        agent._persist_user_message_override = USER_CLEAN
        agent._session_row_replay_pending = session_id

        agent._flush_messages_to_session_db(messages, None)

        stored = db.get_messages_as_conversation(session_id)
        user_rows = [msg for msg in stored if msg.get("role") == "user"]
        assert user_rows[-1]["content"] == f"{RESTORED}\n\n{USER_CLEAN}"
    finally:
        db.close()


def test_plain_user_override_keeps_existing_replacement_behavior():
    agent = SimpleNamespace(
        _persist_user_message_idx=0,
        _persist_user_message_override=USER_CLEAN,
        _persist_user_message_timestamp=None,
        _persist_user_message_platform_id=None,
    )
    messages = [{"role": "user", "content": USER_API}]

    AIAgent._apply_persist_user_message_override(agent, messages)

    assert messages[0]["content"] == USER_CLEAN


def test_merged_user_override_replaces_whole_row_when_tail_no_longer_matches():
    messages, current_idx = _merged_messages()
    messages[current_idx]["content"] = "a context engine rewrote this row"
    agent = SimpleNamespace(
        _persist_user_message_idx=current_idx,
        _persist_user_message_override=USER_CLEAN,
        _persist_user_message_timestamp=None,
        _persist_user_message_platform_id=None,
    )

    AIAgent._apply_persist_user_message_override(agent, messages)

    assert messages[current_idx]["content"] == USER_CLEAN
