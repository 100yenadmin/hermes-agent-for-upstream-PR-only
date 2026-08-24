"""Regression tests for claude-agent-sdk /model picker visibility (#65982).

Two symptoms, one root — the picker's authenticated-provider detection for a
self-authenticating runtime (``oauth_external``) only sees
``CLAUDE_CODE_OAUTH_TOKEN`` in the environment; a macOS-Keychain-only
``claude`` login is invisible to it:

- Token in env: the row appeared but carried a 1-model catalog, because the
  unified-pathway curated fallbacks in ``list_authenticated_providers()``
  looked up ``curated["claude-agent-sdk"]`` (which does not exist) without
  the ``_PROVIDER_CATALOG_DELEGATES`` (claude-agent-sdk -> anthropic) mapping.
- No token (Keychain-only login, lane serving turns fine): the row vanished
  from the interactive TUI picker entirely, because the current-provider
  fallback in ``build_models_payload()`` ran only under ``explicit_only``,
  and the TUI call site passes neither ``explicit_only`` nor
  ``include_unconfigured``.

Reported with repro and patches by 5tevebaker on PR #65982.
"""

import os
from unittest.mock import patch

from hermes_cli.model_switch import list_authenticated_providers
from hermes_cli.models import _PROVIDER_MODELS

def test_authenticated_row_serves_the_delegate_catalog():
    """Token in env -> the row carries anthropic's full curated catalog."""
    with (
        patch("agent.models_dev.fetch_models_dev", return_value={}),
        patch("hermes_cli.models.cached_provider_model_ids", return_value=[]),
        patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-dummy"}),
    ):
        rows = list_authenticated_providers(current_provider="claude-agent-sdk")

    row = next((r for r in rows if r["slug"] == "claude-agent-sdk"), None)
    assert row is not None
    curated = list(_PROVIDER_MODELS["anthropic"])
    assert len(curated) > 1  # sanity: the delegate catalog is a real list
    assert row["total_models"] == len(curated)
    assert set(row["models"]).issubset(set(curated))
