"""Deterministic compaction gaps, with passing controls beside each gap.

ID   | Plain-language effect                         | Tracker / fix PR
P1   | A second summary loses old or new user quotes | #136540
P2   | Long messages lose their final request        | #136535 / #136541 (final request)
P2-mid | Middle facts lack verbatim carry by design  | #136535 (open; no fix PR)
P3   | Skill bodies crowd out the user's instruction | #136535 / #136541
P4   | Mid-turn direction changes lose their words   | #136535 / #136541
N5   | Repeated scaffolding consumes quote space     | #136535 / #136541
P6a  | Demoted skills lose their reload instruction  | #136542 E / #136551
P8a  | Background commands lose their session handle | #136542 A / #136549
P12  | Synthetic rows displace real native asks      | #136542 D

All data is synthetic; the summarizer never uses the network. These assertions
express desired behavior and intentionally fail on unfixed main. P12 exercises
the native checkpoint retention helper; all other gaps run real forced compression.
"""

import json
import socket

import pytest

import agent.context_compressor as cc

SUMMARY = "## Goal\nSynthetic work is recorded."
SKILL = "synthetic-tail-skill"
HANDLE = "proc_0123456789ab"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("The parity kit must run without a network connection.")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(cc, "get_model_context_length", lambda *a, **k: 100_000)
    state = {"requests": [], "summary": SUMMARY, "fail": False}

    def summarize(**kwargs):
        state["requests"].append(kwargs)
        if state["fail"]:
            raise RuntimeError("Synthetic summarizer unavailable.")
        return {"choices": [{"message": {"content": state["summary"]}, "finish_reason": "stop"}]}

    monkeypatch.setattr(cc, "call_llm", summarize)
    state["compressor"] = cc.ContextCompressor(
        model="test/model", config_context_length=100_000, protect_first_n=0,
        protect_last_n=3, quiet_mode=True, tail_mode="lean",
    )
    state["compressor"].tail_token_budget = 50
    return state


def _text(rows):
    return "\n".join(row.get("content") or "" for row in rows)


def _user_section(summary):
    lines = []
    inside = False
    for line in summary.splitlines():
        if line == cc._LEAN_USER_MESSAGES_HEADING:
            inside = True
        elif inside and line.startswith("## "):
            break
        elif inside:
            lines.append(line)
    return "\n".join(lines)


def _progress():
    return [row for i in range(8) for row in (
        {"role": "user", "content": f"Record ordinary progress {i}."},
        {"role": "assistant", "content": f"Progress {i} recorded."},
    )]


def _history(region):
    # Later asks prevent the current-task snapshot from masking a lost quote.
    return [{"role": "system", "content": "Synthetic test conversation."}, *region, *_progress()]


def _compress(harness, rows, targets=(), fallback=False):
    comp = harness["compressor"]
    start, end = comp._compress_window(rows)
    assert all(any(row is target for row in rows[start:end]) for target in targets), \
        "The target messages must be inside the region being summarized."
    before = len(harness["requests"])
    count = comp.compression_count
    out = comp.compress(rows, force=True)
    assert len(harness["requests"]) > before, "The real compressor must call the stub summarizer."
    assert comp.compression_count == count + 1, "The compressor must complete one compaction."
    assert comp._last_summary_fallback_used == fallback, "The compressor must use the intended summary path."
    return out


def _control(harness, gap, region=()):
    row = {"role": "user", "content": f"Please keep NEEDLE-{gap}-CONTROL."}
    out = _compress(harness, _history([*region, row]), [row])
    assert "> " + row["content"] in _text(out), "A short ordinary user request must remain quoted."


def _second(harness, echo):
    a = {"role": "user", "content": "Please keep NEEDLE-P1-A."}
    first = _compress(harness, _history([a]), [a])
    assert "> " + a["content"] in _text(first), "The first compaction must quote the original request."
    harness["summary"] = harness["compressor"]._previous_summary if echo else SUMMARY
    b = {"role": "user", "content": "Please keep NEEDLE-P1-B."}
    second = _compress(harness, first + [b, *_progress()], [b])
    text = _text(second)
    assert text.count(cc._LEAN_USER_MESSAGES_HEADING) == 1, "The summary must have one user quote section."
    return text, a, b


