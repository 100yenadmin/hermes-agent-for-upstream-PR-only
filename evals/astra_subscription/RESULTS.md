# Astra subscription-first compatibility v1 — 2026-09-20

## TL;DR

The existing ChatGPT/Codex subscription worked with real Hermes CLI and loopback
HTTP gateway sessions on the pinned upstream source. Tools, process restart,
synthetic image input and ordinary Hermes steering worked in the scenarios below.
Merged OAuth native compaction returned real provider checkpoints and continued
successfully after restart in all three trials. Local compression also retained
all 12 planted facts in all three trials.

This is **advisory evidence**, not a full-Astra acceptance or readiness claim.
It does **not** test the API-key-only async, WebSocket steering, effort-update or
explicit-compaction implementations against OpenAI. Those branches are preserved.
Their focused offline tests passed; their live route remains a separate gate.

## Identities and scope

- Live control: `NousResearch/hermes-agent@00570550f37e9082676955d50f65c7d9ba846cc9`.
- Advanced offline candidate: `d3ae318d37de4ad1199ab385cfc8e67f6ce3c4aa`.
- Route: `openai-codex`, exact `gpt-6-astra`, official ChatGPT Codex backend.
- Authentication: existing Codex ChatGPT login, imported using Hermes' supported
  existing-credential flow into a fresh isolated `HERMES_HOME`. Account-scoped
  discovery returned HTTP 200 and included exact Astra (10 visible models).
- One Mac; existing Python environment, OpenAI Python SDK 2.24.0; terminal-only
  tools, synthetic data. No installed configuration or existing session changed.
- Run window: 2026-09-19 19:13–19:35 UTC (2026-09-20 Asia/Bangkok).
- **59/60 provider HTTP requests**: 22 catalog GETs and 37 Responses POSTs,
  including auxiliary calls and the invalid gateway fixture attempt. Last dispatch
  was 1,274 seconds after the budget began. Every recorded HTTP status was 200.
- No API-key route, billing fallback, purchase, external messaging account,
  customer session, merge, release or deployment was used.

The guard instruments HTTP clients; it is not a general-purpose tool sandbox.
The tools used only controlled synthetic commands. No subscription credential
was sent to GitHub Actions. Public evidence contains no account identifiers,
credentials, raw transcripts, response IDs or opaque checkpoint payloads.

## Live surface

| Scenario | Observed result | Boundary |
|---|---|---|
| Account discovery | Exact Astra included | One existing entitled account, not universal availability |
| CLI text + function call + restart | Recalled MAPLE-42; append counter remained 1; one paired tool result | Successful restart path, not crash/ambiguous-delivery fault injection |
| HTTP gateway + restart | Recalled BIRCH-73 using persisted `previous_response_id`; append counter remained 1 | Real localhost HTTP adapter; not Telegram, Discord or a customer channel |
| Synthetic image | Correct red-square/blue-circle description | One deterministic image |
| Structured function arguments | Terminal command objects parsed and executed successfully | Not a separate JSON-schema structured-output evaluation |
| Independent safe calls | Two calls in one assistant batch, two matched results, final ALPHA BETA | Existing Hermes scheduler; no provider-native async or timing-overlap claim |
| Sequential calls | Two separate assistant batches of one call each; final STEP_ONE_VERIFIED | No historical tool replay observed |
| Ordinary steering | Existing `/v1/runs/{id}/steer` accepted while a terminal job was active; final BLUE replaced RED | Existing Hermes steering, not `response.steer` or WebSocket transport |
| Ordinary reasoning selection | `low` and `high` were sent and accepted; image scenario used high | No full-ladder, effective-effort, configuration-update or cache-gain claim |

Fresh terminal result IDs were unique and paired with recorded calls. Both append
counters remained one after process restart. These checks do not prove that every
possible crash or transport ambiguity is safe; advanced failure handling is only
covered by the named offline tests below.

## Local versus merged OAuth native compaction

The same 242-message synthetic history was seeded through real `SessionDB`
serialization: 120 completed tool/result pairs and 12 predetermined region codes.
Tool payloads are identical across trials. It targets approximately 24K tokens
using Hermes' rough estimator (about 25K including persistence metadata); the
provider reported approximately 20K input on the first ordinary request.
This is not a precisely tokenized 24,000-provider-token benchmark.

