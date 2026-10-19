"""Gzip native Anthropic Messages bodies (Claude Code parity, 2.1.300+).

Lossless transport encoding: tokens and prompt caching are unchanged. A rejected
encoding disables gzip process-wide and resends the original body once.
"""

import gzip
import json
import logging
import os
import re
import secrets
from functools import cache, lru_cache

import httpx

NATIVE_HOST = "api.anthropic.com"
MIN_BODY_BYTES = 4096
GZIP_LEVEL = 3
_REJECTION_STATUSES = frozenset({400, 403, 415})
_latched_off: str | None = None
logger = logging.getLogger(__name__)


def native_gzip_allowed() -> bool:
    from agent.process_bootstrap import _get_proxy_for_base_url

    # Claude Code skips proxy/custom-CA routes too: middleboxes may reject gzip
    # with resets or 5xx errors that the 400/403/415 classifier cannot see.
    return not _get_proxy_for_base_url(f"https://{NATIVE_HOST}") and not any(
        os.environ.get(name)
        for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
    )


def _padding() -> bytes:
    return b"\n" + "".join(secrets.choice(" \t") for _ in range(secrets.randbelow(257))).encode()


def _eligible(request: httpx.Request) -> bool:
    return (
        _latched_off is None
        and request.method == "POST"
        and request.url.host == NATIVE_HOST
        and request.url.path.startswith("/v1/messages")
        and "content-encoding" not in request.headers
    )


def _compressed(request: httpx.Request, raw: bytes) -> httpx.Request:
    body = gzip.compress(raw + _padding(), compresslevel=GZIP_LEVEL, mtime=0)
    headers = request.headers.copy()
    headers["Content-Encoding"] = "gzip"
    headers["Content-Length"] = str(len(body))
    return httpx.Request(request.method, request.url, headers=headers, content=body, extensions=request.extensions)


def _says_invalid_json(text: str) -> bool:
    try:
        data = json.loads(text)
    except ValueError:
        return re.match(r"^bad json: ", text.strip()) is not None
    if not isinstance(data, dict):
        return False
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return isinstance(message, str) and message.startswith((
            "The request body is not valid JSON",
            "Failed to parse request: could not parse request body as JSON",
        ))
    return data.get("reason") == "bad_json" and isinstance(error, str) and error.startswith("bad json: ")


def _rejection_kind(response: httpx.Response) -> str | None:
    if response.status_code not in _REJECTION_STATUSES:
        return None
    from_anthropic = response.headers.get("request-id", "").startswith("req_") or "cf-ray" in response.headers
    if not from_anthropic:
        return "non_anthropic_rejection"
    if response.status_code == 415:
        return "unsupported_encoding"
    if response.status_code == 400 and _says_invalid_json(response.content[:8192].decode("utf-8", "replace")):
        return "invalid_json_at_origin"
    return None


def _latch(kind: str, status: int) -> None:
    global _latched_off
    if _latched_off is None:
        _latched_off = kind
        logger.warning("Anthropic request gzip disabled: %s (HTTP %s)", kind, status)


class _GzipRequestSend:
    def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        if not _eligible(request):
            return super().send(request, **kwargs)
        raw = request.read()
        if len(raw) < MIN_BODY_BYTES:
            return super().send(request, **kwargs)
        response = super().send(_compressed(request, raw), **kwargs)
        if response.status_code not in _REJECTION_STATUSES:
            return response
        response.read()
        kind = _rejection_kind(response)
        if kind is None:
            return response
        _latch(kind, response.status_code)
        response.close()
        return super().send(request, **kwargs)


@cache
def anthropic_gzip_client_cls(sdk) -> type | None:
    base = getattr(sdk, "DefaultHttpxClient", None)
    if not isinstance(base, type):
        return None

    class GzipHttpxClient(_GzipRequestSend, base):
        pass

    return GzipHttpxClient


@lru_cache(maxsize=1)
def gzip_httpx_client_cls() -> type:
    class GzipHttpxClient(_GzipRequestSend, httpx.Client):
        pass

    return GzipHttpxClient
