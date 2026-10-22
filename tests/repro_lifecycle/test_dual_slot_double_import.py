"""Repro: one plugin package that provides BOTH a memory provider and a context engine is
imported once per loader, under different module names, so the two halves see separate copies
of module-level state.

Real entry points exercised (the ones agent init calls):
  * memory:  plugins.memory.load_memory_provider(name)           (agent/agent_init.py ~:1355)
             -> _load_package -> module name _hermes_user_memory.<dir>__source_<sha16>
                (plugins/memory/__init__.py _module_name ~:104-110)
  * engine:  agent.agent_init._select_context_engine(agent_cfg)  (agent/agent_init.py ~:1904)
             -> plugins.context_engine.load_context_engine(name)
             -> module name _hermes_user_context_engine.<dir>
                (plugins/context_engine/__init__.py _load_engine_from_dir ~:76)

Nothing is stubbed: no LLM/provider call is made; the fixture plugin is pure Python.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

PLUGIN = "dualslot"

STATE_PY = '''
"""Module-level state shared by BOTH halves of the plugin (if it is imported once)."""
LOADS = []      # __name__ of every execution of the package __init__
INSTANCES = []  # (kind, id) of every provider/engine constructed
'''

INIT_PY = '''
"""dualslot: a single package that ships a MemoryProvider and a ContextEngine."""
from agent.memory_provider import MemoryProvider
from agent.context_engine import ContextEngine

from . import state

state.LOADS.append(__name__)


class DualMemory(MemoryProvider):
    def __init__(self):
        state.INSTANCES.append(("memory", id(self)))

    @property
    def name(self):
        return "dualslot"

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []


class DualEngine(ContextEngine):
    def __init__(self):
        state.INSTANCES.append(("engine", id(self)))

    @property
    def name(self):
        return "dualslot"

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        return False

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False, memory_context=""):
        return messages


def register(ctx):
    ctx.register_memory_provider(DualMemory())
    ctx.register_context_engine(DualEngine())
'''

PLUGIN_YAML = '''
name: dualslot
version: 0.0.1
description: repro plugin providing a memory provider and a context engine
'''

CONFIG_YAML = '''
memory:
  provider: dualslot
context:
  engine: dualslot
'''


@pytest.fixture
def dualslot_home():
    home = Path(os.environ["HERMES_HOME"])
    pdir = home / "plugins" / PLUGIN
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.yaml").write_text(textwrap.dedent(PLUGIN_YAML).lstrip())
    (pdir / "__init__.py").write_text(textwrap.dedent(INIT_PY).lstrip())
    (pdir / "state.py").write_text(textwrap.dedent(STATE_PY).lstrip())
    (home / "config.yaml").write_text(textwrap.dedent(CONFIG_YAML).lstrip())
    before = set(sys.modules)
    yield home
    # Drop only the modules this test created so repeated tests start clean.
    for name in set(sys.modules) - before:
        if PLUGIN in name:
            sys.modules.pop(name, None)


def _load_memory():
    from plugins.memory import load_memory_provider

    return load_memory_provider(PLUGIN)


def _load_engine():
    from agent.agent_init import _select_context_engine

    return _select_context_engine({"context": {"engine": PLUGIN}})


def _pkg(obj):
    """The package module object the instance's class was defined in."""
    return sys.modules[type(obj).__module__]


def test_memory_provider_and_context_engine_share_one_module_copy(dualslot_home):
    provider = _load_memory()
    engine = _load_engine()
    assert provider is not None, "memory loader did not load the dualslot provider"
    assert engine is not None, "context engine selection did not load the dualslot engine"

    mem_mod = _pkg(provider)
    eng_mod = _pkg(engine)
    mem_state = mem_mod.state
    eng_state = eng_mod.state
    loaded_names = sorted(n for n in sys.modules if PLUGIN in n and not n.endswith(".state"))

    print(f"memory provider module : {mem_mod.__name__}")
    print(f"context engine module  : {eng_mod.__name__}")
    print(f"dualslot modules in sys.modules: {loaded_names}")
    print(f"package __init__ executions (state.LOADS per copy): mem={mem_state.LOADS} eng={eng_state.LOADS}")

    assert mem_mod is eng_mod and mem_state is eng_state, (
        "DOUBLE IMPORT: one plugin package was executed under two module names, so its "
        "memory-provider half and context-engine half hold separate module-level state. "
        f"memory loader (plugins/memory/__init__.py _module_name) imported it as {mem_mod.__name__!r}; "
        f"context-engine loader (plugins/context_engine/__init__.py _load_engine_from_dir) imported it as "
        f"{eng_mod.__name__!r}. state.py identity: mem={id(mem_state):#x} eng={id(eng_state):#x}; "
        f"memory copy saw INSTANCES={mem_state.INSTANCES}, engine copy saw INSTANCES={eng_state.INSTANCES}."
    )


def test_control_each_loader_reuses_its_own_copy(dualslot_home):
    """Control (passes on main): within ONE slot the loader caches by module name, so a second
    load returns classes from the same module object. The split is only across slots."""
    p1, p2 = _load_memory(), _load_memory()
    e1, e2 = _load_engine(), _load_engine()
    assert p1 is not None and p2 is not None and e1 is not None and e2 is not None
    assert _pkg(p1) is _pkg(p2), "memory loader re-imported its own package"
    assert _pkg(e1) is _pkg(e2), "context-engine loader re-imported its own package"
    assert _pkg(p1).state.LOADS == [_pkg(p1).__name__]
    assert _pkg(e1).state.LOADS == [_pkg(e1).__name__]
