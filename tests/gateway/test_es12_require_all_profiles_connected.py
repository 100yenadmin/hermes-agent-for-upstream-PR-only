"""Regression tests for the all-profiles-connected startup assertion.

``_start_secondary_profile_adapters`` logs-and-continues when a profile's
adapter fails to create or connect. On non-port-binding platforms (Mattermost,
Telegram) nothing raises, so a gateway that serves every employee on a host can
report healthy with one employee silently unreachable.
``gateway.require_all_profiles_connected`` turns that into a fatal startup
error naming the offending profile. Default off — behavior is unchanged for
everyone who does not opt in.

These tests drive the REAL method on a bare runner with the profile-enumeration
and per-profile startup collaborators stubbed, following the idiom in
tests/gateway/test_multiplex_pairing_stores.py.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from gateway.run import GatewayRunner, ProfileConnectivityError


def _bare_runner(require_all: bool, multiplex: bool = True):
    runner = object.__new__(GatewayRunner)
    runner.config = MagicMock(
        multiplex_profiles=multiplex,
        require_all_profiles_connected=require_all,
    )
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._profile_startup_failures = {}
    runner.pairing_store = MagicMock()
    runner.pairing_stores = {}
    runner._adapter_credential_fingerprint = lambda adapter: None
    runner._adapter_listener_claim = lambda platform, adapter: None
    return runner


def _run(runner, tmp_path, profiles, connected_profiles=(), failures=None):
    """Drive _start_secondary_profile_adapters with stubbed per-profile startup."""

    async def _startup(profile_name, profile_home, claimed):
        for platform_value, reason in (failures or {}).get(profile_name, []):
            runner._record_profile_startup_failure(profile_name, platform_value, reason)
        if profile_name in connected_profiles:
            runner._profile_adapters[profile_name] = {"mattermost": MagicMock()}
            return 1
        runner._profile_adapters.setdefault(profile_name, {})
        return 0

    runner._start_one_profile_adapters = _startup

    served = [(name, tmp_path / ".hermes" / "profiles" / name) for name in profiles]
    with patch("hermes_cli.profiles.profiles_to_serve", return_value=served), patch(
        "hermes_cli.profiles.get_active_profile_name", return_value="main"
    ):
        return asyncio.run(runner._start_secondary_profile_adapters())


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_all_profiles_connected_is_healthy(tmp_path):
    """Every served profile online → startup proceeds normally."""
    runner = _bare_runner(require_all=True)

    connected = _run(
        runner,
        tmp_path,
        profiles=["main", "woody", "amanda"],
        connected_profiles=("woody", "amanda"),
    )

    assert connected == 2
    assert runner._profile_startup_failures == {}


def test_failing_profile_raises_naming_it(tmp_path):
    """One profile's adapter failing must abort startup and name that profile."""
    runner = _bare_runner(require_all=True)

    with pytest.raises(ProfileConnectivityError) as exc:
        _run(
            runner,
            tmp_path,
            profiles=["main", "woody", "amanda"],
            connected_profiles=("woody",),
            failures={"amanda": [("mattermost", "adapter creation returned None")]},
        )

    message = str(exc.value)
    assert "amanda" in message
    assert "mattermost" in message
    assert "adapter creation returned None" in message
    # The healthy profile must not be blamed.
    assert "woody" not in message


def test_profile_with_zero_connected_adapters_raises(tmp_path):
    """A profile that connects nothing is offline even with no logged error."""
    runner = _bare_runner(require_all=True)

    with pytest.raises(ProfileConnectivityError) as exc:
        _run(
            runner,
            tmp_path,
            profiles=["main", "woody", "amanda"],
            connected_profiles=("woody",),
        )

    assert "amanda" in str(exc.value)


def test_flag_off_keeps_log_and_continue(tmp_path):
    """Default behavior is unchanged: a failed profile is logged, not fatal."""
    runner = _bare_runner(require_all=False)

    connected = _run(
        runner,
        tmp_path,
        profiles=["main", "woody", "amanda"],
        connected_profiles=("woody",),
        failures={"amanda": [("telegram", "failed to connect")]},
    )

    assert connected == 1
    # The failure is still recorded — the flag only controls whether it is fatal.
    assert "amanda" in runner._profile_startup_failures


def test_assertion_is_fatal_startup_error():
    """ProfileConnectivityError takes the existing fatal-config startup path."""
    from gateway.run import MultiplexConfigError

    assert issubclass(ProfileConnectivityError, MultiplexConfigError)


def test_truthy_sentinel_does_not_arm_the_assertion(tmp_path):
    """A duck-typed/mock config must not be able to abort startup.

    ``MagicMock(multiplex_profiles=True)`` auto-vivifies every other attribute
    as a truthy Mock, so a bare ``if self.config.require_all_profiles_connected``
    would turn this fatal assertion on for existing gateway tests that never
    asked for it (and for any duck-typed config in the wild).
    """
    runner = object.__new__(GatewayRunner)
    runner.config = MagicMock(multiplex_profiles=True)  # flag not set explicitly
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._profile_startup_failures = {}
    runner.pairing_store = MagicMock()
    runner.pairing_stores = {}
    runner._adapter_credential_fingerprint = lambda adapter: None
    runner._adapter_listener_claim = lambda platform, adapter: None

    # No exception: the offline profile is logged and skipped, as before.
    assert _run(runner, tmp_path, profiles=["main", "woody"]) == 0


def test_flag_defaults_off_and_parses_both_config_forms():
    """Default off (upstream-safe); top-level and gateway.* forms both work."""
    from gateway.config import GatewayConfig

    assert GatewayConfig.from_dict({}).require_all_profiles_connected is False
    assert (
        GatewayConfig.from_dict(
            {"require_all_profiles_connected": True}
        ).require_all_profiles_connected
        is True
    )
    # ``hermes config set gateway.require_all_profiles_connected true`` form.
    assert (
        GatewayConfig.from_dict(
            {"gateway": {"require_all_profiles_connected": "true"}}
        ).require_all_profiles_connected
        is True
    )
    # Survives a to_dict/from_dict round trip.
    original = GatewayConfig.from_dict({"require_all_profiles_connected": True})
    assert (
        GatewayConfig.from_dict(original.to_dict()).require_all_profiles_connected
        is True
    )


def test_no_secondary_profiles_is_healthy(tmp_path):
    """A gateway serving only the active profile has nothing to assert."""
    runner = _bare_runner(require_all=True)

    connected = _run(runner, tmp_path, profiles=["main"])

    assert connected == 0