Each arm asked for all codes, restarted the process, then asked for the codes
again after exactly one new harmless terminal call. Pair order was native/local,
local/native, native/local. The third pair used the existing persisted-session
HTTP endpoint `/api/sessions/{id}/chat`.

Effective native threshold was verified on the wire as **16,000**. Local mode's
threshold was **16,000**. Native mode retained a **32,768** local fallback threshold
so the existing 8,192-token safety clamp did not lower the native trigger.
These are existing settings. Native mode was not switched midway through a trial.

| Pair / surface | Local facts after restart | Native facts after restart | Native checkpoint | Subsequent tools |
|---|---:|---:|---|---|
| 1 / CLI | 12/12 | 12/12 | Persisted | One each; no historical execution observed |
| 2 / CLI | 12/12 | 12/12 | Persisted and identical hash replayed | One each; no historical execution observed |
| 3 / HTTP gateway | 12/12 | 12/12 | Persisted and identical hash replayed | One each; no historical execution observed |

In all six sessions the original 242 rows' roles, content, call arguments/IDs and
result IDs remained equal to the planted fixture. Local mode archived 244 rows
and retained its summary/tail; native mode kept the full durable transcript and
replayed its opaque checkpoint. Ordinary request `instructions` hashes remained
unchanged within each trial. Auxiliary summarizer prompts are intentionally
separate. No native rejection/fallback occurred in the three valid native trials.

### Descriptive measurements, not a speedup claim

Input below means provider input **including cached tokens**, summed over the
ordinary requests and local summarizer where applicable. Hermes stores uncached
input separately from cache reads; summing only its `input_tokens` column would
under-count. Output includes reasoning where reported. Catalog calls are excluded
from these token/latency totals but included in the global request budget.

| Pair | Local input / output | Native input / output | Local response seconds | Native response seconds |
|---|---:|---:|---:|---:|
| 1 | 41,124 / 2,549 | 31,263 / 456 | 88.36 | Unavailable |
| 2 | 40,908 / 2,443 | 31,537 / 548 | 82.99 | 24.87 |
| 3 / gateway | 41,493 / 2,762 | 31,095 / 408 | 93.19 | 22.42 |

Across three pairs, median input was 41,124 local (40,908–41,493) and 31,263 native
(31,095–31,537). Median output was 2,549 local (2,443–2,762) and 456 native
(408–548). Each local arm used four Responses calls, including one summary;
each native arm used three. Submitted ordinary input items shrank from 363 to 17
after local compression and from 363 to 5 after native compaction; items are not
tokens and this is not a like-for-like summary-quality metric.

Timing is the sum of dispatch-to-terminal-response intervals, excluding human
restart delays and catalog requests. Local median was 88.36s (82.99–93.19, n=3).
Native median of the **two measured** trials was 23.65s (22.42–24.87, n=2).
Native pair 1 timing was not captured and is not estimated. Small sample size,
different compression timing, nondeterministic outputs, warm caches, different
CLI/gateway system prompts and interleaved offline checks prevent a general
performance or quality conclusion. No p95, significance or cache-savings claim.
The pilot pair also used arm-specific `NATIVE_RESUME_OK` / `LOCAL_RESUME_OK`
terminal markers; later pairs used identical `RESUME_OK` prompts.

## Failures, corrections and unavailable evidence

- Initial isolated CLI authentication was absent. Hermes' existing credential
  import completed normally; no credential was printed. A fresh device-login
  attempt was blocked before dispatch. No account switching or billing fallback.
- The first CLI invocation enabled automatic title generation. Its extra model
  call is counted. Titles, memory, curator and MCP were disabled in the isolated
  configuration for the remaining runs.
- Early Responses streams had no content-type header. The initial telemetry
  observer therefore missed their terminal usage/timing. The observer was fixed
  to identify streaming Responses by request route. Pair 1 native token totals
  come from durable usage accounting; its per-response timing remains unavailable.
- Gateway attempt #44 supplied stateless `conversation_history`, whose existing
  route retains role/content but not tool metadata. The resulting ~5K input did
  not compact. This is **not native success or local fallback**. One bounded
  harness correction used the existing persisted-session endpoint; valid native
  gateway requests are #46, #48 and #49. Attempt #44 remains in the budget.
