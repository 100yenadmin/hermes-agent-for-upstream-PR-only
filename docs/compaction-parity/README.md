# Compaction parity kit

This reproduction kit runs from this branch with synthetic data and no model
calls. It also includes a small bench for your own configured auxiliary model.
The tests assert desired behavior, so failures on unfixed main are expected.
**Do not merge this kit as-is:** upstream CI would go red on purpose. Each fix
PR carries its own green regression tests; this branch is for reproduction.

## What the tests prove

| ID | Desired behavior | What the user sees on unfixed main | Tracker | Fix PR |
| --- | --- | --- | --- | --- |
| P1 | Earlier quotes survive a dropped old section; new quotes survive a copied section at compaction 2. | The agent forgets an earlier request or misses the new one after its second summary. | #136540 | — |
| P2 | The final request in a long paste stays visible. | The agent sees the pasted log but loses the request at its end. | #136535 | #136541 (final request) |
| P2-mid | A fact in the middle of a long message stays verbatim. Only a design with no per-message cap can pass this (a maintainer design call in #136535); no head-and-tail fix can, #136541 included. | A fact deep in a long paste is not in the verbatim record; only the summary or session_search can bring it back. | #136535 | — |
| P3 | Skill instructions and bare command names appear on quote lines without expanded bodies. | The agent keeps skill boilerplate instead of the user's command or instruction. | #136535 | #136541 |
| P4 | The steer text appears on a quote line without internal frame markers. | The agent loses the user's mid-turn change of direction. | #136535 | #136541 |
| N5 | Repeated heartbeats, duplicate asks, and model-only rows leave room for real quotes. | Repeated system chatter pushes the user's requests out of context. | #136535 | #136541 |
| P6a | A demoted skill keeps its reload marker, including at the next compaction. | Nothing tells the agent to reload the skill's instructions; after the next compaction even the skill's name can be gone. | #136542 E | #136551 |
| P8a | Background commands keep their handle, including in fallback summaries. | The agent loses the handle it needs to poll or stop the command, unless it first lists background processes. | #136542 A | #136549 |
| P12 | Real asks take priority over synthetic rows in native retention. | With a tight retention budget, the agent keeps injected notices instead of the user's ask. | #136542 D | — |

The "what the user sees" column describes the verbatim record. With a real
model, the summary may still paraphrase some of the lost content; the stub
summarizer used here does not.

Each `test_control_*` sits beside its gap. Controls cover new quotes in a fresh
second summary, short requests, typed header-like text, a recent request after
heartbeats, fresh skills, normal stale-tool demotion, foreground output, and a
roomy native budget. The separate `test_quality_P8a_*` cases reject fake handles
printed in output or command text. All controls and quality tests pass on main.

All compressor tests call real `ContextCompressor.compress(force=True)` with
stubbed `agent.context_compressor.call_llm`. Fallback variants make that stub
fail. P12 uses real synthetic-message producers and the native checkpoint
retention helper, which does not use the auxiliary summarizer. It deliberately
uses **12 tokens against the 64,000-token default** to expose the priority rule;
it does not show routine loss at the default budget. Native Responses compaction
is **opt-in**. Socket connections are blocked in the test file.

## Run it

From a checkout with the normal development and test environments ready (use
`source ./activate` to select the development interpreter for `python`):

```sh
scripts/run_tests.sh tests/agent/test_compaction_parity_kit.py -q
python scripts/compaction_parity_bench.py --stub
python scripts/compaction_parity_bench.py --stub --seed 7 --context-length 100000 --effort none --json
```

To measure your configured auxiliary compression model, omit `--stub`:

```sh
python scripts/compaction_parity_bench.py --seed 7 --effort low
python scripts/compaction_parity_bench.py --seed 7 --effort unset --json
```

Live runs send only the generated synthetic conversation to your configured
provider and incur its normal cost. The bench never saves config. `--effort`
overrides `auxiliary.compression.reasoning_effort` in memory; `unset` removes
that override for this run. Normal aux routing and fallback still apply, and
the actual model/provider used is printed.

The bench runs two consecutive forced compactions. It reports wall seconds,
summary request/response characters, and exact planted-fact survival in the
summary body, verbatim user block, anchor index, and raw retained tail. Embedded
summary text is removed from the tail measurement to avoid counting it twice.
The second-user fact is planted only before compaction 2, so its compaction 1
cells show `n/a` (`null` in JSON). Presence proves retention, not reasoning.

Stub output is byte-for-byte deterministic for identical flags. Stub wall time
is explicitly **unmeasured** (`null` in JSON); live wall time is measured. The
stub narrative contains no planted facts, testing mechanical carry rather than
model quality. JSON goes to stdout; the aux route line goes to stderr. A failed
summary path stops the bench rather than presenting fallback as model quality.

## Expected results on unfixed main

All 25 `test_gap_*` cases fail with a plain-language `GAP` message; all 14 controls
and two quality cases pass. No test is marked expected-fail or skipped. A setup
error or failed control/quality check is a harness or behavior problem to
inspect separately, not evidence of the reported gap.

P2-mid places a fact at character 7500 of a 10000-character message; it has no
request. It remains open by design: no bounded head-and-tail budget guarantees
middle facts. PR #136541 repairs P2's final request, P3, P4, and N5, and is not a
fix for P2-mid. PR #136549 repairs P8a; PR #136551 repairs P6a. Unrelated gaps
remain expected failures on each individual fix branch. P3 and P4 test quoted
content, not a particular command format or steer label.

## Paste this to your coding agent

> Run `scripts/run_tests.sh tests/agent/test_compaction_parity_kit.py -q`, then
> `python scripts/compaction_parity_bench.py --stub` twice. Report the exact
> source commit, each gap, control and quality result, any setup errors, and
> whether the bench outputs match. Keep the GAP assertion messages.
> Do not mark failures expected-fail, change source to get green, write config,
> or make live model calls without approval. Separate synthetic retention
> evidence from model quality, live latency, release, and runtime behavior.