def test_gap_P1_second_compaction(harness):
    text, _, row = _second(harness, True)
    assert "> " + row["content"] in text, \
        "GAP P1: a second compaction loses the new request when the summarizer copies the old quote section (#136540)."


def test_gap_P1_dropped_previous_quotes(harness):
    text, row, _ = _second(harness, False)
    assert "> " + row["content"] in text, \
        "GAP P1: a second compaction loses the earlier request when the summarizer drops the previous user quote section (#136540)."


def test_control_P1_fresh_heading(harness):
    text, _, row = _second(harness, False)
    assert "> " + row["content"] in text, "A fresh summary must quote the new request at the second compaction."


def test_gap_P2_long_user_tail(harness):
    needle = "NEEDLE-P2-7f3a91"
    head = "Synthetic pasted log starts here.\n"
    ask = "Please fix the error while preserving " + needle + "."
    content = head + "x" * (10_000 - len(head) - len(ask) - 1) + "\n" + ask
    row = {"role": "user", "content": content}
    out = _compress(harness, _history([row]), [row])
    assert needle in _text(out), \
        "GAP P2: compaction loses the final request in a long user message (#136535)."


def test_control_P2_short_request(harness):
    _control(harness, "P2")


def test_gap_P2_mid_long_user_fact(harness):
    needle = "NEEDLE-P2-MID-7f3a91"
    content = "x" * 7499 + " " + needle + " " + "x" * (10_000 - 7501 - len(needle))
    row = {"role": "user", "content": content}
    out = _compress(harness, _history([row]), [row])
    assert needle in _text(out), \
        "GAP P2-mid: a fact in the middle of a long user message is not kept verbatim and needs the summary or session_search to recover it (#136535)."


@pytest.fixture
def skill_invocation(monkeypatch):
    from agent import skill_commands as sc
    from tools import skill_usage

    commands = {"/" + name: {"skill_dir": name} for name in ("synthetic-carry-skill", "synthetic-extra-skill")}
    monkeypatch.setattr(sc, "get_interactive_skill_commands", lambda: commands)
    monkeypatch.setattr(sc, "_load_skill_payload", lambda name, **kw: (
        {"name": name, "content": "SKILL-BODY-" * 600}, None, name,
    ))
    monkeypatch.setattr(sc, "_load_skills_config", dict)
    monkeypatch.setattr(skill_usage, "bump_use", lambda *a, **k: None)
    return sc


@pytest.mark.parametrize("kind", ["single", "bundle", "bare"])
def test_gap_P3_skill_instruction(harness, skill_invocation, kind):
    instruction = "Do X while preserving NEEDLE-P3-7f3a91." if kind != "bare" else ""
    command = "/synthetic-carry-skill"
    if kind == "bundle":
        command += " /synthetic-extra-skill"
        content, _, _ = skill_invocation.build_stacked_skill_invocation_message(command.split(), instruction)
    else:
        content = skill_invocation.build_skill_invocation_message(command, instruction)
    row = {"role": "user", "content": content}
    _compress(harness, _history([row]), [row])
    section = _user_section(harness["compressor"]._previous_summary)
    needle = "NEEDLE-P3-7f3a91" if instruction else "/synthetic-carry-skill"
    assert any(needle in line for line in section.splitlines() if line.startswith("> ")) and "SKILL-BODY-" not in section, \
        "GAP P3: compaction fails to quote the user's skill command or supplied instruction without the expanded skill body (#136535)."


def test_control_P3_typed_scaffold_prefix(harness):
    row = {"role": "user", "content": '[IMPORTANT: The "config" file is broken; please repair it.] NEEDLE-P3-CONTROL.'}
    out = _compress(harness, _history([row]), [row])
    assert "> " + row["content"] in _text(out), "Ordinary typed text sharing a skill header prefix must stay quoted."


