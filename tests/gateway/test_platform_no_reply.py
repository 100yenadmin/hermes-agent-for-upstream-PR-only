"""No-reply decisions through real adapter, persistence and inbound ownership seams."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.run_inbound import GatewayInboundMixin
from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
from hermes_cli.observability import relay_shared_metrics as rsm
from hermes_cli.observability import shared_metrics_gateway as smg


@pytest.fixture
def rows(monkeypatch):
    smg.drain()
    smg._reply_clocks.clear()
    got = []
    monkeypatch.setattr(rsm, "enabled", lambda: True)
    monkeypatch.setattr(rsm, "record_process_mark", lambda mark, data: got.append((mark, dict(data))))
    yield got
    smg.drain()
    smg._reply_clocks.clear()


class Adapter(BasePlatformAdapter):
    def __init__(self, *, success=True):
        super().__init__(PlatformConfig(enabled=True, extra={"typing_indicator": False}), Platform.TELEGRAM)
        self.success = success

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=self.success, message_id="out", error_kind="forbidden")


def event(adapter):
    return MessageEvent(text="question", source=adapter.build_source(chat_id="chat", user_id="user"))


def start(ev):
    ev._reply_clock_token = smg.start_reply_clock(ev.source)
    return ev._reply_clock_token


def no_replies(rows):
    smg.drain()
    return [dims for metric, dims in rows if metric == "hermes.gateway.no_reply"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case,reason", [("empty", "empty"), ("failed", "delivery_failed"),
                                       ("stale", "interrupted"), ("error", "error"), ("dropped", "dropped")])
async def test_completed_adapter_turn_without_delivery_is_counted_once(rows, caplog, monkeypatch, case, reason):
    adapter = Adapter(success=case != "failed")
    ev = event(adapter)

    async def handler(incoming):
        start(incoming)
        if case == "error":
            raise RuntimeError("turn failed")
        if case == "stale":
            adapter._active_sessions["session"].set()
            adapter._pending_messages["session"] = event(adapter)
        return None if case == "empty" else "answer"

    if case == "dropped":
        monkeypatch.setattr(adapter, "_extract_response_content", AsyncMock(return_value=SimpleNamespace(
            text_content="", images=[], media_files=[], local_files=[], pre_extract="answer",
            force_document_attachments=False)))
    monkeypatch.setattr(adapter, "_spawn_drain_task", MagicMock())
    adapter.set_message_handler(handler)
    with caplog.at_level(logging.INFO):
        await adapter._process_message_background(ev, "session")
    assert no_replies(rows) == [{"platform": "telegram", "reason": reason}]
    logs = [r for r in caplog.records if "turn ended without a reply" in r.message]
    assert len(logs) == 1
    assert logs[0].levelname == ("INFO" if reason == "interrupted" else "WARNING")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "media", "tts", "stream"])
async def test_reply_delivery_never_counts_no_reply(rows, monkeypatch, kind):
    adapter = Adapter()
    ev = event(adapter)

    async def handler(incoming):
        start(incoming)
        if kind == "stream":
            smg.stop_reply_clock(adapter, incoming.source.chat_id)
            return None
        return "answer"

    if kind == "media":
        monkeypatch.setattr(adapter, "_extract_response_content", AsyncMock(return_value=SimpleNamespace(
            text_content="", images=["https://example.invalid/image.png"], media_files=[], local_files=[],
            pre_extract="answer", force_document_attachments=False)))

        async def images(ev, images, metadata, delay, record_delivery):
            record_delivery(SendResult(success=True))

        monkeypatch.setattr(adapter, "_send_image_batch", images)
    if kind == "tts":
        monkeypatch.setattr(adapter, "_wants_auto_tts", lambda *_: True)
        monkeypatch.setattr(adapter, "_synthesize_auto_tts", AsyncMock(return_value=(["nonexistent-audio.ogg"], None)))

        async def play(*args):
            args[-1](SendResult(success=True))
            return True

        monkeypatch.setattr(adapter, "_play_tts_file", play)
    adapter.set_message_handler(handler)
    await adapter._process_message_background(ev, "session")
    assert no_replies(rows) == []
    assert not smg._reply_clocks
    assert len([m for m, _ in rows if m == "hermes.gateway.reply_latency"]) == (kind in {"text", "stream"})


@pytest.mark.asyncio
async def test_busy_followup_cannot_claim_running_turn_clock(rows):
    adapter = Adapter()
    owner, followup = event(adapter), event(adapter)
    token = start(owner)
    adapter.set_message_handler(AsyncMock(return_value=None))
    await adapter._process_message_background(followup, "session")
    assert no_replies(rows) == []
    assert smg._reply_clocks[("telegram", "chat")][0] == token


@pytest.mark.asyncio
@pytest.mark.parametrize("rescue", [False, True], ids=["authority-restored", "orphan-rescue"])
async def test_fifo_deferred_or_rescued_event_never_claims_another_event_clock(rows, rescue):
    adapter = Adapter()
    incoming, claimed = event(adapter), event(adapter)
    runner = SimpleNamespace(_delivery_adapter_for=lambda _: adapter,
                             _rescue_orphaned_overflow=lambda *_: claimed, _enqueue_fifo=MagicMock())

    async def handler(ev):
        if rescue:
            actual, _, _ = GatewayInboundMixin._hm_rescue_orphaned_fifo(runner, ev, ev.source, False, "session")
        else:
            # The durable authority reconstructs the claimed event from its committed snapshot.
            actual = claimed
        start(actual)

    adapter.set_message_handler(handler)
    await adapter._process_message_background(incoming, "session")
    assert not hasattr(incoming, "_reply_clock_token")
    assert no_replies(rows) == []
    assert smg._reply_clocks[("telegram", "chat")][0] == claimed._reply_clock_token
    if rescue:
        runner._enqueue_fifo.assert_called_once_with("session", incoming, adapter)


@pytest.mark.asyncio
async def test_unfinished_turn_is_not_counted(rows):
    adapter = Adapter()
    ev, entered, release = event(adapter), asyncio.Event(), asyncio.Event()

    async def handler(incoming):
        start(incoming)
        entered.set()
        await release.wait()

    adapter.set_message_handler(handler)
    task = asyncio.create_task(adapter._process_message_background(ev, "session"))
    try:
        await entered.wait()
        assert no_replies(rows) == []
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("silence,reason", [(True, "silent"), (False, "interrupted")])
async def test_persistence_silence_and_interrupted_turns_count_once_before_adapter_end(rows, silence, reason):
    adapter = Adapter()
    ev = event(adapter)
    runner = SimpleNamespace(_delivery_adapter_for=lambda _: adapter, _should_send_voice_reply=lambda *_a, **_k: False)

    async def handler(incoming):
        start(incoming)
        return await GatewayTurnPersistenceMixin._hmwa_deliver_turn_response(
            runner, incoming, incoming.source, SimpleNamespace(session_id="sid"), "session", 1,
            {"interrupted": not silence, "api_calls": 1}, [], "[SILENT]" if silence else "", "", silence)

    adapter.set_message_handler(handler)
    await adapter._process_message_background(ev, "session")
    assert no_replies(rows) == [{"platform": "telegram", "reason": reason}]
