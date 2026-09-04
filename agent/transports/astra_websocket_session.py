"""Internal Responses WebSocket driver for the direct GPT-6 Astra route.

The driver is deliberately small and synchronous.  Hermes owns the receive loop and
the existing Responses assembler; the socket is only a transport for the provider's
``response.create``/``response.steer`` events.  No other Responses route is eligible.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlsplit
from typing import Any, Callable

logger = logging.getLogger(__name__)


class AstraPreDispatchError(RuntimeError):
    """The WebSocket lane failed before the initial request could be sent."""


class AstraDeliveryUncertainError(RuntimeError):
    """The provider may own bytes already sent; retrying would be unsafe."""

    delivery_uncertain = True


class AstraProtocolError(RuntimeError):
    """The provider returned an explicit protocol/error condition."""


def _base_url_is_official(base_url: Any) -> bool:
    try:
        parsed = urlsplit(str(base_url or "").strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname == "api.openai.com" and parsed.path.rstrip("/") == "/v1"


def is_astra_websocket_eligible(agent: Any, request: dict[str, Any] | None = None) -> bool:
    """Exact API-key Astra gate; all unsupported routes retain the SSE path."""
    model = str(getattr(agent, "model", "") or "").strip().lower().rsplit("/", 1)[-1]
    if getattr(agent, "api_mode", None) != "codex_responses" or model != "gpt-6-astra":
        return False
    if not _base_url_is_official(getattr(agent, "base_url", "")):
        return False
    if not isinstance(getattr(agent, "api_key", None), str) or not agent.api_key.strip():
        return False
    auth_mode = str(getattr(agent, "auth_mode", "api_key") or "api_key").strip().lower()
    if auth_mode not in {"", "api_key", "apikey"}:
        return False
    if getattr(agent, "provider", None) in {"openai-codex", "xai-oauth", "azure", "azure-foundry"}:
        return False
    if callable(getattr(agent, "_is_codex_backend", None)) and agent._is_codex_backend():
        return False
    if getattr(agent, "is_subagent", False) or getattr(agent, "compression_checkpoint_required", False):
        return False
    request = request or {}
    if (request.get("context_management") or request.get("conversation") or request.get("conversation_id")
            or request.get("previous_response_id")):
        return False
    return True


def _event_field(event: Any, name: str, default: Any = None) -> Any:
    value = event.get(name, default) if isinstance(event, dict) else getattr(event, name, default)
    return default if value is None else value


def _event_response_id(event: Any) -> str | None:
    response = _event_field(event, "response")
    raw = _event_field(event, "response_id") or _event_field(response, "id")
    return str(raw).strip() if raw else None


def _event_item_id(event: Any) -> str | None:
    item = _event_field(event, "item")
    raw = _event_field(event, "item_id") or _event_field(item, "id")
    return str(raw).strip() if raw else None


def _ws_url(base_url: str) -> str:
    return "wss://api.openai.com/v1/responses"


def _default_connect(url: str, api_key: str, timeout: float):
    from websockets.sync.client import connect

    return connect(
        url,
        additional_headers={
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": "responses_websockets=2026-02-06",
        },
        open_timeout=timeout,
    )


def _append_pending(agent: Any, text: str) -> None:
    lock = getattr(agent, "_pending_steer_lock", None)
    context = lock if lock is not None else threading.Lock()
    with context:
        existing = getattr(agent, "_pending_steer", None)
        agent._pending_steer = f"{existing}\n{text}" if existing else text


def _remove_pending(agent: Any, text: str) -> None:
    lock = getattr(agent, "_pending_steer_lock", None)
    context = lock if lock is not None else threading.Lock()
    with context:
        existing = getattr(agent, "_pending_steer", None)
        if not existing:
            return
        if existing == text:
            agent._pending_steer = None
        elif existing.startswith(text + "\n"):
            agent._pending_steer = existing[len(text) + 1:] or None
        else:
            parts = existing.splitlines()
            try:
                parts.remove(text)
            except ValueError:
                return
            agent._pending_steer = "\n".join(parts) or None


@dataclass
class _Steer:
    sequence: int
    text: str
    previous_response_id: str
    accepted: bool = False
    failed: bool = False


class AstraWebSocketSession:
    """One owner-thread receive loop plus lock-protected cross-thread steering."""

    def __init__(self, agent: Any, *, connect: Callable[..., Any] | None = None, timeout: float = 30.0) -> None:
        self.agent = agent
        self._connect = connect or _default_connect
        self.timeout = timeout
        self._state = "IDLE"
        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._interrupt = threading.Event()
        self._socket: Any = None
        self._request: dict[str, Any] = {}
        self._response_id: str | None = None
        self._assemblers: dict[str, Any] = {}
        self._steers: dict[int, _Steer] = {}
        self._next_sequence = 0
        self._await_successor = False
        self._continuations: set[str] = set()
        self._seen_events: set[str] = set()
        self._seen_sequences: set[str] = set()
        self._seen_items: set[tuple[str, str, str]] = set()
        self.delivery_uncertain = False
        self.last_error: Exception | None = None

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def response_id(self) -> str | None:
        with self._state_lock:
            return self._response_id

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state

    def _send(self, payload: dict[str, Any]) -> None:
        if self._socket is None:
            raise AstraDeliveryUncertainError("Astra WebSocket is not open")
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._send_lock:
            try:
                self._socket.send(wire)
            except Exception as exc:
                self.delivery_uncertain = True
                self.last_error = exc
                self._set_state("POST_DISPATCH_AMBIGUOUS")
                raise AstraDeliveryUncertainError("Astra WebSocket send outcome is uncertain") from exc

    def _append_steer(self, text: str) -> int:
        with self._state_lock:
            self._next_sequence += 1
            sequence = self._next_sequence
            self._steers[sequence] = _Steer(sequence, text, self._response_id or "")
            self._await_successor = True
            _append_pending(self.agent, text)
            self._set_state("STEER_ADMITTED")
            return sequence

    def request_steer(self, text: str) -> bool:
        """Admit and send one steer, returning False when no active response exists."""
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        with self._state_lock:
            if self._socket is None or self._state not in {"ACTIVE", "SUCCESSOR_CREATED", "ACCEPTED"} or not self._response_id:
                return False
            previous_id = self._response_id
            sequence = self._append_steer(cleaned)
        try:
            # Responses steering deliberately has a tiny wire shape. In particular, stream_id is never sent.
            self._send({
                "type": "response.steer", "previous_response_id": previous_id,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": cleaned}]}],
            })
        except AstraDeliveryUncertainError:
            # The provider may have accepted the input; leaving it in Hermes' fallback queue could duplicate it.
            _remove_pending(self.agent, cleaned)
            raise
        return bool(sequence)

    def request_interrupt(self) -> None:
        self._interrupt.set()
        self.close()

    def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            with self._send_lock:
                try:
                    socket.close()
                except Exception:
                    logger.debug("Astra WebSocket close failed", exc_info=True)

    def _new_assembler(self, response_id: str):
        from agent.codex_runtime import _CodexResponseAssembler

        return _CodexResponseAssembler(
            model=self._request.get("model", getattr(self.agent, "model", "")),
            on_text_delta=self._on_text_delta,
            on_reasoning_delta=self._on_reasoning_delta,
            on_commentary_message=self._on_commentary_message,
            on_first_delta=self._on_first_delta,
            on_async_tool_call=self._async_tool_call,
            on_async_tool_announcement=self._async_tool_announcement,
        )

    def _on_text_delta(self, text: str) -> None:
        if not text:
            return
        self.agent._codex_streamed_text_parts.append(text)
        callback = getattr(self.agent, "_fire_stream_delta", None)
        if callable(callback):
            callback(text)

    def _on_reasoning_delta(self, text: str) -> None:
        callback = getattr(self.agent, "_fire_reasoning_delta", None)
        if callable(callback):
            callback(text)

    def _on_commentary_message(self, text: str) -> None:
        callback = getattr(self.agent, "_fire_streamed_codex_commentary", None)
        if callable(callback):
            callback(text)

    def _on_first_delta(self) -> None:
        callback = getattr(self, "_first_delta", None)
        if callable(callback):
            callback()

    @property
    def _first_delta(self):
        return getattr(self, "_on_first_delta_callback", None)

    def _async_tool_call(self, call: Any) -> None:
        callback = getattr(getattr(self.agent, "_astra_async_executor", None), "admit", None)
        if callable(callback):
            callback(call)

    def _async_tool_announcement(self, call: Any) -> None:
        callback = getattr(getattr(self.agent, "_astra_async_executor", None), "reserve", None)
        if callable(callback):
            callback(call)

    def _saved_tool_result(self, call_id: str) -> Any:
        messages = getattr(self.agent, "_session_messages", None) or []
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            stored_id = str(message.get("tool_call_id") or "").split("|", 1)[0]
            if stored_id == call_id:
                return message.get("content", "")
        return None

    def _fill_required_input(self, required: Any) -> list[Any]:
        if not isinstance(required, list):
            raise AstraProtocolError("Astra steer.pending did not provide required_input")
        filled = []
        for stub in required:
            if not isinstance(stub, dict):
                raise AstraProtocolError("Astra required_input item is not an object")
            item = dict(stub)
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            result = self._saved_tool_result(call_id) if call_id else None
            if result is None:
                raise AstraProtocolError(f"No saved tool result for required call {call_id or '<missing>'}")
            if "result" in item:
                item["result"] = result
            else:
                item["output"] = result
            filled.append(item)
        return filled

    def _send_required_continuation(self, event: Any) -> None:
        parent = _event_response_id(event) or self._response_id
        if not parent or parent in self._continuations:
            return
        required = _event_field(event, "required_input")
        if required is None:
            response = _event_field(event, "response")
            required = _event_field(response, "required_input")
        input_items = self._fill_required_input(required)
        settings = {k: v for k, v in self._request.items() if k not in {"type", "input", "stream", "previous_response_id"}}
        self._send({"type": "response.create", **settings, "previous_response_id": parent, "input": input_items})
        self._continuations.add(parent)
        self._await_successor = True

    def _is_duplicate(self, event: Any, event_type: str, response_id: str | None) -> bool:
        sequence_number = _event_field(event, "sequence_number")
        if sequence_number is not None:
            # Sequence numbers are monotonic only within one response generation; automatic successors may
            # restart at zero/one on the same WebSocket. Scope the key before deciding whether to drop a frame.
            sequence_scope = response_id or self._response_id or "session"
            sequence_key = f"{sequence_scope}:{sequence_number}"
            if sequence_key in self._seen_sequences:
                return True
            self._seen_sequences.add(sequence_key)
        event_id = _event_field(event, "event_id") or _event_field(event, "id")
        if event_id:
            key = str(event_id)
            if key in self._seen_events:
                return True
            self._seen_events.add(key)
        if event_type.endswith("output_item.done"):
            item_id = _event_item_id(event)
            if item_id and response_id:
                key = (response_id, item_id, event_type)
                if key in self._seen_items:
                    return True
                self._seen_items.add(key)
        return False

    def _event_belongs_to_active_response(self, event: Any, response_id: str | None, event_type: str) -> bool:
        if not response_id or not self._response_id or response_id == self._response_id:
            return True
        return event_type == "response.created" and self._await_successor

    def _handle_created(self, event: Any, response_id: str | None) -> None:
        if not response_id:
            raise AstraProtocolError("Astra response.created omitted response id")
        with self._state_lock:
            if self._response_id is None:
                self._response_id = response_id
                self._assemblers[response_id] = self._new_assembler(response_id)
                self._set_state("ACTIVE")
                return
            if response_id == self._response_id:
                return
            if not self._await_successor:
                return
            self._response_id = response_id
            self._assemblers[response_id] = self._new_assembler(response_id)
            self._await_successor = False
            self._set_state("SUCCESSOR_CREATED")

    def _steer_event(self, event: Any, event_type: str) -> None:
        if event_type == "response.steer.accepted":
            with self._state_lock:
                pending = next((item for item in self._steers.values() if not item.accepted and not item.failed), None)
                if pending is not None:
                    pending.accepted = True
                    _remove_pending(self.agent, pending.text)
                self._set_state("ACCEPTED")
        elif event_type == "response.steer.failed":
            with self._state_lock:
                pending = next((item for item in self._steers.values() if not item.accepted and not item.failed), None)
                if pending is not None:
                    pending.failed = True
                self._await_successor = any(not item.failed and not item.accepted for item in self._steers.values())
                self._set_state("ACTIVE")
        elif event_type == "response.steer.pending":
            self._set_state("PENDING_REQUIRED_INPUT")
            self._send_required_continuation(event)

    def _terminal_reason(self, event: Any) -> str:
        response = _event_field(event, "response")
        details = _event_field(response, "incomplete_details") or _event_field(event, "incomplete_details") or {}
        return str(_event_field(details, "reason", "") or "").strip().lower()

    def _settle_async_executor(self, final: Any) -> None:
        """Use the PR2 persist-before-execute settlement boundary for the final WS response."""
        executor = getattr(self.agent, "_astra_async_executor", None)
        if executor is None:
            return
        if getattr(executor, "has_admitted", False) or getattr(executor, "has_pending", False) or getattr(executor, "failed", False):
            if not executor.finish_stream(
                assistant_content=getattr(final, "output_text", "") or "",
                settled_calls=getattr(final, "output", None),
            ):
                raise RuntimeError("Astra async tool execution did not reach a durable result boundary")
        elif executor.retire_empty():
            self.agent._astra_async_executor = None

    def run(self, request: dict[str, Any], *, on_first_delta: Callable[[], None] | None = None) -> Any:
        from agent.stream_single_writer import claim_stream_writer, stream_writer_is_current

        self._request = dict(request)
        self._request.pop("stream", None)
        self._on_first_delta_callback = on_first_delta
        self.agent._codex_streamed_text_parts = []
        writer_token = claim_stream_writer(self.agent)
        try:
            self._set_state("DISPATCHING")
            try:
                self._socket = self._connect(_ws_url(str(getattr(self.agent, "base_url", ""))), self.agent.api_key, self.timeout)
            except Exception as exc:
                self.last_error = exc
                self._set_state("PRE_DISPATCH_FAILURE")
                raise AstraPreDispatchError("Astra WebSocket connection failed before dispatch") from exc
            initial = {"type": "response.create", **{k: v for k, v in self._request.items() if k != "type"}}
            try:
                self._send(initial)
            except AstraDeliveryUncertainError:
                raise
            while True:
                if self._interrupt.is_set() or getattr(self.agent, "_interrupt_requested", False):
                    raise InterruptedError("Astra WebSocket turn interrupted")
                try:
                    raw = self._socket.recv()
                except Exception as exc:
                    if self._interrupt.is_set() or getattr(self.agent, "_interrupt_requested", False):
                        raise InterruptedError("Astra WebSocket turn interrupted") from exc
                    self.delivery_uncertain = True
                    self.last_error = exc
                    self._set_state("POST_DISPATCH_AMBIGUOUS")
                    raise AstraDeliveryUncertainError("Astra WebSocket response delivery is uncertain") from exc
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    event = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError) as exc:
                    raise AstraProtocolError("Astra WebSocket returned invalid JSON") from exc
                if not isinstance(event, dict):
                    raise AstraProtocolError("Astra WebSocket returned a non-object event")
                event_type = str(event.get("type") or "")
                response_id = _event_response_id(event)
                if self._is_duplicate(event, event_type, response_id):
                    continue
                if event_type == "response.created":
                    self._handle_created(event, response_id)
                    continue
                if event_type.startswith("response.steer."):
                    self._steer_event(event, event_type)
                    continue
                if not self._event_belongs_to_active_response(event, response_id, event_type):
                    continue
                assembler = self._assemblers.get(self._response_id or "")
                if assembler is None:
                    raise AstraProtocolError("Astra event arrived before response.created")
                self.agent._codex_stream_last_event_ts = time.time()
                touch = getattr(self.agent, "_touch_activity", None)
                if callable(touch):
                    touch("receiving Astra WebSocket response")
                if not stream_writer_is_current(self.agent, writer_token):
                    raise TimeoutError("Astra WebSocket stream was superseded")
                # Claim terminal ownership before feeding the event.  A concurrent steer that observes
                # terminal processing has begun must return False rather than dispatching after completion.
                terminal_event = event_type in {"response.completed", "response.incomplete", "response.failed"}
                terminal_waits_for_successor = False
                if terminal_event:
                    # The wait decision and terminal claim are one linearization point. A steer can either
                    # acquire this lock first (and force successor wait) or observe TERMINAL_PROCESSING and
                    # return False; it cannot be admitted against a stale pre-lock snapshot.
                    with self._state_lock:
                        terminal_waits_for_successor = self._await_successor or self._terminal_reason(event) == "steered"
                        if terminal_waits_for_successor:
                            pass
                        elif self._state in {"ACTIVE", "SUCCESSOR_CREATED", "ACCEPTED", "STEER_ADMITTED"}:
                            self._set_state("TERMINAL_PROCESSING")
                terminal = assembler.feed(event)
                if terminal:
                    if terminal_waits_for_successor:
                        continue
                    with self._state_lock:
                        self._set_state("COMPLETED" if event_type == "response.completed" else "EXPLICIT_FAILURE")
                    final = assembler.result()
                    self._settle_async_executor(final)
                    return final
        finally:
            self.close()


def run_astra_websocket_stream(agent: Any, request: dict[str, Any], *, on_first_delta=None, connect=None):
    """Attach one turn-owned session so concurrent Hermes control calls can steer it."""
    session = AstraWebSocketSession(agent, connect=connect)
    previous = getattr(agent, "_astra_websocket_session", None)
    if previous is not None:
        previous.close()
    agent._astra_websocket_session = session
    try:
        return session.run(request, on_first_delta=on_first_delta)
    finally:
        if getattr(agent, "_astra_websocket_session", None) is session:
            agent._astra_websocket_session = None


__all__ = [
    "AstraDeliveryUncertainError", "AstraPreDispatchError", "AstraProtocolError", "AstraWebSocketSession",
    "is_astra_websocket_eligible", "run_astra_websocket_stream",
]
