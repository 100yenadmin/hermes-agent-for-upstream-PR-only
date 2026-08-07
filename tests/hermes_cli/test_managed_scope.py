"""Unit tests for hermes_cli.managed_scope (resolver + loaders + key helpers)."""
import textwrap

import pytest


# ── Directory resolver ───────────────────────────────────────────────────────


def test_managed_dir_follows_context_scoped_profile(tmp_path, monkeypatch):
    from hermes_cli import managed_scope
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    profile_root = tmp_path / "managed-profiles"
    jane_managed = profile_root / "jane"
    louis_managed = profile_root / "louis"
    jane_managed.mkdir(parents=True)
    louis_managed.mkdir()
    (jane_managed / "config.yaml").write_text(
        "context:\n  engine: lcm-jane\n",
        encoding="utf-8",
    )
    (louis_managed / "config.yaml").write_text(
        "context:\n  engine: lcm-louis\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EVAOS_HERMES_MANAGED_PROFILE_ROOT", str(profile_root))

    jane_token = set_hermes_home_override(tmp_path / "profiles" / "jane")
    try:
        assert managed_scope.get_managed_dir() == jane_managed
        assert managed_scope.load_managed_config()["context"]["engine"] == "lcm-jane"
    finally:
        reset_hermes_home_override(jane_token)

    louis_token = set_hermes_home_override(tmp_path / "profiles" / "louis")
    try:
        assert managed_scope.get_managed_dir() == louis_managed
        assert managed_scope.load_managed_config()["context"]["engine"] == "lcm-louis"
    finally:
        reset_hermes_home_override(louis_token)






# ── Loaders + key helpers ────────────────────────────────────────────────────


def _write_managed(tmp_path, monkeypatch, *, config=None, env=None):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir(exist_ok=True)
    if config is not None:
        (managed / "config.yaml").write_text(textwrap.dedent(config), encoding="utf-8")
    if env is not None:
        (managed / ".env").write_text(textwrap.dedent(env), encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    return managed








def test_load_managed_env_and_is_env_managed(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    _write_managed(
        tmp_path, monkeypatch, env="OPENAI_API_BASE=https://org.example/v1\n"
    )
    assert managed_scope.load_managed_env() == {
        "OPENAI_API_BASE": "https://org.example/v1"
    }
    assert managed_scope.is_env_managed("OPENAI_API_BASE") is True
    assert managed_scope.is_env_managed("OTHER") is False




def test_managed_dir_env_scrubbed_by_default():
    """conftest must scrub HERMES_MANAGED_DIR so a dev-shell value can't leak in."""
    import os

    assert "HERMES_MANAGED_DIR" not in os.environ