def test_gap_P4_mid_turn_steer(harness):
    from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN, steer_user_row

    steer = "Change direction and preserve NEEDLE-P4-7f3a91."
    row = steer_user_row(steer)
    _compress(harness, _history([row]), [row])
    section = _user_section(harness["compressor"]._previous_summary)
    assert (any(steer in line for line in section.splitlines() if line.startswith("> "))
            and STEER_MARKER_OPEN not in section and STEER_MARKER_CLOSE not in section), \
        "GAP P4: compaction loses the user's mid-turn direction change (#136535)."


def test_control_P4_short_request(harness):
    _control(harness, "P4")


def _heartbeats():
    from hermes_cli.heartbeat import HeartbeatState

    state = HeartbeatState(prompt="", interval_seconds=600)
    state.prompt = "h" * (3700 - len(state.render_prompt()))
    heartbeat = state.render_prompt()
    rows = []
    for i in range(3):
        rows += [{"role": "user", "content": f"Please preserve NEEDLE-N5-{i}."},
                 {"role": "assistant", "content": "Request recorded."}]
    for i in range(10):
        rows += [{"role": "user", "content": heartbeat},
                 {"role": "assistant", "content": f"No changes at tick {i}."}]
    return rows


@pytest.mark.parametrize("kind", ["heartbeat", "duplicates", "internal_notification", "hidden", "auto_continue"])
def test_gap_N5_scaffolding_budget(harness, kind):
    if kind == "heartbeat":
        out = _text(_compress(harness, _history(_heartbeats())))
        survives = all(f"NEEDLE-N5-{i}." in out for i in range(3)) and out.count("> [Heartbeat") <= 1
    elif kind == "duplicates":
        ask = "Repeat this ask NEEDLE-N5-DUPLICATE."
        region = [{"role": "user", "content": ask}, {"role": "assistant", "content": "Recorded."},
                  {"role": "user", "content": "  " + ask + "\n"}]
        out = _text(_compress(harness, _history(region), [region[0], region[2]]))
        survives = out.count("> " + ask) == 1
    else:
        rows = [{"role": "user", "content": "Real ask NEEDLE-N5-REAL."},
                {"role": "user", "content": "[Heartbeat — recurring instruction] NEEDLE-N5-MODEL.", "display_kind": kind}]
        out = _text(_compress(harness, _history(rows), rows))
        survives = "NEEDLE-N5-REAL." in out and "NEEDLE-N5-MODEL." not in out
    assert survives, \
        "GAP N5: repeated or model-only messages consume space intended for real user quotes (#136535)."


def test_control_N5_recent_request(harness):
    _control(harness, "N5", _heartbeats())


def _round(call_id, name, args, content):
    return [{"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args),
    }}]}, {"role": "tool", "tool_call_id": call_id, "tool_name": name, "content": content}]


def _tail_history():
    rows = [{"role": "system", "content": "Synthetic test conversation."},
            {"role": "user", "content": "Summarize the completed earlier work."},
            {"role": "assistant", "content": "Old notes " + "x" * 40_000},
            *_round("older-terminal", "terminal", {"command": "old-job"}, "Earlier output " + "o" * 8000),
            {"role": "user", "content": "Continue the current work."},
            *_round("skill-1", "skill_view", {"name": SKILL}, "Skill instructions " + "s" * 2000)]
    for i in range(6):
        rows += _round(f"fresh-{i}", "terminal", {"command": f"echo {i}"}, f"Step {i} done.")
    return rows + [{"role": "assistant", "content": "All rounds completed."},
                   {"role": "user", "content": "Report the current work."},
                   {"role": "assistant", "content": "Current work reported."}]


def _tool_text(rows, call_id):
    return next(row["content"] for row in rows if row.get("tool_call_id") == call_id)