- One offline command named a nonexistent test file; no tests ran. It was
  corrected to `test_astra_websocket.py`. No production fix was made.
- No deliberately injected live rejection, timeout, quota exhaustion, ambiguous
  side effect or lost steering acknowledgement was attempted.

## Offline coverage

Each file ran separately with the canonical `scripts/run_tests.sh`, one worker,
120-second per-file timeout. The evaluation bootstrap limits `compileall` CPU
discovery to four cores. These are local focused results, **not a new CI run**.

| Source | Test file | Passed |
|---|---|---:|
| Control `00570550` | `tests/agent/test_astra_oauth_native_compaction.py` | 12 |
| Preserved `d3ae318d` | `tests/agent/test_astra_async_tools.py` | 17 |
| Preserved `d3ae318d` | `tests/agent/test_astra_configuration_update.py` | 23 |
| Preserved `d3ae318d` | `tests/agent/test_astra_websocket.py` | 21 |
| Preserved `d3ae318d` | `tests/agent/test_astra_pending_tool_steering.py` | 4 |
| Preserved `d3ae318d` | `tests/run_agent/test_native_compaction.py` | 88 |
| Preserved `d3ae318d` | `tests/gateway/test_compress_command.py` | 9 |
| Evaluation fixtures | `evals/astra_subscription/test_live_guard.py` | 10 |

**184 passed, 0 failed** across these named files. Tests exercise synthetic source
behavior, not live provider acceptance. The preserved candidate was tested in a
separate detached worktree and was not rebased or edited.

## Feature disposition and exact remaining proof

| Capability | Classification in this phase | Recommendation |
|---|---|---|
| Discovered Astra, ordinary tools, image, restart and Hermes steering | Exercised live | Retain existing implementation; broaden only for a demonstrated failure |
| Merged OAuth automatic compaction (`context_management`) | Exercised live | Retain opt-in native mode and local fallback; keep off by default |
| Provider-marked async tools, PR #103129 | Tested offline only; OAuth excluded by our route gate | Await exact direct-API live evidence of overlap, ordering and interruption safety |
| Native WebSocket steering, PR #103144 | Tested offline only; OAuth excluded by our route/auth gates | Await direct-API accepted successor, pending-tool and uncertain-delivery evidence |
| Cache-preserving `configuration_update`, PR #103183 | Tested offline only; OAuth excluded by our implementation | Await direct-API low→high→low, restart and cache-prefix telemetry |
| Explicit `compaction_trigger` with effort updates, PR #103278 | Tested offline only; OAuth excluded by our implementation | Await direct-API checkpoint/resume/effort/steering evidence |
| Advanced provider features on the subscription backend | Provider support unestablished by this phase | Do not infer unsupported; do not relax gates or invent a subscription transport |

OpenAI's [reasoning contract](https://developers.openai.com/api/docs/guides/reasoning#change-reasoning-mid-conversation)
separates `configuration_update` from automatic compaction: effort-update histories
use explicit `compaction_trigger`. Therefore the live automatic OAuth result does
not replace #103278's distinct proof. See also the official
[compaction](https://developers.openai.com/api/docs/guides/compaction),
[async tools](https://developers.openai.com/api/docs/guides/async-tool-calling),
[steering](https://developers.openai.com/api/docs/guides/steering) and
[authentication](https://learn.chatgpt.com/docs/auth) references, rechecked for this phase.

## Work-graph reconciliation

At publication preflight, tracker #103015 and validation #103020 are closed;
#103129, #103144 and #103183 are closed, not merged. #103278 remains open/draft at
`d3ae318d37de4ad1199ab385cfc8e67f6ce3c4aa`. Landed baseline #105185 and OAuth
compaction #115890 are comparison context, not evidence that the closed stack landed.
No lifecycle state is changed by this report. The original full-Astra objective
and its direct-route acceptance gaps remain separate.

Machine-readable allowlisted evidence: [results-20260920.json](results-20260920.json).
Reproduction and fixture boundaries: [README.md](README.md).
Independent evidence review is recorded with the published evaluation commit.

Next owner: the contributor. The next live advanced gate requires a separately
approved direct-API account and spending cap. No such run was authorized or made
here, and this report assigns no new obligation to upstream maintainers.
