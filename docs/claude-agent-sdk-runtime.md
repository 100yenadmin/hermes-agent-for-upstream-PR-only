# Claude Agent SDK Runtime

> **Audience:** Gateway developers and maintainers
> **Source files:** `agent/claude_sdk_runtime.py`, `agent/transports/claude_agent_sdk_session.py`, `gateway/run.py`
> **Last updated:** 2026-08-16

## Overview

The `claude-agent-sdk` provider is not an API client. It spawns the **Claude Code
CLI as a subprocess** and drives it over stream-json. That single fact produces
most of this lane's surprises: the runtime owns a real child process with its own
lifecycle, its own context window, and its own compaction — none of which Hermes
controls.

Every other provider is a stateless HTTP call. This one is a long-lived process
tree.

Two modules matter:

- `agent/claude_sdk_runtime.py` — `run_claude_agent_sdk_turn()`, the turn loop.
  Owns prompt assembly, the compaction status edges, and budget enforcement.
- `agent/transports/claude_agent_sdk_session.py` — `ClaudeAgentSdkSession`,
  the process/option layer. Owns option construction, the environment handed to
  the child, hooks, and teardown.

---

## 1. Configuration

All keys live under `agent.claude_agent_sdk` in `config.yaml`.

| Key | Type | Default | Effect |
|---|---|---|---|
| `streaming` | bool | off | Stream partial output rather than delivering at turn end. |
| `permission_mode` | str | *(SDK default)* | Passed to the SDK **verbatim**; validated against the installed SDK's literals (`default`, `acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `auto`). An invalid value is rejected rather than guessed. |
| `setting_sources` | list | *(none)* | Which on-disk setting sources the CLI may read. Empty by default — opt in explicitly with `["user"]`. Unknown entries are dropped with a warning. |
| `append_file` | path | *(none)* | Operator persona/guidance file appended to the system prompt. Set-but-unreadable warns rather than silently continuing. |
| `allow_metered_key` | bool | false | Explicit "bill me metered" opt-in. Disables the credential scrub (§5). |
| `deliver_background_results` | bool | false | Deliver results produced by background work. |
| `max_budget_usd` | float | *(none)* | Forwarded to the SDK's `max_budget_usd`; the query stops with `error_max_budget_usd` once exceeded. Non-numeric, non-positive, and boolean values are ignored with a warning — a `0` cap would fail every turn instantly, and YAML `true` would `float()` to a nonsense `1.0`. |
| `env` | mapping | `{}` | Arbitrary environment passed to the CLI subprocess (§3). |

---

## 2. Context is owned by the CLI, not by Hermes

**Hermes does not compact this lane.** `conversation_compression` short-circuits
when `api_mode == "claude_agent_sdk"`, because Hermes summarizing its own copy of
the transcript cannot shrink the context the CLI is actually sending. The gateway
logs the skip:

```
Session hygiene: skipping compression for <session>; the claude-agent-sdk lane
compacts inside the CLI, so Hermes compaction cannot shrink it
```

Two consequences that have each caused real incidents:

**Hermes' own token estimate is wrong here** — it has over-reported by ~10×
(1.5–2.4M for a ~111k transcript). Never size a decision on it. Ask the CLI:
`ClaudeAgentSdkSession.context_usage()` returns the CLI's ground truth
(`maxTokens`, `contextWindow`, `autoCompactThreshold`, `isAutoCompactEnabled`).

**Routine hygiene must not run here.** A no-op compression pass that still
evicted the cached agent cost ~273k cache-write tokens *per turn* — pure waste,
invisible in logs.

---

## 3. Autocompact knobs (`env`)

The CLI reads operational knobs from its environment that the SDK exposes no
typed option for. `agent.claude_agent_sdk.env` is a generic passthrough:

```yaml
agent:
  claude_agent_sdk:
    env:
      CLAUDE_CODE_AUTO_COMPACT_WINDOW: '300000'
```

It is deliberately generic because **most of these knobs do not work.** Measured
against `claude-agent-sdk 0.2.120` by probing `get_context_usage()` on fresh
clients:

| Variable | `maxTokens` | threshold | Verdict |
|---|---|---|---|
| *(baseline)* | 1,000,000 | 967,000 | — |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000` | **300,000** | **267,000** | **works** |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS=300000` | 1,000,000 | 967,000 | inert |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50` | 1,000,000 | 967,000 | inert |
| `CLAUDE_CODE_DISABLE_AUTO_COMPACT` | 1,000,000 | `None` | disables |

Three of four plausible knobs do nothing, and they are undocumented enough to
shift between CLI builds. That is the case for a config surface over a named
option per knob — and the case for verifying any knob with `context_usage()`
rather than trusting its name.

**The default threshold is 967,000 — 96.7% of the window, not the ~80% one would
assume.** This is the single most misleading default on the lane: it makes
autocompaction look broken at 600–700k when it is simply not due yet.

Lowering the window is a real trade. Measured on a live session, dropping to
300k moved `cache_read` from 678,625 to 36,516 per request. But 36k is the
*post-compaction floor*, climbing back toward the threshold — the honest
steady-state saving is ~75–80%, not 95%. The cost is a ~2 minute stall per
compaction, arriving ~3.6× more often (267k of growth per cycle instead of 967k).

---

## 4. Child process lifecycle

`close()` must outlive the SDK's own shutdown ladder. `_SDK_DISCONNECT_TIMEOUT_S`
is **25.0s** for exactly this reason: a shorter timeout than the SDK's internal
~20s ladder abandons the child mid-shutdown and strands a ~260 MB process that
GC can never reap.

If disconnect still fails, teardown escalates: `SIGTERM` → 5s → `SIGKILL`. Two
guards apply before any signal:

- `_is_own_sdk_child(pid)` — the PID must be a live child of *this* process.
  Guards against PID reuse killing an unrelated process.
- A **zombie counts as already dead** — it holds no RSS and needs no signal.

Prefer `release_clients()` (soft) over `close()` (hard) where the sandbox,
browser, and background processes should survive.

---

## 5. Billing safety

The lane is a subscription lane. `_scrubbed_sdk_env()` blanks every metered
billing vector present in the parent environment (`ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, the Bedrock/Vertex switches, AWS credentials,
`GOOGLE_APPLICATION_CREDENTIALS`). Only keys **actually present** are blanked —
writing `""` for absent ones can itself confuse credential chains.

