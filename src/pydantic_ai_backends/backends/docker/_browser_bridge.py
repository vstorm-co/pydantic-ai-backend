"""CDP over Docker exec stdio; no container or host debugger port is published."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import threading
from collections.abc import Mapping
from contextlib import suppress
from typing import Protocol, runtime_checkable

import anyio

# The helper runs in the session container, never in the sandboxd host network.
# Its sole destination is that container's own loopback Chromium endpoint.
BRIDGE_SCRIPT = r"""
import json, sys, threading, urllib.request
import websocket
with urllib.request.urlopen('http://127.0.0.1:9222/json/version', timeout=10) as response:
    info=json.load(response)
from urllib.parse import urlsplit
path=urlsplit(info['webSocketDebuggerUrl']).path
ws=websocket.create_connection('ws://127.0.0.1:9222'+path, timeout=10, suppress_origin=True)
ws.settimeout(None)
def incoming():
    try:
        for line in sys.stdin:
            ws.send(line.rstrip('\n'))
    finally:
        ws.close()
threading.Thread(target=incoming, daemon=True).start()
try:
    while True:
        message=ws.recv()
        if not message: break
        if len(message.encode()) > 16*1024*1024: raise RuntimeError("CDP message too large")
        print(message, flush=True)
finally:
    ws.close()
"""

logger = logging.getLogger(__name__)

MAX_MESSAGE = 16 * 1024 * 1024
OPEN_SECONDS = 15
SEND_SECONDS = 15
IO_POLL_SECONDS = 0.25


class BrowserChannel(Protocol):
    async def send(self, message: str) -> None: ...
    async def receive(self) -> str: ...
    async def aclose(self) -> None: ...


class BrowserChannelError(RuntimeError):
    """The exact live browser channel is unavailable; commands are never replayed."""


class ExecAPI(Protocol):
    def exec_create(
        self, container: str, cmd: list[str], *, stdin: bool, stdout: bool, stderr: bool
    ) -> Mapping[str, object]: ...
    def exec_start(self, exec_id: str, *, socket: bool) -> object: ...


@runtime_checkable
class SocketOwner(Protocol):
    def close(self) -> None: ...


def close_exec_socket(raw: socket.socket, owner: SocketOwner | None) -> None:
    """Close Docker's HTTP response before its SocketIO flush target disappears."""
    response: object = getattr(owner, "_response", None)
    resources: list[SocketOwner] = []
    if isinstance(response, SocketOwner):
        resources.append(response)
    resources.append(raw)
    if owner is not None and owner is not raw:
        resources.append(owner)
    failure: Exception | None = None
    for resource in resources:
        try:
            resource.close()
        except Exception as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise failure


