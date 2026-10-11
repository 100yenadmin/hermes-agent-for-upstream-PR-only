#!/usr/bin/env python3
"""Measure two forced compactions of public, seeded synthetic conversation data."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import random
import socket
import sys
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _round(call_id, name, args, content):
    return [{"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args),
    }}]}, {"role": "tool", "tool_call_id": call_id, "tool_name": name, "content": content}]


def _progress(label):
    return [row for i in range(8) for row in (
        {"role": "user", "content": f"Record {label} progress {i}."},
        {"role": "assistant", "content": f"Progress {i} recorded."},
    )]


def _conversation(seed, stack):
    from agent import skill_commands as sc
    from agent.prompt_builder import steer_user_row
    from tools import skill_usage
    from tools.terminal_tool import _YIELDED_NOTE

    rng = random.Random(seed)
    facts = {name: f"FACT_{name.upper()}_{rng.randrange(10**8):08d}" for name in (
        "user", "long_user_end", "tool_middle", "assistant", "skill", "background", "steer", "anchor", "tail", "second_user",
    )}
    facts["background"] = f"proc_{rng.getrandbits(48):012x}"
    facts["anchor"] = f"docs/synthetic-{rng.randrange(10**8):08d}.txt"
    skill = "synthetic-bench-skill"
    stack.enter_context(patch.object(sc, "get_interactive_skill_commands", return_value={"/" + skill: {"skill_dir": skill}}))
    stack.enter_context(patch.object(sc, "_load_skill_payload", return_value=(
        {"name": skill, "content": "Synthetic skill instructions. " + "s" * 6000}, None, skill,
    )))
    stack.enter_context(patch.object(sc, "_load_skills_config", return_value={}))
    stack.enter_context(patch.object(skill_usage, "bump_use", return_value=None))
    invocation = sc.build_skill_invocation_message("/" + skill, "Preserve " + facts["skill"] + ".")
    rows = [
        {"role": "system", "content": "Use only the synthetic facts in this conversation."},
        {"role": "user", "content": "The requested label is " + facts["user"] + "."},
        {"role": "assistant", "content": "The chosen assistant label is " + facts["assistant"] + "."},
        {"role": "user", "content": "Synthetic pasted log:\n" + "x" * 9800 + "\nThe final request is " + facts["long_user_end"] + "."},
        *_round("long-tool", "terminal", {"command": "synthetic-inspect"}, json.dumps({
            "output": "o" * 9000 + facts["tool_middle"] + "o" * 9000, "exit_code": 0, "error": None,
        })),
        {"role": "user", "content": invocation},
        *_round("skill-load", "skill_view", {"name": skill}, "Synthetic skill body " + "s" * 2000),
        *_round("background", "terminal", {"command": "synthetic-job", "background": True}, json.dumps({
            "output": "Synthetic progress " + "o" * 1500, "exit_code": None, "error": None,
            "status": "yielded_to_background", "session_id": facts["background"], "pid": 12345,
            "notify_on_complete": True, "note": _YIELDED_NOTE,
        })),
        steer_user_row("Change direction and preserve " + facts["steer"] + "."),
        {"role": "assistant", "content": "The synthetic artifact is " + facts["anchor"] + "."},
        *_progress("first"),
        {"role": "user", "content": "Report the newest label " + facts["tail"] + "."},
        {"role": "assistant", "content": "The latest label is recorded."},
    ]
    return rows, facts


def _channels(compressor, rows):
    import agent.context_compressor as cc

    channels = {"summary_body": [], "verbatim_user_block": [], "anchor_index": []}
    current = "summary_body"
    for line in compressor._previous_summary.splitlines():
        if line.startswith("## "):
            current = {cc._LEAN_USER_MESSAGES_HEADING: "verbatim_user_block",
                       cc._LEAN_ANCHOR_HEADING: "anchor_index"}.get(line, "summary_body")
        channels[current].append(line)
    result = {key: "\n".join(lines) for key, lines in channels.items()}
    tail = [compressor._strip_context_summary_handoff_message(row)
            for row in rows if row.get("_compaction_tail")]
    result["tail"] = "\n".join(str(row.get("content") or "") for row in tail if row)
    return result


def _response_text(response):
    # Aux adapters return either dictionaries or OpenAI-shaped response objects.
    if isinstance(response, dict):
        return response["choices"][0]["message"].get("content") or ""
    return response.choices[0].message.content or ""


def _blocked_network(*args, **kwargs):
    raise RuntimeError("Stub mode must not open a network connection.")


def _run(args, stack):
    import agent.context_compressor as cc
    from hermes_cli import config as config_module
    from hermes_cli import config_backups

    # Config reads normally keep a last-good backup; the bench must leave config files alone.
    stack.enter_context(patch.object(config_backups, "backup_config", return_value=None))
    stack.enter_context(patch.object(config_module, "save_config", side_effect=RuntimeError(
        "The bench must not write configuration.",
    )))
    config = copy.deepcopy(config_module.load_config_readonly())
    compression = config.setdefault("auxiliary", {}).setdefault("compression", {})
    if args.effort == "unset":
        compression.pop("reasoning_effort", None)
    else:
        compression["reasoning_effort"] = args.effort
    stack.enter_context(patch.object(config_module, "load_config_readonly", return_value=config))
    stack.enter_context(patch.object(config_module, "load_config", side_effect=lambda: copy.deepcopy(config)))
    main_config = config.get("model", {})
    main = main_config if isinstance(main_config, dict) else {"default": main_config}
    stack.enter_context(patch.object(cc, "get_model_context_length", return_value=args.context_length))
    if args.stub:
        # Freeze the prompt's date as well as the response, including across midnight.
        from datetime import UTC, datetime
        import hermes_time

        stack.enter_context(patch.object(hermes_time, "now", return_value=datetime(2000, 1, 1, tzinfo=UTC)))
        stack.enter_context(patch.object(socket.socket, "connect", _blocked_network))
        stack.enter_context(patch.object(socket.socket, "connect_ex", _blocked_network))
        stack.enter_context(patch.object(socket, "create_connection", _blocked_network))
    calls = []
    real_call = cc.call_llm

    def summarize(**kwargs):
        if args.stub:
            kwargs["route_info"].update(model="deterministic-stub", provider="offline")
            response = {"choices": [{"message": {"content": "## Goal\nSynthetic work is recorded."}, "finish_reason": "stop"}]}
        else:
            response = real_call(**kwargs)
        calls.append({"input_chars": sum(len(str(row.get("content") or "")) for row in kwargs["messages"]),
                      "output_chars": len(_response_text(response)),
                      "model": kwargs["route_info"].get("model", "unreported"),
                      "provider": kwargs["route_info"].get("provider", "unreported")})
        return response

    stack.enter_context(patch.object(cc, "call_llm", summarize))
    comp = cc.ContextCompressor(
        model=str(main.get("default") or "synthetic/model"), provider=str(main.get("provider") or ""),
        base_url=str(main.get("base_url") or ""), api_key=str(main.get("api_key") or ""),
        api_mode=str(main.get("api_mode") or ""), config_context_length=args.context_length,
        protect_first_n=0, protect_last_n=3, quiet_mode=True, tail_mode="lean",
    )
    comp.tail_token_budget = 50
    rows, facts = _conversation(args.seed, stack)
    report = {"seed": args.seed, "context_length": args.context_length, "effort": args.effort,
              "stub": args.stub, "facts": facts, "compactions": []}
    for iteration in (1, 2):
        if iteration == 2:
            rows += [{"role": "user", "content": "Preserve the new label " + facts["second_user"] + "."},
                     *_progress("second")]
        before = len(calls)
        start = time.perf_counter()
        rows = comp.compress(rows, force=True)
        elapsed = time.perf_counter() - start
        if comp.compression_count != iteration or comp._last_summary_fallback_used or len(calls) != before + 1:
            raise RuntimeError("The bench requires one successful aux summary call per compaction.")
        channels = _channels(comp, rows)
        report["compactions"].append({"compaction": iteration,
            "wall_seconds": None if args.stub else round(elapsed, 6),
            "summary_input_chars": calls[-1]["input_chars"], "summary_output_chars": calls[-1]["output_chars"],
            "aux_model": calls[-1]["model"], "aux_provider": calls[-1]["provider"],
            "survival": {name: {channel: None if name == "second_user" and iteration == 1 else fact in text
                                for channel, text in channels.items()} for name, fact in facts.items()},
        })
    return report


def _print_report(report, as_json):
    routes = sorted({(row["aux_provider"], row["aux_model"]) for row in report["compactions"]})
    label = "Aux model/provider used: " + ", ".join(f"{model} / {provider}" for provider, model in routes)
    if as_json:
        # Keep stdout valid JSON; the human-readable route line goes to stderr.
        print(label, file=sys.stderr)
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(label)
    print(f"Seed: {report['seed']}; context length: {report['context_length']}; effort: {report['effort']}")
    print("Compaction | wall seconds | summary input chars | summary output chars")
    for row in report["compactions"]:
        wall = "unmeasured (stub)" if row["wall_seconds"] is None else str(row["wall_seconds"])
        print(f"{row['compaction']} | {wall} | {row['summary_input_chars']} | {row['summary_output_chars']}")
    print("Compaction | planted fact | summary body | verbatim user block | anchor index | tail")
    for row in report["compactions"]:
        for name, cells in row["survival"].items():
            values = " | ".join("n/a" if cells[channel] is None else "yes" if cells[channel] else "no" for channel in (
                "summary_body", "verbatim_user_block", "anchor_index", "tail",
            ))
            print(f"{row['compaction']} | {name} | {values}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--context-length", type=int, default=100_000)
    parser.add_argument("--effort", choices=("unset", "none", "low", "medium", "high"), default="unset")
    parser.add_argument("--stub", action="store_true", help="Offline deterministic response; wall time is unmeasured.")
    parser.add_argument("--json", action="store_true", help="Emit JSON on stdout and the aux route line on stderr.")
    args = parser.parse_args()
    if args.context_length < 4096:
        parser.error("--context-length must be at least 4096")
    with ExitStack() as stack:
        if args.stub:
            home = stack.enter_context(tempfile.TemporaryDirectory(prefix="compaction-parity-"))
            stack.enter_context(patch.dict(os.environ, {"HERMES_HOME": home, "HERMES_DISABLE_LAZY_INSTALLS": "1"}))
        report = _run(args, stack)
    _print_report(report, args.json)


if __name__ == "__main__":
    main()
