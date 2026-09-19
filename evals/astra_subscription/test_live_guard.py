"""Offline checks for the evaluation guard; never read account credentials."""

import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest

from evals.astra_subscription.live_guard import LiveBudget, sanitized_usage


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"started_unix": time.time(), "requests": []}))
    return path


def request(url="https://chatgpt.com/backend-api/codex/responses", model="gpt-6-astra"):
    return httpx.Request("POST", url, json={"model": model, "input": []})


@pytest.mark.parametrize("url,model", [
    ("https://api.openai.com/v1/responses", "gpt-6-astra"),
    ("https://chatgpt.com.evil.invalid/backend-api/codex/responses", "gpt-6-astra"),
    ("http://chatgpt.com/backend-api/codex/responses", "gpt-6-astra"),
    ("https://chatgpt.com/backend-api/codex/responses", "other-model"),
])
def test_rejects_wrong_route_or_model_before_dispatch(ledger, url, model):
    with pytest.raises(RuntimeError):
        LiveBudget(ledger, "test").before(request(url, model))
    assert json.loads(ledger.read_text())["requests"] == []


@pytest.mark.parametrize("count,age", [(60, 0), (0, 3601)])
def test_shared_budget_never_resets(ledger, count, age):
    ledger.write_text(json.dumps({"started_unix": time.time() - age, "requests": [{}] * count}))
    with pytest.raises(RuntimeError, match="budget exhausted"):
        LiveBudget(ledger, "next scenario").before(request())
    assert len(json.loads(ledger.read_text())["requests"]) == count


def test_usage_is_numeric_allowlist():
    usage = sanitized_usage({
        "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
        "input_tokens_details": {"cached_tokens": 3, "attribution": [{"text": "private"}]},
        "opaque": "private", "output_tokens_details": {"reasoning_tokens": 1},
    })
    assert "private" not in json.dumps(usage)
    assert usage["input_tokens_details"] == {"cached_tokens": 3}


def test_stream_without_content_type_is_unchanged_and_sanitized(ledger, monkeypatch):
    import requests

    # Preserve global methods: install is deliberately process-scoped in live runs.
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    monkeypatch.setattr(httpx.AsyncClient, "send", httpx.AsyncClient.send)
    monkeypatch.setattr(requests.Session, "send", requests.Session.send)
    events = [
        {"type": "response.output_item.done", "item": {"type": "compaction", "encrypted_content": "opaque-test"}},
        {"type": "response.completed", "response": {"output": [], "usage": {"input_tokens": 9}}},
    ]
    wire = b"".join(b"data: " + json.dumps(x).encode() + b"\n\n" for x in events)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, stream=httpx.ByteStream(wire)))
    LiveBudget(ledger, "test").install()
    with httpx.Client(transport=transport) as client:
        with client.stream("POST", str(request().url), json={"model": "gpt-6-astra"}) as response:
            assert b"".join(response.iter_raw()) == wire
    item = json.loads(ledger.read_text())["requests"][0]
    assert item["terminal_event"] == "response.completed"
    assert item["checkpoint_sha256"] == [hashlib.sha256(b"opaque-test").hexdigest()]
    assert "opaque-test" not in ledger.read_text()


def test_fixture_has_twelve_facts_and_paired_history():
    from evals.astra_subscription.seed_compaction import fixture

    messages, facts = fixture()
    calls = [call["id"] for m in messages for call in m.get("tool_calls", [])]
    outputs = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert len(facts) == 12
    assert len(calls) == len(set(calls)) == 120
    assert calls == outputs
    assert len(messages) == 242


def test_frozen_receipt_classification_and_accounting():
    receipt = json.loads(Path(__file__).with_name("results-20260920.json").read_text())
    assert receipt["request_count"] == len(receipt["requests"]) == 59
    assert receipt["counter_lines"] == {"astra-proof-counter.txt": 1, "gateway-proof-counter.txt": 1}
    requests = {item["number"]: item for item in receipt["requests"]}
    for label, trial in receipt["trials"].items():
        session = receipt["sessions"][label]
        assert session["retained_facts"] == 12
        assert session["original_synthetic_prefix_preserved"]
        assert session["fresh_terminal_results"] == 1
        assert session["fresh_results_have_calls"] and session["fresh_call_ids_unique"]
        assert not session["historical_call_reexecution_observed"]
        assert trial["ordinary_instructions_unchanged"]
        if label != "native_1":
            wire = [requests[number] for number in trial["request_numbers"]]
            assert trial["provider_input_tokens_including_cache"] == sum(x["usage"]["input_tokens"] for x in wire)
            assert trial["output_tokens"] == sum(x["usage"]["output_tokens"] for x in wire)
            if label.startswith("native"):
                assert wire[0]["checkpoint_sha256"] == session["persisted_checkpoint_sha256"]
                assert wire[1]["input_checkpoint_sha256"] == wire[0]["checkpoint_sha256"]
        else:
            assert trial["summed_response_seconds"] is None
    assert not requests[44]["checkpoint_sha256"]  # Invalid fixture attempt is not native success.
    assert receipt["sessions"]["sequential_tools"]["fresh_tool_batch_sizes"] == [1, 1]
    assert receipt["sessions"]["independent_tools"]["fresh_tool_batch_sizes"] == [2]
