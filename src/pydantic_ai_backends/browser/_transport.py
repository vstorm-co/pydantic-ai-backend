"""A bounded CDP connection; browser lifetime belongs to its caller, not a model run."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from pydantic import TypeAdapter

_MESSAGE = TypeAdapter(dict[str, object])


class BrowserError(RuntimeError):
    """A browser operation failed without implying that its side effect did not happen."""


class CDPTransport(Protocol):
    async def call(
        self, method: str, params: Mapping[str, object], *, session_id: str | None = None
    ) -> dict[str, object]: ...

    async def aclose(self) -> None: ...


class BrowserSocket(Protocol):
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...


def debugger_url(endpoint: str, advertised: str) -> str:
    """Keep the vetted endpoint's authority; a CDP reply cannot redirect credentials."""
    origin, remote = urlsplit(endpoint), urlsplit(advertised)
    if origin.scheme not in ("http", "https") or not origin.hostname:
        raise ValueError("Browser discovery needs an HTTP or HTTPS endpoint")
    if remote.scheme not in ("ws", "wss") or not remote.path:
        raise BrowserError("The endpoint did not report a WebSocket debugger path")
    return urlunsplit(
        ("wss" if origin.scheme == "https" else "ws", origin.netloc, remote.path, remote.query, "")
    )


class WebSocketCDP:
    """One multiplexed CDP transport with bounded commands and cancellation-safe close.

    Endpoints are trusted operator configuration. Applications remain responsible
    for endpoint authorization and browser egress isolation. There is no automatic
    reconnect/replay: a timed-out input may already have reached the browser.
    """

    def __init__(self, socket: BrowserSocket, *, timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise ValueError("The browser command timeout must be positive")
        self._socket = socket
        self._timeout = timeout
        self._sequence = 0
        self._pending: dict[int, asyncio.Future[dict[str, object]]] = {}
        self._slots = asyncio.Semaphore(32)
        self._closed = False
        self._close_complete = False
        self._close_lock = asyncio.Lock()
        self._reader = asyncio.create_task(self._receive(), name="browser-cdp-reader")

    @classmethod
    async def connect(
        cls,
        endpoint: str,
        *,
        timeout: float = 30.0,
        additional_headers: Mapping[str, str] | None = None,
    ) -> WebSocketCDP:
        """Connect directly to WS(S), or discover Chromium over HTTP(S)."""
        if timeout <= 0:
            raise ValueError("The browser command timeout must be positive")
        try:
            import httpx
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise ImportError("Install pydantic-ai-backend[browser] to connect a browser") from exc
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.hostname:
            raise ValueError("Browser endpoints must use HTTP(S) or WS(S)")
        if parsed.fragment:
            raise ValueError("Browser endpoints cannot have a fragment")
        socket_url = endpoint
        if parsed.scheme in ("http", "https"):
            async with httpx.AsyncClient(timeout=timeout) as client:
                discovery = urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path.rstrip("/") + "/json/version",
                        parsed.query,
                        "",
                    )
                )
                response = await client.get(discovery, headers=additional_headers)
                response.raise_for_status()
                answer = _MESSAGE.validate_json(response.content)
            advertised = answer.get("webSocketDebuggerUrl")
            if not isinstance(advertised, str):
                raise BrowserError("The endpoint did not report its debugger connection")
            socket_url = debugger_url(endpoint, advertised)
        socket = await connect(
            socket_url,
            open_timeout=timeout,
            max_size=32 * 1024 * 1024,
            additional_headers=additional_headers,
        )
        return cls(socket, timeout=timeout)

    async def call(
        self, method: str, params: Mapping[str, object], *, session_id: str | None = None
    ) -> dict[str, object]:
        async with self._slots:
            if self._closed or self._reader.done():
                raise BrowserError("The browser connection is closed")
            self._sequence += 1
            identifier = self._sequence
            future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
            self._pending[identifier] = future
            message: dict[str, object] = {
                "id": identifier,
                "method": method,
                "params": dict(params),
            }
            if session_id is not None:
                message["sessionId"] = session_id
            try:
                await asyncio.wait_for(self._socket.send(json.dumps(message)), self._timeout)
                return await asyncio.wait_for(future, self._timeout)
            finally:
                self._pending.pop(identifier, None)
                if future.done() and not future.cancelled():
                    future.exception()
                else:
                    future.cancel()

    async def _receive(self) -> None:
        try:
            async for raw in self._socket:
                message = _MESSAGE.validate_json(raw)
                identifier = message.get("id")
                if not isinstance(identifier, int):
                    continue
                future = self._pending.get(identifier)
                if future is None or future.done():
                    continue
                result = message.get("result")
                if message.get("error") is not None:
                    future.set_exception(BrowserError("The browser refused the CDP command"))
                elif isinstance(result, dict):
                    future.set_result(_MESSAGE.validate_python(result))
                else:
                    future.set_exception(BrowserError("The browser returned an invalid CDP result"))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Provider payloads may contain credentials/page data; keep them out
            # of the public error while failing every blocked command explicitly.
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(BrowserError("The browser connection was lost"))

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._close_complete:
                return
            self._closed = True
            self._reader.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader
            await asyncio.wait_for(self._socket.close(), self._timeout)
            self._close_complete = True
