"""CDP transport contracts, including disconnect/cancellation and endpoint discovery."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from typing import Literal

import httpx
import pytest
from pydantic import TypeAdapter

from pydantic_ai_backends.browser import BrowserError, WebSocketCDP
from pydantic_ai_backends.browser._transport import debugger_url

_MESSAGE = TypeAdapter(dict[str, object])


class Socket:
    def __init__(self, mode: str = "result") -> None:
        self.mode = mode
        self.incoming: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[str | bytes]:
        while (message := await self.incoming.get()) is not None:
            yield message

    async def send(self, message: str) -> None:
        request = _MESSAGE.validate_json(message)
        self.sent.append(request)
        if self.mode == "wait":
            return
        if self.mode == "failure":
            raise OSError("disconnected")
        if self.mode == "malformed":
            await self.incoming.put("not-json")
            return
        if self.mode == "end":
            await self.incoming.put(None)
            return
        field = "error" if self.mode in ("error", "errorend") else "result"
        payload: object = {"value": 7} if self.mode != "invalid" else None
        await self.incoming.put(json.dumps({"id": request["id"], field: payload}))
        if self.mode == "errorend":
            await self.incoming.put(None)

    async def close(self) -> None:
        self.closed = True


async def test_response_multiplexing_and_ignored_events() -> None:
    socket = Socket()
    cdp = WebSocketCDP(socket)
    await socket.incoming.put(json.dumps({"method": "Target.targetCreated"}))
    await socket.incoming.put(json.dumps({"id": 999, "result": {}}))
    assert await cdp.call("Runtime.test", {"a": 1}, session_id="page") == {"value": 7}
    assert socket.sent[0]["sessionId"] == "page"
    await cdp.aclose()
    await cdp.aclose()
    assert socket.closed
    with pytest.raises(BrowserError, match="closed"):
        await cdp.call("Runtime.test", {})


@pytest.mark.parametrize("mode", ["error", "errorend", "invalid", "malformed", "end"])
async def test_provider_error_wakes_all_waiters(mode: str) -> None:
    cdp = WebSocketCDP(Socket(mode))
    try:
        with pytest.raises(BrowserError):
            await cdp.call("Runtime.test", {})
    finally:
        await cdp.aclose()


async def test_timeout_and_cancellation_do_not_replay_input() -> None:
    socket = Socket("wait")
    cdp = WebSocketCDP(socket, timeout=0.01)
    with pytest.raises(TimeoutError):
        await cdp.call("Input.insertText", {"text": "once"})
    command = asyncio.create_task(cdp.call("Input.insertText", {"text": "twice"}))
    while len(socket.sent) < 2:
        await asyncio.sleep(0)
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    await socket.incoming.put(json.dumps({"id": 1, "result": {}}))
    await asyncio.sleep(0)
    assert len(socket.sent) == 2
    await cdp.aclose()


async def test_send_failure_does_not_leave_a_pending_command() -> None:
    cdp = WebSocketCDP(Socket("failure"))
    try:
        with pytest.raises(OSError):
            await cdp.call("Runtime.test", {})
    finally:
        await cdp.aclose()


def test_discovery_never_follows_the_advertised_authority() -> None:
    assert debugger_url("https://browser.example:443", "ws://169.254.169.254/devtools/x?key=a") == (
        "wss://browser.example:443/devtools/x?key=a"
    )
    for endpoint, advertised in [("file:///tmp", "ws://host/path"), ("http://host", "http://bad")]:
        with pytest.raises((ValueError, BrowserError)):
            debugger_url(endpoint, advertised)


async def test_invalid_connection_configuration_is_rejected() -> None:
    socket = Socket()
    with pytest.raises(ValueError):
        WebSocketCDP(socket, timeout=0)
    with pytest.raises(ValueError):
        await WebSocketCDP.connect("ws://localhost", timeout=0)
    for endpoint in ["file:///tmp", "ws:///missing", "http://host/#fragment"]:
        with pytest.raises(ValueError):
            await WebSocketCDP.connect(endpoint)


@pytest.mark.parametrize("scheme", ["http", "ws"])
async def test_optional_websocket_connection(
    monkeypatch: pytest.MonkeyPatch, scheme: Literal["http", "ws"]
) -> None:
    import websockets.asyncio.client

    socket = Socket()
    connected: list[str] = []

    async def connect(url: str, **_kwargs: object) -> Socket:
        connected.append(url)
        return socket

    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"webSocketDebuggerUrl": "ws://untrusted/devtools/browser/opaque"}
            )
        )
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    cdp = await WebSocketCDP.connect(f"{scheme}://browser:9222")
    assert connected == [
        "ws://browser:9222/devtools/browser/opaque" if scheme == "http" else "ws://browser:9222"
    ]
    await cdp.aclose()
    await client.aclose()


async def test_discovery_requires_debugger_address(monkeypatch: pytest.MonkeyPatch) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    with pytest.raises(BrowserError, match="debugger connection"):
        await WebSocketCDP.connect("http://browser:9222")


async def test_failed_socket_close_can_be_retried() -> None:
    class FlakySocket(Socket):
        async def close(self) -> None:
            if not self.closed:
                self.closed = True
                raise OSError("temporary close failure")
            await super().close()

    socket = FlakySocket()
    cdp = WebSocketCDP(socket)
    with pytest.raises(OSError):
        await cdp.aclose()
    with pytest.raises(BrowserError, match="closed"):
        await cdp.call("Input.insertText", {})
    await cdp.aclose()
    await cdp.aclose()
    assert socket.closed


async def test_missing_optional_transport_has_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "websockets.asyncio.client", None)
    with pytest.raises(ImportError, match="pydantic-ai-backend\\[browser\\]"):
        await WebSocketCDP.connect("ws://browser:9222")
