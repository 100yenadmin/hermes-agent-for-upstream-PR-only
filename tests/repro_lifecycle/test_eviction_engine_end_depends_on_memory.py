"""Repro: gateway cache eviction tells the context engine the session ended ONLY when an external
memory provider is configured.

Mechanism on upstream main:
  gateway/run_agent_cache.py  _sweep_idle_cached_agents (idle TTL) / _enforce_agent_cache_cap (LRU) /
  _release_pressure_batch (pressure) all call ``_commit_then_release_soft(agent, key)`` with the
  default ``boundary=False``  ->  ``_commit_memory_before_soft_evict`` computes
      has_provider = agent._memory_manager is not None
      has_engine   = boundary and agent.context_compressor is not None   # False on eviction
  and returns early when neither is true. ``commit_memory_session`` (run_agent.py) is the only call
  that reaches ``_notify_context_engine_session_end``, so the engine's ``on_session_end`` fires as a
  SIDE EFFECT of a memory provider being present.

Red tests: the same eviction with no memory provider -> engine never hears on_session_end.
Controls: (a) same eviction WITH a memory provider -> engine hears it; (b) the reset path
(``boundary=True``) -> engine hears it with no provider.

Real code exercised: GatewayRunner eviction methods (on a ``__new__`` runner, as tests/gateway do),
real ``AIAgent`` (no network: no turn is run), real ``MemoryManager``. Stubs: a recording
ContextEngine subclass installed as ``agent.context_compressor`` and a recording MemoryProvider.
"""

import threading
import time
from collections import OrderedDict

import pytest

from agent.context_engine import ContextEngine


class _RecordingEngine(ContextEngine):
    def __init__(self, events):
        self.events = events

    @property
    def name(self):
        return "recording"

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        return False

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False, memory_context=""):
        return messages

    def on_session_end(self, session_id, messages):
        self.events.append(("engine.on_session_end", session_id))


def _recording_provider(events):
    from agent.memory_provider import MemoryProvider

    class _Provider(MemoryProvider):
        @property
        def name(self):
            return "recordingmem"

        def is_available(self):
            return True

        def initialize(self, session_id, **kwargs):
            pass

        def get_tool_schemas(self):
            return []

        def on_session_end(self, messages):
            events.append(("memory.on_session_end",))

        def shutdown(self):
            events.append(("memory.shutdown",))

    return _Provider()


def _agent(session_id, events, with_memory):
    from agent.memory_manager import MemoryManager
    from run_agent import AIAgent

    agent = AIAgent(
        model="anthropic/claude-sonnet-4", api_key="test",
        base_url="https://openrouter.ai/api/v1", provider="openrouter",
        max_iterations=5, quiet_mode=True,
        skip_context_files=True, skip_memory=True, session_id=session_id,
    )
    agent.context_compressor = _RecordingEngine(events)
    agent._session_messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    if with_memory:
        manager = MemoryManager()
        manager.add_provider(_recording_provider(events))
        agent._memory_manager = manager
    else:
        agent._memory_manager = None
    agent._last_activity_ts = time.time() - 10_000  # idle far past any TTL
    return agent


def _runner():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    return runner


def _join_release_threads(prefix):
    for t in list(threading.enumerate()):
        if t.name.startswith(prefix):
            t.join(timeout=10)


def _evict_idle(runner):
    assert runner._sweep_idle_cached_agents() == 2
    _join_release_threads("agent-cache-idle-")


def _evict_lru(runner, monkeypatch):
    from gateway import run as gw_run

    monkeypatch.setattr(gw_run, "_AGENT_CACHE_MAX_SIZE", 1)
    runner._agent_cache_bounds_cache = None
    from gateway.agent_cache_pressure import resolve_agent_cache_bounds
    runner._agent_cache_bounds_cache = resolve_agent_cache_bounds({})
    # A third, fresh entry pushes both test agents past the cap of 1.
    runner._agent_cache["agent:main:telegram:dm:fresh"] = (None, "sig")
    runner._agent_cache.move_to_end("agent:main:telegram:dm:fresh")
    with runner._agent_cache_lock:
        runner._enforce_agent_cache_cap()
    _join_release_threads("agent-cache-evict-")


def _evict_pressure(runner):
    # _release_pressure_batch is what the pressure valve's daemon thread runs; call it directly.
    plan = [(k, v[0]) for k, v in list(runner._agent_cache.items())]
    runner._agent_cache.clear()
    runner._release_pressure_batch(plan)


@pytest.mark.parametrize("path", ["idle_ttl", "lru_cap", "pressure"])
def test_evicted_engine_hears_session_end_with_and_without_memory(path, monkeypatch):
    with_mem_events, no_mem_events = [], []
    a_mem = _agent("sess-with-memory", with_mem_events, with_memory=True)
    a_nomem = _agent("sess-no-memory", no_mem_events, with_memory=False)
    runner = _runner()
    runner._agent_cache["agent:main:telegram:dm:with-mem"] = (a_mem, "sig")
    runner._agent_cache["agent:main:telegram:dm:no-mem"] = (a_nomem, "sig")
    try:
        if path == "idle_ttl":
            _evict_idle(runner)
        elif path == "lru_cap":
            _evict_lru(runner, monkeypatch)
        else:
            _evict_pressure(runner)

        assert "agent:main:telegram:dm:with-mem" not in runner._agent_cache
        assert "agent:main:telegram:dm:no-mem" not in runner._agent_cache

        # CONTROL: with a memory provider, eviction reaches the engine via commit_memory_session.
        assert ("engine.on_session_end", "sess-with-memory") in with_mem_events, with_mem_events

        # RED on main: same eviction, no memory provider -> engine never told.
        assert ("engine.on_session_end", "sess-no-memory") in no_mem_events, (
            f"[{path}] context engine on_session_end NOT delivered for the evicted agent without a memory "
            "provider (with-memory sibling DID get it): gateway/run_agent_cache.py "
            "_commit_memory_before_soft_evict returns early because has_provider=False and "
            "has_engine requires boundary=True, which eviction never passes; the engine end only rides "
            f"on commit_memory_session. events={no_mem_events}"
        )
    finally:
        a_mem.close()
        a_nomem.close()


def test_control_reset_boundary_reaches_engine_without_memory():
    """CONTROL: the reset/compress path passes boundary=True, so the engine hears on_session_end even
    with no memory provider. Shows the engine-end path exists and is gated only by ``boundary``."""
    events = []
    agent = _agent("sess-reset-no-memory", events, with_memory=False)
    runner = _runner()
    try:
        runner._commit_then_release_soft(agent, "agent:main:telegram:dm:reset", True)
        assert ("engine.on_session_end", "sess-reset-no-memory") in events, events
    finally:
        agent.close()
