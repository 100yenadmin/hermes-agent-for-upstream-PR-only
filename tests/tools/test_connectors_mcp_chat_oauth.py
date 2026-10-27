"""Chat-app OAuth redirects and next-message adoption, without a live provider."""

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from hermes_constants import get_hermes_home, hermes_home_key
from tools.connectors import mcp, mcp_oauth
from tools.connectors.tool import manage_connections
from tui_gateway import mcp_oauth_sessions


@pytest.fixture
def chat_oauth(monkeypatch):
    from hermes_cli import mcp_catalog, mcp_config

    home = get_hermes_home()
    cfg = {"url": "https://mcp.example/mcp", "auth": "oauth"}
    entry = SimpleNamespace(auth=SimpleNamespace(type="oauth", provider=None, env=[]),
                            post_install="")
    monkeypatch.setattr(mcp, "_catalog_names", lambda: ["reports"])
    monkeypatch.setattr(mcp, "_configured_names", lambda: ["reports"])
    monkeypatch.setattr(mcp, "_catalog_entry", lambda _name: entry)
    monkeypatch.setattr(mcp_config, "_get_mcp_servers", lambda: {"reports": cfg})
    monkeypatch.setattr(mcp_catalog, "card_install_config", lambda _entry: cfg)
    monkeypatch.setattr(mcp_catalog, "is_installed", lambda _name: True)
    listeners = []

    def receiver(flow):
        listeners.append(flow)
        return SimpleNamespace(server_address=("127.0.0.1", 54321))

    def worker(_home, _name, _cfg, _reconnect, *, flow, **_kwargs):
        asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?" + urlencode({
            "redirect_uri": flow.redirect_uri, "state": "fixture-state"})))

    monkeypatch.setattr(mcp_oauth, "_start_loopback_receiver", receiver)
    monkeypatch.setattr(mcp_oauth, "run_worker", worker)
    mcp._LATE_ATTEMPTS.clear()
    mcp_oauth._ACTIVE.clear()
    mcp_oauth_sessions._sessions.clear()
    yield home, listeners
    mcp._LATE_ATTEMPTS.clear()
    mcp_oauth._ACTIVE.clear()
    mcp_oauth_sessions._sessions.clear()


def _connect(action="authorize"):
    return json.loads(manage_connections(
        {"action": action, "connectors": [{"name": "reports", "mcp": True}]},
        connection_callback=None, session_id="chat"))


@pytest.mark.parametrize("action", ["authorize", "install"])
def test_chat_public_redirect_and_next_turn_adoption(chat_oauth, monkeypatch, action):
    home, listeners = chat_oauth
    (home / "config.yaml").write_text(
        "webapp:\n  enabled: true\n  public_url: https://agent.example/hermes/\n")
    out = _connect(action)
    target = out["targets"][0]
    redirect = parse_qs(urlparse(target["connect_url"]).query)["redirect_uri"][0]
    assert redirect == "https://agent.example/hermes/api/mcp/oauth/callback/reports"
    assert listeners == []
    assert "next message" in out["note"]
    assert "not ask" in out["note"]

    agent = SimpleNamespace(session_id="chat", enabled_toolsets=[])
    attempt = mcp._LATE_ATTEMPTS[(hermes_home_key(), "chat")]["reports"]
    assert mcp.adopt_late_connections(agent) == []
    from hermes_cli.web_routers.mcp import mcp_oauth_callback

    response = asyncio.run(mcp_oauth_callback(
        "reports", code="fixture-code", state="fixture-state"))
    assert response.status_code == 200
    asyncio.run(attempt.flow.wait_for_callback(timeout=0))
    attempt.flow.mark_approved()
    registered = []
    monkeypatch.setattr("tools.mcp_tool_config._load_mcp_config", lambda: {"reports": {"url": "https://mcp.example"}})
    monkeypatch.setattr("tools.mcp_tool_discovery.register_mcp_servers",
                        lambda servers: registered.extend(servers))
    # A different served profile must not adopt this chat's grant.
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    token = set_hermes_home_override(home / "other")
    try:
        assert mcp.adopt_late_connections(agent) == []
    finally:
        reset_hermes_home_override(token)
    assert mcp.adopt_late_connections(agent) == ["reports"]
    assert registered == ["reports"] and agent.enabled_toolsets == ["reports"]
    assert mcp.adopt_late_connections(agent) == []
    assert not mcp._LATE_ATTEMPTS


@pytest.mark.parametrize("webapp", [
    "", "webapp:\n  enabled: false\n  public_url: https://agent.example\n",
    "webapp:\n  enabled: true\n",
    "webapp:\n  enabled: true\n  public_url: https://agent.example?bad=query\n",
])
def test_chat_without_public_webapp_keeps_loopback_and_honest_note(chat_oauth, webapp):
    home, listeners = chat_oauth
    (home / "config.yaml").write_text(
        "dashboard:\n  public_url: https://separate-dashboard.example\n" + webapp)
    out = _connect()
    redirect = parse_qs(urlparse(out["targets"][0]["connect_url"]).query)["redirect_uri"][0]
    assert redirect == "http://127.0.0.1:54321/callback"
    assert len(listeners) == 1
    assert "gateway machine" in out["note"] and "webapp.public_url" in out["note"]
    assert "discovery_error" in out["note"] and "Do not re-ask" in out["note"]


def test_card_backend_keeps_loopback_even_with_public_webapp(chat_oauth):
    home, listeners = chat_oauth
    (home / "config.yaml").write_text(
        "webapp:\n  enabled: true\n  public_url: https://agent.example\n")
    attempt = mcp._CatalogBackend().start_oauth("reports")
    assert attempt.flow.redirect_uri == "http://127.0.0.1:54321/callback"
    assert len(listeners) == 1


@pytest.mark.parametrize("pinned,client,public,expected", [
    (True, "http://localhost:8412/callback", "https://agent.example", "http://localhost:27890/callback"),
    (False, "http://localhost:8412/callback", "https://agent.example", "http://localhost:8412/callback"),
    (False, None, "https://agent.example/prefix", "https://agent.example/prefix/api/mcp/oauth/callback/a%2Fb%20c"),
    (False, None, None, "http://127.0.0.1:54321/callback"),
])
def test_callback_receiver_precedence(monkeypatch, pinned, client, public, expected):
    bound = []
    monkeypatch.setattr(mcp_oauth, "_start_loopback_receiver",
                        lambda flow: bound.append(flow) or SimpleNamespace(server_address=("127.0.0.1", 54321)))
    flow = SimpleNamespace(server_name="a/b c", redirect_uri="")
    cfg = {"oauth": {"client_id": "fixture-client", "redirect_host": "localhost", "redirect_port": 27890}} if pinned else {}
    receiver = mcp_oauth.choose_callback_receiver(flow, cfg, client, public_base=public)
    assert flow.redirect_uri == expected
    assert bool(bound) == (public is None)
    assert (receiver is not None) == bool(bound)
