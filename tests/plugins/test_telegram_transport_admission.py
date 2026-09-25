"""Request-write admission tests with synthetic socket I/O only.

The real HTTPX/httpcore request path runs above the fake asyncio stream.  This
keeps the admission/write boundary under test without contacting Telegram.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
import threading

import httpx
import pytest

from plugins.platforms.telegram import transport_admission as wire


class AdmissionSource:
    def __init__(self) -> None:
        self.active = True
        self.lock = threading.RLock()
        self.registration = SimpleNamespace(lock=threading.RLock())

    def admitted(self) -> bool:
        return self.active


class SocketWriter:
    def __init__(self, pause: str | None = None, *, respond: bool = True) -> None:
        self.reader = asyncio.StreamReader()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.pause = pause
        self.respond_enabled = respond
        self.writes: list[bytes] = []
        self.closed = False
        self.connecting = True
        self.buffer = b""
        self.responded = False
        self.socket_options: list[tuple] = []
        self.transport = SimpleNamespace(abort=self.close)

    async def drain(self) -> None:
        if self.pause == "drain" and not self.writes:
            self.entered.set()
            await self.release.wait()

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        self.buffer += data
        if self.respond_enabled and b"\r\n\r\n" in self.buffer and not self.responded:
            headers, body = self.buffer.split(b"\r\n\r\n", 1)
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            if len(body) >= length:
                self.responded = True
                self.reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    async def start_tls(self, context, *, server_hostname, ssl_handshake_timeout) -> None:
        self.connecting = False
        if self.pause == "tls":
            self.entered.set()
            await self.release.wait()

    def get_extra_info(self, name: str):
        if name == "socket":
            return SimpleNamespace(
                fileno=lambda: -1 if self.closed else 1,
                setsockopt=lambda *args: self.socket_options.append(args),
            )
        return None

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@contextmanager
def admitted_operation(source: AdmissionSource):
    admission = wire.OperationAdmission(source)
    token = wire.operation_admission.set(admission)
    try:
        yield admission
    finally:
        wire.operation_admission.reset(token)


async def _request_through_writer(
    monkeypatch,
    writer: SocketWriter,
    *,
    url: str = "http://api.telegram.org/bot123:synthetic/getMe",
    content: bytes | None = None,
):
    async def connect(*args, **kwargs):
        if writer.pause == "connect":
            writer.entered.set()
            await writer.release.wait()
        return writer.reader, writer

    monkeypatch.setattr(wire.asyncio, "open_connection", connect)
    async with httpx.AsyncClient(
        transport=wire.AdmissionHTTPTransport(), timeout=2
    ) as client:
        if content is None:
            return await client.get(url)
        return await client.post(url, content=content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pause", "url"),
    [
        ("connect", "http://api.telegram.org/bot123:synthetic/getMe"),
        ("tls", "https://api.telegram.org/bot123:synthetic/getMe"),
        ("drain", "http://api.telegram.org/bot123:synthetic/getMe"),
    ],
)
async def test_revocation_before_request_write_is_definitely_unsent(
    monkeypatch, pause, url
):
    source = AdmissionSource()
    writer = SocketWriter(pause)
    with admitted_operation(source) as admission:
        task = asyncio.create_task(
            _request_through_writer(monkeypatch, writer, url=url)
        )
        await asyncio.wait_for(writer.entered.wait(), 2)
        with source.registration.lock, source.lock:
            source.active = False
        writer.release.set()
        with pytest.raises(wire.AdmissionRevoked):
            await task

    assert writer.writes == []
    assert admission.dispatched is False


def test_check_and_write_share_the_revocation_locks():
    held: list[str] = []

    class TrackingLock:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self):
            held.append(self.name)

        def __exit__(self, *exc):
            assert held.pop() == self.name

    source = AdmissionSource()
    source.registration.lock = TrackingLock("registration")
    source.lock = TrackingLock("operation")
    admission = wire.OperationAdmission(source)
    writer = SimpleNamespace(
        write=lambda data: (
            held == ["registration", "operation"]
            or pytest.fail("request write escaped the admission locks")
        )
    )

    admission.write(writer, b"request")

    assert admission.dispatched is True
    assert held == []


def test_synchronous_write_error_is_conservatively_dispatched():
    source = AdmissionSource()
    admission = wire.OperationAdmission(source)

    def fail_write(data: bytes) -> None:
        raise OSError("synthetic enqueue failure")

    with pytest.raises(OSError, match="enqueue failure"):
        admission.write(SimpleNamespace(write=fail_write), b"request")

    assert admission.dispatched is True


@pytest.mark.asyncio
async def test_revocation_after_headers_preserves_uncertain_outcome(monkeypatch):
    source = AdmissionSource()
    writer = SocketWriter(respond=False)
    original_write = writer.write

    def revoke_after_headers(data: bytes) -> None:
        original_write(data)
        if data.startswith(b"POST "):
            with source.registration.lock, source.lock:
                source.active = False

    writer.write = revoke_after_headers
    with admitted_operation(source) as admission:
        with pytest.raises(wire.AdmissionRevoked):
            await _request_through_writer(monkeypatch, writer, content=b"payload")

    assert len(writer.writes) == 1
    assert admission.dispatched is True


@pytest.mark.asyncio
async def test_request_without_admission_keeps_ordinary_http_behavior(monkeypatch):
    writer = SocketWriter()

    response = await _request_through_writer(monkeypatch, writer)

    assert response.status_code == 200
    assert response.text == "ok"
    assert writer.writes[0].startswith(b"GET ")
    assert wire.operation_admission.get() is None
