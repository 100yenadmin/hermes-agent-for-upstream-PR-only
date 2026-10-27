"""Turn-level failure evidence shared by cron execution and receipt recovery."""
from typing import Optional

CRON_FAILURE_MARKER = "[CRON_FAILURE]"


def _cron_failure_marker_error(text: str) -> Optional[str]:
    """Only the exact, standalone first line declares failure; keep the rest as evidence."""
    lines = (text or "").splitlines()
    if not lines or lines[0].rstrip() != CRON_FAILURE_MARKER:
        return None
    evidence = "\n".join(lines[1:]).strip()
    return evidence or "Cron agent reported failure."


def refused_turn_error(result: dict, final_response: str) -> Optional[str]:
    """A completed turn that dispatched nothing cannot claim its attempted work succeeded."""
    if _cron_failure_marker_error(final_response) is not None:
        return None
    attempted, executed = result.get("tool_calls_attempted"), result.get("tool_calls_executed")
    if attempted is None or executed is None or attempted <= 0 or executed > 0:
        return None
    names = ", ".join(result.get("tool_calls_refused_names") or [])
    return f"Every tool call this run attempted ({attempted}: {names}) was refused before it ran; no work was done."