@pytest.mark.parametrize("stage", ["demotion", "next_compaction", "multiple_unnamed"])
def test_gap_P6a_skill_reload(harness, stage):
    again = stage == "next_compaction"
    comp = harness["compressor"]
    comp.tail_token_budget = 2000
    if again:
        comp.bind_session_state(session_id="0123abcd-4567-89ef-0123-456789abcdef")
    rows = _tail_history()
    if stage == "multiple_unnamed":
        extra = _round("skill-2", "skill_view", {"name": "synthetic-other-skill"}, "Other skill instructions " + "t" * 2000)
        rows[6]["tool_calls"] += extra[0]["tool_calls"]
        rows.insert(8, extra[1])
        rows[7].pop("tool_name")
        rows[8].pop("tool_name")
    _, end = comp._compress_window(rows)
    assert any(row.get("tool_call_id") == "skill-1" for row in rows[end:]), \
        "The loaded skill must reach the protected tail before demotion."
    first = _compress(harness, rows)
    stub = _tool_text(first, "skill-1")
    assert "output demoted at compaction" in stub, "The compressor must demote the stale skill output."
    text = stub + "\n" + comp._previous_summary
    if again:
        later = [{"role": "user", "content": "Next task."}]
        for i in range(8):
            later += _round(f"later-{i}", "terminal", {"command": f"echo later {i}"}, f"Later step {i} " + "l" * 1400)
        text = _text(_compress(harness, first + later + [{"role": "assistant", "content": "Done."}]))
    has_reload = SKILL in cc._extract_pruned_skill_names(text)
    if stage == "multiple_unnamed":
        has_reload = (cc._extract_pruned_skill_names(stub) == [SKILL]
                      and cc._extract_pruned_skill_names(_tool_text(first, "skill-2")) == ["synthetic-other-skill"])
    assert has_reload, \
        "GAP P6a: a demoted skill loses the name and instruction needed to reload it (#136542 E)."


@pytest.mark.parametrize("kind", ["fresh_skill", "stale_terminal"])
def test_control_P6a_fresh_skill(harness, kind):
    comp = harness["compressor"]
    comp.tail_token_budget = 2000
    rows = _tail_history()
    if kind == "stale_terminal":
        rows[6:8] = _round("skill-1", "terminal", {"command": "synthetic-job"}, "Terminal output " + "o" * 2000)
        out = _compress(harness, rows)
        assert "output demoted at compaction" in _tool_text(out, "skill-1"), \
            "An old ordinary tool result must still be demoted."
        assert all(_tool_text(out, f"fresh-{i}") == f"Step {i} done." for i in range(6)), \
            "The newest tool rounds must remain unchanged."
        return
    rows[-2:-2] = _round("new-skill", "skill_view", {"name": "synthetic-new-skill"}, "Fresh skill " + "s" * 2000)
    out = _compress(harness, rows)
    assert _tool_text(out, "new-skill") == "Fresh skill " + "s" * 2000, \
        "A freshly loaded skill must remain available in the protected tail."


def _terminal_result(kind):
    from tools.terminal_tool import _YIELDED_NOTE

    if kind == "started":
        return json.dumps({"output": "Background process started", "session_id": HANDLE,
                           "pid": 12345, "exit_code": 0, "error": None, "hint": "h" * 400})
    output = "Synthetic progress " + "o" * 1500
    if kind == "yielded_printed_old_stub":
        output = "; background session_id=proc_ffffffffffff " + output
    return json.dumps({"output": output, "exit_code": None,
                       "error": None, "status": "yielded_to_background", "session_id": HANDLE,
                       "pid": 12345, "notify_on_complete": True, "note": _YIELDED_NOTE})


def _background_stub(harness, kind):
    command = "synthetic-job"
    if kind == "fallback_quoted_command":
        command = 'echo \'{"session_id": "proc_ffffffffffff"}\''
    if kind == "yielded_long_command":
        command = "x" * 200
    fallback = kind.startswith("fallback")
    comp = harness["compressor"]
    comp.protect_first_n = 0 if fallback else 3
    harness["fail"] = fallback
    rows = _history([{"role": "user", "content": "Start a synthetic long command."},
                     *_round("bg-1", "terminal", {"command": command}, _terminal_result(kind))])
    out = _compress(harness, rows, fallback=fallback)
    text = comp._previous_summary if fallback else _tool_text(out, "bg-1")
    if not fallback:
        assert len(text) < cc._PRUNE_MIN_CHARS, \
            "The old command result must become a small stub."
    return text


