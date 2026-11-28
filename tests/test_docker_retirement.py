"""Container retirement owns every private browser channel and keeps failed stops retryable."""

import asyncio
import socket
import threading
from types import SimpleNamespace

import pytest
from docker.errors import APIError, NotFound

from pydantic_ai_backends import SessionManager
from pydantic_ai_backends.backends.docker import _browser_bridge as bridge
from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox
from pydantic_ai_backends.protocol import SandboxUnavailableError
from tests.test_browser_bridge import ExecAPI, Wrapper


class Container:
    def __init__(self):
        self.id = "owned-container"
        self.status = "running"
        self.failure = None
        self.removed = False
        self.stopped = False

    def stop(self):
        if self.failure:
            raise self.failure
        self.stopped = True
        self.status = "exited"

    def remove(self, *, force):
        assert force
        if self.failure:
            raise self.failure
        self.removed = True

    def reload(self):
        if self.removed:
            raise NotFound("gone")
        if self.failure:
            raise self.failure


async def test_container_stop_closes_unclaimed_channel_and_unblocks_native_reader(monkeypatch):
    raw, peer = socket.socketpair()
    wrapper = Wrapper(raw)
    container = Container()
    container.client = SimpleNamespace(api=ExecAPI(wrapper))
    sandbox = DockerSandbox(image="not-started")
    sandbox._container = container
    monkeypatch.setattr(sandbox, "is_alive", lambda: True)
    channel = await sandbox.open_browser_channel()
    entered = threading.Event()
    failed = []
    read = channel._read_exactly

    def tracked_read(size):
        entered.set()
        return read(size)

    monkeypatch.setattr(channel, "_read_exactly", tracked_read)

    def receive():
        try:
            channel._receive()
        except (bridge.BrowserChannelError, OSError) as error:
            failed.append(error)

    worker = threading.Thread(target=receive)
    worker.start()
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        sandbox.stop()
        await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()
        assert failed
        assert wrapper.closed == 1
        assert container.stopped
        assert peer.recv(1) == b""
    finally:
        channel.close()
        peer.close()
        worker.join(timeout=2)


async def test_attachment_finishing_after_retirement_is_closed_before_it_can_escape(monkeypatch):
    raw, peer = socket.socketpair()
    wrapper = Wrapper(raw)
    channel = bridge.DockerBrowserChannel(raw, wrapper)
    container = Container()
    container.client = SimpleNamespace(api=object())
    sandbox = DockerSandbox(image="not-started")
    sandbox._container = container
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def open_channel(_api, _id):
        entered.set()
        await proceed.wait()
        return channel

    monkeypatch.setattr(sandbox, "is_alive", lambda: True)
    monkeypatch.setattr(bridge.DockerBrowserChannel, "open", open_channel)
    opening = asyncio.create_task(sandbox.open_browser_channel())
    try:
        await entered.wait()
        sandbox.stop()
        proceed.set()
        with pytest.raises(bridge.BrowserChannelError, match="retired"):
            await opening
        assert wrapper.closed == 1
    finally:
        proceed.set()
        await asyncio.gather(opening, return_exceptions=True)
        channel.close()
        peer.close()


@pytest.mark.parametrize("purge", [False, True])
def test_checked_retirement_requires_authoritative_stopped_or_gone_state(purge):
    sandbox = DockerSandbox(image="not-started")
    sandbox.stop_confirmed(purge=purge)
    container = Container()
    sandbox._container = container
    sandbox.stop_confirmed(purge=purge)
    assert sandbox._container is None
    assert container.removed if purge else container.stopped


@pytest.mark.parametrize("failure", [APIError("daemon unavailable"), OSError("lost response")])
def test_failed_checked_stop_keeps_exact_container_for_retry(failure):
    sandbox = DockerSandbox(image="not-started")
    container = Container()
    container.failure = failure
    sandbox._container = container
    with pytest.raises(SandboxUnavailableError, match="confirm"):
        sandbox.stop_confirmed()
    assert sandbox._container is container
    container.failure = None
    sandbox.stop_confirmed()
    assert sandbox._container is None


@pytest.mark.parametrize("purge", [False, True])
def test_quiet_provider_refusal_does_not_count_as_retirement(purge):
    sandbox = DockerSandbox(image="not-started")
    container = Container()
    container.stop = lambda: None
    container.remove = lambda *, force: None
    sandbox._container = container
    try:
        with pytest.raises(SandboxUnavailableError, match="confirmed"):
            sandbox.stop_confirmed(purge=purge)
        assert sandbox._container is container
    finally:
        sandbox._container = None


@pytest.mark.parametrize("idle", [False, True])
async def test_confirmed_manager_preserves_failed_provider_identity_until_retry(idle):
    import time

    from tests.test_session import MockDockerSandbox

    class ConfirmedSandbox(MockDockerSandbox):
        failed = True

        def stop_confirmed(self, *, purge=False):
            assert not purge
            if self.failed:
                raise SandboxUnavailableError("stop was refused")
            self.stop()

    sandbox = ConfirmedSandbox(session_id="one")
    sandbox._last_activity = time.time() - 100
    manager = SessionManager(sandbox_factory=lambda _id: sandbox, confirm_stop=True)
    await manager.get_or_create("one")
    with pytest.raises(SandboxUnavailableError):
        if idle:
            await manager.cleanup_idle(max_idle=1)
        else:
            await manager.release("one")
    assert manager.sessions["one"] is sandbox
    sandbox.failed = False
    if idle:
        assert await manager.cleanup_idle(max_idle=1) == 1
    else:
        assert await manager.release("one")
    assert manager.sessions == {}


def test_container_retirement_attempts_other_channels_if_one_close_fails(caplog):
    class Channel:
        def __init__(self, *, fails):
            self.fails = fails
            self.closed = False

        def close(self):
            if self.fails:
                raise OSError("private payload")
            self.closed = True

    sandbox = DockerSandbox(image="not-started")
    good, bad = Channel(fails=False), Channel(fails=True)
    sandbox._browser_channels.add(good)
    sandbox._browser_channels.add(bad)
    container = Container()
    sandbox._container = container
    sandbox.stop()
    assert good.closed
    assert container.stopped
    assert "transport retirement failed" in caplog.text
    assert "private payload" not in caplog.text
