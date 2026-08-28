"""First-party Hermes observability integrations."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def observe_lifecycle(hook_name: str, **kwargs: Any) -> None:
    """Dispatch a Hermes lifecycle event to built-in observability features."""
    from . import relay_shared_metrics

    _safe_observe(relay_shared_metrics.observe_lifecycle, hook_name, kwargs)


def handles_hook(hook_name: str) -> bool:
    """Return whether any built-in observability feature handles a hook."""
    from . import relay_shared_metrics

    return relay_shared_metrics.handles_hook(hook_name)


def _safe_observe(callback: Any, hook_name: str, kwargs: dict[str, Any]) -> None:
    try:
        callback(hook_name, **kwargs)
    except Exception:
        from hermes_cli.lifecycle import current_observer_failure_log

        fixed_log = current_observer_failure_log()
        if fixed_log is not None:
            logger.debug("%s", fixed_log)
        else:
            logger.warning(
                "Built-in observability hook failed: %s", hook_name, exc_info=True
            )
