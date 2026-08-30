"""Deterministic coverage for opaque delegate_task route aliases."""

import json
import random
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools import delegate_tool as dt

GOOD_A = "Inspect the session expiry implementation and report concrete findings"
GOOD_B = "Inspect the login boundary and report concrete findings"


def _parent():
    parent = MagicMock()
    for key, value in {
        "base_url": "https://parent.invalid/v1",
        "api_key": "parent-key",
        "provider": "nous",
        "api_mode": "chat_completions",
        "model": "parent-model",
        "platform": "cli",
        "providers_allowed": None,
        "providers_ignored": None,
        "providers_order": None,
        "provider_sort": None,
        "_session_db": None,
        "_delegate_depth": 0,
        "_active_children": [],
        "_print_fn": None,
        "_memory_manager": None,
        "tool_progress_callback": None,
        "session_estimated_cost_usd": 0.0,
        "session_cost_source": "none",
        "session_cost_status": "unknown",
    }.items():
        setattr(parent, key, value)
    parent._active_children_lock = threading.Lock()
    return parent


def _cfg(routes, *, allow_metered=False, provider="", model=""):
    return {
        "max_iterations": 5,
        "max_concurrent_children": 3,
        "max_spawn_depth": 1,
        "routes": routes,
        "allow_metered_routes": allow_metered,
        "provider": provider,
        "model": model,
    }


def _creds(provider, model, *, api_key="synthetic-key", auth_type="oauth_external", keyless=False):
    return {
        "provider": provider,
        "model": model,
        "base_url": "https://provider.invalid/v1",
        "api_key": api_key,
        "api_mode": "chat_completions",
        "auth_type": auth_type,
        "keyless": keyless,
        "request_overrides": None,
        "max_output_tokens": None,
    }


def _run(tasks, cfg, resolver, *, auth_type="oauth_external", keyless=False, billing_mode="subscription_included"):
    built = []
    children = []

    def build(**kwargs):
        child = SimpleNamespace(
            provider=kwargs.get("override_provider"),
            model=kwargs.get("model"),
            base_url="https://provider.invalid/v1",
            session_cost_status="estimated",
            session_estimated_cost_usd=0.0,
            session_prompt_tokens=0,
            session_completion_tokens=0,
            session_reasoning_tokens=0,
            session_id=f"child-{len(children)}",
            tool_progress_callback=None,
            _delegate_role="leaf",
        )
        built.append(kwargs)
        children.append(child)
        return child

    def resolve(route, parent):
        return resolver(route, parent)

    def run(index, goal, child, parent_agent, **kwargs):
        return {
            "task_index": index,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.01,
            "_child_role": "leaf",
            "_child_cost_usd": 0.0,
            "cost_usd": 0.0,
            "cost_status": "estimated",
        }

    with (
        patch.object(dt, "_load_config", return_value=cfg),
        patch.object(dt, "_resolve_delegation_credentials", side_effect=resolve) as resolved,
        patch.object(dt, "_build_child_preserving_parent_tools", side_effect=build),
        patch.object(dt, "_run_single_child", side_effect=run),
        patch(
            "hermes_cli.providers.get_provider",
            return_value=SimpleNamespace(auth_type=auth_type, keyless=keyless),
        ),
        patch(
            "agent.usage_pricing.resolve_billing_route",
            return_value=SimpleNamespace(billing_mode=billing_mode),
        ),
        patch("tools.delegation_live_log.create_live_transcripts", return_value=("", [], [])),
    ):
        result = json.loads(dt.delegate_task(tasks=tasks, parent_agent=_parent()))
    return result, built, resolved


def test_defaults_and_schema_are_opaque():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    delegation = DEFAULT_CONFIG["delegation"]
    assert delegation["routes"] == {}
    assert delegation["allow_metered_routes"] is False
    props = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert props["route"]["type"] == "string"
    assert "codex-fast" not in props["route"]["description"]
    with patch.object(
        dt,
        "_load_config",
        return_value=_cfg({"codex-fast": {"provider": "openai-codex", "model": "gpt-5.2-codex"}}),
    ):
        assert "codex-fast" not in json.dumps(dt._build_dynamic_schema_overrides())