class _Opening:
    """Close a socket returned after the async owner already timed out/cancelled."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.abandoned = False
        self.result: tuple[socket.socket, SocketOwner] | None = None

    @staticmethod
    def _dispose(raw: socket.socket, owner: SocketOwner) -> None:
        try:
            close_exec_socket(raw, owner)
        except Exception as error:
            logger.warning("Abandoned browser socket cleanup failed (%s)", type(error).__name__)

    def publish(self, raw: socket.socket, owner: SocketOwner) -> tuple[socket.socket, SocketOwner]:
        with self.lock:
            if self.abandoned:
                self._dispose(raw, owner)
            else:
                self.result = raw, owner
        return raw, owner

    def abandon(self) -> None:
        with self.lock:
            self.abandoned = True
            if self.result is not None:
                raw, owner = self.result
                self.result = None
                self._dispose(raw, owner)

    def transfer(self) -> None:
        with self.lock:
            self.result = None


class DockerBrowserChannel:
    """A single bounded text channel carried by Docker's multiplexed exec socket."""

    def __init__(self, stream: socket.socket, owner: SocketOwner | None = None) -> None:
        self._stream = stream
        self._owner = owner
        self._buffer = bytearray()
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._close_lock = threading.Lock()
        self._stream.settimeout(IO_POLL_SECONDS)

    @classmethod
    async def open(cls, api: ExecAPI, container_id: str) -> DockerBrowserChannel:
        from docker.errors import DockerException

        opening = _Opening()

        def start() -> tuple[socket.socket, SocketOwner]:
            created = api.exec_create(
                container_id,
                ["python3", "-u", "-c", BRIDGE_SCRIPT],
                stdin=True,
                stdout=True,
                stderr=True,
            )
            exec_id = created.get("Id")
            if not isinstance(exec_id, str):
                raise BrowserChannelError("Docker did not return a browser channel identity")
            connection = api.exec_start(exec_id, socket=True)
            raw = getattr(connection, "_sock", connection)
            if not isinstance(raw, socket.socket):
                close = getattr(connection, "close", None)
                if callable(close):
                    close()
                raise BrowserChannelError("Docker did not return a supported exec socket")
            if not isinstance(connection, SocketOwner):
                raw.close()
                raise BrowserChannelError("Docker returned an unowned browser socket")
            return opening.publish(raw, connection)

        try:
            with anyio.fail_after(OPEN_SECONDS):
                raw, owner = await anyio.to_thread.run_sync(start, abandon_on_cancel=True)
            channel = cls(raw, owner)
            opening.transfer()
        except TimeoutError as error:
            opening.abandon()
            raise BrowserChannelError("Opening the browser channel timed out") from error
        except (DockerException, OSError) as error:
            opening.abandon()
            raise BrowserChannelError("Docker could not open the browser channel") from error
        except BaseException:
            opening.abandon()
            raise
        return channel

    async def send(self, message: str) -> None:
        if self._closed:
            raise BrowserChannelError("The browser channel is closed")
        if len(message.encode()) > MAX_MESSAGE or "\n" in message:
            raise BrowserChannelError("Invalid browser channel message")
        # Validate JSON before forwarding; a protocol message always is an object.
        try:
            value: object = json.loads(message)
        except ValueError as error:
            raise BrowserChannelError("Invalid browser protocol JSON") from error
        if not isinstance(value, dict):
            raise BrowserChannelError("Browser protocol messages must be objects")
        try:
            with anyio.fail_after(SEND_SECONDS):
                async with self._send_lock:
                    if self._closed:
                        raise BrowserChannelError("The browser channel is closed")
                    await anyio.to_thread.run_sync(
                        self._send, (message + "\n").encode(), abandon_on_cancel=True
                    )
        except TimeoutError as error:
            await self._abort()
            raise BrowserChannelError(
                "The browser channel write timed out; outcome unknown"
            ) from error
        except OSError as error:
            await self._abort()
            raise BrowserChannelError("The browser channel disconnected") from error
        except BaseException:
            await self._abort()
            raise

    def _send(self, message: bytes) -> None:
        remaining = memoryview(message)
        while remaining:
            if self._closed:
                raise BrowserChannelError("The browser channel is closed")
            try:
                sent = self._stream.send(remaining)
            except TimeoutError:
                continue
            if sent <= 0:
                raise BrowserChannelError("The browser channel disconnected")
            remaining = remaining[sent:]

    def _read_exactly(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            if self._closed:
                raise BrowserChannelError("The browser channel is closed")
            try:
                part = self._stream.recv(length - len(chunks))
            except TimeoutError:
                continue
            if not part:
                raise BrowserChannelError("The browser channel disconnected")
            chunks.extend(part)
        return bytes(chunks)

    def _receive(self) -> str:
        while b"\n" not in self._buffer:
            header = self._read_exactly(8)
            stream, length = header[0], struct.unpack(">I", header[4:])[0]
            if length > MAX_MESSAGE:
                raise BrowserChannelError("The browser frame exceeded its limit")
            payload = self._read_exactly(length)
            if stream == 2:
                # Never expose traceback payloads which may contain page content.
                raise BrowserChannelError("The browser bridge failed inside its container")
            if stream != 1:
                raise BrowserChannelError("Unexpected browser channel stream")
            self._buffer.extend(payload)
            if len(self._buffer) > MAX_MESSAGE:
                raise BrowserChannelError("The browser message exceeded its limit")
        line, _, remainder = self._buffer.partition(b"\n")
        self._buffer = bytearray(remainder)
        try:
            return line.decode("utf-8")
        except UnicodeError as error:
            raise BrowserChannelError("The browser returned invalid text") from error

    async def receive(self) -> str:
        if self._closed:
            raise BrowserChannelError("The browser channel is closed")
        try:
            return await anyio.to_thread.run_sync(self._receive, abandon_on_cancel=True)
        except OSError as error:
            await self._abort()
            raise BrowserChannelError("The browser channel disconnected") from error
        except BaseException:
            await self._abort()
            raise

    async def _abort(self) -> None:
        """Retain the primary operation/cancellation error if transport disposal fails."""
        try:
            await self.aclose()
        except Exception as error:
            logger.warning("Browser channel cleanup failed (%s)", type(error).__name__)

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        """Unblock native readers when either their request or container retires."""
        with self._close_lock:
            self._closed = True
            with suppress(OSError):
                self._stream.shutdown(socket.SHUT_RDWR)
            close_exec_socket(self._stream, self._owner)
            self._owner = None
