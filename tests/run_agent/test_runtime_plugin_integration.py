"""External whole-turn runtime integration through the real AIAgent seam."""

from __future__ import annotations

import run_agent

from agent.runtime_api import (
    RUNTIME_API_VERSION,
    RuntimeCancelledEvent,
    RuntimeCompletedEvent,
    RuntimeDescriptor,
    RuntimeFailedEvent,
    RuntimeFailure,
    RuntimeFailurePhase,
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


class _ExternalRuntime:
    def __init__(self, counters):
        self._counters = counters

    def preflight(self, request):
        self._counters["preflight"] += 1
        return None

    async def run_turn(self, request, host):
        self._counters["turn"] += 1
        self._counters["prompt_snapshot"] = request.prompt_snapshot
        self._counters["tool_inventory"] = request.tool_inventory
        self._counters["tool_schema_names"] = tuple(
            schema["function"]["name"] for schema in request.tool_schemas
        )
        yield RuntimeCompletedEvent(
            result={
                "final_response": "external runtime reply",
                "messages": list(request.messages),
                "completed": True,
                "partial": False,
                "error": None,
            }
        )

    async def close(self):
        self._counters["close"] += 1


def test_external_plugin_runtime_is_selected_before_the_ordinary_model_loop(
    monkeypatch,
):
    from tools import tool_search

    monkeypatch.setattr(
        tool_search,
        "load_config",
        lambda: tool_search.ToolSearchConfig.from_raw({"enabled": "off"}),
    )
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(PluginManifest(name="external-runtime"), manager)
    counters = {
        "factory": 0,
        "preflight": 0,
        "turn": 0,
        "close": 0,
        "prompt_snapshot": None,
    }

    def factory():
        counters["factory"] += 1
        return _ExternalRuntime(counters)

    context.register_agent_runtime(
        descriptor=RuntimeDescriptor(
            runtime_id="external-test-runtime",
            plugin_version="0.1.0",
            runtime_api_min=RUNTIME_API_VERSION,
            runtime_api_max=RUNTIME_API_VERSION,
            required_host_capabilities=frozenset({"cancellation_v1"}),
            provider_ids=frozenset({"openai"}),
            api_modes=frozenset({"chat_completions"}),
            session_state_schema_version=1,
        ),
        factory=factory,
    )
    context.register_tool(
        name="synthetic_plugin_tool",
        toolset="synthetic-plugin-tools",
        schema={
            "name": "synthetic_plugin_tool",
            "description": "Synthetic plugin tool",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda: "synthetic",
    )

    import hermes_cli.plugins as plugins_module

    monkeypatch.setattr(plugins_module, "_plugin_manager", manager)
    agent = run_agent.AIAgent(
        api_key="synthetic-test-value",
        base_url="https://test.invalid",
        provider="openai",
        model="synthetic-model",
        api_mode="chat_completions",
        enabled_toolsets=["synthetic-plugin-tools"],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._cached_system_prompt = "composed synthetic prompt"

    result = agent.run_conversation("hello")
    second = agent.run_conversation("again")

    assert result["final_response"] == "external runtime reply"
    assert second["final_response"] == "external runtime reply"
    inventory = counters.pop("tool_inventory")
    tool_schema_names = counters.pop("tool_schema_names")
    assert tuple(entry.name for entry in inventory.tools) == tuple(
        sorted(tool_schema_names)
    )
    plugin_entry = next(
        entry for entry in inventory.tools if entry.name == "synthetic_plugin_tool"
    )
    assert plugin_entry.declared_by == "plugin"
    assert plugin_entry.enabled is True
    assert counters == {
        "factory": 1,
        "preflight": 2,
        "turn": 2,
        "close": 0,
        "prompt_snapshot": "composed synthetic prompt",
    }
    agent.release_clients()
    assert counters["close"] == 1


def test_external_runtime_reply_is_persisted_once_by_host_finalization(
    monkeypatch, tmp_path
):
    from hermes_state import SessionDB

    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(PluginManifest(name="external-runtime"), manager)
    context.register_agent_runtime(
        descriptor=RuntimeDescriptor(
            runtime_id="external-persistence-runtime",
            plugin_version="0.1.0",
            runtime_api_min=RUNTIME_API_VERSION,
            runtime_api_max=RUNTIME_API_VERSION,
            required_host_capabilities=frozenset({"cancellation_v1"}),
            provider_ids=frozenset({"openai"}),
            api_modes=frozenset({"chat_completions"}),
            session_state_schema_version=1,
        ),
        factory=lambda: _ExternalRuntime({
            "preflight": 0,
            "turn": 0,
            "close": 0,
            "prompt_snapshot": None,
        }),
    )

    import hermes_cli.plugins as plugins_module

    monkeypatch.setattr(plugins_module, "_plugin_manager", manager)
    monkeypatch.setattr(
        "agent.turn_context._maybe_title_session_at_turn_start",
        lambda *_args, **_kwargs: None,
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = run_agent.AIAgent(
        api_key="synthetic-test-value",
        base_url="https://test.invalid",
        provider="openai",
        model="synthetic-model",
        api_mode="chat_completions",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id="external-persistence-session",
        session_db=db,
    )
    agent._cached_system_prompt = "composed synthetic prompt"

    result = agent.run_conversation("hello", task_id="external-persistence-session")

    assert result["agent_persisted"] is True
    persisted = db.get_messages_as_conversation("external-persistence-session")
    assert [(message["role"], message.get("content")) for message in persisted] == [
        ("user", "hello"),
        ("assistant", "external runtime reply"),
    ]


def test_runtime_failure_reaches_host_policy_with_phase_and_replay_classification(
    monkeypatch,
):
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(PluginManifest(name="failing-runtime"), manager)

    class _FailingRuntime:
        def __init__(self):
            self.close_calls = 0

        def preflight(self, request):
            return None

        async def run_turn(self, request, host):
            yield RuntimeFailedEvent(
                failure=RuntimeFailure(
                    code="synthetic_transport_failure",
                    message="synthetic transport failure",
                    phase=RuntimeFailurePhase.BEFORE_VISIBLE_OUTPUT,
                    replay_safe=True,
                    retryable=True,
                )
            )

        async def close(self):
            self.close_calls += 1

    instances = []

    def factory():
        runtime = _FailingRuntime()
        instances.append(runtime)
        return runtime

    context.register_agent_runtime(
        descriptor=RuntimeDescriptor(
            runtime_id="failing-test-runtime",
            plugin_version="0.1.0",
            runtime_api_min=RUNTIME_API_VERSION,
            runtime_api_max=RUNTIME_API_VERSION,
            required_host_capabilities=frozenset({"cancellation_v1"}),
            provider_ids=frozenset({"openai"}),
            api_modes=frozenset({"chat_completions"}),
            session_state_schema_version=1,
        ),
        factory=factory,
    )

    import hermes_cli.plugins as plugins_module

    monkeypatch.setattr(plugins_module, "_plugin_manager", manager)
    agent = run_agent.AIAgent(
        api_key="synthetic-test-value",
        base_url="https://test.invalid",
        provider="openai",
        model="synthetic-model",
        api_mode="chat_completions",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._cached_system_prompt = "composed synthetic prompt"

    result = agent.run_conversation("hello")

    assert result["failed"] is True
    assert result["failure"].phase is RuntimeFailurePhase.BEFORE_VISIBLE_OUTPUT
    assert result["replay_safe"] is True
    assert result["error"] == "synthetic transport failure"
    assert result["agent_persisted"] is False
    assert instances[0].close_calls == 0
    agent.close()
    assert instances[0].close_calls == 1


def test_external_runtime_cancellation_does_not_claim_persistence(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(PluginManifest(name="cancelled-runtime"), manager)

    class _CancelledRuntime:
        def preflight(self, request):
            return None

        async def run_turn(self, request, host):
            yield RuntimeCancelledEvent(reason="synthetic cancellation")

        async def close(self):
            return None

    context.register_agent_runtime(
        descriptor=RuntimeDescriptor(
            runtime_id="cancelled-test-runtime",
            plugin_version="0.1.0",
            runtime_api_min=RUNTIME_API_VERSION,
            runtime_api_max=RUNTIME_API_VERSION,
            required_host_capabilities=frozenset({"cancellation_v1"}),
            provider_ids=frozenset({"openai"}),
            api_modes=frozenset({"chat_completions"}),
            session_state_schema_version=1,
        ),
        factory=_CancelledRuntime,
    )

    import hermes_cli.plugins as plugins_module

    monkeypatch.setattr(plugins_module, "_plugin_manager", manager)
    agent = run_agent.AIAgent(
        api_key="synthetic-test-value",
        base_url="https://test.invalid",
        provider="openai",
        model="synthetic-model",
        api_mode="chat_completions",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._cached_system_prompt = "composed synthetic prompt"

    result = agent.run_conversation("hello")

    assert result["interrupted"] is True
    assert result["agent_persisted"] is False
    agent.close()


def test_external_plugin_runtime_mode_skips_provider_client(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(PluginManifest(name="synthetic-runtime"), manager)
    counters = {"factory": 0, "preflight": 0, "turn": 0, "close": 0}

    class _Runtime(_ExternalRuntime):
        async def run_turn(self, request, host):
            self._counters["turn"] += 1
            yield RuntimeCompletedEvent(
                result={
                    "final_response": "synthetic runtime reply",
                    "messages": list(request.messages),
                }
            )

    def factory():
        counters["factory"] += 1
        return _Runtime(counters)

    context.register_agent_runtime(
        descriptor=RuntimeDescriptor(
            runtime_id="synthetic-runtime",
            plugin_version="0.1.0",
            runtime_api_min=RUNTIME_API_VERSION,
            runtime_api_max=RUNTIME_API_VERSION,
            required_host_capabilities=frozenset({"cancellation_v1"}),
            provider_ids=frozenset({"synthetic-runtime-provider"}),
            api_modes=frozenset({"agent_runtime"}),
            session_state_schema_version=1,
        ),
        factory=factory,
    )

    import hermes_cli.plugins as plugins_module

    monkeypatch.setattr(plugins_module, "_plugin_manager", manager)
    def fail_client(*_args, **_kwargs):
        raise AssertionError("agent_runtime must not construct a provider client")

    monkeypatch.setattr(run_agent.AIAgent, "_create_openai_client", fail_client)
    agent = run_agent.AIAgent(
        base_url="runtime://synthetic",
        provider="synthetic-runtime-provider",
        model="synthetic-model",
        api_mode="agent_runtime",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._cached_system_prompt = "composed synthetic prompt"

    result = agent.run_conversation("hello")

    assert result["final_response"] == "synthetic runtime reply"
    assert agent.client is None
    assert agent._client_kwargs == {}
    assert agent.api_key == ""
    assert counters == {"factory": 1, "preflight": 1, "turn": 1, "close": 0}
    agent.close()
    assert counters["close"] == 1
