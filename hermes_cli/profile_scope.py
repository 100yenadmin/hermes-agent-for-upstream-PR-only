"""Request-local evaOS managed profile authorization.

The public broker and VM-local proxy authenticate the customer session before
these headers reach the loopback-only Hermes dashboard.  This module keeps the
resulting profile authority in a ContextVar so concurrent requests never share
profile state.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re
from typing import Iterable, Mapping

_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class ManagedProfilePrincipal:
    user_id: str
    allowed_profiles: tuple[str, ...]
    primary_profile: str
    admin: bool
    session_id: str = ""


_principal: ContextVar[ManagedProfilePrincipal | None] = ContextVar(
    "evaos_managed_profile_principal",
    default=None,
)


def current_principal() -> ManagedProfilePrincipal | None:
    return _principal.get()


def principal_from_headers(headers: Mapping[str, str]) -> ManagedProfilePrincipal | None:
    raw_allowed = (headers.get("x-evaos-allowed-profiles") or "").strip()
    raw_primary = (headers.get("x-evaos-primary-profile") or "").strip()
    if not raw_allowed and not raw_primary:
        return None
    allowed = tuple(part.strip() for part in raw_allowed.split(",") if part.strip())
    if (
        not allowed
        or len(set(allowed)) != len(allowed)
        or any(not _PROFILE_RE.fullmatch(profile) for profile in allowed)
        or not _PROFILE_RE.fullmatch(raw_primary)
        or raw_primary not in allowed
    ):
        raise ValueError("invalid managed profile scope")
    user_id = (headers.get("x-evaos-principal-user") or "").strip()
    if not user_id:
        raise ValueError("invalid managed profile principal")
    return ManagedProfilePrincipal(
        user_id=user_id,
        allowed_profiles=tuple(sorted(allowed)),
        primary_profile=raw_primary,
        admin=(headers.get("x-evaos-profile-admin") or "").strip() == "1",
        session_id=(headers.get("x-evaos-session-id") or "").strip(),
    )


def require_profile(name: str | None, *, allow_selectors: Iterable[str] = ()) -> str:
    """Return the effective profile or raise PermissionError.

    With no managed principal this is a no-op.  Under managed authority an
    omitted/default/current profile resolves to the server-selected primary.
    Route-specific selectors such as ``all`` may be explicitly allowed but do
    not grant access to any profile outside the principal's set.
    """

    principal = current_principal()
    requested = (name or "").strip()
    if principal is None:
        return requested
    if requested in set(allow_selectors):
        return requested
    if requested in {"", "default", "current"}:
        return principal.primary_profile
    if requested not in principal.allowed_profiles:
        raise PermissionError("profile is not authorized")
    return requested


def filter_profile_names(names: Iterable[str]) -> set[str]:
    principal = current_principal()
    values = set(names)
    if principal is None:
        return values
    return values.intersection(principal.allowed_profiles)


@contextmanager
def managed_profile_context(principal: ManagedProfilePrincipal | None):
    token = _principal.set(principal)
    home_token = None
    if principal is not None:
        from hermes_cli import profiles as profiles_mod
        from hermes_constants import set_hermes_home_override

        profile_home = profiles_mod._get_profiles_root() / principal.primary_profile
        home_token = set_hermes_home_override(str(profile_home))
    try:
        yield principal
    finally:
        if home_token is not None:
            from hermes_constants import reset_hermes_home_override

            reset_hermes_home_override(home_token)
        _principal.reset(token)

