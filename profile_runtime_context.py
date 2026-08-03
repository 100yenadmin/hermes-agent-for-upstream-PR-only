"""Context-local runtime settings for profile-multiplexed execution.

A multiplexed Hermes process serves profiles with different terminal backends.
Process-global ``TERMINAL_*`` variables cannot represent that safely, so this
module carries the routed profile's resolved terminal mapping in a ContextVar.
Profile config references resolve against the already-installed profile secret
scope; the result is never written into ``os.environ``.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
import copy
import hashlib
import os
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping


@dataclass(frozen=True)
class ProfileRuntimeContext:
    """Immutable execution settings derived from one profile home."""

    profile_key: str
    terminal_env: Mapping[str, str]


_PROFILE_RUNTIME_CONTEXT: ContextVar[ProfileRuntimeContext | None] = ContextVar(
    "hermes_profile_runtime_context",
    default=None,
)
_TERMINAL_ENV_OVERRIDES: ContextVar[Mapping[str, str] | None] = ContextVar(
    "hermes_terminal_env_overrides",
    default=None,
)

# Documented terminal settings that predate the YAML bridge and remain
# environment-only. Routed profiles source them from their own secret scope;
# they must never fall through to the launch profile's process environment.
_PROFILE_TERMINAL_ENV_ONLY_KEYS = (
    "TERMINAL_SCRATCH_DIR",
    "TERMINAL_MAX_FOREGROUND_TIMEOUT",
    "TERMINAL_DISK_WARNING_GB",
)


def current_profile_runtime_context() -> ProfileRuntimeContext | None:
    """Return the active profile execution context, if one is installed."""

    return _PROFILE_RUNTIME_CONTEXT.get()


def current_profile_cache_key() -> str | None:
    """Return the opaque profile key used to partition process-local caches."""

    context = current_profile_runtime_context()
    return context.profile_key if context is not None else None


def profile_scoped_key(raw_key: str) -> str:
    """Qualify *raw_key* while preserving legacy unscoped behavior."""

    profile_key = current_profile_cache_key()
    if not profile_key:
        return raw_key
    prefix = f"profile:{profile_key}:"
    return raw_key if raw_key.startswith(prefix) else f"{prefix}{raw_key}"


def terminal_getenv(name: str, default: str | None = None) -> str | None:
    """Read terminal settings from the routed profile before process env."""

    overrides = _TERMINAL_ENV_OVERRIDES.get()
    if overrides is not None and name in overrides:
        return overrides[name]
    context = current_profile_runtime_context()
    if context is not None:
        return context.terminal_env.get(name, default)
    return os.getenv(name, default)


def overlay_terminal_env(
    target: dict[str, str],
    names: tuple[str, ...] | None = None,
) -> None:
    """Overlay the active profile terminal mapping onto a child environment."""

    context = current_profile_runtime_context()
    if context is None:
        return
    source = context.terminal_env
    if names is None:
        target.update(source)
    else:
        for name in names:
            value = source.get(name)
            if value is not None:
                target[name] = value
    overrides = _TERMINAL_ENV_OVERRIDES.get()
    if overrides:
        if names is None:
            target.update(overrides)
        else:
            for name in names:
                value = overrides.get(name)
                if value is not None:
                    target[name] = value


def terminal_scope_active() -> bool:
    """Return whether terminal settings are bound to a routed profile."""

    return current_profile_runtime_context() is not None


@contextmanager
def use_terminal_env_overrides(
    overrides: Mapping[str, str],
) -> Iterator[Mapping[str, str]]:
    """Temporarily override terminal settings without mutating ``os.environ``."""

    token, merged = set_terminal_env_overrides(overrides)
    try:
        yield merged
    finally:
        reset_terminal_env_overrides(token)


def set_terminal_env_overrides(
    overrides: Mapping[str, str],
) -> tuple[Token[Mapping[str, str] | None], Mapping[str, str]]:
    """Install nested terminal overrides and return a reset token plus mapping."""

    merged = dict(_TERMINAL_ENV_OVERRIDES.get() or {})
    merged.update({str(key): str(value) for key, value in overrides.items()})
    immutable = MappingProxyType(merged)
    return _TERMINAL_ENV_OVERRIDES.set(immutable), immutable


def reset_terminal_env_overrides(token: Token[Mapping[str, str] | None]) -> None:
    """Restore the previous terminal override mapping."""

    _TERMINAL_ENV_OVERRIDES.reset(token)


def _build_profile_context(profile_home: Path) -> ProfileRuntimeContext:
    """Resolve one profile's canonical terminal mapping without global writes."""

    from agent.secret_scope import current_secret_scope
    from hermes_cli import managed_scope
    from hermes_cli.config import (
        DEFAULT_CONFIG,
        _deep_merge,
        _expand_env_vars,
        apply_terminal_config_to_env,
        read_user_config_raw,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    resolved_home = profile_home.expanduser().resolve()
    home_token = set_hermes_home_override(str(resolved_home))
    try:
        raw_config = read_user_config_raw()
        effective_config = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), raw_config)
        profile_secrets = current_secret_scope() or {}
        effective_config = _expand_env_vars(
            effective_config,
            profile_secrets.get,
        )
        # Managed administrator values intentionally retain their documented
        # process-env expansion semantics and win after user-profile expansion.
        managed_config = managed_scope.load_managed_config()
        effective_config = managed_scope.apply_managed_overlay(effective_config)
        raw_terminal = raw_config.get("terminal")
        managed_terminal = managed_config.get("terminal") if isinstance(managed_config, dict) else None
        explicit_terminal_keys = {
            *(
                raw_terminal.keys()
                if isinstance(raw_terminal, dict)
                else ()
            ),
            *(
                managed_terminal.keys()
                if isinstance(managed_terminal, dict)
                else ()
            ),
        }
        terminal_env: dict[str, str] = {
            str(key): str(value)
            for key, value in profile_secrets.items()
            if str(key).startswith("TERMINAL_")
        }
        apply_terminal_config_to_env(
            env=terminal_env,
            config=effective_config,
            override=True,
            explicit_terminal_keys=explicit_terminal_keys,
        )
        for env_key in _PROFILE_TERMINAL_ENV_ONLY_KEYS:
            value = profile_secrets.get(env_key)
            if value is not None:
                terminal_env[env_key] = value
    finally:
        reset_hermes_home_override(home_token)

    digest = hashlib.sha256(str(resolved_home).encode("utf-8")).hexdigest()[:16]
    return ProfileRuntimeContext(
        profile_key=digest,
        terminal_env=MappingProxyType(terminal_env),
    )


@contextmanager
def use_profile_runtime_context(profile_home: str | Path) -> Iterator[ProfileRuntimeContext]:
    """Install one profile's home/execution settings and reset them reliably."""

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    resolved_home = Path(profile_home).expanduser().resolve()
    home_token = set_hermes_home_override(str(resolved_home))
    try:
        context = _build_profile_context(resolved_home)
        token: Token[ProfileRuntimeContext | None] = _PROFILE_RUNTIME_CONTEXT.set(context)
        override_token = _TERMINAL_ENV_OVERRIDES.set(None)
        try:
            try:
                from tools.process_registry import process_registry

                process_registry.recover_from_checkpoint()
            except Exception:
                pass
            yield context
        finally:
            _TERMINAL_ENV_OVERRIDES.reset(override_token)
            _PROFILE_RUNTIME_CONTEXT.reset(token)
    finally:
        reset_hermes_home_override(home_token)
