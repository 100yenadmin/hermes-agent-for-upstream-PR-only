"""Evaluation-only network budget; no changes to Hermes provider behavior.

Receipts contain request shape/status only, never headers, prompt text or opaque
provider outputs. The process must start with a clean environment and private
HERMES_HOME. This is a client instrumentation guard, not a general tool sandbox.
"""

import contextlib
import fcntl
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit


def sanitized_usage(usage):
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "output_tokens", "total_tokens")
    result = {key: usage[key] for key in keys if isinstance(usage.get(key), (int, float))}
    for key in ("input_tokens_details", "output_tokens_details"):
        result[key] = {name: value for name, value in (usage.get(key) or {}).items()
                       if name in {"cached_tokens", "cache_write_tokens", "reasoning_tokens"}
                       and isinstance(value, (int, float))}
    return result


class LiveBudget:
    def __init__(self, ledger, scenario):
        self.path = Path(ledger)
        self.scenario = scenario

    @contextlib.contextmanager
    def _state(self):
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            state = json.load(handle)
            yield state
            handle.seek(0)
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.truncate()
            handle.flush()

    def before(self, request):
        url = urlsplit(str(request.url))
        if not (url.scheme == "https" and url.hostname == "chatgpt.com"
                and url.path.startswith("/backend-api/codex/")):
            raise RuntimeError("Evaluation blocked non-subscription HTTP destination")
        body = {}
        raw = getattr(request, "content", None)
        if raw is None:
            raw = getattr(request, "body", None)
        if raw:
            try:
                body = json.loads(raw)
            except (ValueError, TypeError):
                raise RuntimeError("Evaluation requires inspectable JSON requests") from None
        if body and body.get("model") != "gpt-6-astra":
            raise RuntimeError("Evaluation blocked unexpected model")
        with self._state() as state:
            if len(state["requests"]) >= 60 or time.time() - state["started_unix"] >= 3600:
                raise RuntimeError("Evaluation request/time budget exhausted")
            number = len(state["requests"]) + 1
            state["requests"].append({
                "number": number, "scenario": self.scenario,
                "method": request.method, "path": url.path,
                "started_unix": time.time(),
                "model": body.get("model"),
                "reasoning": body.get("reasoning"),
                "input_types": [x.get("type", x.get("role")) for x in body.get("input", []) if isinstance(x, dict)],
                "input_checkpoint_sha256": [
                    hashlib.sha256(str(x.get("encrypted_content", "")).encode()).hexdigest()
                    for x in body.get("input", []) if isinstance(x, dict) and x.get("type") == "compaction"
                ],
                "context_management": body.get("context_management"),
                "instructions_sha256": hashlib.sha256(str(body.get("instructions", "")).encode()).hexdigest(),
                "status": "DISPATCHED",
            })
        return number

    def after(self, number, *, status=None, error=None):
        with self._state() as state:
            item = state["requests"][number - 1]
            item["http_status"] = status
            item["error_class"] = error
            item["headers_elapsed_seconds"] = time.time() - item["started_unix"]
            item["status"] = "HEADERS_RECEIVED" if status else "TRANSPORT_ERROR"

    def install(self):
        import httpx
        import requests

        original_sync = httpx.Client.send
        original_async = httpx.AsyncClient.send
        original_requests = requests.Session.send
        budget = self

        class ObservedStream(httpx.SyncByteStream):
            def __init__(self, stream, number):
                self.stream, self.number = stream, number

            def __iter__(self):
                pending = b""
                for chunk in self.stream:
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if not line.startswith(b"data: "):
                            continue
                        try:
                            event = json.loads(line[6:])
                        except (ValueError, TypeError):
                            continue
                        if event.get("type") == "response.output_item.done":
                            output = event.get("item", {})
                            with budget._state() as state:
                                item = state["requests"][self.number - 1]
                                item.setdefault("output_types", []).append(output.get("type"))
                                if output.get("type") == "compaction":
                                    item.setdefault("checkpoint_sha256", []).append(
                                        hashlib.sha256(str(output.get("encrypted_content", "")).encode()).hexdigest())
                        if event.get("type") not in {"response.completed", "response.failed", "response.incomplete"}:
                            continue
                        response = event.get("response", {})
                        with budget._state() as state:
                            item = state["requests"][self.number - 1]
                            item["terminal_event"] = event.get("type")
                            item["total_elapsed_seconds"] = time.time() - item["started_unix"]
                            item["usage"] = sanitized_usage(response.get("usage"))
                            item.setdefault("output_types", [x.get("type") for x in response.get("output", [])])
                            item.setdefault("checkpoint_sha256", [
                                hashlib.sha256(str(x.get("encrypted_content", "")).encode()).hexdigest()
                                for x in response.get("output", []) if x.get("type") == "compaction"
                            ])
                    yield chunk

            def close(self):
                self.stream.close()

        def sync(client, request, *args, **kwargs):
            number = self.before(request)
            try:
                response = original_sync(client, request, *args, **kwargs)
            except Exception as exc:
                self.after(number, error=type(exc).__name__)
                raise
            self.after(number, status=response.status_code)
            with self._state() as state:
                state["requests"][number - 1]["content_type"] = response.headers.get("content-type")
                state["requests"][number - 1]["stream_requested"] = bool(kwargs.get("stream"))
            if kwargs.get("stream") and urlsplit(str(request.url)).path.endswith("/responses"):
                response.stream = ObservedStream(response.stream, number)
            return response

        async def asynchronous(client, request, *args, **kwargs):
            number = self.before(request)
            try:
                response = await original_async(client, request, *args, **kwargs)
            except Exception as exc:
                self.after(number, error=type(exc).__name__)
                raise
            self.after(number, status=response.status_code)
            return response

        def requests_send(client, request, *args, **kwargs):
            number = self.before(request)
            try:
                response = original_requests(client, request, *args, **kwargs)
            except Exception as exc:
                self.after(number, error=type(exc).__name__)
                raise
            self.after(number, status=response.status_code)
            return response

        httpx.Client.send = sync
        httpx.AsyncClient.send = asynchronous
        requests.Session.send = requests_send
