"""hermes-ws-recovery-v1 loopback fault scenarios."""

import threading
import time
import types

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.ws import handle_ws


class _WorkerState:
    def __init__(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


def _rpc(ws, request_id: str, method: str, params: dict) -> dict:
    ws.send_json(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
    )
    return ws.receive_json()


def test_disconnect_mid_turn_reconnect_recovers_one_persisted_final(
    monkeypatch, tmp_path
):
    """A replacement socket recovers backend truth without another prompt."""
    db = SessionDB(tmp_path / "state.db")
    stored_id = "20260101_010101_a1b2c3"
    runtime_id = "runtime1"
    prompt = "return the persisted result"
    final = "the persisted result"
    worker = _WorkerState()

    db.create_session(stored_id, source="desktop")
    db.append_message(stored_id, role="user", content=prompt)

    session = {
        "agent": types.SimpleNamespace(model="loopback-model"),
        "cols": 80,
        "created_at": time.time(),
        "cwd": str(tmp_path),
        "display_history_prefix": [],
        "history": [],
        "history_lock": threading.Lock(),
        "inflight_turn": {
            "assistant": "",
            "status": "streaming",
            "user": prompt,
        },
        "last_active": time.time(),
        "running": True,
        "session_key": stored_id,
        "source": "desktop",
        "_run_thread": worker,
    }

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent: {"model": "loopback-model", "desktop_contract": 1},
    )
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    server._sessions.clear()
    server._live_transports.clear()
    server._sessions[runtime_id] = session

    app = FastAPI()

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await handle_ws(ws)

    try:
        with TestClient(app) as client:
            # First renderer owns the live turn, then disconnects before the
            # terminal message.complete frame could be displayed.
            with client.websocket_connect("/ws") as first:
                ready = first.receive_json()
                assert ready["params"]["payload"]["heartbeat"] is True
                activated = _rpc(
                    first,
                    "activate-first",
                    "session.activate",
                    {"session_id": runtime_id},
                )
                assert activated["result"]["running"] is True

            # The worker completes and persists while no renderer transport is
            # attached. Its old terminal event is intentionally not replayed.
            db.append_message(
                stored_id,
                role="assistant",
                content=final,
                finish_reason="stop",
            )
            worker.alive = False

            # A replacement socket opens without another prompt. Activation is
            # the recovery contract: authoritative rows plus repaired idle state.
            with client.websocket_connect("/ws") as replacement:
                replacement.receive_json()  # gateway.ready
                recovered = _rpc(
                    replacement,
                    "activate-replacement",
                    "session.activate",
                    {"session_id": runtime_id},
                )["result"]

        roles = [message["role"] for message in recovered["messages"]]
        assert roles == ["user", "assistant"]
        assert [message["text"] for message in recovered["messages"]] == [
            prompt,
            final,
        ]
        assert recovered["running"] is False
        assert recovered["status"] == "idle"
        assert "inflight" not in recovered
    finally:
        server._sessions.clear()
        server._live_transports.clear()
        db.close()