def test_valid_codex_route_receipt_and_precedence():
    cfg = _cfg(
        {"codex-fast": {"provider": "openai-codex", "model": "gpt-5.2-codex"}},
        provider="openrouter",
        model="global-model",
    )
    result, built, resolved = _run(
        [{"goal": GOOD_A, "route": "codex-fast"}],
        cfg,
        lambda route, parent: _creds(route["provider"], route["model"]),
    )
    assert resolved.call_args.args[0] == {"provider": "openai-codex", "model": "gpt-5.2-codex"}
    assert built[0]["override_provider"] == "openai-codex"
    assert built[0]["model"] == "gpt-5.2-codex"
    entry = result["results"][0]
    assert {entry[k] for k in ("route", "provider", "model", "billing_mode", "cost_status")} == {
        "codex-fast",
        "openai-codex",
        "gpt-5.2-codex",
        "subscription_included",
        "estimated",
    }
    assert result["mixed_routes"] is False
    assert result["provider"] == "openai-codex"
    assert "synthetic-key" not in json.dumps(result)
    assert "provider.invalid" not in json.dumps(result)


def test_malformed_unknown_partial_and_credential_failures_are_atomic():
    valid = {"codex-fast": {"provider": "openai-codex", "model": "gpt-5.2-codex"}}
    cases = [
        (_cfg({"codex-fast": {"provider": "openai-codex", "model": "gpt", "api_key": "x"}}),
         [{"goal": GOOD_A, "route": "codex-fast"}]),
        (_cfg(valid), [{"goal": GOOD_A, "route": "missing"}]),
        (_cfg(valid), [{"goal": GOOD_A, "route": "codex-fast"}, {"goal": GOOD_B}]),
    ]
    for case_cfg, task_list in cases:
        result, built, resolved = _run(
            task_list,
            case_cfg,
            lambda route, parent: _creds(route["provider"], route["model"]),
        )
        assert "error" in result
        assert built == []
        assert resolved.call_count == 0

    def fail_second(route, parent):
        if route["model"] == "gpt-5.2-mini":
            raise ValueError("synthetic credential failure")
        return _creds(route["provider"], route["model"])

    result, built, resolved = _run(
        [{"goal": GOOD_A, "route": "codex-fast"}, {"goal": GOOD_B, "route": "codex-mini"}],
        _cfg({
            "codex-fast": {"provider": "openai-codex", "model": "gpt-5.2-codex"},
            "codex-mini": {"provider": "openai-codex", "model": "gpt-5.2-mini"},
        }),
        fail_second,
    )
    assert "error" in result and built == [] and resolved.call_count == 2


def test_mixed_routes_have_safe_per_task_receipts():
    cfg = _cfg({
        "codex-fast": {"provider": "openai-codex", "model": "gpt-5.2-codex"},
        "codex-mini": {"provider": "openai-codex", "model": "gpt-5.2-mini"},
    })
    result, built, _ = _run(
        [{"goal": GOOD_A, "route": "codex-fast"}, {"goal": GOOD_B, "route": "codex-mini"}],
        cfg,
        lambda route, parent: _creds(route["provider"], route["model"]),
    )
    assert len(built) == 2
    assert result["mixed_routes"] is True
    assert result["provider"] is None and result["model"] is None
    assert [entry["route"] for entry in result["results"]] == ["codex-fast", "codex-mini"]


def test_keyless_and_trusted_auth_routes_do_not_require_a_key():
    keyless_cfg = _cfg({"free": {"provider": "opencode-free", "model": "x-preview-f-free"}})
    result, built, _ = _run(
        [{"goal": GOOD_A, "route": "free"}],
        keyless_cfg,
        lambda route, parent: _creds(
            route["provider"], route["model"], api_key="", auth_type="api_key", keyless=True
        ),
        auth_type="api_key",
        keyless=True,
        billing_mode="unknown",
    )
    assert "error" not in result and built
    assert built[0]["override_api_key"] == ""
    assert result["results"][0]["billing_mode"] == "non_metered"
    assert result["results"][0]["cost_status"] == "included"


