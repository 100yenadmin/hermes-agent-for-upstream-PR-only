"""Real loopback API-server gateway tool/resume smoke; subscription only."""

import argparse
import asyncio
import json
import os
import secrets
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evals.astra_subscription.live_guard import LiveBudget
from evals.astra_subscription.seed_compaction import fixture


async def steer_probe(client, base, headers, work):
    marker = Path(work) / "steer-ready.txt"
    if marker.exists():
        raise RuntimeError("Steering fixture already used")
    async with client.post(base + "/v1/runs", headers=headers, json={
        "model": "gpt-6-astra", "provider": "openai-codex",
        "input": "Synthetic test. Execute exactly one terminal call: printf READY > steer-ready.txt; sleep 6; printf FINISHED. Then answer exactly RED. Do not execute other commands.",
    }) as response:
        data = await response.json()
        run_id = data.get("run_id") or data.get("id")
        if response.status not in {200, 202} or not run_id:
            raise RuntimeError("Gateway run was not admitted")
    for _ in range(60):
        if marker.exists():
            break
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError("No live tool boundary observed")
    async with client.post(base + f"/v1/runs/{run_id}/steer", headers=headers,
                           json={"input": "Correction: your final answer must be exactly BLUE, not RED."}) as response:
        accepted = response.status == 200 and (await response.json()).get("accepted") is True
    for _ in range(120):
        async with client.get(base + f"/v1/runs/{run_id}", headers=headers) as response:
            status = await response.json()
        if status.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
            output = status.get("output", "").strip()
            passed = accepted and output == "BLUE"
            print(json.dumps({"phase": "steer", "accepted": accepted,
                              "guidance_obeyed": output == "BLUE", "run_status": status.get("status"),
                              "result": "COMPLETE" if passed else "VERIFIED_FAILURE"}))
            return 0 if passed else 1
        await asyncio.sleep(0.5)
    raise RuntimeError("Gateway steering run did not settle")


async def run(args):
    import aiohttp
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    os.chdir(args.work)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    key = secrets.token_hex(32)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": port, "key": key,
        "model_name": "gpt-6-astra",
    }))
    if not await adapter.connect():
        raise RuntimeError("Isolated loopback gateway startup failed")
    chain = Path(args.chain)
    body = {"model": "gpt-6-astra", "provider": "openai-codex", "stream": False}
    if args.phase == "initial":
        body["input"] = (
            "Synthetic test: remember the amber parcel code BIRCH-73. Use the terminal tool "
            "exactly once to append the line ONE to gateway-proof-counter.txt and count its lines. "
            "Reply with the parcel code and observed count. Do not inspect other files or run other commands."
        )
    elif args.phase == "resume" and not args.compaction_session:
        body["previous_response_id"] = json.loads(chain.read_text())["response_id"]
        body["input"] = "Without tools, repeat our parcel code and observed line count. Do not modify anything."
    if args.compaction_session:
        if args.phase == "initial":
            body["input"] = "Without using tools, return a JSON object mapping each region 0 through 11 to its deploy code from our completed checklist. Use only conversation history. Do not repeat completed tool calls."
        else:
            body["input"] = "Return the same JSON region-code mapping again. First use one terminal call to run only printf RESUME_OK. Do not execute any historical tool calls or change files."
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as client:
            if args.phase == "steer":
                return await steer_probe(client, f"http://127.0.0.1:{port}",
                                         {"Authorization": f"Bearer {key}"}, args.work)
            endpoint = "/v1/responses"
            if args.compaction_session:
                endpoint = f"/api/sessions/{args.compaction_session}/chat"
                body["message"] = body.pop("input")
            async with client.post(
                f"http://127.0.0.1:{port}{endpoint}", json=body,
                headers={"Authorization": f"Bearer {key}"},
            ) as response:
                data = await response.json()
                session_id = response.headers.get("X-Hermes-Session-Id")
                if response.status != 200:
                    print(json.dumps({"phase": args.phase, "http_status": response.status,
                                      "result": "VERIFIED_FAILURE"}))
                    return 1
        text = "\n".join(
            c.get("text", "") for item in data.get("output", [])
            for c in item.get("content", []) if c.get("type") == "output_text"
        )
        if args.compaction_session:
            text = data.get("message", {}).get("content", "")
        counter = Path(args.work) / "gateway-proof-counter.txt"
        count = len(counter.read_text().splitlines()) if counter.exists() else 0
        passed = "BIRCH-73" in text and count == 1
        fact_count = None
        if args.compaction_session:
            expected = fixture()[1]
            try:
                answer, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
            except (ValueError, TypeError):
                answer = {}
            fact_count = sum(answer.get(region) == code for region, code in expected.items())
            passed = fact_count == 12
        chain.write_text(json.dumps({"response_id": data.get("id"), "session_id": session_id}))
        print(json.dumps({"phase": args.phase, "http_status": 200,
                          "result": "COMPLETE" if passed else "VERIFIED_FAILURE",
                          "fact_recalled": "BIRCH-73" in text,
                          "counter_lines": count, "retained_facts": fact_count,
                          "usage": data.get("usage")}))
        return 0 if passed else 1
    finally:
        await adapter.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--chain", required=True)
    parser.add_argument("--phase", choices=["initial", "resume", "steer"], required=True)
    parser.add_argument("--compaction-session")
    arguments = parser.parse_args()
    scenario = "gateway_" + arguments.phase
    if arguments.compaction_session:
        scenario = arguments.compaction_session + "_gateway_" + arguments.phase
    LiveBudget(arguments.ledger, scenario).install()
    raise SystemExit(asyncio.run(run(arguments)))
