"""``hermes doctor`` check: environment variables that silently override the configured model route.

A stray ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_API_KEY`` inherited from a parent shell (or left in the
profile ``.env``) redirects or re-authenticates every turn away from what ``config.yaml`` selects, and
nothing said so until real turns failed. Only variables the runtime actually consults for the
configured provider are reported; the precedence mirrors the resolver, not a guess:

* ``<PROVIDER>_BASE_URL`` (the registry ``base_url_env_var``) rides on the credential-pool rows seeded
  from that provider's env API key (``agent.credential_pool._seed_from_env``). A trustworthy
  ``model.base_url`` still wins for Anthropic and Actual (``_pool_entry_mode_and_url``); for every other
  API-key provider the env URL wins over it.
* ``provider: openai`` expands to a custom route at ``OPENAI_BASE_URL`` ahead of api.openai.com unless a
  ``providers.openai`` entry exists (``expand_direct_api_alias``).
* ``ANTHROPIC_TOKEN`` / ``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY`` come before a stored Hermes
  login (``resolve_anthropic_token``); an API key with no OAuth variable also drops OAuth logins from
  rotation (``_seed_anthropic_singletons``).

Never reported, because nothing consults them for the configured route: ``ANTHROPIC_AUTH_TOKEN`` (the
Anthropic client omits the SDK's env fallback, ``_new_sdk_client``), ``OPENAI_BASE_URL`` under
OpenRouter / custom (``_resolve_openrouter_runtime`` never picks an endpoint from it), and any variable
of a provider other than the configured one. Values are never printed: the variable name, where it came
from, and for URLs only scheme + host.
"""

from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlsplit

from hermes_cli.doctor_report import check_info, check_ok, check_warn, doctor_check

# A configured model.base_url beats an env-seeded pool row's URL for these (_pool_entry_mode_and_url).
_CONFIG_URL_WINS = frozenset({"anthropic", "actual"})
# Resolved before the pool by a requested-name shortcut (azure-foundry), or pool rows carry a fixed URL
# (openrouter): their registry base-URL variable never reaches the route through the generic env row.
_NO_ENV_ROW_URL = frozenset({"azure-foundry", "openrouter"})
_ANTHROPIC_OAUTH_VARS = ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


class EnvOverride(NamedTuple):
    var: str
    source: str     # where the value came from — never the value itself
    overrides: str  # what it changes: the route host, or the auth mode
    fix: str
    warn: bool      # True: points away from config; False: matches config / is the only source


def _env(var: str) -> str:
    """What the credential pool seeds from: the profile ``.env`` first, then the process environment."""
    from agent.credential_pool import get_env_prefer_dotenv
    return (get_env_prefer_dotenv(var) or "").strip()


def _in_dotenv(var: str) -> bool:
    from hermes_cli.config import load_env
    return bool(str(load_env().get(var) or "").strip())


def _source(var: str) -> str:
    from hermes_cli.env_loader import get_secret_source
    from hermes_constants import display_hermes_home
    if _in_dotenv(var):
        return f"{display_hermes_home()}/.env"
    label = get_secret_source(var)
    return f"secret source '{label}'" if label else "the process environment"


def _unset_hint(var: str) -> str:
    if _in_dotenv(var):
        from hermes_constants import display_hermes_home
        return f"remove {var} from {display_hermes_home()}/.env"
    return f"unset {var} in the shell or service that launches Hermes"


def _origin(url: str) -> str:
    """``scheme://host`` only: paths, ports, userinfo and queries can carry tokens."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "an unparseable URL"
    return f"{parts.scheme}://{host}" if parts.scheme and host else "an unparseable URL"


def _route_key(url: str, *, strip_v1: bool) -> tuple:
    """Comparable base URL: scheme, host, port, path without a trailing ``/`` (and ``/v1`` on the
    Anthropic wire, which the adapter strips before appending ``/v1/messages``)."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return (url.strip(),)
    path = parts.path.rstrip("/")
    if strip_v1 and path.endswith("/v1"):
        path = path[:-3]
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port, path)


def _route_override(var: str, provider: str, env_url: str, configured: str, via: str) -> EnvOverride:
    strip_v1 = provider == "anthropic"
    same = _route_key(env_url, strip_v1=strip_v1) == _route_key(configured, strip_v1=strip_v1)
    env_o, cfg_o = _origin(env_url), _origin(configured)
    if same:
        what = f"{provider} route {env_o} {via} (matches the configured route)"
    else:
        tail = " (same host, different path or port)" if env_o == cfg_o else ""
        what = f"{provider} route {cfg_o} → {env_o} {via}{tail}"
    fix = (f"{var} ({_source(var)}) redirects {provider} requests away from config.yaml: {_unset_hint(var)}, "
           "or put the endpoint in config.yaml if the redirect is intended")
    return EnvOverride(var, _source(var), what, fix, warn=not same)


