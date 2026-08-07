from pathlib import Path

import pytest

from hermes_cli import profiles
from hermes_cli.profile_scope import (
    current_effective_profile,
    managed_profile_context,
    principal_from_headers,
    require_profile,
)


def _principal(admin: bool = False):
    return principal_from_headers(
        {
            "x-evaos-allowed-profiles": "jane,louis",
            "x-evaos-primary-profile": "jane",
            "x-evaos-profile-admin": "1" if admin else "0",
            "x-evaos-principal-user": "user-1",
            "x-evaos-session-id": "session-1",
        }
    )


def test_managed_scope_defaults_to_primary_and_rejects_other_profile(monkeypatch, tmp_path):
    profiles_root = tmp_path / "profiles"
    for name in ("jane", "louis", "other"):
        (profiles_root / name).mkdir(parents=True)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)

    with managed_profile_context(_principal()):
        assert require_profile(None) == "jane"
        assert current_effective_profile() == "jane"
        assert profiles.get_profile_dir("default") == profiles_root / "jane"
        assert profiles.get_profile_dir("louis") == profiles_root / "louis"
        assert profiles.profile_exists("other") is False
        with pytest.raises(PermissionError):
            profiles.get_profile_dir("other")

    with managed_profile_context(_principal(), effective_profile="louis"):
        assert current_effective_profile() == "louis"


def test_managed_profile_header_rejects_primary_outside_allowlist():
    with pytest.raises(ValueError):
        principal_from_headers(
            {
                "x-evaos-allowed-profiles": "jane,louis",
                "x-evaos-primary-profile": "regan",
                "x-evaos-principal-user": "user-1",
            }
        )
