"""Check only the configured model and endpoint, without auxiliary defaults or recovery.

One request is issued, except Anthropic's same-model, same-endpoint stream-unavailable
fallback may issue two. Never use call_llm or aux_probe_mode here. External runtimes
(app-server, process, MoA, Bedrock and unknown wire modes) cannot be verified this way.
"""

import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread, get_ident
from urllib.parse import urlsplit

_SKIP = ContextVar("setup_skip_verify", default=False)

@dataclass
class VerifyResult:
    ok: bool
    kind: str
    provider: str = ""
    model: str = ""
    seconds: float = 0.0
    detail: str = ""
    endpoint: str = ""

@contextmanager
def verification_scope(args):
    """Keep --no-verify local to this wizard invocation (including nested flows)."""
    token = _SKIP.set(bool(getattr(args, "no_verify", False)))
    try:
        yield
    finally:
        _SKIP.reset(token)


def _disabled():
    return _SKIP.get() or os.environ.get("HERMES_SETUP_SKIP_VERIFY", "").lower() in {"1", "true", "yes", "on"}  # health: allow HX002 -- SPEC requires this setup-only test/automation opt-out


def finish_model_setup(config):
    """Shared epilogue; a skipped check must not perturb existing setup output."""
    if not _disabled():
        print_verify_result(verify_model_route(config))


def _failure(exc, provider, model, start, endpoint=""):
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    status = status if isinstance(status, int) else None
    code = getattr(exc, "code", None)
    kind = "other"
    if status in {401, 403} or any(word in name for word in ("Authentication", "PermissionDenied", "AuthError")):
        kind = "auth"
    elif status == 404 or code in ("model_not_found", "invalid_model", "model_not_available"):
        kind = "model"
    elif status == 429:
        kind = "rate_limited"
    elif any(word in name.lower() for word in ("connection", "timeout", "connecterror")):
        kind = "network"
    # Never stringify an exception or copy its message/body: providers can echo credentials.
    detail = f"{name}{' ' + str(status) if status is not None else ''}"
    return VerifyResult(False, kind, provider, model, time.monotonic() - start, detail, endpoint)


def _unsupported(runtime):
    if runtime.get("api_mode") == "codex_app_server":
        return "the app server"
    if (runtime.get("api_mode") not in {"chat_completions", "codex_responses", "anthropic_messages"}
            or runtime.get("command") or runtime.get("provider") in {"moa", "bedrock", "vertex"}):
        return "its own runtime"
    if runtime.get("provider") == "gemini":
        from agent.gemini_native_adapter import is_native_gemini_base_url
        if is_native_gemini_base_url(runtime.get("base_url", "")):
            return "its own runtime"
    return ""

@contextmanager
def _quiet_request():
    # Provider/SDK debug logs can echo credentials, including after the caller's deadline.
    worker = get_ident()
    loggers = [logging.getLogger(), *[v for v in list(logging.Logger.manager.loggerDict.values())
                                     if isinstance(v, logging.Logger)]]
    targets = [*loggers, *[h for logger in loggers for h in logger.handlers], logging.lastResort]
    targets = set(t for t in targets if t is not None)
    keep = lambda record: record.thread != worker
    for target in targets:
        target.addFilter(keep)
    try:
        yield
    finally:
        for target in targets:
            target.removeFilter(keep)


def _request(runtime, model, timeout):
    from agent.auxiliary_client import _endpoint_default_headers, build_or_headers, resolve_provider_client
    from hermes_cli.config import get_custom_provider_extra_headers
    if not runtime.get("base_url") or not runtime.get("api_key"):
        raise ValueError("The configured route is incomplete")
    # Bind concrete endpoint/key rather than re-resolving provider pools or auxiliary defaults.
    client, _ = resolve_provider_client(
        "custom", model=model, explicit_base_url=runtime["base_url"], explicit_api_key=runtime["api_key"],
        api_mode=runtime["api_mode"], main_runtime=runtime,
    )
    headers = _endpoint_default_headers(runtime["base_url"], runtime["provider"]) or {}
    if runtime["provider"] == "openrouter":
        headers = {**build_or_headers(), **headers}
    headers.update(get_custom_provider_extra_headers(runtime["base_url"]))
    kwargs = {"extra_headers": headers} if headers else {}
    client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Reply with the word OK."}],
                                   max_tokens=16, timeout=timeout, **kwargs)


def _verify(provider, model, start, timeout):
    endpoint = "the configured endpoint"
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        runtime = resolve_runtime_provider(requested=provider, target_model=model)
        provider = runtime["provider"]
        reason = _unsupported(runtime)
        if reason:
            return VerifyResult(False, "skipped", provider, model, detail=reason)
        parsed = urlsplit(runtime["base_url"])
        endpoint = f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else endpoint
        if time.monotonic() - start >= timeout:
            raise TimeoutError("Resolution exceeded the deadline")
        _request(runtime, model, timeout)
        return VerifyResult(True, "ok", provider, model, time.monotonic() - start)
    except Exception as exc:  # health: allow BLE001 -- optional diagnostic reports sanitized failures
        return _failure(exc, provider, model, start, endpoint)


def verify_model_route(config, *, timeout=20.0) -> VerifyResult:
    """Bound resolution and the request; abandoned workers never report late results."""
    start = time.monotonic()
    provider = model = ""
    try:
        section = config.get("model", {})
        provider = section.get("provider", "") if isinstance(section, dict) else ""
        model = section.get("default", "") if isinstance(section, dict) else section
        if _disabled() or not provider or not model:
            return VerifyResult(False, "skipped")
        results = Queue(maxsize=1)
        def request():
            with _quiet_request():
                results.put(_verify(provider, model, start, timeout))
        Thread(target=copy_context().run, args=(request,), daemon=True).start()
        try:
            return results.get(timeout=max(0, timeout - (time.monotonic() - start)))
        except Empty:
            return VerifyResult(False, "network", provider, model, time.monotonic() - start,
                                f"no answer within {timeout:g} s", "the configured endpoint")
    except Exception as exc:  # health: allow BLE001 -- optional diagnostic must never fail setup
        return _failure(exc, provider, model, start)


def print_verify_result(result):
    from hermes_cli.setup import print_info, print_success, print_warning
    if result.kind == "skipped":
        if result.detail:
            print_info(f"Model check skipped: this route runs through {result.detail}; your first message will confirm it.")
        return
    if result.ok:
        print_success(f"Model check: {result.provider}/{result.model} answered in {result.seconds:.1f} s")
        return
    messages = {
        "auth": f"the provider rejected the credentials ({result.detail}). Run `hermes model` to sign in again or re-enter the key.",
        "model": f"the provider does not offer {result.model} to this account ({result.detail}). Run `hermes model` to pick another model.",
        "network": f"could not reach {result.endpoint} ({result.detail}). Check the base URL and your network; `hermes doctor` also lists environment variables that redirect the route.",
        "other": f"({result.detail}). `hermes doctor` has more detail.",
    }
    if result.kind == "rate_limited":
        print_info(f"Model check: {result.provider} answered but is rate-limiting this key right now; setup is fine.")
    else:
        print_warning(f"Model check failed: {messages[result.kind]}")
