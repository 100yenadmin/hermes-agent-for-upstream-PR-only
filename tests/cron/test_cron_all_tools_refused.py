"""Refused turns fail through the canonical cron result, with no new job flag (#135544)."""
import asyncio
import json
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cron import scheduler as s, scheduler_authority
from gateway import session_cron


def _result(answer="done", **overrides):
    return {"final_response": answer, "completed": True, "tool_calls_attempted": 2,
            "tool_calls_executed": 0, "tool_calls_refused_names": ["terminal", "write_file"], **overrides}


def test_refused_turn_raises_with_tool_names():
    with pytest.raises(RuntimeError, match="2: terminal, write_file.*refused before it ran"):
        s._final_response_from_result(_result(), "job", "Job", MagicMock())


@pytest.mark.parametrize("result", [
    _result(tool_calls_executed=1), {"final_response": "done"},
    _result("[CRON_FAILURE]\nchild failed"),
    _result(tool_calls_attempted=0),
])
def test_success_and_declared_failure_are_unchanged(result):
    assert s._final_response_from_result(result, "job", "Job", MagicMock()) == result["final_response"]


def test_marker_import_compatibility():
    from cron.scheduler import CRON_FAILURE_MARKER, _cron_failure_marker_error
    assert CRON_FAILURE_MARKER == "[CRON_FAILURE]"
    assert _cron_failure_marker_error("[CRON_FAILURE]\nchild failed") == "child failed"
    assert _cron_failure_marker_error("quoted [CRON_FAILURE]") is None


@pytest.fixture
def run_result(tmp_path, monkeypatch):
    """Stub setup and the model, keeping run_job's verdict/exception tuple real."""
    monkeypatch.setattr(s, "_reload_dotenv_and_publish_delivery_target", lambda *_: None)
    monkeypatch.setattr(s, "_load_cron_job_config", lambda *_: SimpleNamespace(cfg={}, model="test"))
    monkeypatch.setattr(s, "_resolve_cron_agent_setup", lambda *_: s._CronAgentSetup(model="test"))
    monkeypatch.setattr(s, "_open_cron_session_db", lambda *_: None)
    agent = MagicMock(session_api_calls=1)
    monkeypatch.setattr(s, "_construct_cron_agent", lambda *_a, **_kw: agent)
    monkeypatch.setattr(s, "_teardown_cron_agent", lambda *_: None)
    result = _result()
    monkeypatch.setattr(s, "_run_agent_with_watchdog", lambda *_a, **_kw: result)
    owner = SimpleNamespace(db=SimpleNamespace(db_path=tmp_path / "state.db"))

    def run(job, **kwargs):
        token = session_cron._execution.set((owner, "session", job["id"], kwargs.get("execution_id")))
        try:
            return s.run_job(job, **kwargs)
        finally:
            session_cron._execution.reset(token)
    return result, run


@pytest.mark.parametrize("answer", ["done", "[SILENT]"])
def test_firer_books_failure_and_delivers_generic_notice(monkeypatch, run_result, answer):
    from cron.scheduler_failure_copy import classify_cron_failure_reason
    result, run = run_result
    result["final_response"] = answer
    monkeypatch.setattr("cron.scheduler_authority.run_canonical_job", run)
    monkeypatch.setattr(s, "save_job_output", lambda *_: "saved.md")
    marked, delivered = [], []
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **kw: marked.append((a, kw)))
    monkeypatch.setattr("cron.delivery_queue.enqueue",
                        lambda _eid, _job, content, **kw: delivered.append(content) or {"status": "pending"})
    assert s._run_one_job_body({"id": "refused", "name": "Report", "prompt": "report", "deliver": "telegram"})
    assert marked[0][0][1] is False
    error = marked[0][0][2]
    assert classify_cron_failure_reason(error) == "unknown"
    assert "refused before it ran" in error
    assert len(delivered) == 1 and "refused before it ran" in delivered[0]
    assert "model service" not in delivered[0] and "/login" not in delivered[0]


def test_canonical_gateway_result_is_a_failure(tmp_path, monkeypatch, run_result):
    from gateway import run as gateway_run
    from gateway.session_contract import SessionRef
    from hermes_cli import gateway_client
    _result_value, _ = run_result
    authority = SimpleNamespace(
        db=SimpleNamespace(db_path=tmp_path / "state.db"), pending_results={},
        sessions={"session": SimpleNamespace(source=SimpleNamespace(user_id="cron-owner"))})
    row = {"payload": {"text": ""}, "request_id": "cron:refused:fire",
           "principal_id": "cron-owner", "admission_id": "admission"}
    job = {"id": "refused", "prompt": "report"}
    policy = SimpleNamespace(request_json=json.dumps({"extra_prompt": None,
        "request_id": row["request_id"], "cron_job": job}))
    monkeypatch.setattr(gateway_run, "_profile_runtime_scope", lambda _: nullcontext())
    # The gateway executes the real run_job and records its envelope even when it raises.
    try:
        asyncio.run(session_cron.execute(authority, SessionRef("profile", "session"), row, policy))
    except RuntimeError as exc:
        assert "refused before it ran" in str(exc)
    envelope = authority.pending_results["admission"]["result"]

    class Peer:
        async def rpc(self, method, **params):
            if method == "cron.submit":
                return {"session_id": "session", "admission_id": "admission"}
            return {"status": "terminal", "job": job, "result": envelope["cron_result"],
                    "job_flags": envelope["cron_job_flags"]}
    @asynccontextmanager
    async def connected():
        yield Peer()
    monkeypatch.setattr(gateway_client, "connect_gateway", connected)
    observed = scheduler_authority.run_canonical_job(job, execution_id="fire")
    assert observed[0] is False
    assert "refused before it ran" in observed[3]
    assert envelope["failed"] is True and envelope["completed"] is False
    assert envelope["cron_job_flags"] == {}
