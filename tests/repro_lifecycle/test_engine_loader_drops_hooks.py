"""Repro: a context engine loaded by the context-engine directory loader cannot register
plugin hooks or tools, while the memory-provider loader forwards the same calls.

Mechanism on upstream main:
- ``plugins/context_engine/__init__.py`` ``_load_engine_from_dir`` passes
  ``_EngineCollector(engine_name=name)`` to ``plugin_loader.instance_from_module``.
- ``_EngineCollector`` subclasses ``plugins/plugin_loader.py`` ``NoopPluginContext``, whose
  ``register_tool = register_hook = register_cli_command = register_memory_provider = _noop``.
  Only ``register_context_engine`` and ``register_command`` are overridden, so an engine's
  ``ctx.register_hook(...)`` / ``ctx.register_tool(...)`` silently do nothing.
- ``plugins/memory/__init__.py`` ``_ProviderCollector`` forwards ``register_hook`` through
  ``PluginManager._register_fallback_hook`` and every other ``register_*`` call to a real
  ``PluginContext`` via ``__getattr__``.

Entry points used are the real ones: ``plugins.context_engine.load_context_engine`` (what
``agent.agent_init._select_context_engine`` calls for ``context.engine: <name>``) and
``plugins.memory.load_memory_provider``. Nothing is stubbed; no LLM is called.
"""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

HOOK = "pre_llm_call"

_ENGINE_SRC = dedent('''
    from agent.context_engine import ContextEngine

    def _engine_pre_llm_call(**kwargs):
        return None
    _engine_pre_llm_call.__repro_marker__ = "{marker}"

    def _engine_tool_handler(args, **kwargs):
        return "ok"

    class ReproEngine(ContextEngine):
        @property
        def name(self):
            return "{name}"
        def update_from_response(self, usage):
            pass
        def should_compress(self, prompt_tokens=None):
            return False
        def compress(self, messages, current_tokens=None):
            return messages

    def register(ctx):
        ctx.register_context_engine(ReproEngine())
        ctx.register_hook("pre_llm_call", _engine_pre_llm_call)
        ctx.register_tool(
            name="{tool}", toolset="{name}",
            schema={{"name": "{tool}", "description": "repro", "parameters": {{"type": "object", "properties": {{}}}}}},
            handler=_engine_tool_handler,
        )
''')

_MEMORY_SRC = dedent('''
    from agent.memory_provider import MemoryProvider

    def _memory_pre_llm_call(**kwargs):
        return None
    _memory_pre_llm_call.__repro_marker__ = "{marker}"

    def _memory_tool_handler(args, **kwargs):
        return "ok"

    class ReproMemory(MemoryProvider):
        @property
        def name(self):
            return "{name}"
        def is_available(self):
            return True
        def initialize(self, session_id, **kwargs):
            pass
        def get_tool_schemas(self):
            return []

    def register(ctx):
        ctx.register_memory_provider(ReproMemory())
        ctx.register_hook("pre_llm_call", _memory_pre_llm_call)
        ctx.register_tool(
            name="{tool}", toolset="{name}",
            schema={{"name": "{tool}", "description": "repro", "parameters": {{"type": "object", "properties": {{}}}}}},
            handler=_memory_tool_handler,
        )
''')


def _home() -> Path:
    home = Path(os.environ["HERMES_HOME"])
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    return home


def _write_plugin(name: str, src: str) -> Path:
    d = _home() / "plugins" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text(src)
    return d


def _markers(hook: str) -> set[str]:
    from hermes_cli.plugins import get_plugin_manager
    return {getattr(cb, "__repro_marker__", None) for cb in get_plugin_manager()._hooks.get(hook, [])}


def _plugin_tools() -> set[str]:
    from hermes_cli.plugins import get_plugin_manager
    return set(get_plugin_manager()._plugin_tool_names)


def test_engine_loader_forwards_register_hook_and_register_tool():
    """RED on main: _EngineCollector inherits NoopPluginContext's no-op register_hook/register_tool."""
    name, marker, tool = "repro_ctx_engine", "engine-hook-marker", "repro_ctx_engine_tool"
    _write_plugin(name, _ENGINE_SRC.format(name=name, marker=marker, tool=tool))

    from plugins.context_engine import _EngineCollector, load_context_engine
    from plugins.plugin_loader import NoopPluginContext

    engine = load_context_engine(name)
    # The engine itself loads fine through the real entry point; only its registrations vanish.
    assert engine is not None and engine.name == name, "precondition: engine must load via load_context_engine"

    hook_registered = marker in _markers(HOOK)
    tool_registered = tool in _plugin_tools()
    mechanism = (
        "plugins/context_engine/__init__.py _EngineCollector subclasses plugins/plugin_loader.py "
        "NoopPluginContext (register_tool = register_hook = register_cli_command = _noop); "
        f"_EngineCollector.register_hook is NoopPluginContext._noop: "
        f"{_EngineCollector.register_hook is NoopPluginContext._noop}, "
        f"register_tool is _noop: {_EngineCollector.register_tool is NoopPluginContext._noop}"
    )
    assert hook_registered and tool_registered, (
        f"context engine loaded via load_context_engine({name!r}) called ctx.register_hook({HOOK!r}) "
        f"and ctx.register_tool({tool!r}), but hook_registered={hook_registered}, "
        f"tool_registered={tool_registered} on the plugin manager. Mechanism: {mechanism}"
    )


def test_engine_loader_via_select_context_engine_drops_hook():
    """RED on main: same, through the agent's config-driven selection (context.engine=<name>)."""
    name, marker, tool = "repro_ctx_engine_sel", "engine-sel-hook-marker", "repro_ctx_engine_sel_tool"
    _write_plugin(name, _ENGINE_SRC.format(name=name, marker=marker, tool=tool))

    from agent.agent_init import _select_context_engine

    engine = _select_context_engine({"context": {"engine": name}})
    assert engine is not None and engine.name == name, "precondition: engine selected by context.engine"
    assert marker in _markers(HOOK), (
        f"_select_context_engine(context.engine={name!r}) -> load_context_engine -> _EngineCollector "
        f"(NoopPluginContext) silently dropped ctx.register_hook({HOOK!r}); no callback reached "
        "PluginManager._hooks"
    )


def test_control_memory_loader_forwards_register_hook_and_register_tool():
    """CONTROL (green on main): same register(ctx) shape loaded as a memory provider forwards both."""
    name, marker, tool = "repro_mem_provider", "memory-hook-marker", "repro_mem_provider_tool"
    _write_plugin(name, _MEMORY_SRC.format(name=name, marker=marker, tool=tool))

    from plugins.memory import load_memory_provider

    provider = load_memory_provider(name)
    assert provider is not None and provider.name == name
    assert marker in _markers(HOOK), (
        "memory loader _ProviderCollector.register_hook -> PluginManager._register_fallback_hook "
        "should have registered the hook"
    )
    assert tool in _plugin_tools(), (
        "memory loader _ProviderCollector.__getattr__ should forward register_tool to a real PluginContext"
    )
