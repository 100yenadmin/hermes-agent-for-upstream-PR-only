"""Repro: a rewind (/undo) through the canonical gateway paths never tells memory providers.

On upstream main the only rewind paths that notify memory providers with
``on_session_switch(..., rewound=True)`` are the legacy CLI ``undo_last``
(hermes_cli/cli_session_mixin.py) and the TUI ``command.dispatch /undo``
(tui_gateway/methods_tools.py). The canonical ``session.mutate`` rewind
(gateway/session_mutations.py ``_project_result``) and the messaging ``/undo``
(gateway/slash_commands_session.py ``_handle_undo_command``) only call
``runner._evict_cached_agent(route)``, a plain evict that releases clients and
fires no memory hook.

Red tests assert the DESIRED behaviour (provider told ``rewound=True``).
Control test shows the CLI sibling path that does notify.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
import threading

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class RecordingProvider(MemoryProvider):
    """External memory provider that records every lifecycle notification."""

    def __init__(self):
        self.switches = []

    @property
    def name(self) -> str:
        return "recorder"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def on_session_switch(self, new_session_id, *, parent_session_id="", reset=False, **kwargs):
        self.switches.append({"session_id": new_session_id, "reset": reset, **kwargs})


def _manager_with_recorder():
    mm = MemoryManager()
    provider = RecordingProvider()
    mm.add_provider(provider)
    return mm, provider


def _cached_agent(mm, released):
    agent = SimpleNamespace(_memory_manager=mm, _session_messages=[{"role": "user", "content": "hi"}])
    agent.release_clients = lambda: released.append(True)
    agent.commit_memory_session = lambda msgs=None: None
    return agent


def _runner(monkeypatch, agent, route):
    """A ``GatewayRunner`` with the REAL ``_evict_cached_agent``; only the release thread is
    run inline (stub) so the eviction is observable synchronously."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._draining = False
    runner._agent_cache = {route: (agent, "sig")}
    runner._agent_cache_lock = threading.Lock()
    runner._peek_session_state = lambda key: None
    runner._running_agent_ids = lambda: set()
    monkeypatch.setattr(runner, "_spawn_release_thread",
                        lambda target, args, name, *, inline_fallback, session_key=None: target(*args))
    return runner


def _rewound_calls(provider):
    return [c for c in provider.switches if c.get("rewound") is True]


@pytest.mark.asyncio
async def test_canonical_session_mutate_rewind_notifies_memory_provider(tmp_path, monkeypatch):
    """RED on main: session.mutate rewind evicts the cached agent but never calls
    on_session_switch(rewound=True) on its memory manager."""
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state import SessionDB
    import hermes_state_runtime as rt

    mm, provider = _manager_with_recorder()
    released = []
    agent = _cached_agent(mm, released)
    runner = _runner(monkeypatch, agent, "route")

    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s", source="test")
        db.append_message("s", role="user", content="first ask")
        db.append_message("s", role="assistant", content="first answer")
        db.append_message("s", role="user", content="second ask")
        db.append_message("s", role="assistant", content="second answer")
        target = [m for m in db.get_messages("s") if m["content"] == "second ask"][0]["id"]
        epoch = rt.begin_runtime_epoch(db, instance_id="owner")
        authority = SessionAuthority(runner, profile_id="owned", instance_id="owner", db=db, epoch=epoch)
        authority.sessions["s"] = LiveSession(None, "route")
        conn = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {"user_id": "human"})
        try:
            response = await conn.dispatch({"id": 1, "method": "session.mutate", "params": {
                "session_id": "s", "request_id": "rewind-1", "expected_revision": 0,
                "expected_generation": 0, "operation": "rewind",
                "payload": {"target_message_id": target}}})
        finally:
            await conn.close()
        remaining = [m["content"] for m in db.get_messages("s")]

    # Positive controls: the rewind committed and the canonical projection evicted the cached agent.
    assert "result" in response, response
    assert "second ask" not in remaining and "first ask" in remaining, remaining
    assert "route" not in runner._agent_cache and released == [True], \
        "rewind did not reach _project_result's _evict_cached_agent"

    assert _rewound_calls(provider), (
        "session.mutate rewind (gateway/session_mutations.py _project_result -> "
        "authority.runner._evict_cached_agent(live.route)) only evicted the cached agent; "
        "no on_session_switch(..., rewound=True) reached the memory provider "
        f"(provider saw: {provider.switches!r})")


