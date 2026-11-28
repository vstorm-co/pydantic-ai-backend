"""Exact interactive reattachment must not trust cached liveness."""

from dataclasses import dataclass

import pytest
from docker.errors import APIError, NotFound

from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox
from pydantic_ai_backends.protocol import SandboxProbeError


@dataclass
class Container:
    status: str = "running"
    failure: Exception | None = None
    calls: int = 0

    def reload(self):
        self.calls += 1
        if self.failure is not None:
            raise self.failure


def test_missing_and_stopped_container_do_not_reappear(monkeypatch):
    sandbox = DockerSandbox(image="not-started")
    assert sandbox.is_alive_fresh() is False
    container = Container(status="exited")
    monkeypatch.setattr(sandbox, "_container", container)
    monkeypatch.setattr(sandbox, "_alive", True)
    assert sandbox.is_alive_fresh() is False
    assert container.calls == 1
    container.failure = NotFound("gone")
    assert sandbox.is_alive_fresh() is False
    assert container.calls == 2


def test_fresh_probe_ignores_cached_true_and_refreshes_cache(monkeypatch):
    sandbox = DockerSandbox(image="not-started")
    container = Container()
    monkeypatch.setattr(sandbox, "_container", container)
    monkeypatch.setattr(sandbox, "_alive", False)
    assert sandbox.is_alive_fresh() is True
    assert sandbox.is_alive() is True
    assert container.calls == 1


@pytest.mark.parametrize("failure", [APIError("temporary failure"), OSError("disconnected")])
def test_provider_failure_is_not_reported_as_gone(monkeypatch, failure):
    sandbox = DockerSandbox(image="not-started")
    container = Container(failure=failure)
    monkeypatch.setattr(sandbox, "_container", container)
    monkeypatch.setattr(sandbox, "_alive", True)
    previous = sandbox._alive_checked_at
    with pytest.raises(SandboxProbeError):
        sandbox.is_alive_fresh()
    assert sandbox._alive is True
    assert sandbox._alive_checked_at == previous


@pytest.mark.parametrize("state", ["missing", "invalid-id", "dead", "replaced", "live"])
async def test_browser_channel_keeps_exact_container_without_waking_or_renewing(monkeypatch, state):
    from unittest.mock import AsyncMock, MagicMock

    from pydantic_ai_backends.backends.docker._browser_bridge import (
        BrowserChannelError,
        DockerBrowserChannel,
    )

    sandbox = DockerSandbox(image="not-started")
    original = MagicMock()
    original.id = 123 if state == "invalid-id" else "owned-container"
    sandbox._container = None if state == "missing" else original
    prior_activity = sandbox._last_activity
    ensure = MagicMock(side_effect=AssertionError("must never create or wake a container"))
    monkeypatch.setattr(sandbox, "_ensure_container", ensure)

    def alive():
        if state == "replaced":
            sandbox._container = MagicMock()
        return state != "dead"

    monkeypatch.setattr(sandbox, "is_alive", alive)
    channel = MagicMock()
    opened = AsyncMock(return_value=channel)
    monkeypatch.setattr(DockerBrowserChannel, "open", opened)
    if state == "live":
        assert await sandbox.open_browser_channel() is channel
        opened.assert_awaited_once_with(original.client.api, "owned-container")
    else:
        with pytest.raises(BrowserChannelError):
            await sandbox.open_browser_channel()
        opened.assert_not_awaited()
    ensure.assert_not_called()
    assert sandbox._last_activity == prior_activity
