"""Seed completed synthetic history through the real SessionDB API.

Uses the existing compaction eval fixture, never real transcripts. Native mode
keeps its local fallback at 32K so the requested 16K provider trigger gets first
opportunity; local mode triggers at 16K. This threshold distinction is reported.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.model_metadata import estimate_messages_tokens_rough
from evals.compaction.fixtures import synthetic_transcript
from hermes_state import SessionDB


def fixture():
    messages = synthetic_transcript(n_turns=120, seed=7)[1:]
    expected = re.findall(r"region (\d+) is (Z\d+)", json.dumps(messages))
    low, high = 1, 400
    while low < high:
        repeats = (low + high) // 2
        for message in messages:
            if message["role"] == "tool":
                message["content"] = "step output " * repeats
        if estimate_messages_tokens_rough(messages) < 24000:
            low = repeats + 1
        else:
            high = repeats
    for message in messages:
        if message["role"] == "tool":
            message["content"] = "step output " * low
    return messages, dict(expected)


def main():
    import yaml
    from hermes_constants import get_hermes_home

    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--arm", choices=["local", "native"], required=True)
    args = parser.parse_args()
    home = get_hermes_home()
    if not home.name.startswith("astra-subscription-runtime."):
        raise RuntimeError("Refusing to seed a non-evaluation Hermes home")
    messages, facts = fixture()
    config_path = home / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["compression"] = {
        "enabled": True, "codex_responses_native": args.arm == "native",
        "codex_responses_compact_threshold": 16000,
        "threshold_tokens": 32768 if args.arm == "native" else 16000,
        "codex_gpt55_autoraise": False,
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    db = SessionDB()
    if db.get_session(args.session):
        raise RuntimeError("Refusing to overwrite an existing evaluation session")
    db.create_session(args.session, source="cli", model="gpt-6-astra",
                      model_config={"provider": "openai-codex"})
    db.append_messages_batch(args.session, messages)
    replay = db.get_messages_as_conversation(args.session)
    if len(replay) != len(messages):
        raise RuntimeError("Synthetic history did not round-trip")
    db.close()
    print(json.dumps({"session": args.session, "arm": args.arm,
                      "estimated_history_tokens": estimate_messages_tokens_rough(messages),
                      "rows": len(messages), "facts": facts,
                      "configured_local_threshold": config["compression"]["threshold_tokens"],
                      "configured_native_threshold": 16000}))


if __name__ == "__main__":
    main()
