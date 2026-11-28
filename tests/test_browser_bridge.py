"""Docker exec channels preserve socket ownership and bound untrusted frames."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from pydantic_ai_backends.backends.docker import _browser_bridge as bridge


@dataclass
class Wrapper:
    _sock: socket.socket
    closed: int = 0

    def close(self) -> None:
        self.closed += 1
        self._sock.close()


class ExecAPI:
    def __init__(self, stream: object) -> None:
        self.stream = stream
        self.created: Mapping[str, object] = {"Id": "exec-one"}
        self.command: list[str] = []

    def exec_create(
        self, container: str, cmd: list[str], *, stdin: bool, stdout: bool, stderr: bool
    ) -> Mapping[str, object]:
        assert container == "owned-container"
        assert stdin and stdout and stderr
        self.command = cmd
        return self.created

    def exec_start(self, exec_id: str, *, socket: bool) -> object:
        assert exec_id == "exec-one" and socket
        return self.stream


def frame(payload: bytes, stream: int = 1) -> bytes:
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


@pytest.fixture
def sockets():
    left, right = socket.socketpair()
    try:
        yield left, right
    finally:
        left.close()
        right.close()


async def test_channel_sends_literal_json_and_reads_fragmented_and_buffered_frames(sockets):
    local, peer = sockets
    channel = await bridge.DockerBrowserChannel.open(ExecAPI(local), "owned-container")
    await channel.send('{"id":1,"method":"Target.getTargets"}')
    assert peer.recv(4096) == b'{"id":1,"method":"Target.getTargets"}\n'
    payload = frame(b'{"id":1,') + frame(b'"result":{}}\n{"id":2}\n')
    peer.sendall(payload[:3])
    receiving = asyncio.create_task(channel.receive())
    await asyncio.sleep(0)
    peer.sendall(payload[3:])
    assert await receiving == '{"id":1,"result":{}}'
    assert await channel.receive() == '{"id":2}'
    await channel.aclose()
    await channel.aclose()
    assert peer.recv(1) == b""
    for operation in (channel.receive, lambda: channel.send("{}")):
        with pytest.raises(bridge.BrowserChannelError, match="closed"):
            await operation()


async def test_channel_keeps_and_closes_docker_socket_wrapper(sockets):
    local, _ = sockets
    wrapper = Wrapper(local)
    channel = await bridge.DockerBrowserChannel.open(ExecAPI(wrapper), "owned-container")
    await channel.aclose()
    await channel.aclose()
    assert wrapper.closed == 1


async def test_missing_exec_identity_and_unsupported_socket_are_explicit(sockets):
    local, _ = sockets
    api = ExecAPI(local)
    api.created = {}
    with pytest.raises(bridge.BrowserChannelError, match="identity"):
        await bridge.DockerBrowserChannel.open(api, "owned-container")

    class Unsupported:
        closed = False

        def close(self) -> None:
            self.closed = True

    unsupported = Unsupported()
    with pytest.raises(bridge.BrowserChannelError, match="supported"):
        await bridge.DockerBrowserChannel.open(ExecAPI(unsupported), "owned-container")
    assert unsupported.closed
    with pytest.raises(bridge.BrowserChannelError, match="supported"):
        await bridge.DockerBrowserChannel.open(ExecAPI(object()), "owned-container")


@pytest.mark.parametrize("message", ["not-json", "[]", "{}\n"])
async def test_invalid_outgoing_messages_never_reach_container(sockets, message):
    local, _ = sockets
    channel = bridge.DockerBrowserChannel(local)
    with pytest.raises(bridge.BrowserChannelError):
        await channel.send(message)
    await channel.aclose()


async def test_outgoing_message_limit_is_bytes_not_characters(sockets, monkeypatch):
    local, _ = sockets
    monkeypatch.setattr(bridge, "MAX_MESSAGE", 10)
    channel = bridge.DockerBrowserChannel(local)
    with pytest.raises(bridge.BrowserChannelError, match="Invalid"):
        await channel.send('{"x":"☃☃"}')
    await channel.aclose()


@pytest.mark.parametrize(
    "payload,match",
    [
        (frame(b"private-provider-token", stream=2), "bridge failed"),
        (frame(b"other", stream=3), "Unexpected"),
        (frame(b"\xff\n"), "invalid text"),
        (bytes([1, 0, 0, 0]) + (bridge.MAX_MESSAGE + 1).to_bytes(4, "big"), "frame exceeded"),
    ],
)
async def test_invalid_incoming_frames_fail_without_provider_payload(sockets, payload, match):
    local, peer = sockets
    channel = bridge.DockerBrowserChannel(local)
    peer.sendall(payload)
    with pytest.raises(bridge.BrowserChannelError, match=match) as failure:
        await channel.receive()
    assert "private-provider-token" not in str(failure.value)
    await channel.aclose()


async def test_aggregate_message_limit_applies_across_docker_frames(sockets, monkeypatch):
    local, peer = sockets
    monkeypatch.setattr(bridge, "MAX_MESSAGE", 5)
    channel = bridge.DockerBrowserChannel(local)
    peer.sendall(frame(b"1234") + frame(b"5678"))
    with pytest.raises(bridge.BrowserChannelError, match="message exceeded"):
        await channel.receive()
    await channel.aclose()


async def test_peer_disconnect_and_os_failures_are_explicit(sockets):
    local, peer = sockets
    channel = bridge.DockerBrowserChannel(local)
    peer.close()
    with pytest.raises(bridge.BrowserChannelError, match="disconnected"):
        await channel.receive()
    with pytest.raises(bridge.BrowserChannelError, match="closed"):
        await channel.send("{}")
    await channel.aclose()


async def test_close_unblocks_a_waiting_reader(sockets):
    local, _ = sockets
    channel = bridge.DockerBrowserChannel(local)
    receiving = asyncio.create_task(channel.receive())
    await asyncio.sleep(0)
    await channel.aclose()
    with pytest.raises(bridge.BrowserChannelError, match="closed|disconnected"):
        await asyncio.wait_for(receiving, 1)


async def test_valid_socket_without_wrapper_owner_is_closed_before_refusal(sockets):
    local, _ = sockets

    class Unowned:
        _sock = local

    with pytest.raises(bridge.BrowserChannelError, match="unowned"):
        await bridge.DockerBrowserChannel.open(ExecAPI(Unowned()), "owned-container")
    assert local.fileno() == -1


async def test_local_socket_failure_becomes_a_channel_error(sockets):
    local, _ = sockets
    channel = bridge.DockerBrowserChannel(local)
    local.close()
    with pytest.raises(bridge.BrowserChannelError, match="disconnected"):
        await channel.receive()
    await channel.aclose()


async def test_blocked_send_has_a_deadline_and_closes_socket(sockets, monkeypatch):
    local, _peer = sockets
    local.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    monkeypatch.setattr(bridge, "SEND_SECONDS", 0.02)
    wrapper = Wrapper(local)
    channel = bridge.DockerBrowserChannel(local, wrapper)
    message = '{"payload":"' + "x" * (1024 * 1024) + '"}'
    with pytest.raises(bridge.BrowserChannelError, match="outcome unknown"):
        await asyncio.wait_for(channel.send(message), 1)
    assert local.fileno() == -1
    assert wrapper.closed == 1
    await channel.aclose()
    assert wrapper.closed == 1


async def test_cancelled_send_closes_the_socket_and_preserves_cancellation(sockets):
    local, _peer = sockets
    local.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    wrapper = Wrapper(local)
    channel = bridge.DockerBrowserChannel(local, wrapper)
    message = '{"payload":"' + "x" * (1024 * 1024) + '"}'
    task = asyncio.create_task(channel.send(message))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert local.fileno() == -1
    assert wrapper.closed == 1


async def test_cancelled_receive_closes_and_unblocks_the_abandoned_reader(sockets):
    local, _peer = sockets
    wrapper = Wrapper(local)
    channel = bridge.DockerBrowserChannel(local, wrapper)
    task = asyncio.create_task(channel.receive())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert local.fileno() == -1
    assert wrapper.closed == 1


async def test_socket_closed_while_sender_waits_cannot_be_used_again(sockets):
    local, _peer = sockets
    channel = bridge.DockerBrowserChannel(local)
    await channel._send_lock.acquire()
    task = asyncio.create_task(channel.send("{}"))
    await asyncio.sleep(0)
    await channel.aclose()
    channel._send_lock.release()
    with pytest.raises(bridge.BrowserChannelError, match="closed"):
        await task


@pytest.mark.parametrize("cancel", [False, True])
async def test_startup_timeout_or_cancellation_closes_late_docker_socket(
    sockets, monkeypatch, cancel
):
    import threading

    local, _peer = sockets
    wrapper = Wrapper(local)
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class LateAPI(ExecAPI):
        def exec_start(self, exec_id: str, *, socket: bool) -> object:
            started.set()
            assert release.wait(2)
            return super().exec_start(exec_id, socket=socket)

    original_close = wrapper.close

    def close():
        original_close()
        closed.set()

    monkeypatch.setattr(wrapper, "close", close)
    monkeypatch.setattr(bridge, "OPEN_SECONDS", 0.02 if not cancel else 1)
    task = asyncio.create_task(
        bridge.DockerBrowserChannel.open(LateAPI(wrapper), "owned-container")
    )
    try:
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else bridge.BrowserChannelError):
            await asyncio.wait_for(task, 1)
        release.set()
        for _ in range(1000):
            if closed.is_set():
                break
            await asyncio.sleep(0.001)
        assert closed.is_set()
        assert wrapper.closed == 1
        assert local.fileno() == -1
    finally:
        release.set()


def test_abandoned_published_socket_is_closed_before_ownership_transfer(sockets):
    local, _peer = sockets
    wrapper = Wrapper(local)
    opening = bridge._Opening()
    opening.publish(local, wrapper)
    opening.abandon()
    assert local.fileno() == -1
    assert wrapper.closed == 1
    opening.abandon()
    assert wrapper.closed == 1


async def test_failed_socket_write_closes_wrapper_without_replay(sockets):
    local, peer = sockets
    wrapper = Wrapper(local)
    channel = bridge.DockerBrowserChannel(local, wrapper)
    peer.close()
    with pytest.raises(bridge.BrowserChannelError, match="disconnected"):
        await channel.send("{}")
    assert wrapper.closed == 1
    with pytest.raises(bridge.BrowserChannelError, match="closed"):
        await channel.send("{}")


async def test_failed_owner_close_remains_retryable_without_reopening_socket(sockets):
    local, _peer = sockets

    class FlakyOwner:
        attempts = 0

        def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("Close acknowledgement lost")

    owner = FlakyOwner()
    channel = bridge.DockerBrowserChannel(local, owner)
    with pytest.raises(OSError, match="acknowledgement"):
        await channel.aclose()
    assert local.fileno() == -1
    await channel.aclose()
    await channel.aclose()
    assert owner.attempts == 2


async def test_cancellation_is_not_replaced_by_wrapper_disposal_failure(sockets, caplog):
    local, _peer = sockets
    local.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)

    class FailedOwner:
        fail = True

        def close(self) -> None:
            if self.fail:
                raise OSError("private-owner-token")

    owner = FailedOwner()
    channel = bridge.DockerBrowserChannel(local, owner)
    task = asyncio.create_task(channel.send('{"payload":"' + "x" * (1024 * 1024) + '"}'))
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert "cleanup" in caplog.text
        assert "private-owner-token" not in caplog.text
    finally:
        owner.fail = False
        await channel.aclose()


@pytest.mark.parametrize("late", [False, True])
def test_abandoned_socket_cleanup_error_is_logged_without_replacing_cancellation(
    sockets, caplog, late
):
    local, _peer = sockets

    class FailedOwner:
        def close(self) -> None:
            raise OSError("private-owner-token")

    opening = bridge._Opening()
    owner = FailedOwner()
    if late:
        opening.abandon()
        opening.publish(local, owner)
    else:
        opening.publish(local, owner)
        opening.abandon()
    assert local.fileno() == -1
    assert "cleanup failed" in caplog.text
    assert "private-owner-token" not in caplog.text


@pytest.mark.parametrize("failure", [OSError("private-docker-token"), "docker"])
async def test_docker_open_failures_have_explicit_sanitized_channel_contract(sockets, failure):
    from docker.errors import DockerException

    local, _peer = sockets
    error = DockerException("private-docker-token") if failure == "docker" else failure

    class FailedAPI(ExecAPI):
        def exec_start(self, exec_id: str, *, socket: bool) -> object:
            raise error

    with pytest.raises(bridge.BrowserChannelError, match="Docker could not open") as caught:
        await bridge.DockerBrowserChannel.open(FailedAPI(local), "owned-container")
    assert "private-docker-token" not in str(caught.value)


def test_native_reader_polls_closed_state_even_when_shutdown_does_not_wake_recv(monkeypatch):
    import threading

    monkeypatch.setattr(bridge, "IO_POLL_SECONDS", 0.01)
    entered = threading.Event()

    class UnwokenSocket:
        def settimeout(self, seconds):
            self.timeout = seconds

        def recv(self, _size):
            entered.set()
            threading.Event().wait(self.timeout)
            raise TimeoutError()

        def shutdown(self, _direction):
            pass

        def close(self):
            pass

    channel = bridge.DockerBrowserChannel(UnwokenSocket())
    failures = []

    def read():
        try:
            channel._read_exactly(8)
        except bridge.BrowserChannelError as error:
            failures.append(error)

    worker = threading.Thread(target=read)
    worker.start()
    try:
        assert entered.wait(1)
        channel.close()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert failures
    finally:
        channel.close()
        worker.join(timeout=1)


@pytest.mark.parametrize("stop", [False, True])
def test_polled_writer_preserves_offsets_and_does_not_replay_partial_bytes(stop):
    output = bytearray()

    class PartialSocket:
        calls = 0

        def settimeout(self, _seconds):
            pass

        def send(self, data):
            self.calls += 1
            if self.calls == 1:
                output.extend(data[:1])
                return 1
            if self.calls == 2:
                raise TimeoutError()
            if stop:
                return 0
            output.extend(data)
            return len(data)

    channel = bridge.DockerBrowserChannel(PartialSocket())
    if stop:
        with pytest.raises(bridge.BrowserChannelError, match="disconnected"):
            channel._send(b"complete")
        assert output == b"c"
    else:
        channel._send(b"complete")
        assert output == b"complete"
    channel._closed = True
    with pytest.raises(bridge.BrowserChannelError, match="closed"):
        channel._send(b"more")


async def test_socket_initialization_failure_does_not_lose_published_wrapper(sockets, monkeypatch):
    raw, _peer = sockets
    wrapper = Wrapper(raw)

    def failed(_self, _raw, _owner):
        raise OSError("socket configuration failed")

    monkeypatch.setattr(bridge.DockerBrowserChannel, "__init__", failed)
    with pytest.raises(bridge.BrowserChannelError, match="open"):
        await bridge.DockerBrowserChannel.open(ExecAPI(wrapper), "owned-container")
    assert wrapper.closed == 1
    assert raw.fileno() == -1
