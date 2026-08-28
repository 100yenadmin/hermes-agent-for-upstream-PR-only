"""Deterministic coverage for opaque delegate_task route aliases."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from tools import delegate_tool as dt
GOOD_A = "Inspect the session expiry implementation and report concrete findings"
GOOD_B = "Inspect the login boundary and report concrete findings"
def _parent():
    p = MagicMock()
    for key, value in {
        "base_url": "https://parent.invalid/v1", "api_key": "parent-key",
        "provider": "nous", "api_mode": "chat_completions", "model": "parent-model",
        "platform": "cli", "providers_allowed": None, "providers_ignored": None,
        "providers_order": None, "provider_sort": None, "_session_db": None,
        "_delegate_depth": 0, "_active_children": [], "_print_fn": None,
        "tool_progress_callback": None, "session_estimated_cost_usd": 0.0,
        "session_cost_source": "none", "session_cost_status": "unknown",
    }.items():
        setattr(p, key, value)
    p._active_children_lock = threading.Lock()
    return p
def _cfg(routes, *, allow_metered=False, provider="", model=""):
    return {"max_iterations": 5, "max_concurrent_children": 3,
            "max_spawn_depth": 1, "routes": routes,
            "allow_metered_routes": allow_metered, "provider": provider,
            "model": model}
def _creds(provider, model):
    return {"provider": provider, "model": model,
            "base_url": "https://provider.invalid/v1", "api_key": "synthetic-key",
            "api_mode": "chat_completions", "request_overrides": None,
            "max_output_tokens": None}
def _run(tasks, cfg, resolver, *, auth_type="oauth_external", billing_mode="subscription_included"):
    built, children = [], []

    def build(**kwargs):
        child = SimpleNamespace(provider=kwargs.get("override_provider"), model=kwargs.get("model"),
                                base_url="https://provider.invalid/v1", session_cost_status="estimated",
                                session_estimated_cost_usd=0.0, session_prompt_tokens=0,
                                session_completion_tokens=0, session_reasoning_tokens=0,
                                session_id=f"child-{len(children)}", tool_progress_callback=None,
                                _delegate_role="leaf")
        built.append(kwargs); children.append(child); return child

    def run(index, goal, child, parent_agent, **kwargs):
        return {"task_index": index, "status": "completed", "summary": "done",
                "api_calls": 1, "duration_seconds": 0.01, "_child_role": "leaf",
                "_child_cost_usd": 0.0, "cost_usd": 0.0, "cost_status": "estimated"}

    with (patch.object(dt, "_load_config", return_value=cfg),
          patch.object(dt, "_resolve_delegation_credentials", side_effect=resolver) as resolve,
          patch.object(dt, "_build_child_preserving_parent_tools", side_effect=build),
          patch.object(dt, "_run_single_child", side_effect=run),
          patch("hermes_cli.providers.get_provider", return_value=SimpleNamespace(auth_type=auth_type)),
          patch("agent.usage_pricing.resolve_billing_route",
                return_value=SimpleNamespace(billing_mode=billing_mode)),
          patch("tools.delegation_live_log.create_live_transcripts", return_value=("", [], []))):
        result = json.loads(dt.delegate_task(tasks=tasks, parent_agent=_parent()))
    return result, built, resolve
def test_defaults_and_opaque_schema():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    delegation = DEFAULT_CONFIG["delegation"]
    assert delegation["routes"] == {} and delegation["allow_metered_routes"] is False
    props = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert props["route"]["type"] == "string" and "codex-fast" not in props["route"]["description"]
    with patch.object(dt, "_load_config", return_value=_cfg({"codex-fast": {"provider": "openai-codex", "model": "gpt-5.2-codex"}})):
        assert "codex-fast" not in json.dumps(dt._build_dynamic_schema_overrides())
def test_valid_codex_route_receipt_and_precedence():
    cfg = _cfg({"codex-fast": {"provider": "openai-codex", "model": "gpt-5.2-codex"}},
               provider="openrouter", model="global-model")
    result, built, resolve = _run([{"goal": GOOD_A, "route": "codex-fast"}], cfg,
                                 lambda route, parent: _creds(route["provider"], route["model"]))
    assert resolve.call_args.args[0] == {"provider": "openai-codex", "model": "gpt-5.2-codex"}
    assert built[0]["override_provider"] == "openai-codex" and built[0]["model"] == "gpt-5.2-codex"
    entry = result["results"][0]
    assert {entry[k] for k in ("route", "provider", "model", "billing_mode", "cost_status")} == {
        "codex-fast", "openai-codex", "gpt-5.2-codex", "subscription_included", "estimated"}
    assert result["mixed_routes"] is False and result["provider"] == "openai-codex"
    assert "api_key" not in json.dumps(result) and "base_url" not in json.dumps(result)
def test_malformed_unknown_and_partial_reject_before_build():
    valid = {"codex-fast": {"provider": "openai-codex", "model": "gpt-5.2-codex"}}
    cases = [(_cfg({"codex-fast": {"provider": "openai-codex", "model": "gpt", "api_key": "x"}}),
             [{"goal": GOOD_A, "route": "codex-fast"}]),
             (_cfg(valid), [{"goal": GOOD_A, "route": "missing"}]),
             (_cfg(valid), [{"goal": GOOD_A, "route": "codex-fast"}, {"goal": GOOD_B}])]
    for cfg, tasks in cases:
        result, built, resolve = _run(tasks, cfg, lambda route, parent: _creds(route["provider"], route["model"]))
        assert "error" in result and built == [] and resolve.call_count == 0
def test_all_routes_preflight_before_first_build_and_mixed_receipts():
    cfg = _cfg({"codex-fast": {"provider": "openai-codex", "model": "gpt-5.2-codex"},
                "codex-mini": {"provider": "openai-codex", "model": "gpt-5.2-mini"}})
    events = []
    def resolve(route, parent):
        events.append(route["model"])
        return _creds(route["provider"], route["model"])
    result, built, _ = _run([{"goal": GOOD_A, "route": "codex-fast"}, {"goal": GOOD_B, "route": "codex-mini"}], cfg, resolve)
    assert len(built) == 2 and events == ["gpt-5.2-codex", "gpt-5.2-mini"]
    assert result["mixed_routes"] is True and result["provider"] is None and result["model"] is None
    assert [e["route"] for e in result["results"]] == ["codex-fast", "codex-mini"]
def test_meter_policy_unknown_and_opt_in():
    cfg = _cfg({"paid": {"provider": "openrouter", "model": "provider/model"}})
    for auth, billing in (("api_key", "official_models_api"), ("oauth_external", "unknown")):
        result, built, _ = _run([{"goal": GOOD_A, "route": "paid"}], cfg,
                                lambda route, parent: _creds(route["provider"], route["model"]),
                                auth_type=auth, billing_mode=billing)
        assert "error" in result and built == []
    result, built, _ = _run([{"goal": GOOD_A, "route": "paid"}],
                            _cfg(cfg["routes"], allow_metered=True),
                            lambda route, parent: _creds(route["provider"], route["model"]),
                            auth_type="api_key", billing_mode="official_models_api")
    assert "error" not in result and built[0]["override_provider"] == "openrouter"
def test_legacy_global_resolution_is_unchanged():
    cfg = _cfg({}, provider="openrouter", model="global-model")
    result, built, resolve = _run([{"goal": GOOD_A}], cfg,
                                 lambda route, parent: _creds(route["provider"], route["model"]))
    assert "error" not in result and resolve.call_count == 1 and resolve.call_args.args[0] is cfg
    assert built[0]["model"] == "global-model" and "mixed_routes" not in result
