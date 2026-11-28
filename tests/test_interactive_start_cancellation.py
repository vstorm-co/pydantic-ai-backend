"""Cancelled first admission must retire its unpublished Browser runtime."""

from __future__ import annotations

import asyncio

import pytest

from pydantic_ai_backends.remote import wire
from pydantic_ai_backends.remote._interactive import EnvironmentStart, InteractiveGateway
from tests.test_interactive_gateway import SERVICE_TOKEN, Harness


async def test_cancelled_first_browser_start_releases_admission_and_stops_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    service = harness.app.state.service
    gateway = InteractiveGateway(service)
    await service.open_session(wire.CreateSessionRequest(session_id="one"))
    sandbox = harness.built["one"]
    entered = asyncio.Event()

    async def blocked_start(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sandbox, "run_command", blocked_start)
    task = asyncio.create_task(gateway.start("one", "browser", EnvironmentStart(), SERVICE_TOKEN))
    try:
        await entered.wait()
        assert service._inflight["one"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sandbox.stopped == 1
        assert "one" not in gateway.resources
        assert not service._inflight
        assert not gateway.registry._entries
        assert service.manager.session_count == 0
    finally:
        task.cancel()
        await gateway.registry.shutdown()
        await service.shutdown()
