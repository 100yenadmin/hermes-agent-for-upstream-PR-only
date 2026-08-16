"""Subscription-safe auxiliary routing for the Claude Agent SDK."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType

import pytest

from agent import auxiliary_client as M
from agent import claude_sdk_aux_client as AUX
from agent.claude_sdk_aux_client import ClaudeSdkAuxClient, ClaudeSdkAuxError
from agent.transports import claude_agent_sdk_session as SESSION


def _plant_sdk(monkeypatch, messages):
    """Install the optional SDK's minimal typed surface for one test."""
    module = ModuleType("claude_agent_sdk")

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        def __init__(
            self,
            *,
            subtype="success",
            is_error=False,
            result="",
            errors=None,
            usage=None,
            stop_reason=None,
        ):
            self.subtype = subtype
            self.is_error = is_error
            self.result = result
            self.errors = errors or []
            self.usage = usage
            self.stop_reason = stop_reason

    captured = {}

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        for message in messages:
            yield message

    module.TextBlock = TextBlock
    module.AssistantMessage = AssistantMessage
    module.ResultMessage = ResultMessage
    module.ClaudeAgentOptions = ClaudeAgentOptions
    module.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return module, captured


def test_auto_sdk_runtime_uses_one_shot_subscription_aux(monkeypatch):
    monkeypatch.setattr(
        M,
        "_normalize_main_runtime",
        lambda runtime: {
            "api_mode": "claude_agent_sdk",
            "model": "claude-sonnet-5",
            "provider": "claude-agent-sdk",
        },
    )

    client, model, provider = M._resolve_auto_route(main_runtime={})

    assert isinstance(client, ClaudeSdkAuxClient)
    assert model == "claude-sonnet-5"
    assert provider == "claude-agent-sdk"


def test_explicit_sdk_aux_provider_returns_sdk_client():
    client, model = M.resolve_provider_client(
        "claude-agent-sdk", model="claude-sonnet-5"
    )

    assert isinstance(client, ClaudeSdkAuxClient)
    assert model == "claude-sonnet-5"


def test_prompt_formatter_preserves_roles_and_only_text_content():
    prompt = AUX._messages_to_prompt(
        [
            {"role": "system", "content": "Summarize precisely."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image_url", "image_url": {"url": "secret-url"}},
                ],
            },
            {"role": "tool", "content": {"content": "tool output"}},
        ]
    )

    assert "System:\nSummarize precisely." in prompt
    assert "User:\nhello" in prompt
    assert "Tool result:\ntool output" in prompt
    assert "secret-url" not in prompt


def test_one_shot_query_has_no_tools_and_scrubs_child_env(monkeypatch):
    module, captured = _plant_sdk(monkeypatch, [])
    messages = [
        module.AssistantMessage([module.TextBlock("answer")]),
        module.ResultMessage(usage={"input_tokens": 2}),
    ]
    # The async generator closes over this list, so populate it after the
    # stand-in classes are available.
    monkeypatch.setattr(
        sys.modules["claude_agent_sdk"],
        "query",
        lambda **kwargs: _async_messages(messages, captured, kwargs),
    )
    monkeypatch.setattr(
        SESSION,
        "_sdk_env_overrides",
        lambda: {"ANTHROPIC_API_KEY": ""},
    )

    text, usage, _ = asyncio.run(AUX._collect_text("prompt", model="claude-sonnet-5"))

    assert text == "answer"
    assert usage == {"input_tokens": 2}
    assert captured["tools"] == []
    assert captured["allowed_tools"] == []
    assert captured["mcp_servers"] == {}
    assert captured["env"] == {"ANTHROPIC_API_KEY": ""}


async def _async_messages(messages, captured, kwargs):
    captured["prompt"] = kwargs["prompt"]
    captured["options"] = kwargs["options"]
    for message in messages:
        yield message


@pytest.mark.parametrize(
    "result_kwargs",
    [
        {"is_error": True, "subtype": "error_during_execution", "result": "boom"},
        {"subtype": "error_max_turns", "result": "limit"},
    ],
)
def test_terminal_error_never_returns_partial_text(monkeypatch, result_kwargs):
    module, captured = _plant_sdk(monkeypatch, [])
    messages = [
        module.AssistantMessage([module.TextBlock("partial")]),
        module.ResultMessage(**result_kwargs),
    ]
    monkeypatch.setattr(
        module,
        "query",
        lambda **kwargs: _async_messages(messages, captured, kwargs),
    )
    monkeypatch.setattr(SESSION, "_sdk_env_overrides", lambda: {})

    with pytest.raises(ClaudeSdkAuxError):
        asyncio.run(AUX._collect_text("prompt", model="claude-sonnet-5"))


def test_terminal_error_is_redacted(monkeypatch):
    module, captured = _plant_sdk(monkeypatch, [])
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    messages = [module.ResultMessage(is_error=True, result=f"bad key {secret}")]
    monkeypatch.setattr(
        module,
        "query",
        lambda **kwargs: _async_messages(messages, captured, kwargs),
    )
    monkeypatch.setattr(SESSION, "_sdk_env_overrides", lambda: {})

    with pytest.raises(ClaudeSdkAuxError) as raised:
        asyncio.run(AUX._collect_text("prompt", model="claude-sonnet-5"))

    assert secret not in str(raised.value)


def test_stream_ending_without_result_is_not_partial_success(monkeypatch):
    module, captured = _plant_sdk(monkeypatch, [])
    messages = [module.AssistantMessage([module.TextBlock("partial")])]
    monkeypatch.setattr(
        module,
        "query",
        lambda **kwargs: _async_messages(messages, captured, kwargs),
    )
    monkeypatch.setattr(SESSION, "_sdk_env_overrides", lambda: {})

    with pytest.raises(ClaudeSdkAuxError, match="without a terminal result"):
        asyncio.run(AUX._collect_text("prompt", model="claude-sonnet-5"))


def test_sdk_exception_is_redacted_at_openai_facade(monkeypatch):
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"

    async def _boom(prompt, *, model):
        raise RuntimeError(f"transport exposed {secret}")

    monkeypatch.setattr(AUX, "_collect_text", _boom)
    client = ClaudeSdkAuxClient()

    with pytest.raises(ClaudeSdkAuxError) as raised:
        client.chat.completions.create(
            messages=[{"role": "user", "content": "summarize this"}]
        )

    assert secret not in str(raised.value)
