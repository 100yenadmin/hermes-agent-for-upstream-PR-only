"""One-shot auxiliary client backed by the Claude Agent SDK.

LOCAL DIVERGENCE (2026-08-14).

Why this exists
---------------
``_resolve_auto_route`` fails closed when the MAIN provider is the
claude-agent-sdk (see auxiliary_client.py, "Fail-closed subscription lane",
#25267): auto-detection returns ``(None, None, "")`` so auxiliary tasks can
never be silently re-routed onto a METERED provider and break the
subscription billing contract through the side door.

That guard is correct, but it left ``auto`` meaning "no client at all" on the
SDK lane -- verified against a live runtime, ``web_extract``,
``tts_audio_tags`` and ``kanban_decomposer`` all resolved to ``None`` while
only explicitly-pinned channels worked.  The operator's escape hatch was an
explicit pin at ``auxiliary.<task>.provider``, which in practice meant
``claude-cli-live`` -- a pre-SDK shim that spawns and manages its own
persistent ``claude`` process.

This client closes that gap natively: it runs a ONE-SHOT
``claude_agent_sdk.query()`` against the SAME subscription the main lane
already uses.  The child still reports its selected billing lane, so this
adapter applies the same API-key and Extra Usage fail-closed checks as the
persistent SDK session before accepting any result.

Design constraints
------------------
* **Text only.**  Auxiliary tasks (compression, title generation, web
  extraction, ...) summarise text; image/file blocks fail explicitly instead
  of being silently dropped.  ``tools=[]`` removes the built-in Claude Code
  tools (``allowed_tools`` is only a permission allowlist), while
  ``mcp_servers={}`` keeps MCP tools absent.  This also avoids the cost of
  booting MCP servers for a one-line summary.
* **No inherited settings.**  ``setting_sources=[]`` keeps user/project
  CLAUDE.md and settings.json out of an auxiliary prompt, so aux behaviour
  does not drift with the operator's editor config.
* **``permission_mode="dontAsk"``.**  With no tools enabled this is
  effectively moot, but it is the mode proven to work under root -- the
  ``bypassPermissions`` mode maps to ``--dangerously-skip-permissions``,
  which Claude Code refuses to run as root (repaired 2026-08-14 09:48).
* **OpenAI-shaped surface.**  Every aux caller goes through
  ``client.chat.completions.create(...)`` and reads
  ``resp.choices[0].message.content``; the return shape here mirrors
  ``claude_cli_live_client._LiveCompletions.create`` exactly.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from types import SimpleNamespace
from typing import Any

from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TIMEOUT = 600.0
_AUX_SYSTEM_GUARD = (
    "You are performing a non-interactive auxiliary text task for Hermes. "
    "Follow the trusted system instructions, return the requested answer "
    "directly, do not use tools, and do not ask follow-up questions."
)
_UNSUPPORTED_BLOCK_TYPES = {
    "file",
    "image",
    "image_url",
    "input_file",
    "input_image",
}
_UNSUPPORTED_BLOCK_KEYS = {"file", "file_id", "image", "image_url"}


class ClaudeSdkAuxError(RuntimeError):
    """Raised when a one-shot auxiliary SDK query cannot produce text."""


def _render_message_content(content: Any) -> str:
    """Render only the textual portion of an OpenAI-shaped message.

    Auxiliary SDK calls are deliberately text-only.  Image/file blocks are
    skipped rather than serialised into a misleading Python/JSON blob, while
    ordinary structured text and tool results keep their readable content.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        for key in ("text", "content"):
            value = content.get(key)
            if isinstance(value, str):
                return value.strip()
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts)
    return str(content).strip()


def _contains_unsupported_multimodal_content(content: Any) -> bool:
    """Whether an OpenAI-shaped content value contains image/file input."""
    if isinstance(content, list):
        return any(_contains_unsupported_multimodal_content(item) for item in content)
    if not isinstance(content, dict):
        return False
    block_type = str(content.get("type") or "").strip().lower()
    if block_type in _UNSUPPORTED_BLOCK_TYPES:
        return True
    if any(key in content for key in _UNSUPPORTED_BLOCK_KEYS):
        return True
    nested = content.get("content")
    return _contains_unsupported_multimodal_content(nested)


def _messages_to_sdk_inputs(
    messages: list[dict[str, Any]],
) -> tuple[str, str]:
    """Keep trusted system instructions out of the SDK user prompt."""
    system_sections = [_AUX_SYSTEM_GUARD]
    prompt_sections: list[str] = []
    labels = {
        "user": "User",
        "assistant": "Assistant",
        "tool": "Tool result",
    }
    for message in messages:
        if not isinstance(message, dict):
            continue
        rendered = _render_message_content(message.get("content"))
        if not rendered:
            continue
        role = str(message.get("role") or "context").strip().lower()
        if role == "system":
            system_sections.append(rendered)
            continue
        label = labels.get(role, "Context")
        prompt_sections.append(f"{label}:\n{rendered}")
    prompt_sections.append("Complete the trusted auxiliary task.")
    return "\n\n".join(prompt_sections), "\n\n".join(system_sections)