def test_credential_resolver_keeps_trusted_empty_keys_isolated():
    runtime = {
        "provider": "openai-codex",
        "model": "gpt-5.2-codex",
        "base_url": "https://provider.invalid/v1",
        "api_key": "",
        "api_mode": "codex_responses",
        "auth_type": "oauth_external",
        "keyless": False,
    }
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime):
        credentials = dt._resolve_delegation_credentials(
            {"provider": "openai-codex", "model": "gpt-5.2-codex"}, _parent()
        )
    assert credentials["api_key"] == ""
    assert credentials["auth_type"] == "oauth_external"

    runtime["auth_type"] = "unknown"
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime):
        try:
            dt._resolve_delegation_credentials(
                {"provider": "openai-codex", "model": "gpt-5.2-codex"}, _parent()
            )
        except ValueError as exc:
            assert "no API key" in str(exc)
        else:
            raise AssertionError("unknown empty-key auth must fail closed")
    result, built, _ = _run(
        [{"goal": GOOD_A, "route": "codex"}],
        _cfg({"codex": {"provider": "openai-codex", "model": "gpt-5.2-codex"}}),
        lambda route, parent: _creds(
            route["provider"], route["model"], api_key="", auth_type="oauth_external"
        ),
    )
    assert "error" not in result and built


def test_metered_and_unknown_billing_fail_closed():
    cfg = _cfg({"paid": {"provider": "openrouter", "model": "provider/model"}})
    result, built, _ = _run(
        [{"goal": GOOD_A, "route": "paid"}],
        cfg,
        lambda route, parent: _creds(route["provider"], route["model"], auth_type="api_key"),
        auth_type="api_key",
        billing_mode="official_models_api",
    )
    assert "error" in result and built == []

    with patch("hermes_cli.providers.get_provider", return_value=None), patch(
        "agent.usage_pricing.resolve_billing_route",
        return_value=SimpleNamespace(billing_mode="subscription_included"),
    ):
        try:
            dt._route_meter_policy(
                _cfg({}),
                _creds("unknown-provider", "model", auth_type="oauth_external"),
            )
        except ValueError as exc:
            assert "authentication metadata" in str(exc)
        else:
            raise AssertionError("unknown provider auth metadata must fail closed")

    result, built, _ = _run(
        [{"goal": GOOD_A, "route": "paid"}],
        _cfg(cfg["routes"], allow_metered=True),
        lambda route, parent: _creds(route["provider"], route["model"], auth_type="api_key"),
        auth_type="api_key",
        billing_mode="official_models_api",
    )
    assert "error" not in result and built[0]["override_provider"] == "openrouter"

    result, built, _ = _run(
        [{"goal": GOOD_A, "route": "paid"}],
        _cfg(cfg["routes"], allow_metered=True),
        lambda route, parent: _creds(route["provider"], route["model"], auth_type="api_key"),
        auth_type="api_key",
        billing_mode="unknown",
    )
    assert "error" in result and built == []


def test_legacy_global_and_parent_inheritance_keep_route_shape_unchanged():
    cfg = _cfg({}, provider="openrouter", model="global-model")
    result, built, resolved = _run(
        [{"goal": GOOD_A}],
        cfg,
        lambda route, parent: _creds("openrouter", "global-model"),
    )
    assert "error" not in result
    assert resolved.call_args.args[0] is cfg
    assert built[0]["model"] == "global-model"
    assert "mixed_routes" not in result
    assert "route" not in result["results"][0]

    result, built, _ = _run(
        [{"goal": GOOD_A}],
        _cfg({}),
        lambda route, parent: _creds("nous", "parent-model"),
    )
    assert "error" not in result and built[0]["override_provider"] == "nous"
    assert "route" not in result["results"][0]


def test_seeded_10000_route_validation_cases_are_atomic():
    """A large seeded corpus exercises validation without ever constructing a child."""
    rng = random.Random(20260830)
    valid = 0
    rejected = 0
    for index in range(10_000):
        alias = f"route-{index}"
        entry = {"provider": "openai-codex", "model": f"gpt-{index}"}
        if rng.randrange(4) == 0:
            entry["credential"] = "synthetic-only"
            rejected += 1
        else:
            valid += 1
        try:
            dt._validate_delegation_routes({"routes": {alias: entry}})
        except ValueError:
            assert "credential" in entry
        else:
            assert "credential" not in entry
    assert valid + rejected == 10_000

    built = []
    with (
        patch.object(dt, "_load_config", return_value=_cfg({"bad": {"provider": "openai-codex", "model": "gpt", "command": "x"}})),
        patch.object(dt, "_build_child_preserving_parent_tools", side_effect=lambda **kwargs: built.append(kwargs)),
        patch.object(dt, "_resolve_delegation_credentials") as resolver,
    ):
        result = json.loads(
            dt.delegate_task(tasks=[{"goal": GOOD_A, "route": "bad"}], parent_agent=_parent())
        )
    assert "error" in result and built == [] and resolver.call_count == 0