`allow_metered_key: true` is the operator's explicit opt-in and disables the
scrub, since the documented escape hatch would otherwise hand the CLI a blanked
key.

**Ordering matters.** Configured `env` is applied *after* the scrub so deliberate
knobs win over defaults — but a plain `update()` would let
`env: {ANTHROPIC_API_KEY: ...}` overwrite the scrub's `""` and silently re-arm
metered billing behind `allow_metered_key: false`, from a file that looks like it
only holds tuning knobs. `_sdk_env_overrides()` therefore drops denylisted keys
with a warning unless the metered opt-in is set.

That merge lives in a module-level function rather than inline in
`build_option_fields()` specifically so the guard is testable — see
`tests/agent/test_claude_sdk_configured_env.py`.

> Separately: the SDK serializes the stdio MCP config — env included — onto the
> child's argv, readable by any local user via `ps`. That env is a strict
> allowlist and must never carry a secret.

---

## 6. Compaction visibility

Because Hermes does not compact here, a turn can stall for two minutes inside a
CLI compaction with nothing to show the user. The SDK's `PreCompact` hook is the
only honest signal.

`_build_compaction_hooks()` registers it **only when `on_compaction` is wired**,
so the default option set is unchanged for callers that do not want it. The hook:

- announces `auto` triggers only — a manual `/compact` is the user's own action
  and already has feedback;
- **always returns `{}`**, because refusing a hook can block the compaction
  itself;
- reuses `COMPACTION_STATUS` / `COMPACTION_DONE_STATUS` from
  `agent/conversation_compression.py`. This is not stylistic: the gateway's
  Telegram noise filter is **built from those same constants**, so a re-inlined
  string is silently dropped on chat surfaces.

The completion edge fires after the turn loop breaks — a completed turn is the
terminal edge, since the CLI cannot produce a result without finishing a
compaction it started.

On chat surfaces these are gated behind `compression.progress_notices: true`.

---

## 7. Agent cache interaction

The gateway caches one agent per session key. Every **eviction** path releases
what it pops, but a plain cache **overwrite** originally released nothing,
dropping the displaced agent's provider session to GC. On this lane that means an
orphaned CLI subprocess. Measured: 13 turns produced 11 SDK sessions but only 2
closes — 11 orphans holding 2.9 GB.

It was self-reinforcing. Orphans pushed RSS past `memory_high_mb`, triggering
memory-pressure sweeps, which displaced more agents.

`_release_displaced_agent()` now handles this. It skips `None` and the pending
sentinel, **skips mid-turn agents** (their own completion path owns teardown —
releasing one kills a live turn), releases on a daemon thread with contained
exceptions, and falls back to an inline release when a thread cannot start
(interpreter shutdown).

---

## 8. Observability

> Both `_sweep_agent_cache_under_pressure` and `_evict_cached_agent` contain
> **zero `logger.` calls.** That is why the leaks above hid for so long, and why
> the tests are the only regression guard.

The gateway logs inbound messages but **never logs an outbound send**
(`grep -c "outbound\|sending message\|sent message" gateway.log` → `0`). A path
that talks to chat and writes nothing to disk is unfalsifiable after the fact:
after a real compaction it was impossible to determine whether the completion
notice had reached the user, because the only witness was someone watching the
screen. Both compaction emit paths now log, including the silent-return branch
where `status_callback` is absent — that is the branch that actually loses the
notice.

When adding a user-visible edge on this lane, log it. Chat delivery is not
evidence.

### Triage

| Symptom | First check |
|---|---|
| Idle `claude` subprocesses accumulating | Orphan reap (§4) and cache displacement (§7) |
| Context never compacts | `context_usage()` — the default threshold is 967,000, not 80% |
| Compaction knob has no effect | It is probably inert (§3); confirm with `context_usage()` |
| Notice never appeared | `compression.progress_notices`; confirm wording is single-sourced (§6) |
| Cache-write cost per turn | Hygiene should be skipped on this lane (§2) |

---

## 9. Testing

| File | Covers |
|---|---|
| `tests/agent/test_claude_sdk_child_reap.py` | Disconnect timeout, PID-reuse guard, zombie handling |
| `tests/agent/test_claude_sdk_context_usage.py` | CLI ground-truth context reporting |
| `tests/agent/test_claude_sdk_compaction_status.py` | PreCompact hook, trigger forwarding, status single-sourcing, emit logging |
| `tests/agent/test_claude_sdk_configured_env.py` | `env` passthrough, stringification, metered-denylist guard |
| `tests/gateway/test_agent_cache_displacement.py` | Displaced-agent release, mid-turn protection |

**Known gap:** the compaction *start* log sits in a closure inside
`run_claude_agent_sdk_turn()` and cannot be exercised without standing up a full
turn. It is left to production verification rather than covered by a source-text
assertion, which would claim coverage without evidence the line ever runs.
