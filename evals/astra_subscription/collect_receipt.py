"""Emit allowlisted aggregates from the isolated synthetic run (no raw payloads).

The caller redirects nothing: --output names the generated sanitized receipt.
Opaque checkpoint bytes are compared in memory and represented only by hashes.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evals.astra_subscription.live_guard import sanitized_usage
from evals.astra_subscription.seed_compaction import fixture


def fact_count(text, expected):
    try:
        answer, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    except (ValueError, TypeError):
        return 0
    return sum(answer.get(key) == value for key, value in expected.items()) if isinstance(answer, dict) else 0


def session_receipt(db, session_id, expected):
    rows = [dict(row) for row in db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,))]
    tools = [row for row in rows if row["role"] == "tool" and row["tool_name"] == "terminal"]
    checkpoints = []
    for row in rows:
        for item in json.loads(row["codex_reasoning_items"] or "[]"):
            if isinstance(item, dict) and item.get("type") == "compaction":
                checkpoints.append(hashlib.sha256(str(item.get("encrypted_content", "")).encode()).hexdigest())
    final = next((row["content"] or "" for row in reversed(rows)
                  if row["role"] == "assistant" and not row["tool_calls"]), "")
    ids = [row["tool_call_id"] for row in tools]
    recorded_calls = {call["id"] for row in rows for call in json.loads(row["tool_calls"] or "[]")}
    fresh_batches = [sum(call["id"] in ids for call in json.loads(row["tool_calls"] or "[]")) for row in rows]
    usage = [dict(row) for row in db.execute(
        "SELECT api_call_count,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens FROM session_model_usage WHERE session_id=?",
        (session_id,))]
    planted, _ = fixture()
    prefix_matches = len(rows) >= len(planted) and all(
        row["role"] == message["role"] and row["content"] == message["content"]
        and row["tool_call_id"] == message.get("tool_call_id")
        and json.loads(row["tool_calls"] or "[]") == message.get("tool_calls", [])
        for row, message in zip(rows, planted)
    )
    return {
        "durable_rows": len(rows), "archived_rows": sum(row["active"] == 0 for row in rows),
        "retained_facts": fact_count(final, expected) if session_id.startswith("astra-eval-") else None,
        "fresh_tool_batch_sizes": [size for size in fresh_batches if size],
        "fresh_terminal_results": len(tools), "fresh_call_ids_unique": len(ids) == len(set(ids)),
        "fresh_results_have_calls": all(call_id in recorded_calls for call_id in ids),
        "historical_call_reexecution_observed": any(call_id in {f"c{i}" for i in range(120)} for call_id in ids),
        "original_synthetic_prefix_preserved": prefix_matches if session_id.startswith("astra-eval-") else None,
        "persisted_checkpoint_sha256": checkpoints, "session_usage_rows": usage,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.runtime.name.startswith("astra-subscription-runtime."):
        raise RuntimeError("Only the isolated synthetic runtime may be summarized")
    ledger = json.loads(args.ledger.read_text())
    requests = []
    for item in ledger["requests"]:
        receipt = {key: item.get(key) for key in (
            "number", "scenario", "method", "path", "model", "reasoning", "http_status", "error_class",
            "terminal_event", "total_elapsed_seconds", "context_management", "instructions_sha256",
            "checkpoint_sha256", "input_checkpoint_sha256",
        )}
        receipt["input_type_counts"] = dict(Counter(item.get("input_types") or []))
        receipt["output_type_counts"] = dict(Counter(item.get("output_types") or []))
        receipt["usage"] = sanitized_usage(item.get("usage"))
        requests.append(receipt)
    db = sqlite3.connect(f"file:{args.runtime / 'state.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    expected = fixture()[1]
    sessions = {}
    for arm in ("native", "local"):
        for trial in (1, 2):
            label = f"{arm}_{trial}"
            sessions[label] = session_receipt(db, f"astra-eval-{arm}-{trial}", expected)
        chain = args.runtime / f"gateway-{arm}-3.json"
        if chain.exists():
            identity = json.loads(chain.read_text())
            if identity.get("session_id"):
                sessions[f"{arm}_3_gateway"] = session_receipt(db, identity["session_id"], expected)
    for label, session_id in {
        "cli_baseline": "20260920_021531_056e02",
        "independent_tools": "20260920_022359_d0e15f",
        "sequential_tools": "20260920_023407_db8436",
    }.items():
        sessions[label] = session_receipt(db, session_id, expected)
    db.close()
    trial_numbers = {
        "native_1": [16, 18, 19], "local_1": [21, 23, 24, 25],
        "native_2": [39, 41, 42], "local_2": [33, 35, 36, 37],
        "native_3_gateway": [46, 48, 49], "local_3_gateway": [51, 53, 54, 55],
    }
    trials = {}
    for label, numbers in trial_numbers.items():
        selected = [item for item in requests if item["number"] in numbers]
        usage_rows = sessions.get(label, {}).get("session_usage_rows", [])
        main = [item for item in selected if sum(item["input_type_counts"].values()) > 1]
        timings = [item["total_elapsed_seconds"] for item in selected]
        trials[label] = {
            "request_numbers": numbers,
            "provider_input_tokens_including_cache": sum(
                row["input_tokens"] + row["cache_read_tokens"] + row["cache_write_tokens"] for row in usage_rows),
            "output_tokens": sum(row["output_tokens"] for row in usage_rows),
            "provider_responses": sum(row["api_call_count"] for row in usage_rows),
            "summed_response_seconds": sum(timings) if len(timings) == len(numbers) and all(
                isinstance(t, (float, int)) for t in timings) else None,
            "ordinary_instructions_unchanged": len({item["instructions_sha256"] for item in main}) == 1,
            "first_input_items": sum(main[0]["input_type_counts"].values()) if main else None,
            "resumed_input_items": sum(main[1]["input_type_counts"].values()) if len(main) > 1 else None,
        }
    result = {
        "suite": "Astra subscription-first compatibility v1", "claim_class": "advisory",
        "control_sha": "00570550f37e9082676955d50f65c7d9ba846cc9",
        "advanced_offline_sha": "d3ae318d37de4ad1199ab385cfc8e67f6ce3c4aa",
        "request_count": len(requests), "request_limit": 60,
        "elapsed_to_last_dispatch_seconds": max(x.get("started_unix", ledger["started_unix"])
                                                for x in ledger["requests"]) - ledger["started_unix"],
        "requests": requests, "sessions": sessions, "trials": trials,
        "counter_lines": {name: len((args.runtime / "work" / name).read_text().splitlines())
                          for name in ("astra-proof-counter.txt", "gateway-proof-counter.txt")},
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"request_count": len(requests), "sessions": sessions, "counter_lines": result["counter_lines"]}))


if __name__ == "__main__":
    main()
