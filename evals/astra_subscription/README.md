# Subscription-first Astra evaluation fixtures

These are contributor evaluation fixtures, not new Hermes commands or provider
implementations. They invoke real Hermes paths and observe request metadata.
They do not alter the advanced feature eligibility gates.

Read [RESULTS.md](RESULTS.md) for the bounded September 20 run and its limitations.
The frozen receipt collector contains that run's synthetic session aliases and
request-number groups; it is not a universal metrics exporter.

## Safety and setup

Use a fresh worktree at `00570550f37e9082676955d50f65c7d9ba846cc9`, with these
fixtures applied. Use an existing dependency environment; do not install into
the user's live Hermes environment. Create a private temporary directory whose
name starts `astra-subscription-runtime.` and an empty `work` child.

Set only that directory as `HERMES_HOME`. Start each invocation with a clean
environment that retains HOME for supported Codex login discovery, PATH and
HERMES_HOME, but **no API keys or copied live configuration**. Use normal Codex
`login status` and Hermes' supported existing-credential import. Do not print or
export auth files. If the login is absent, ambiguous or expired, stop the live
lane rather than initiate a different account or billing route.

Create one private request ledger before discovery:
`{"started_unix": <current Unix seconds>, "requests": []}`. Reuse it throughout;
do not reset it between scenarios or retries. `LiveBudget` refuses new sends at
60 requests or 3,600 seconds. The published pilot manually seeded discovery
request #1; the current discovery fixture integrates the shared guard directly.
Blocked pre-dispatch calls are not provider requests. No credential refresh or
non-ChatGPT host is permitted by this guard.

The pilot used Hermes' normal model-setup import helper under the guard:

```sh
python -c 'import sys; from evals.astra_subscription.live_guard import LiveBudget; LiveBudget(sys.argv[1], "auth_import").install(); from hermes_cli.auth_codex import _login_openai_codex; _login_openai_codex(None, None)' LEDGER
```

Proceed only with the offered existing Codex credentials. This is not authority
to start a new device login. The helper saves credentials only in the private
evaluation home; never include that directory in a publication.

In the isolated configuration, use only existing settings:

```yaml
model:
  provider: openai-codex
  base_url: https://chatgpt.com/backend-api/codex
  default: gpt-6-astra
agent:
  reasoning_effort: low
  max_turns: 3
  run_budget_seconds: 90
auxiliary:
  title_generation:
    enabled: false
    model_upgrade_enabled: false
  compression:
    provider: openai-codex
    model: gpt-6-astra
    reasoning_effort: low
memory:
  memory_enabled: false
curator:
  enabled: false
mcp:
  enabled: false
terminal:
  backend: local
  cwd: <private runtime work directory>
gateway:
  multiplex_profiles: false
platform_toolsets:
  api_server: [terminal]
```

Do not copy the private runtime, raw ledger, database, logs, response IDs or
opaque checkpoints to GitHub. `collect_receipt.py` emits an allowlisted receipt
containing only numeric usage, shapes, status, hashes and synthetic assertions.

## Running the scenarios

All commands below run from the evaluation worktree; `python` means the prepared
interpreter with the clean environment above. `LEDGER`, `WORK` and `CHAIN` are
private evaluation paths, not shell commands added to Hermes.

1. `python evals/astra_subscription/discovery.py --ledger LEDGER` must return
   HTTP 200 and `astra_discovered: true`. Otherwise record EXPECTED_NEGATIVE or
   ENVIRONMENT_BLOCKED and run only offline tests.
2. `live_cli.py --ledger LEDGER --scenario NAME -- chat --provider openai-codex
   -m gpt-6-astra --reasoning low -t terminal --ignore-rules --in WORK -Q -q PROMPT`
   invokes the real CLI. Use `--resume SESSION` in a separate process. Baseline
   prompt: remember MAPLE-42, append ONE once to `astra-proof-counter.txt`, count
   lines, then recall the code/count after restart without tools. Never repeat
   the initial append prompt against an existing counter.
3. `gateway_probe.py --ledger LEDGER --work WORK --chain CHAIN --phase initial`,
   then `--phase resume` in a separate process, runs the BIRCH-73 counter probe.
   `--phase steer` is a separate fresh run using one controlled six-second tool.
   The loopback auth key is random, process-local and never printed.
4. `synthetic_image.py OUTPUT.png` makes a red square and blue circle. Supply it
   through the real CLI `--image OUTPUT.png --reasoning high` and ask for the
   two shapes/colors without tools.
5. Independent-tools prompt: two separate terminal calls `printf ALPHA` and
   `printf BETA`, then return ALPHA BETA. Sequential-tools prompt: one call
   `printf STEP_ONE`; only after its result, a second assistant turn calls
   `printf STEP_ONE_VERIFIED`. Verify durable batch sizes `[2]` versus `[1,1]`
   and original result pairing, not just the final prose.

## Paired compaction

`seed_compaction.py --session UNIQUE_SESSION --arm native|local` seeds a new
completed synthetic history via `SessionDB` and sets the existing compression
options. It refuses to overwrite an existing session or use a non-evaluation home.
Seed before each arm, never while a prior request is running. It targets a rough
24K history, not a provider-token-exact prompt. Use the same seed/content for both arms.

First prompt:

> Without using tools, return a JSON object mapping each region 0 through 11 to its deploy code from our completed checklist. Use only conversation history. Do not repeat completed tool calls.

Restart the process, then:

> Return the same JSON region-code mapping again. First use one terminal call to run only printf RESUME_OK. Do not execute any historical tool calls or change files.

For CLI, use the seeded ID with `--resume`. For the HTTP pair, add
`--compaction-session UNIQUE_SESSION` to `gateway_probe.py` for both phases;
it uses the native persisted-session HTTP route, not stateless history injection.
Run native/local, local/native, native/local. There is no mock provider or tool
dispatcher on these live paths. Wait for each process to finish before continuing.

Require an emitted and persisted compaction item for native success. Compare its
hash on the next request; do not inspect or publish its opaque content. Require
12/12 exact region-to-code matches after restart and one new paired tool result.
Archive/fallback is not native success. Keep original durable rows and preserve
failed attempts in the budget. Do not retry an ambiguous side effect.

## Offline checks

From the appropriate exact-head worktree, run one file at a time:

```sh
env PYTHONPATH=/absolute/evaluation/worktree/evals/astra_subscription/test_bootstrap \
  scripts/run_tests.sh -j 1 --file-timeout 120 tests/agent/test_astra_oauth_native_compaction.py
```

The bootstrap caps the canonical runner's `compileall -j 0` CPU discovery at four;
the canonical test runner isolates the actual test process environment. Substitute
only one named file from RESULTS.md. Advanced tests belong to a separate detached
worktree at `d3ae318d37de4ad1199ab385cfc8e67f6ce3c4aa`, not the preserved working copy.
No credentials, live account or remote CI secret is needed for these tests.
