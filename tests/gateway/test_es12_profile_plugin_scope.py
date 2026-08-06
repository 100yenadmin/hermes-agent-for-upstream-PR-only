"""Regression tests for the profile-scoped PluginManager cache.

A multiplexed gateway routes each inbound message to a different profile under
``gateway/run.py::_profile_runtime_scope``. Plugin discovery used to run against
one process-global PluginManager whose ``_discovered`` flag latched on the first
profile to trip it, so every profile routed afterwards got the first profile's
plugin registry and its own ``plugins/`` directory was never scanned.
"""

from pathlib import Path

import pytest
import yaml

import hermes_cli.plugins as plugins_mod


def _make_profile(home: Path, plugin_name: str) -> Path:
    """Create a profile home holding exactly one enabled plugin."""
    plugin_dir = home / "plugins" / plugin_name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": plugin_name, "version": "0.1.0"}), encoding="utf-8",
    )
    # Register a hook so the plugin is observable through the manager's public
    # surface rather than only through the manifest list.
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        f"    ctx.register_hook('transform_llm_output', lambda **kw: {plugin_name!r})\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [plugin_name]}}), encoding="utf-8",
    )
    return home


@pytest.fixture(autouse=True)
def _clean_manager_cache():
    """The cache is process-global; do not leak managers between tests."""
    plugins_mod.reset_plugin_managers()
    yield
    plugins_mod.reset_plugin_managers()


@pytest.fixture
def two_profiles(tmp_path):
    profile_a = _make_profile(tmp_path / "profile_a", "es12_plugin_a")
    profile_b = _make_profile(tmp_path / "profile_b", "es12_plugin_b")
    return profile_a, profile_b


def _loaded_plugin_names(manager) -> set:
    return {p["name"] for p in manager.list_plugins() if p["enabled"]}


def test_each_profile_gets_its_own_manager(two_profiles):
    """Two profiles must not share one PluginManager instance."""
    from gateway.run import _profile_runtime_scope

    profile_a, profile_b = two_profiles

    with _profile_runtime_scope(profile_a):
        manager_a = plugins_mod.get_plugin_manager()
    with _profile_runtime_scope(profile_b):
        manager_b = plugins_mod.get_plugin_manager()

    assert manager_a is not manager_b

    # Re-entering a profile returns that profile's manager, not a fresh one —
    # the cache lifecycle matches the old singleton within a profile.
    with _profile_runtime_scope(profile_a):
        assert plugins_mod.get_plugin_manager() is manager_a


def test_second_profile_plugins_load_after_first_profile_discovered(two_profiles):
    """The bug: profile A tripping discovery must not blank profile B."""
    from gateway.run import _profile_runtime_scope

    profile_a, profile_b = two_profiles

    with _profile_runtime_scope(profile_a):
        plugins_mod.discover_plugins()
        names_a = _loaded_plugin_names(plugins_mod.get_plugin_manager())

    # Profile B is routed *after* A has already latched discovery.
    with _profile_runtime_scope(profile_b):
        plugins_mod.discover_plugins()
        names_b = _loaded_plugin_names(plugins_mod.get_plugin_manager())

    assert "es12_plugin_a" in names_a
    assert "es12_plugin_b" in names_b
    # And neither profile sees the other's plugin.
    assert "es12_plugin_b" not in names_a
    assert "es12_plugin_a" not in names_b


def test_profile_hooks_do_not_bleed(two_profiles):
    """Hook registries are per-profile, not merged into one process registry."""
    from gateway.run import _profile_runtime_scope

    profile_a, profile_b = two_profiles

    with _profile_runtime_scope(profile_a):
        plugins_mod.discover_plugins()
    with _profile_runtime_scope(profile_b):
        plugins_mod.discover_plugins()
        results_b = plugins_mod.invoke_hook("transform_llm_output", response_text="x")
    with _profile_runtime_scope(profile_a):
        results_a = plugins_mod.invoke_hook("transform_llm_output", response_text="x")

    assert results_a == ["es12_plugin_a"]
    assert results_b == ["es12_plugin_b"]


def test_single_profile_gateway_behaviour_unchanged(tmp_path, monkeypatch):
    """Without a profile scope the manager is a stable process singleton."""
    monkeypatch.setenv("HERMES_HOME", str(_make_profile(tmp_path / "solo", "es12_solo")))

    first = plugins_mod.get_plugin_manager()
    second = plugins_mod.get_plugin_manager()

    assert first is second


def test_explicit_pin_still_wins(tmp_path, monkeypatch):
    """``_plugin_manager`` remains the override seam tests already rely on."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "solo"))
    pinned = plugins_mod.PluginManager()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", pinned)

    assert plugins_mod.get_plugin_manager() is pinned