def _aux_billing_guard_error(message: Any, *, allow_metered: bool) -> str | None:
    """Return a fail-closed billing error for one SDK stream message."""
    if allow_metered:
        return None

    name = type(message).__name__
    if name == "SystemMessage" and getattr(message, "subtype", "") == "init":
        data = getattr(message, "data", None)
        if not isinstance(data, dict):
            return None
        source = data.get("apiKeySource", data.get("api_key_source"))
        if source is None:
            return None
        if str(source or "none").strip().lower() != "none":
            return (
                "claude-agent-sdk auxiliary billing guard: the CLI reported "
                "a metered API-key source; remove it or explicitly enable "
                "agent.claude_agent_sdk.allow_metered_key"
            )
        return None

    if name != "RateLimitEvent":
        return None
    info = getattr(message, "rate_limit_info", None)
    if info is None:
        return None
    raw = getattr(info, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    is_using_overage = raw.get("isUsingOverage")
    overage_status = getattr(info, "overage_status", None)
    if overage_status is None:
        overage_status = raw.get("overageStatus")
    rate_limit_type = getattr(info, "rate_limit_type", None)
    if rate_limit_type is None:
        rate_limit_type = raw.get("rateLimitType")

    if is_using_overage is True or (
        str(rate_limit_type or "").lower() == "overage"
        and is_using_overage is not False
    ):
        return (
            "claude-agent-sdk auxiliary billing guard: metered subscription "
            "Extra Usage is active; disable Extra Usage or explicitly enable "
            "agent.claude_agent_sdk.allow_metered_key"
        )
    if str(overage_status or "").lower() in {"allowed", "allowed_warning"}:
        return (
            "claude-agent-sdk auxiliary billing guard: subscription Extra "
            "Usage is enabled and could become metered; disable Extra Usage "
            "or explicitly enable agent.claude_agent_sdk.allow_metered_key"
        )
    return None


def _run_coro_blocking(coro, timeout: float):
    """Run ``coro`` to completion from sync code, loop-safe.

    Auxiliary clients are called from both sync paths (compression) and from
    inside a running event loop (gateway request handlers).  ``asyncio.run``
    raises if a loop is already running in this thread, so in that case the
    coroutine is handed to a dedicated worker thread with its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(asyncio.wait_for(coro, timeout=timeout))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            lambda: asyncio.run(asyncio.wait_for(coro, timeout=timeout))
        ).result(timeout=timeout + 30)


async def _collect_text(
    prompt: str,
    *,
    model: str,
    system_prompt: str = _AUX_SYSTEM_GUARD,
) -> tuple[str, Any, str]:
    """Run a one-shot SDK query and return (text, usage, stop_reason)."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    # Import lazily to keep this lightweight facade importable without the
    # optional SDK extra.  The same override builder as the persistent lane is
    # load-bearing here: query() also spawns a CLI inheriting the parent env.
    from agent.transports.claude_agent_sdk_session import (
        _provider_flag,
        _sdk_env_overrides,
    )

    allow_metered = _provider_flag("allow_metered_key")

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        tools=[],
        allowed_tools=[],
        mcp_servers={},
        setting_sources=[],
        permission_mode="dontAsk",
        max_turns=1,
        env=_sdk_env_overrides(metered_allowed=allow_metered),
    )

    parts: list[str] = []
    usage: Any = None
    stop_reason = "stop"
    terminal_error: str | None = None
    saw_result = False

    async for message in query(prompt=prompt, options=options):
        billing_error = _aux_billing_guard_error(
            message,
            allow_metered=allow_metered,
        )
        if billing_error is not None:
            raise ClaudeSdkAuxError(billing_error)
        if isinstance(message, AssistantMessage):
            for block in getattr(message, "content", None) or []:
                # ThinkingBlock and friends are deliberately skipped -- aux
                # callers want the answer text, not the reasoning trace.
                if isinstance(block, TextBlock):
                    text = getattr(block, "text", "") or ""
                    if text:
                        parts.append(text)
        elif isinstance(message, ResultMessage):
            saw_result = True
            usage = getattr(message, "usage", None)
            subtype = str(getattr(message, "subtype", None) or "")
            stop_reason = getattr(message, "stop_reason", None) or "stop"
            if getattr(message, "is_error", False) or subtype not in ("", "success"):
                errors = getattr(message, "errors", None) or []
                detail = (
                    "; ".join(str(error) for error in errors)
                    or str(getattr(message, "result", None) or subtype or "unknown error")
                )
                terminal_error = redact_sensitive_text(detail, force=True)

    if terminal_error is not None:
        # Fail closed even when partial assistant text preceded the terminal
        # error.  Returning that text as a successful compression/title can
        # silently persist a truncated result.
        raise ClaudeSdkAuxError(
            f"claude-agent-sdk auxiliary query failed: {terminal_error}"
        )
    if not saw_result:
        raise ClaudeSdkAuxError(
            "claude-agent-sdk auxiliary query ended without a terminal result"
        )

    return "".join(parts), usage, stop_reason


