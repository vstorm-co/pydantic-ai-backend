"""Private binary exec transfer is bounded, complete, and owns its socket wrapper."""

import hashlib
import io
import socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from pydantic_ai_backends.backends.docker import _profile_exec as native
from pydantic_ai_backends.remote._browser_profiles import BrowserProfileError
from tests.test_browser_profiles import archive


def frame(data, stream=1, reserved=b"\0\0\0"):
    return bytes([stream]) + reserved + struct.pack(">I", len(data)) + data


def envelope(data):
    return native.MAGIC + struct.pack(">Q", len(data)) + hashlib.sha256(data).digest() + data


class Owner:
    def __init__(self, raw):
        self._sock = raw
        self.closed = False

    def close(self):
        self.closed = True
        self._sock.close()


class Container:
    def __init__(self, owner):
        self.owner = owner
        self.command = None

    def exec_run(self, command, *, stdin, socket):
        assert stdin and socket
        self.command = command
        return SimpleNamespace(output=self.owner)


class Stream:
    def __init__(self, data):
        self.data = io.BytesIO(data)
        self.timeouts = []

    def settimeout(self, seconds):
        self.timeouts.append(seconds)

    def recv(self, size):
        return self.data.read(size)


@pytest.mark.parametrize("restore", [False, True])
def test_native_transfer_uses_private_stdio_and_closes_socket_owner(restore):
    caller, peer = socket.socketpair()
    owner = Owner(caller)
    container = Container(owner)
    payload = archive()
    received = []
    errors = []

    def serve():
        try:
            with peer:
                if restore:
                    data = bytearray()
                    while len(data) < len(payload) + native.HEADER_BYTES:
                        data.extend(peer.recv(65536))
                    received.append(bytes(data))
                    peer.sendall(frame(native.ACK))
                else:
                    peer.sendall(frame(envelope(payload)))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        if restore:
            native.import_archive(container, payload)
            assert received == [envelope(payload)]
        else:
            assert native.export_archive(container) == payload
        assert owner.closed
        assert caller.fileno() == -1
        assert container.command[:3] == ["python3", "-u", "-c"]
        assert "private-login" not in container.command[-1]
    finally:
        caller.close()
        peer.close()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []


@pytest.mark.parametrize("kind", ["none", "closeable", "unowned"])
def test_open_refuses_unsupported_exec_socket_and_disposes_owned_parts(kind):
    caller, peer = socket.socketpair()
    closed = []
    if kind == "none":
        owner = None
    elif kind == "closeable":
        owner = SimpleNamespace(close=lambda: closed.append(True))
    else:
        owner = SimpleNamespace(_sock=caller)
    try:
        with pytest.raises(BrowserProfileError):
            native._open(Container(owner), "script")
        assert bool(closed) == (kind == "closeable")
        if kind == "unowned":
            assert caller.fileno() == -1
    finally:
        caller.close()
        peer.close()


@pytest.mark.parametrize(
    "data",
    [b"", b"bad", envelope(b"x")[:-1], native.MAGIC + struct.pack(">Q", 1) + b"x" * 32 + b"x"],
)
def test_export_requires_a_complete_matching_digest(data, monkeypatch):
    caller, peer = socket.socketpair()
    owner = Owner(caller)
    with peer:
        peer.sendall(frame(data))
    monkeypatch.setattr(native, "_open", lambda _container, _script: (caller, owner))
    with pytest.raises(BrowserProfileError):
        native.export_archive(object())
    assert owner.closed


@pytest.mark.parametrize(
    "header",
    [frame(b"private-profile", stream=2), frame(b"x", reserved=b"bad"), frame(b"oversize")],
)
def test_frame_transport_refuses_stderr_reserved_bits_and_size_before_returning_data(header):
    with pytest.raises(BrowserProfileError):
        native._frames(Stream(header), 1, time.monotonic() + 2)


def test_frame_transport_rejects_truncation_and_expired_deadline():
    with pytest.raises(BrowserProfileError, match="completion"):
        native._frames(Stream(b"\1"), 100, time.monotonic() + 2)
    with pytest.raises(BrowserProfileError, match="deadline"):
        native._read_exact(Stream(b"a"), 1, time.monotonic() - 1)
    assert native._read_exact(Stream(b""), 1, time.monotonic() + 1, eof=True) == b""


@pytest.mark.parametrize("payload", [b"", b"long"])
def test_restore_input_bound_is_checked_before_open(payload, monkeypatch):
    monkeypatch.setattr(native, "MAX_PROFILE_BYTES", 1)
    with pytest.raises(BrowserProfileError, match="bound"):
        native.import_archive(object(), payload)


def test_restore_rejects_missing_completion_ack(monkeypatch):
    caller, peer = socket.socketpair()
    owner = Owner(caller)
    with peer:
        peer.sendall(frame(b"no"))
    monkeypatch.setattr(native, "_open", lambda _container, _script: (caller, owner))

    # A fake send avoids writing to the deliberately closed peer.
    class SendingStream:
        def settimeout(self, seconds):
            caller.settimeout(seconds)

        def sendall(self, _data):
            pass

        def recv(self, size):
            return caller.recv(size)

        def close(self):
            caller.close()

    monkeypatch.setattr(native, "_open", lambda _container, _script: (SendingStream(), owner))
    with pytest.raises(BrowserProfileError, match="acknowledge"):
        native.import_archive(object(), b"x")
    assert owner.closed


def test_restore_expired_send_deadline_still_closes_owner(monkeypatch):
    caller, peer = socket.socketpair()
    owner = Owner(caller)
    monkeypatch.setattr(native, "_open", lambda _container, _script: (caller, owner))
    clock = iter([0, 31])
    monkeypatch.setattr(native.time, "monotonic", lambda: next(clock))
    try:
        with pytest.raises(BrowserProfileError, match="deadline"):
            native.import_archive(object(), b"x")
        assert owner.closed
    finally:
        peer.close()


def test_transport_cleanup_failure_is_logged_without_profile_body(caplog):
    class Broken:
        def close(self):
            raise OSError("private-profile-body")

    native._close(Broken(), Broken())
    assert "cleanup failed" in caplog.text
    assert "private-profile-body" not in caplog.text
