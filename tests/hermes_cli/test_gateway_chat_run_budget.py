"""Regression for #143735: the accepted budget must reach the thin-client RPC.

Forwarding omission identified by Mpdev007:
https://github.com/NousResearch/hermes-agent/pull/143759#issuecomment-6882057553
"""
import argparse
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def budget_peer(monkeypatch, tmp_path):
    from hermes_cli import gateway_chat
    from hermes_cli.gateway_chat_view import GatewayChatView

    class Peer:
        parameters = ["cwd", "run_budget"]

        def __init__(self):
            self.calls = []

        async def rpc(self, method, **params):
            self.calls.append((method, params))
            if method == "runtime.describe":
                return {"session_create": {"sources": ["cli"], "parameters": self.parameters}}
            return {"stored_session_id": "fixture", "info": {"launch_request": {"run_budget": 120.0}}}

    peer = Peer()

    @asynccontextmanager
    async def connected():
        yield peer

    monkeypatch.setattr(gateway_chat, "connect_gateway", connected)
    monkeypatch.setattr(GatewayChatView, "run", AsyncMock(return_value=0))
    monkeypatch.chdir(tmp_path)
    return peer


def budget_args(budget):
    from hermes_cli._parser import build_top_level_parser
    argv = ["chat", "-q", "fixture task", "-Q", "--format", "stream-json"]
    if budget is not None:
        argv += ["--run-budget", str(budget)]
    return build_top_level_parser()[0].parse_args(argv)


def test_run_budget_passes_validation():
    """Pre-fix this raised GatewayClientError (the exit-2 refusal)."""
    from hermes_cli import gateway_chat
    gateway_chat.validate_options(argparse.Namespace(run_budget=30.0))


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [120, None])
async def test_run_budget_reaches_session_create(budget_peer, budget):
    from hermes_cli import gateway_chat
    assert await gateway_chat.run_gateway_chat(budget_args(budget)) == 0
    create = next(params for method, params in budget_peer.calls if method == "session.create")
    if budget is None:
        assert "run_budget" not in create
    else:
        assert create["run_budget"] == 120.0


@pytest.mark.asyncio
async def test_run_budget_requires_gateway_support(budget_peer):
    from hermes_cli import gateway_chat
    budget_peer.parameters = ["cwd"]
    with pytest.raises(gateway_chat.GatewayClientError, match="creation options: run_budget"):
        await gateway_chat.run_gateway_chat(budget_args(120))
    assert all(method != "session.create" for method, _ in budget_peer.calls)


@pytest.mark.asyncio
async def test_run_budget_resume_retains_frozen_value(budget_peer):
    from hermes_cli import gateway_chat
    args = budget_args(120)
    args.resume = "fixture"
    assert await gateway_chat.run_gateway_chat(args) == 0
    args.run_budget = 60.0
    with pytest.raises(gateway_chat.GatewayClientError, match="--run-budget differs"):
        await gateway_chat.run_gateway_chat(args)
