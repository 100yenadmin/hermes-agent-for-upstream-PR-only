"""Account-scoped Astra discovery without printing or persisting credentials.

Run from the repository root with an isolated HERMES_HOME and a clean environment.
Uses Hermes' existing read-only Codex credential import and catalog parser.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    import httpx
    from evals.astra_subscription.live_guard import LiveBudget

    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    args = parser.parse_args()
    LiveBudget(args.ledger, "account_discovery").install()

    from agent.credential_pool import _codex_principal_identity
    from agent.model_metadata import CODEX_MODELS_CATALOG_URL
    from hermes_cli.auth_codex import _import_codex_cli_tokens
    from hermes_cli.codex_models import _fetch_models_from_api

    tokens = _import_codex_cli_tokens()
    result = {
        "scenario": "account_discovery",
        "provider": "openai-codex",
        "model": "gpt-6-astra",
        "credential_source": "supported_read_only_codex_cli_import",
        "principal_resolved": bool(tokens and _codex_principal_identity(tokens["access_token"])),
        "dispatched_requests": 0,
        "http_status": None,
    }
    if not tokens or not result["principal_resolved"]:
        result["result"] = "ENVIRONMENT_BLOCKED"
        print(json.dumps(result, sort_keys=True))
        return 2

    original_get = httpx.get

    def catalog_get(url, **kwargs):
        if url != CODEX_MODELS_CATALOG_URL or result["dispatched_requests"]:
            raise RuntimeError("Only one official catalog request is permitted")
        result["dispatched_requests"] += 1
        try:
            response = original_get(url, **kwargs)
        except Exception as exc:
            result["transport_error_class"] = type(exc).__name__
            raise
        result["http_status"] = response.status_code
        return response

    httpx.get = catalog_get
    try:
        models = _fetch_models_from_api(tokens["access_token"])
    finally:
        httpx.get = original_get
    result["astra_discovered"] = "gpt-6-astra" in models
    result["visible_model_count"] = len(models)
    result["result"] = (
        "ENVIRONMENT_BLOCKED" if result["http_status"] != 200 else
        "COMPLETE" if result["astra_discovered"] else "EXPECTED_NEGATIVE"
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] != "ENVIRONMENT_BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
