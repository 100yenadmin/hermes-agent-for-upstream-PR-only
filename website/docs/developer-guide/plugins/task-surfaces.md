---
title: Scoped task surfaces
---

# Scoped task surfaces

These optional host interfaces let an external presentation plugin render live todo progress,
durable canonical task cards, bounded blocker decisions and read-only task detail. They do not
add a model tool, polling loop or task database. The Telegram Experience plugin is the concrete
consumer; its renderers and browser assets remain outside the host repository.

## Registration and ownership

`PluginContext` exposes four local capability versions, each currently `2`:
`live_todo_capability`, `task_card_capability`, `task_decision_capability` and
`task_read_capability`. A consumer checks the capability it needs before registering; it must
not fall back to monkey-patching an unsupported host.

| Registration | Ownership |
| --- | --- |
| `register_live_todo(factory, scope=...)` | Host binds one live run and transport; plugin renders todo snapshots. |
| `register_task_cards(factory, scope=...)` | Host owns canonical snapshots, subscription generation, receipt leases and reconciliation. |
| `register_task_decisions(callback_prefix="task:")` | Requires the same plugin's active card registration; host authenticates native callbacks and performs the bounded transition. |
| `register_task_detail(scope=...)` | Host authenticates Telegram identity and projects bounded canonical fields through the existing API application. |

Factories receive host-owned delivery handles, not bot credentials or an unrestricted adapter.
Unload callbacks revoke new admissions, including cleanup after a failed plugin registration.
A configuration-only disable is not a substitute for unloading/restarting the running consumer.

A plugin that removes its own native handlers during unload can register its platform factory
with `reload_safe=True` (also supported by `register_telegram_handler`). The host then keys wiring
to that registration generation, allowing the replacement to wire once on the same native client.
This is an explicit cleanup contract: the plugin must remove its old handlers. The default remains
qualname-based deduplication across rediscovery, preserving existing plugins that do not own cleanup.
Rebuilding the native client still wires the current factories afresh.

## Exact admission scope

Both keys are required, including for a todo-only consumer:

```yaml
scope:
  routes:
    - profile: default
      platform: telegram
      chat_id: "-1000000000001"
      thread_id: "7"
  task_resources:
    - board: default
      task_id: t_0123abcd
```

These are synthetic examples. Profile, platform, chat and topic match exactly. A null topic is
an exact no-topic route, never a wildcard. Missing, invalid or duplicate entries deny registration.
Todo-only use can supply an empty task resource list; cards/detail need an explicit resource.
The host checks scope before todo binding, card receipt/cursor ownership, and transport admission.
Out-of-scope subscriptions retain their existing ordinary notification path.

Scope does not grant reads or actions. `kanban.decision_grants` authorizes one actor and exact
profile/board/task for `unblock_needs_input`; native actor, topic, message, receipt and current
policy must still match. Replayed or revoked controls cannot repeat a canonical transition.
`kanban.read_grants` separately binds the actor, profile, board, task incarnation and read
permission. Telegram-signed initData establishes identity, not resource authority.

## Persistence and recovery

Durable receipts and action audit records use the existing canonical board database. A sent card
is reconciled by its verified message ID. Ambiguous dispatched attempts are quarantined rather
than blindly resent. Legacy ambiguous receipt migration does not invent a successful send.
Live todo bubbles are run-scoped and are not reconstructed after a process crash.

Read-only detail excludes task bodies, transcripts and arbitrary metadata. It never mutates
canonical task/event/action data; normal SQLite read-only WAL access may create coordination files.
Do not use SQLite `immutable=1` on a live WAL database.

A host rollback must preserve current databases and legitimate activity. Do not restore an old
whole-profile snapshot as a routine code rollback. Prove old-host reopening for the exact tested
schema and retain the host/patch identity alongside the separately installed plugin artifact.

## Validation

Host contract tests cover admission, policy revocation, transport fences, durable receipts,
recovery and bounded decisions. Tests requiring the separate plugin skip when it is absent;
the plugin repository's installed-host CI runs those against its wheel. Browser assets and
real Telegram/Mini App/mobile acceptance belong to that consumer's delivery gate. Unit tests
or a patched host alone do not establish live client behavior or catalog availability.