class _AuxCompletions:
    def __init__(self, owner: "ClaudeSdkAuxClient") -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> SimpleNamespace:
        model = str(kwargs.get("model") or self._owner.default_model or DEFAULT_MODEL)
        messages = kwargs.get("messages") or []
        timeout = float(kwargs.get("timeout") or self._owner.timeout)

        if kwargs.get("stream"):
            # Auxiliary callers never need token streaming; refusing here is
            # clearer than silently returning a non-iterable.
            raise ClaudeSdkAuxError(
                "claude-agent-sdk auxiliary client does not support stream=True"
            )

        # Validate the CALLER'S messages, not the assembled prompt: the SDK
        # inputs always contain a trusted guard, so checking those would let an
        # empty message list burn a live subscription call on boilerplate.
        if not any(
            str((m or {}).get("content") or "").strip()
            for m in messages
            if isinstance(m, dict)
        ):
            raise ClaudeSdkAuxError("refusing to send an empty auxiliary prompt")

        if any(
            _contains_unsupported_multimodal_content((message or {}).get("content"))
            for message in messages
            if isinstance(message, dict)
        ):
            raise ClaudeSdkAuxError(
                "claude-agent-sdk auxiliary client is text-only and refuses "
                "image or file content"
            )

        prompt, system_prompt = _messages_to_sdk_inputs(messages)

        try:
            text, usage, stop_reason = _run_coro_blocking(
                _collect_text(
                    prompt,
                    model=model,
                    system_prompt=system_prompt,
                ),
                timeout,
            )
        except ClaudeSdkAuxError:
            raise
        except Exception as exc:
            safe_error = redact_sensitive_text(str(exc), force=True)
            raise ClaudeSdkAuxError(
                f"claude-agent-sdk auxiliary query failed: {safe_error}"
            ) from None

        if not text.strip():
            raise ClaudeSdkAuxError(
                f"claude-agent-sdk auxiliary query returned no text "
                f"(model={model}, stop_reason={stop_reason})"
            )

        message = SimpleNamespace(content=text, tool_calls=None, role="assistant")
        choice = SimpleNamespace(message=message, finish_reason="stop", index=0)
        return SimpleNamespace(
            id=f"claude-agent-sdk-aux-{int(time.time())}",
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[choice],
            usage=usage,
            provider_data={"claude_agent_sdk_aux": {"stop_reason": stop_reason}},
        )


class _AuxChat:
    def __init__(self, owner: "ClaudeSdkAuxClient") -> None:
        self.completions = _AuxCompletions(owner)


class _AsyncAuxCompletions:
    """Awaitable adapter for the one-shot synchronous SDK facade."""

    def __init__(self, sync_adapter: _AuxCompletions) -> None:
        self._sync = sync_adapter

    async def create(self, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._sync.create, **kwargs)


class _AsyncAuxChat:
    def __init__(self, sync_adapter: _AuxCompletions) -> None:
        self.completions = _AsyncAuxCompletions(sync_adapter)


class AsyncClaudeSdkAuxClient:
    """Async-compatible facade for subscription-safe one-shot SDK aux calls."""

    def __init__(self, sync_wrapper: "ClaudeSdkAuxClient") -> None:
        self.chat = _AsyncAuxChat(sync_wrapper.chat.completions)
        self.api_key = sync_wrapper.api_key
        self.base_url = sync_wrapper.base_url
        self.default_model = sync_wrapper.default_model

    async def close(self) -> None:  # pragma: no cover - no persistent client
        return None


class ClaudeSdkAuxClient:
    """OpenAI-shaped one-shot client over ``claude_agent_sdk.query()``."""

    def __init__(
        self,
        *,
        default_model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.default_model = default_model or DEFAULT_MODEL
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        # Parity with the other local facades: aux routing code reads these.
        self.base_url = ""
        self.api_key = "claude-subscription-oauth"
        self.chat = _AuxChat(self)

    def close(self) -> None:  # pragma: no cover - nothing persistent to release
        """No persistent process: each call is an independent one-shot query."""
        return None
