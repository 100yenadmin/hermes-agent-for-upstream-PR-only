"""Subscription-safe auxiliary routing for the Claude Agent SDK."""

from __future__ import annotations

from agent import auxiliary_client as M
from agent.claude_sdk_aux_client import ClaudeSdkAuxClient


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