def _registry_base_url_override(provider: str, model_cfg: dict) -> EnvOverride | None:
    """The configured API-key provider's ``base_url_env_var``, when an env-seeded pool row carries it."""
    from hermes_cli.auth import PROVIDER_REGISTRY
    pconfig = PROVIDER_REGISTRY.get(provider)
    var = getattr(pconfig, "base_url_env_var", "") if pconfig else ""
    if not var or pconfig.auth_type != "api_key" or provider in _NO_ENV_ROW_URL:
        return None
    env_url = _env(var).rstrip("/")
    key_var = next((v for v in pconfig.api_key_env_vars if _env(v)), "")
    if not env_url or not key_var:
        return None  # only rows seeded from an env API key carry the env URL
    from agent.credential_pool import _profile_seeds_pool
    if _profile_seeds_pool(provider):
        return None  # the provider profile seeds its own rows; the generic env URL is not applied
    from hermes_cli.runtime_provider import _anthropic_cfg_base_url, _config_base_url_for_provider
    default = (pconfig.inference_base_url or "").rstrip("/")
    cfg_url = _anthropic_cfg_base_url(model_cfg) if provider == "anthropic" else _config_base_url_for_provider(model_cfg, provider)
    if cfg_url and (provider in _CONFIG_URL_WINS or env_url == default):
        return None  # model.base_url wins; the variable is ignored
    return _route_override(var, provider, env_url, cfg_url or default, f"for requests authenticated with {key_var}")


def _is_openai_alias(raw_provider: str) -> bool:
    """``provider: openai`` without a ``providers.openai`` entry expands to a custom route first."""
    from hermes_cli.runtime_provider import _get_named_custom_provider
    from hermes_cli.runtime_provider_custom import _DIRECT_API_BASE_URLS
    return raw_provider in _DIRECT_API_BASE_URLS and _get_named_custom_provider(raw_provider) is None


def _openai_alias_override(raw_provider: str) -> EnvOverride | None:
    """The expanded ``provider: openai`` route goes to ``OPENAI_BASE_URL`` ahead of the public endpoint."""
    from agent.secret_scope import get_secret_str
    from hermes_cli.runtime_provider_custom import _DIRECT_API_BASE_URLS
    env_url = (get_secret_str("OPENAI_BASE_URL", "") or "").strip().rstrip("/")
    if not env_url:
        return None
    return _route_override("OPENAI_BASE_URL", raw_provider, env_url, _DIRECT_API_BASE_URLS[raw_provider],
                           "(provider: openai follows it)")


def _anthropic_auth_override() -> EnvOverride | None:
    """Which env credential Anthropic requests authenticate with, against a stored Hermes OAuth login.
    Claude Code's borrowed login is not read here: on macOS that is a Keychain read doctor must not trigger."""
    var = next((v for v in (*_ANTHROPIC_OAUTH_VARS, "ANTHROPIC_API_KEY") if _env(v)), "")
    if not var:
        return None
    from agent.anthropic_credentials import _is_oauth_token, read_hermes_oauth_credentials
    value = _env(var)
    mode = "OAuth token" if _is_oauth_token(value) else "API key"
    stored = str((read_hermes_oauth_credentials() or {}).get("accessToken") or "").strip()
    if not stored or stored == value:
        return EnvOverride(var, _source(var), f"Anthropic auth: {mode} from {var}", "", warn=False)
    if var == "ANTHROPIC_API_KEY":
        what = "Anthropic auth: API key (Console, per-token billing) instead of the stored Hermes OAuth login"
    else:
        what = f"Anthropic auth: static {mode} from {var} ahead of the stored (refreshable) Hermes OAuth login"
    fix = (f"{var} ({_source(var)}) replaces your stored Anthropic login: {_unset_hint(var)} to use the login, "
           "or drop the login ('hermes auth list anthropic', then 'hermes auth remove anthropic <id>') if the "
           "env credential is the one you want")
    return EnvOverride(var, _source(var), what, fix, warn=True)


def find_route_env_overrides(model_cfg: dict) -> list:
    """Every env override of the configured route/auth, in display order (pure: no network, no writes)."""
    raw = str(model_cfg.get("provider") or "").strip().lower()
    if not raw or raw == "auto":
        return []  # auto-detection has no configured route to compare against
    if _is_openai_alias(raw):
        return [o for o in (_openai_alias_override(raw),) if o is not None]
    from hermes_cli.providers import normalize_provider
    provider = normalize_provider(raw)
    found = [_registry_base_url_override(provider, model_cfg)]
    if provider == "anthropic":
        found.append(_anthropic_auth_override())
    return [o for o in found if o is not None]


@doctor_check(on_error="Environment overrides", detail="(could not check: {e})")
def _check_route_env_overrides(should_fix: bool, f) -> None:
    """Warn when an inherited or ``.env`` variable redirects/re-authenticates the configured model route."""
    from hermes_cli.doctor_connectivity import _model_cfg
    overrides = find_route_env_overrides(_model_cfg())
    if not any(o.warn for o in overrides):
        check_ok("No environment variable overrides the configured model route")
    for o in overrides:
        if o.warn:
            check_warn(f"{o.var} from {o.source}", f"({o.overrides})")
            f.manual_issues.append(o.fix)
        else:
            check_info(f"{o.var} from {o.source}: {o.overrides}")