@pytest.mark.parametrize("kind", ["yielded", "started", "yielded_long_command", "yielded_printed_old_stub", "fallback", "fallback_quoted_command"])
def test_gap_P8a_background_handle(harness, kind):
    stub = _background_stub(harness, kind)
    assert HANDLE in stub, \
        "GAP P8a: compaction loses the session handle needed to poll or stop a background command (#136542 A)."


@pytest.mark.parametrize("kind", ["yielded_printed_old_stub", "fallback_quoted_command"])
def test_quality_P8a_decoy_handle(harness, kind):
    stub = _background_stub(harness, kind)
    decoy = "session_id=proc_ffff" if kind == "fallback_quoted_command" else "proc_ffff"
    assert decoy not in stub, \
        "A fake handle printed in output or command text must not be promoted into a background session handle."


@pytest.mark.parametrize("printed", ["done", json.dumps({"session_id": HANDLE}),
    f"GET /api?session_id={HANDLE}", f"[terminal] ran `job` -> exit ?, 1 lines output; background session_id={HANDLE}"])
def test_control_P8a_foreground_output(harness, printed):
    harness["compressor"].protect_first_n = 3
    result = json.dumps({"output": printed + "o" * 1500,
                         "exit_code": 0, "error": None})
    rows = _history([{"role": "user", "content": "Run the synthetic foreground command."},
                     *_round("fg-1", "terminal", {"command": "synthetic-job"}, result)])
    stub = _tool_text(_compress(harness, rows), "fg-1")
    assert "session_id" not in stub, \
        "Text printed by a finished foreground command must not become a background handle."


def _native_retention(kind, budget):
    from agent.conversation_loop import _get_continuation_prompt
    from agent.native_compaction import prune_pre_checkpoint_items
    from tools.process_registry_notifications import format_process_notification
    from tools.todo_tool import TodoStore

    source = {"role": "user"}
    if kind == "todo-snapshot":
        store = TodoStore()
        store.write([{"id": "1", "content": "Do the synthetic administrative task.", "status": "pending"}])
        synthetic = store.format_for_injection()
        source["_todo_snapshot_synthetic"] = True
    elif kind == "system-nudge":
        synthetic = _get_continuation_prompt(False)
        source["_length_continuation_nudge"] = True
    else:
        synthetic = format_process_notification({"type": "completion", "session_id": "synthetic-session",
                                                 "command": "synthetic-job", "exit_code": 0, "output": "Done."})
    source["content"] = synthetic
    real = {"type": "message", "role": "user", "content": "Please keep NEEDLE-P12-7f3a91."}
    fake = {"type": "message", "role": "user", "content": synthetic}
    checkpoint = {"type": "compaction", "encrypted_content": "synthetic-opaque-checkpoint"}
    out = prune_pre_checkpoint_items([real, fake, checkpoint], retained_user_token_budget=budget,
                                    item_sources=[real, source, checkpoint])
    assert out[0] == checkpoint, "The native checkpoint must remain first in the retained context."
    return json.dumps(out)


@pytest.mark.parametrize("kind", ["todo-snapshot", "system-nudge", "background-notice"])
def test_gap_P12_native_synthetic_rows(kind):
    assert "NEEDLE-P12-7f3a91" in _native_retention(kind, 12), \
        "GAP P12: synthetic messages use the native retention budget before the real user request (#136542 D)."


@pytest.mark.parametrize("kind", ["todo-snapshot", "system-nudge", "background-notice"])
def test_control_P12_roomy_budget(kind):
    assert "NEEDLE-P12-7f3a91" in _native_retention(kind, 2000), \
        "The real request must remain when the native retention budget has enough room."
