"""Optional parallel presentation of a queued approval through a plugin transport."""

import asyncio
import inspect
import logging

from agent.memory_provider import ctx_bound, spawn_context_thread
from tools import approval_context as _ctx

logger = logging.getLogger("tools.approval")


async def _present(present, request):
    """Keep async callback execution inside the same scope as sync callbacks."""
    value = present(request)
    return await value if inspect.isawaitable(value) else value


def _run_mirror(session_key, entry, registered, request):
    from hermes_cli.approval_transport import invoke_approval_transport
    from tools.approval import resolve_gateway_approval

    try:
        # invoke_approval_transport starts another worker: bind that hop too, including the
        # coroutine body (binding only the call that creates a coroutine would lose its scope).
        present = ctx_bound(lambda req: asyncio.run(_present(registered.present, req)))
        result = invoke_approval_transport(
            present, request, timeout_seconds=request.timeout_seconds,
            is_interrupted=entry.event.is_set,
        )
        if result.failure is not None:
            logger.debug("Mirrored approval transport did not answer (%s)", result.failure)
        elif result.choice in request.allowed_choices:
            resolve_gateway_approval(session_key, result.choice, request_id=request.request_id)
    except Exception:  # health: allow BLE001 -- plugin exceptions may contain secrets; never log their tracebacks
        # Plugin exceptions may contain secrets; failures never answer for the human.
        logger.debug("Mirrored approval transport failed")


def start_mirror(session_key, entry):
    """Start only for a published leader; all answers use the queue's atomic resolver."""
    if _ctx._get_approval_transport_mode() != "mirror" or entry.event.is_set():
        return
    try:
        from agent.redact import redact_sensitive_text
        from hermes_cli.approval_transport import ApprovalRequest
        from tools.approval_prompt import get_plugin_manager

        name, _ = _ctx._get_approval_transport_config()
        if name == "builtin":
            return
        registered = get_plugin_manager().get_approval_transport(name)
        if registered is None:
            return
        data = entry.data
        key = data.get("pattern_key", "")
        request = ApprovalRequest.create(
            command=redact_sensitive_text(data.get("command", ""), force=True),
            description=redact_sensitive_text(data.get("description", ""), force=True),
            pattern_key=key, pattern_keys=tuple(data.get("pattern_keys", [key])),
            session_key=session_key, surface=entry.surface,
            allow_session=data.get("allow_session", True),
            allow_permanent=data.get("allow_permanent", True),
            timeout_seconds=_ctx._get_approval_timeout(), request_id=data["request_id"],
        )
        spawn_context_thread(_run_mirror, name=f"approval-mirror-{request.request_id[:8]}",
                             args=(session_key, entry, registered, request)).start()
    except Exception:  # health: allow BLE001 -- discovery/redaction failures must leave the built-in prompt live without logging secrets
        logger.debug("Could not start mirrored approval transport")