@pytest.mark.asyncio
async def test_messaging_undo_notifies_memory_provider(monkeypatch):
    """RED on main: messaging /undo (_handle_undo_command) evicts the cached agent but never
    notifies its memory manager. Stub: the session store's rewind (a fake returning a rewind
    result) — the DB truncation is not under test, only the post-rewind notification."""
    mm, provider = _manager_with_recorder()
    released = []
    agent = _cached_agent(mm, released)
    runner = _runner(monkeypatch, agent, "route")

    entry = SimpleNamespace(session_id="s", last_prompt_tokens=123)

    async def _get_or_create(source):
        return entry

    async def _rewind(session_id, n):
        return {"target_text": "second ask", "turns_undone": 1, "rewound_count": 2}

    # ``async_session_store`` is a property that reuses ``_async_session_store`` while its
    # ``_store`` is ``runner.session_store``.
    runner.session_store = object()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, get_or_create_session=_get_or_create, rewind_session=_rewind)
    runner._session_key_for_source = lambda source: "route"
    runner._record_model_friction = lambda *a, **k: None
    event = SimpleNamespace(source=SimpleNamespace(platform="telegram"), get_command_args=lambda: "")

    reply = await runner._handle_undo_command(event)

    assert entry.last_prompt_tokens == 0, reply
    assert "route" not in runner._agent_cache and released == [True], \
        "messaging /undo did not evict the cached agent"
    assert _rewound_calls(provider), (
        "messaging /undo (gateway/slash_commands_session.py _handle_undo_command -> "
        "self._evict_cached_agent(...)) only evicted the cached agent; no "
        "on_session_switch(..., rewound=True) reached the memory provider "
        f"(provider saw: {provider.switches!r})")


def test_context_engine_abc_has_a_rewind_notice():
    """RED on main (structural): the ContextEngine ABC exposes no rewind hook, so a context
    engine plugin cannot learn its transcript was truncated by /undo or session.mutate rewind."""
    from agent.context_engine import ContextEngine

    hooks = [n for n in dir(ContextEngine) if "rewind" in n.lower() or "undo" in n.lower()]
    assert hooks, ("agent/context_engine.py ContextEngine defines no rewind/undo lifecycle method "
                   "(has on_session_start/on_session_end/on_session_reset only)")


def test_control_cli_undo_last_notifies_memory_provider(tmp_path):
    """CONTROL (passes on main): the legacy CLI undo_last calls
    _memory_manager.on_session_switch(session_id, ..., rewound=True)."""
    from hermes_state import SessionDB
    from tests.hermes_cli.test_cli_init import _make_cli

    mm, provider = _manager_with_recorder()
    cli = _make_cli()
    cli._session_db.close()
    db = SessionDB(db_path=tmp_path / "state.db")
    cli._session_db = db
    cli.session_id = "cli-undo"
    db.create_session(cli.session_id, source="cli")
    db.append_message(cli.session_id, "user", "first ask")
    db.append_message(cli.session_id, "assistant", "first answer")
    db.append_message(cli.session_id, "user", "second ask")
    db.append_message(cli.session_id, "assistant", "second answer")
    cli.conversation_history = db.get_messages_as_conversation(cli.session_id)
    cli._prefill_input_buffer = MagicMock()
    cli.agent = SimpleNamespace(
        _session_messages=cli.conversation_history,
        _last_flushed_db_idx=len(cli.conversation_history),
        _db_flush_scan_prefix=list(cli.conversation_history),
        _invalidate_system_prompt=MagicMock(),
        _memory_manager=mm,
    )
    try:
        cli.undo_last()
    finally:
        db.close()

    assert _rewound_calls(provider) == [{"session_id": "cli-undo", "reset": False, "rewound": True}], \
        provider.switches
