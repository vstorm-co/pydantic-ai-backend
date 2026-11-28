"""Interactive service routes enforce session authority without reviving lost DOM."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pydantic_ai_backends.backends.docker._browser_bridge import BrowserChannelError
from pydantic_ai_backends.protocol import SandboxUnavailableError
from pydantic_ai_backends.remote import wire
from pydantic_ai_backends.remote.server import SandboxdConfig, SandboxRuntime, create_app
from tests.test_remote_sandbox import FakeSandbox

SERVICE_TOKEN = "owned-test-service-token"
SERVICE = {wire.TOKEN_HEADER: SERVICE_TOKEN}


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


class Channel:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.replies: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False
        self.fail_send = False

    async def send(self, message: str) -> None:
        if self.fail_send:
            raise BrowserChannelError("Channel lost")
        parsed = json.loads(message)
        self.sent.append(parsed)
        await self.replies.put(json.dumps({"id": parsed.get("id"), "result": {}}))

    async def receive(self) -> str:
        return await self.replies.get()

    async def aclose(self) -> None:
        self.closed = True


class BrowserSandbox(FakeSandbox):
    def __init__(self, session_id: str, runtime: SandboxRuntime) -> None:
        super().__init__(session_id, runtime)
        self.channels: list[Channel] = []
        self.touched = 0

    def touch(self) -> None:
        self.touched += 1

    async def open_browser_channel(self) -> Channel:
        if not self.alive:
            raise SandboxUnavailableError("The exact browser container is gone")
        channel = Channel()
        self.channels.append(channel)
        return channel


class Harness:
    def __init__(self) -> None:
        self.built: dict[str, BrowserSandbox] = {}
        self.config = SandboxdConfig(
            token=SERVICE_TOKEN,
            runtimes={"browser": "browser:test", "desktop": "desktop:test"},
            default_runtime="browser",
            cleanup_interval=1000,
        )
        self.app = create_app(self.config, sandbox_builder=self.build)
        self.clock = Clock()
        self.app.state.interactive._now = self.clock

    def build(self, session_id: str, runtime: SandboxRuntime) -> BrowserSandbox:
        sandbox = BrowserSandbox(session_id, runtime)
        self.built[session_id] = sandbox
        return sandbox


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def client(harness: Harness) -> Iterator[TestClient]:
    with TestClient(harness.app) as client:
        yield client


def opened(client: TestClient, session_id: str = "one", runtime: str = "browser") -> dict[str, str]:
    response = client.post(
        "/sessions", headers=SERVICE, json={"session_id": session_id, "runtime": runtime}
    )
    assert response.status_code == 200, response.text
    return {wire.TOKEN_HEADER: response.json()["token"]}


def start(client: TestClient, headers: dict[str, str], kind: str = "browser", **body: int):
    return client.post(f"/sessions/one/environments/{kind}", headers=headers, json=body)


def test_auth_and_cross_session_tokens_cannot_discover_or_drive_environments(client, harness):
    first = opened(client)
    second = opened(client, "two")
    for method, path, body in (
        ("POST", "/sessions/one/environments/browser", {}),
        ("GET", "/sessions/one/environments/browser", None),
        ("DELETE", "/sessions/one/environments/browser", None),
        ("POST", "/sessions/one/environments/browser/lease", {"owner": "wrong"}),
    ):
        assert client.request(method, path, headers=second, json=body).status_code == 401
        assert client.request(method, path, json=body).status_code == 401
    with (
        pytest.raises(WebSocketDisconnect) as failure,
        client.websocket_connect("/sessions/one/browser/cdp", headers=second),
    ):
        pytest.fail("Foreign token admitted")
    assert failure.value.code == 1008
    assert harness.built["one"].commands == []
    assert start(client, first).status_code == 200


def test_browser_requires_dedicated_runtime_but_desktop_reuses_workspace(client, harness):
    headers = opened(client, runtime="desktop")
    assert start(client, headers).status_code == 409
    assert start(client, headers, "desktop").status_code == 200
    assert client.delete("/sessions/one/environments/desktop", headers=headers).status_code == 204
    assert harness.built["one"].stopped == 0


def test_start_is_idempotent_and_policy_changes_require_explicit_end(client, harness):
    headers = opened(client)
    assert start(client, headers).status_code == 200
    assert start(client, headers).status_code == 200
    assert len(harness.built["one"].commands) == 1
    assert start(client, headers, idle_seconds=1).status_code == 409
    assert start(client, headers, idle_seconds=86401).status_code == 422
    assert client.delete("/sessions/one/environments/browser", headers=headers).status_code == 204
    assert harness.built["one"].stopped == 1


def test_failed_browser_provision_closes_owned_resource_and_publishes_no_live_handle(
    client, harness
):
    headers = opened(client)
    harness.built["one"].exit_code = 1
    assert start(client, headers).status_code == 503
    assert harness.built["one"].stopped == 1
    assert harness.app.state.interactive._entries == {}


def test_inspect_absent_and_expired_lease_refuse_without_starting_resources(client, harness):
    headers = opened(client)
    path = "/sessions/one/environments/browser"
    assert client.get(path, headers=headers).status_code == 410
    assert client.post(path + "/lease", headers=headers, json={"owner": "qa"}).status_code == 410
    assert harness.built["one"].commands == []
    assert start(client, headers, idle_seconds=1).status_code == 200
    harness.clock.now = 2
    assert client.get(path, headers=headers).status_code == 410
    assert client.post(path + "/lease", headers=headers, json={"owner": "qa"}).status_code == 410


def test_lease_is_exclusive_and_tokens_are_not_in_status(client):
    headers = opened(client)
    start(client, headers)
    path = "/sessions/one/environments/browser"
    lease = client.post(path + "/lease", headers=headers, json={"owner": "qa"})
    assert lease.status_code == 200
    assert lease.json()["token"]
    assert client.post(path + "/lease", headers=headers, json={"owner": "other"}).status_code == 409
    assert "token" not in client.get(path, headers=headers).json()


def test_cdp_owner_is_service_container_and_disconnect_preserves_contexts(client, harness):
    headers = opened(client)
    start(client, headers)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json(
            {"id": 1, "method": "Target.createBrowserContext", "params": {"disposeOnDetach": True}}
        )
        assert websocket.receive_json()["id"] == 1
        websocket.close()
        assert websocket.receive()["type"] == "websocket.close"
    sandbox = harness.built["one"]
    assert sandbox.channels[0].sent[0]["params"] == {"disposeOnDetach": False}
    assert sandbox.channels[0].closed
    assert sandbox.stopped == 0
    assert (
        client.get("/sessions/one/environments/browser", headers=headers).json()["state"] == "ready"
    )
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json({"id": 2, "method": "Target.getTargets"})
        assert websocket.receive_json()["id"] == 2
        websocket.close()
        assert websocket.receive()["type"] == "websocket.close"
    assert sandbox.started == 1


def test_passive_cdp_does_not_extend_idle_but_input_does(client, harness):
    headers = opened(client)
    start(client, headers, idle_seconds=10)
    initial_touches = harness.built["one"].touched
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        harness.clock.now = 4
        websocket.send_json({"id": 1, "method": "Page.captureScreenshot"})
        assert websocket.receive_json()["id"] == 1
        assert harness.app.state.interactive.inspect("one:browser").expires_at == 10
        websocket.send_json({"id": 2, "method": "Input.insertText", "params": {"text": "qa"}})
        assert websocket.receive_json()["id"] == 2
        assert harness.app.state.interactive.inspect("one:browser").expires_at == 14
        websocket.close()
        assert websocket.receive()["type"] == "websocket.close"
    assert harness.built["one"].touched == initial_touches + 1


def test_dead_browser_attach_does_not_heal_or_replace_container(client, harness):
    headers = opened(client)
    start(client, headers)
    sandbox = harness.built["one"]
    sandbox.alive = False
    with (
        pytest.raises(WebSocketDisconnect) as failure,
        client.websocket_connect("/sessions/one/browser/cdp", headers=headers),
    ):
        pytest.fail("Dead container admitted")
    assert failure.value.code == 1008
    assert sandbox.started == 1
    assert len(sandbox.commands) == 1


def test_expired_cdp_refuses_at_handshake_and_after_receiving_input(client, harness):
    headers = opened(client)
    start(client, headers, idle_seconds=10)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        harness.clock.now = 11
        websocket.send_json({"id": 1, "method": "Input.insertText", "params": {"text": "stale"}})
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()
    assert harness.built["one"].channels[0].sent == []
    with (
        pytest.raises(WebSocketDisconnect) as failure,
        client.websocket_connect("/sessions/one/browser/cdp", headers=headers),
    ):
        pytest.fail("Expired environment admitted")
    assert failure.value.code == 1008


def test_environment_supervisor_closed_start_refuses_without_new_commands(client, harness):
    headers = opened(client)
    client.portal.call(harness.app.state.interactive.shutdown)
    assert start(client, headers).status_code == 410
    assert harness.built["one"].commands == []


def test_desktop_start_failure_keeps_shared_workspace(client, harness):
    headers = opened(client, runtime="desktop")
    harness.built["one"].exit_code = 1
    assert start(client, headers, "desktop").status_code == 503
    assert harness.built["one"].stopped == 0


def test_lease_renew_release_refusals_and_heartbeats_do_not_renew_idle(client, harness):
    headers = opened(client)
    path = "/sessions/one/environments/browser/lease"
    for action in ("renew", "release"):
        assert (
            client.post(path + "/" + action, headers=headers, json={"token": "bad"}).status_code
            == 410
        )
    start(client, headers, idle_seconds=10)
    lease = client.post(path, headers=headers, json={"owner": "qa"}).json()
    harness.clock.now = 2
    renewed = client.post(path + "/renew", headers=headers, json={"token": lease["token"]})
    assert renewed.status_code == 200
    assert harness.app.state.interactive.inspect("one:browser").expires_at == 10
    assert client.post(path + "/renew", headers=headers, json={"token": "wrong"}).status_code == 409
    assert (
        client.post(path + "/release", headers=headers, json={"token": "wrong"}).status_code == 409
    )
    assert (
        client.post(path + "/release", headers=headers, json={"token": lease["token"]}).status_code
        == 204
    )


def test_cdp_can_use_an_explicit_current_lease(client, harness):
    headers = opened(client)
    start(client, headers)
    lease = client.post(
        "/sessions/one/environments/browser/lease", headers=headers, json={"owner": "qa"}
    ).json()
    attached_headers = {**headers, "X-Environment-Lease": lease["token"]}
    with client.websocket_connect(
        "/sessions/one/browser/cdp", headers=attached_headers
    ) as websocket:
        websocket.send_json({"id": 1, "method": "Input.insertText", "params": {"text": "owned"}})
        assert websocket.receive_json()["id"] == 1
        websocket.close()
        assert websocket.receive()["type"] == "websocket.close"


@pytest.mark.parametrize("message", ["[]", '{"method":"Target.createBrowserContext","params":[]}'])
def test_invalid_cdp_payloads_close_the_channel_without_forwarding(client, harness, message):
    headers = opened(client)
    start(client, headers)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_text(message)
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()
    assert harness.built["one"].channels[0].sent == []


def test_cdp_with_no_method_is_not_classified_as_user_activity(client, harness):
    headers = opened(client)
    start(client, headers)
    initial_touches = harness.built["one"].touched
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json({"id": 1})
        assert websocket.receive_json()["id"] == 1
        websocket.close()
        assert websocket.receive()["type"] == "websocket.close"
    assert harness.built["one"].touched == initial_touches


def test_outgoing_frame_cannot_escape_after_environment_expiry(client, harness):
    headers = opened(client)
    start(client, headers, idle_seconds=10)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json({"id": 1, "method": "Target.getTargets"})
        assert websocket.receive_json()["id"] == 1
        channel = harness.built["one"].channels[0]
        harness.clock.now = 11
        client.portal.call(channel.replies.put, '{"id":2,"result":{"secret":"stale"}}')
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_a_runtime_without_private_browser_channels_is_refused(client, harness, monkeypatch):
    def build(session_id: str, runtime: SandboxRuntime) -> FakeSandbox:
        sandbox = FakeSandbox(session_id, runtime)
        harness.built[session_id] = sandbox
        return sandbox

    monkeypatch.setattr(harness.app.state.service, "_build_sandbox", build)
    headers = opened(client)
    start(client, headers)
    with (
        pytest.raises(WebSocketDisconnect) as failure,
        client.websocket_connect("/sessions/one/browser/cdp", headers=headers),
    ):
        pytest.fail("Unsupported browser channel admitted")
    assert failure.value.code == 1008


async def test_resource_disposal_preserves_replacement_and_retries_nonabsence_errors(
    harness, monkeypatch
):
    from fastapi import HTTPException

    from pydantic_ai_backends.remote._interactive import InteractiveResource

    service = harness.app.state.service
    original = object()
    resource = InteractiveResource(service, "one", original, "browser")
    monkeypatch.setattr(service, "peek", lambda _session_id: object())
    await resource.aclose()

    def absent(_session_id: str):
        raise HTTPException(404, "gone")

    monkeypatch.setattr(service, "peek", absent)
    await resource.aclose()

    def unavailable(_session_id: str):
        raise HTTPException(503, "temporarily unavailable")

    monkeypatch.setattr(service, "peek", unavailable)
    with pytest.raises(HTTPException) as failure:
        await resource.aclose()
    assert failure.value.status_code == 503


async def test_runtime_without_command_contract_is_refused_before_start(harness, monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from pydantic_ai_backends.remote._interactive import EnvironmentStart, InteractiveGateway

    service = harness.app.state.service
    monkeypatch.setattr(service, "sandbox", AsyncMock(return_value=object()))
    with pytest.raises(HTTPException) as failure:
        await InteractiveGateway(service).open_resource("one", "browser", EnvironmentStart())
    assert failure.value.status_code == 501


async def test_maintenance_retries_cleanup_and_logs_failure_without_payload(caplog, monkeypatch):
    from pydantic_ai_backends.remote import _interactive
    from pydantic_ai_backends.remote._environments import ManagedEnvironmentRegistry

    class FailedResource:
        async def checkpoint_due(self) -> None:
            pass

        async def aclose(self) -> None:
            raise OSError("sensitive-provider-payload")

    registry = ManagedEnvironmentRegistry[FailedResource]()

    async def provision() -> FailedResource:
        return FailedResource()

    await registry.ensure("own", kind="browser", open_resource=provision)
    registry.mark_gone("own")
    calls = 0

    async def interval(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(_interactive.asyncio, "sleep", interval)
    with pytest.raises(asyncio.CancelledError):
        await _interactive.maintain_interactive(registry)
    assert "own" in caplog.text
    assert "sensitive-provider-payload" not in caplog.text


async def test_normally_finished_forwarder_still_closes_channel_and_joins_peer(
    harness, monkeypatch
):
    from unittest.mock import AsyncMock

    from pydantic_ai_backends.remote._interactive import EnvironmentStart, InteractiveGateway

    service = harness.app.state.service
    admitted = await service.open_session(wire.CreateSessionRequest(session_id="one"))
    gateway = InteractiveGateway(service)
    await gateway.start("one", "browser", EnvironmentStart(), admitted.token)

    class Socket:
        headers = {wire.TOKEN_HEADER: admitted.token}
        accepted = False

        async def accept(self) -> None:
            self.accepted = True

        async def close(self, code: int) -> None:
            pytest.fail(f"Successful forwarder should only detach: {code}")

    socket = Socket()
    monkeypatch.setattr(gateway, "incoming", AsyncMock())
    try:
        await gateway.browser_socket(socket, "one")
        assert socket.accepted
        assert harness.built["one"].channels[0].closed
        assert harness.built["one"].stopped == 0
    finally:
        await gateway.registry.shutdown()
        await service.shutdown()


async def test_failed_provision_cleanup_cannot_replace_original_failure(
    harness, monkeypatch, caplog
):
    from unittest.mock import AsyncMock

    from pydantic_ai_backends.remote._interactive import EnvironmentStart, InteractiveGateway

    service = harness.app.state.service
    await service.open_session(wire.CreateSessionRequest(session_id="one"))
    sandbox = harness.built["one"]
    original = RuntimeError("Original provisioning error")
    sandbox.run_error = original
    close = service.close_session
    monkeypatch.setattr(
        service, "close_session", AsyncMock(side_effect=OSError("Cleanup unavailable"))
    )
    try:
        with pytest.raises(RuntimeError, match="Original provisioning error"):
            await InteractiveGateway(service).open_resource("one", "browser", EnvironmentStart())
        assert "cleanup" in caplog.text.lower()
        assert "one" in caplog.text
    finally:
        monkeypatch.setattr(service, "close_session", close)
        await service.shutdown()


def test_reusing_workspace_name_cannot_make_old_environment_describe_new_container(client, harness):
    first = opened(client)
    assert start(client, first).status_code == 200
    original = harness.built["one"]
    assert client.delete("/sessions/one", headers=first).status_code == 204
    replacement_headers = opened(client)
    replacement = harness.built["one"]
    assert replacement is not original
    response = client.get("/sessions/one/environments/browser", headers=replacement_headers)
    assert response.status_code == 410 or response.json()["state"] != "ready"
    assert replacement.commands == []
    assert replacement.stopped == 0


async def test_original_identity_check_distinguishes_gone_from_transient_probe_failure(
    harness, monkeypatch
):
    from fastapi import HTTPException

    from pydantic_ai_backends.remote._environments import EnvironmentUnavailableError
    from pydantic_ai_backends.remote._interactive import EnvironmentStart, InteractiveGateway

    service = harness.app.state.service
    admitted = await service.open_session(wire.CreateSessionRequest(session_id="one"))
    gateway = InteractiveGateway(service)
    await gateway.start("one", "browser", EnvironmentStart(), admitted.token)
    peek = service.peek

    def temporarily_unavailable(_session_id: str):
        raise HTTPException(503, "temporarily unavailable")

    monkeypatch.setattr(service, "peek", temporarily_unavailable)
    try:
        with pytest.raises(HTTPException) as failure:
            await gateway._original("one", "browser")
        assert failure.value.status_code == 503
        assert gateway.registry.inspect("one:browser").state == "ready"

        def gone(_session_id: str):
            raise HTTPException(404, "gone")

        monkeypatch.setattr(service, "peek", gone)
        with pytest.raises(EnvironmentUnavailableError, match="original environment is gone"):
            await gateway._original("one", "browser")
        assert gateway.registry.inspect("one:browser").state == "expired"
    finally:
        monkeypatch.setattr(service, "peek", peek)
        await gateway.registry.shutdown()
        await service.shutdown()


def test_reused_environment_start_and_control_refuse_dead_exact_container(client, harness):
    headers = opened(client)
    start(client, headers)
    lease = client.post(
        "/sessions/one/environments/browser/lease", headers=headers, json={"owner": "qa"}
    ).json()
    sandbox = harness.built["one"]
    sandbox.alive = False
    assert start(client, headers).status_code == 410
    assert (
        client.post(
            "/sessions/one/environments/browser/lease/renew",
            headers=headers,
            json={"token": lease["token"]},
        ).status_code
        == 410
    )
    assert sandbox.started == 1


def test_failed_channel_open_closes_websocket_without_touching_container(
    client, harness, monkeypatch
):
    from unittest.mock import AsyncMock

    headers = opened(client)
    start(client, headers)
    sandbox = harness.built["one"]
    monkeypatch.setattr(
        sandbox, "open_browser_channel", AsyncMock(side_effect=BrowserChannelError("Unavailable"))
    )
    with (
        client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket,
        pytest.raises(WebSocketDisconnect),
    ):
        websocket.receive_json()
    assert sandbox.stopped == 0


async def test_failed_provision_cleanup_timeout_preserves_original_failure(
    harness, monkeypatch, caplog
):
    from unittest.mock import AsyncMock

    import anyio

    from pydantic_ai_backends.remote import _interactive

    service = harness.app.state.service
    await service.open_session(wire.CreateSessionRequest(session_id="one"))
    harness.built["one"].run_error = RuntimeError("Original failure")
    close = service.close_session
    original_scope = anyio.move_on_after

    async def hung_close(_session_id: str) -> None:
        await asyncio.Event().wait()

    def short_scope(_seconds: float, *, shield: bool):
        return original_scope(0.001, shield=shield)

    monkeypatch.setattr(service, "close_session", AsyncMock(side_effect=hung_close))
    monkeypatch.setattr(_interactive.anyio, "move_on_after", short_scope)
    try:
        with pytest.raises(RuntimeError, match="Original failure"):
            await _interactive.InteractiveGateway(service).open_resource(
                "one", "browser", _interactive.EnvironmentStart()
            )
        assert "cleanup timed out" in caplog.text
    finally:
        monkeypatch.setattr(service, "close_session", close)
        await service.shutdown()


@pytest.mark.parametrize("direction", ["incoming", "outgoing"])
async def test_a_channel_cannot_follow_a_newer_registry_resource(harness, monkeypatch, direction):
    from unittest.mock import AsyncMock

    from pydantic_ai_backends.remote._environments import EnvironmentUnavailableError
    from pydantic_ai_backends.remote._interactive import (
        InteractiveGateway,
        InteractiveResource,
        _BrowserRelay,
    )

    service = harness.app.state.service
    admitted = await service.open_session(wire.CreateSessionRequest(session_id="one"))
    gateway = InteractiveGateway(service)
    sandbox = harness.built["one"]
    replacement = InteractiveResource(service, "one", object(), "browser")
    monkeypatch.setattr(gateway, "_original", AsyncMock(return_value=replacement))

    class Socket:
        headers = {wire.TOKEN_HEADER: admitted.token}

        async def receive_text(self) -> str:
            return '{"id":1,"method":"Target.getTargets"}'

        async def send_text(self, _message: str) -> None:
            pytest.fail("An old channel leaked a frame")

    channel = Channel()
    await channel.replies.put('{"id":1,"result":{}}')
    relay = _BrowserRelay(gateway, "one", channel)
    try:
        with pytest.raises(EnvironmentUnavailableError, match="older container"):
            await getattr(gateway, direction)(Socket(), "one", sandbox, relay)
        assert channel.sent == []
    finally:
        await service.shutdown()


def test_control_remains_held_until_browser_acknowledges_the_input(client, harness, monkeypatch):
    """A socket write is not evidence that Chromium has finished the input."""
    headers = opened(client)
    start(client, headers)
    sandbox = harness.built["one"]
    submitted = threading.Event()
    channel = Channel()

    async def delayed_send(message: str) -> None:
        parsed = json.loads(message)
        channel.sent.append(parsed)
        submitted.set()

    async def channel_for_test() -> Channel:
        return channel

    monkeypatch.setattr(channel, "send", delayed_send)
    monkeypatch.setattr(sandbox, "open_browser_channel", channel_for_test)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json({"id": 1, "method": "Input.insertText", "params": {"text": "owned"}})
        assert submitted.wait(1)
        response = client.post(
            "/sessions/one/environments/browser/lease", headers=headers, json={"owner": "other"}
        )
        assert response.status_code == 409
        client.portal.call(channel.replies.put, '{"id":1,"result":{}}')
        assert websocket.receive_json()["id"] == 1
        websocket.close()
        assert websocket.receive()["type"] == "websocket.close"


def test_intercepted_navigation_can_continue_while_its_control_lease_is_held(
    client, harness, monkeypatch
):
    headers = opened(client)
    start(client, headers)
    sandbox = harness.built["one"]
    channel = Channel()

    async def intercepted_send(message: str) -> None:
        parsed = json.loads(message)
        channel.sent.append(parsed)
        if parsed["method"] == "Page.navigate":
            await channel.replies.put(
                '{"method":"Fetch.requestPaused","params":{"requestId":"request-one"}}'
            )
        else:
            await channel.replies.put('{"id":2,"result":{}}')
            await channel.replies.put('{"id":1,"result":{}}')

    async def channel_for_test() -> Channel:
        return channel

    monkeypatch.setattr(channel, "send", intercepted_send)
    monkeypatch.setattr(sandbox, "open_browser_channel", channel_for_test)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json(
            {"id": 1, "method": "Page.navigate", "params": {"url": "https://example.test"}}
        )
        assert websocket.receive_json()["method"] == "Fetch.requestPaused"
        import time

        sandbox._last_activity = time.time() - 100
        assert client.portal.call(harness.app.state.service.manager.cleanup_idle, 1) == 0
        assert sandbox.stopped == 0
        assert client.get("/sessions/one/environments/browser", headers=headers).status_code == 200
        assert (
            client.post(
                "/sessions/one/environments/browser/lease", headers=headers, json={"owner": "other"}
            ).status_code
            == 409
        )
        websocket.send_json(
            {"id": 2, "method": "Fetch.continueRequest", "params": {"requestId": "request-one"}}
        )
        assert [websocket.receive_json()["id"], websocket.receive_json()["id"]] == [2, 1]
        websocket.close()
        assert websocket.receive()["type"] == "websocket.close"
    assert [message["method"] for message in channel.sent] == [
        "Page.navigate",
        "Fetch.continueRequest",
    ]


@pytest.mark.parametrize("bad_id", [True, "one", None])
def test_cdp_input_requires_unambiguous_command_identity(client, harness, bad_id):
    headers = opened(client)
    start(client, headers)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json({"id": bad_id, "method": "Input.insertText"})
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()
    assert harness.built["one"].channels[0].sent == []


@pytest.mark.parametrize("duplicate", [False, True])
def test_pending_command_limit_and_duplicate_ids_are_refused(
    client, harness, monkeypatch, duplicate
):
    from pydantic_ai_backends.remote import _interactive

    headers = opened(client)
    start(client, headers)
    sandbox = harness.built["one"]
    channel = Channel()

    async def delayed_send(message: str) -> None:
        channel.sent.append(json.loads(message))

    async def channel_for_test() -> Channel:
        return channel

    monkeypatch.setattr(channel, "send", delayed_send)
    monkeypatch.setattr(sandbox, "open_browser_channel", channel_for_test)
    if not duplicate:
        monkeypatch.setattr(_interactive, "MAX_PENDING_COMMANDS", 1)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json({"id": 1, "method": "Page.captureScreenshot"})
        websocket.send_json({"id": 1 if duplicate else 2, "method": "Page.captureScreenshot"})
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()
    assert len(channel.sent) == 1


def test_unacknowledged_command_times_out_without_replay_and_quarantines_resource(
    client, harness, monkeypatch
):
    from pydantic_ai_backends.remote import _interactive

    headers = opened(client)
    start(client, headers)
    sandbox = harness.built["one"]
    channel = Channel()

    async def delayed_send(message: str) -> None:
        channel.sent.append(json.loads(message))

    async def channel_for_test() -> Channel:
        return channel

    monkeypatch.setattr(channel, "send", delayed_send)
    monkeypatch.setattr(sandbox, "open_browser_channel", channel_for_test)
    monkeypatch.setattr(_interactive, "CDP_COMMAND_SECONDS", 0.01)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json({"id": 1, "method": "Input.insertText", "params": {"text": "once"}})
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()
    assert len(channel.sent) == 1
    assert harness.app.state.interactive.inspect("one:browser").state == "unavailable"
    assert client.get("/sessions/one/environments/browser", headers=headers).status_code == 503
    assert start(client, headers).status_code == 503
    with (
        pytest.raises(WebSocketDisconnect) as failure,
        client.websocket_connect("/sessions/one/browser/cdp", headers=headers),
    ):
        pytest.fail("Unavailable environment admitted")
    assert failure.value.code == 1013


def test_disconnect_with_unacknowledged_input_blocks_unsafe_immediate_takeover(
    client, harness, monkeypatch
):
    headers = opened(client)
    start(client, headers)
    sandbox = harness.built["one"]
    submitted = threading.Event()
    channel = Channel()

    async def delayed_send(message: str) -> None:
        channel.sent.append(json.loads(message))
        submitted.set()

    async def channel_for_test() -> Channel:
        return channel

    monkeypatch.setattr(channel, "send", delayed_send)
    monkeypatch.setattr(sandbox, "open_browser_channel", channel_for_test)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json({"id": 1, "method": "Input.insertText", "params": {"text": "once"}})
        assert submitted.wait(1)
        websocket.close()
        assert websocket.receive()["type"] == "websocket.close"
    assert harness.app.state.interactive.inspect("one:browser").state == "unavailable"
    assert (
        client.post(
            "/sessions/one/environments/browser/lease", headers=headers, json={"owner": "new"}
        ).status_code
        == 503
    )
    assert len(channel.sent) == 1


async def test_peer_disappearing_before_server_close_is_a_normal_transport_exit(
    harness, monkeypatch
):
    from unittest.mock import AsyncMock

    from pydantic_ai_backends.remote._interactive import EnvironmentStart, InteractiveGateway

    service = harness.app.state.service
    admitted = await service.open_session(wire.CreateSessionRequest(session_id="one"))
    gateway = InteractiveGateway(service)
    await gateway.start("one", "browser", EnvironmentStart(), admitted.token)

    class Socket:
        headers = {wire.TOKEN_HEADER: admitted.token}

        async def accept(self) -> None:
            return None

        async def close(self, code: int) -> None:
            raise WebSocketDisconnect(1006)

    monkeypatch.setattr(gateway, "incoming", AsyncMock(side_effect=WebSocketDisconnect(1000)))
    try:
        await gateway.browser_socket(Socket(), "one")
        assert harness.built["one"].channels[0].closed
        assert gateway.registry.inspect("one:browser").state == "ready"
    finally:
        await gateway.registry.shutdown()
        await service.shutdown()


def test_stale_environment_end_does_not_destroy_a_reused_workspace_container(client, harness):
    first = opened(client)
    assert start(client, first).status_code == 200
    original = harness.built["one"]
    assert client.delete("/sessions/one", headers=first).status_code == 204
    replacement_headers = opened(client)
    replacement = harness.built["one"]
    assert replacement is not original
    assert (
        client.delete("/sessions/one/environments/browser", headers=replacement_headers).status_code
        == 204
    )
    assert replacement.stopped == 0


def test_end_cleans_named_browser_created_before_gui_admission(client, harness):
    headers = opened(client)
    assert harness.built["one"].commands == []
    assert client.delete("/sessions/one/environments/browser", headers=headers).status_code == 204
    assert harness.built["one"].stopped == 1


def test_browser_end_without_registry_cannot_destroy_a_different_runtime(client, harness):
    headers = opened(client, runtime="desktop")
    assert client.delete("/sessions/one/environments/browser", headers=headers).status_code == 204
    assert harness.built["one"].stopped == 0


async def test_end_preserves_unknown_provider_state_when_workspace_probe_fails(
    harness, monkeypatch
):
    from fastapi import HTTPException

    from pydantic_ai_backends.remote._interactive import InteractiveGateway

    service = harness.app.state.service
    admitted = await service.open_session(wire.CreateSessionRequest(session_id="one"))

    def temporarily_unavailable(_session_id: str):
        raise HTTPException(503, "temporarily unavailable")

    monkeypatch.setattr(service, "peek", temporarily_unavailable)
    try:
        with pytest.raises(HTTPException) as failure:
            await InteractiveGateway(service).end("one", "browser", admitted.token)
        assert failure.value.status_code == 503
        assert harness.built["one"].stopped == 0
    finally:
        await service.shutdown()


async def test_relay_rejects_unknown_response_identity_and_closes_remaining_pending_commands(
    harness,
):
    from pydantic_ai_backends.remote._interactive import InteractiveGateway, _BrowserRelay

    relay = _BrowserRelay(InteractiveGateway(harness.app.state.service), "one", Channel())
    await relay.acknowledge({"id": 999, "result": {}})
    await relay.acknowledge({"id": True, "result": {}})
    await relay.acknowledge({"method": "Fetch.requestPaused"})
    await relay.forward(
        {"id": 1, "method": "Target.getTargets"}, active=False, sandbox=object(), control=None
    )
    assert relay.pending
    await relay.aclose()
    assert relay.pending == {}
    assert relay.failure.cancelled()


def test_effective_environment_policy_uses_operator_limits_and_service_idle(client, harness):
    harness.config.interactive_idle_timeout = 1200
    harness.config.interactive_max_lifetime = 5000
    harness.config.idle_timeout = 1000
    headers = opened(client)
    snapshot = start(client, headers, idle_seconds=3600, lifetime_seconds=86400).json()
    assert snapshot["expires_at"] == 1000
    entry = harness.app.state.interactive._entries["one:browser"]
    assert entry.idle_seconds == 1000
    assert entry.lifetime_seconds == 5000


def test_disabling_container_idle_does_not_disable_operator_environment_deadline(client, harness):
    harness.config.interactive_idle_timeout = 600
    harness.config.idle_timeout = 0
    headers = opened(client)
    assert start(client, headers, idle_seconds=3600).json()["expires_at"] == 600


@pytest.mark.parametrize("unavailable", [False, True])
def test_explicit_inspection_uses_fresh_probe_and_distinguishes_provider_outage(
    client, harness, monkeypatch, unavailable
):
    from pydantic_ai_backends.protocol import SandboxProbeError

    headers = opened(client)
    start(client, headers)
    sandbox = harness.built["one"]
    sandbox.alive = True
    calls = 0

    def fresh_probe() -> bool:
        nonlocal calls
        calls += 1
        if unavailable:
            raise SandboxProbeError("sensitive-daemon-payload")
        return False

    monkeypatch.setattr(sandbox, "is_alive_fresh", fresh_probe, raising=False)
    response = client.get("/sessions/one/environments/browser", headers=headers)
    assert response.status_code == (503 if unavailable else 410)
    assert "sensitive-daemon-payload" not in response.text
    assert calls == 1
    assert harness.app.state.interactive.inspect("one:browser").state == (
        "ready" if unavailable else "expired"
    )


def test_passive_frames_use_cached_liveness_after_fresh_attach(client, harness, monkeypatch):
    headers = opened(client)
    start(client, headers)
    sandbox = harness.built["one"]
    calls = 0

    def fresh_probe() -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(sandbox, "is_alive_fresh", fresh_probe, raising=False)
    with client.websocket_connect("/sessions/one/browser/cdp", headers=headers) as websocket:
        websocket.send_json({"id": 1, "method": "Page.captureScreenshot"})
        assert websocket.receive_json()["id"] == 1
        websocket.close()
        assert websocket.receive()["type"] == "websocket.close"
    assert calls == 1


def test_end_is_idempotent_if_workspace_vanishes_after_authorization(client, harness, monkeypatch):
    from fastapi import HTTPException

    headers = opened(client)

    def absent(_session_id: str):
        raise HTTPException(404, "gone")

    monkeypatch.setattr(harness.app.state.service, "peek", absent)
    assert client.delete("/sessions/one/environments/browser", headers=headers).status_code == 204


async def test_custom_browser_channel_without_touch_still_holds_physical_control(harness):
    from pydantic_ai_backends.remote._interactive import (
        EnvironmentStart,
        InteractiveGateway,
        _BrowserRelay,
    )

    service = harness.app.state.service
    admitted = await service.open_session(wire.CreateSessionRequest(session_id="one"))
    gateway = InteractiveGateway(service)
    await gateway.start("one", "browser", EnvironmentStart(), admitted.token)
    channel = Channel()
    relay = _BrowserRelay(gateway, "one", channel)
    try:
        await relay.forward(
            {"id": 1, "method": "Input.insertText"}, active=True, sandbox=object(), control=None
        )
        response = json.loads(await channel.receive())
        await relay.acknowledge(response)
        await asyncio.gather(*list(relay.tasks))
        assert relay.pending == {}
        assert not relay.failure.done()
    finally:
        await relay.aclose()
        await gateway.registry.shutdown()
        await service.shutdown()


def test_fresh_daemon_probe_deadline_does_not_expire_a_live_resource(client, harness, monkeypatch):
    from pydantic_ai_backends.remote import _interactive

    headers = opened(client)
    start(client, headers)
    sandbox = harness.built["one"]
    release = threading.Event()

    def stalled_probe() -> bool:
        assert release.wait(1)
        return True

    monkeypatch.setattr(sandbox, "is_alive_fresh", stalled_probe, raising=False)
    monkeypatch.setattr(_interactive, "FRESH_PROBE_SECONDS", 0.01)
    try:
        response = client.get("/sessions/one/environments/browser", headers=headers)
        assert response.status_code == 503
        assert harness.app.state.interactive.inspect("one:browser").state == "ready"
    finally:
        release.set()


def test_browser_start_honors_initial_display_and_window_dimensions(client, harness):
    headers = opened(client)
    assert start(client, headers, width=1024, height=640).status_code == 200
    command = harness.built["one"].commands[0][0]
    assert "-screen 0 1024x640x24" in command
    assert "--window-size=1024,640" in command
    assert "__WIDTH__" not in command and "__HEIGHT__" not in command


async def test_active_reply_is_not_forwarded_before_lease_cleanup_finishes(harness, monkeypatch):
    from contextlib import asynccontextmanager

    from pydantic_ai_backends.remote._interactive import (
        EnvironmentStart,
        InteractiveGateway,
        _BrowserRelay,
    )

    service = harness.app.state.service
    admitted = await service.open_session(wire.CreateSessionRequest(session_id="one"))
    gateway = InteractiveGateway(service)
    await gateway.start("one", "browser", EnvironmentStart(), admitted.token)
    sandbox = harness.built["one"]
    channel = Channel()
    relay = _BrowserRelay(gateway, "one", channel)
    cleanup_entered = asyncio.Event()
    finish_cleanup = asyncio.Event()
    original_use = gateway.registry.use

    @asynccontextmanager
    async def slow_cleanup(*args, **kwargs):
        async with original_use(*args, **kwargs) as resource:
            yield resource
            cleanup_entered.set()
            await finish_cleanup.wait()

    class Socket:
        headers = {wire.TOKEN_HEADER: admitted.token}
        sent: list[str] = []

        async def send_text(self, message: str) -> None:
            self.sent.append(message)

    socket = Socket()
    monkeypatch.setattr(gateway.registry, "use", slow_cleanup)
    outgoing = asyncio.create_task(gateway.outgoing(socket, "one", sandbox, relay))
    try:
        await relay.forward(
            {"id": 1, "method": "Input.insertText"}, active=True, sandbox=sandbox, control=None
        )
        await cleanup_entered.wait()
        assert socket.sent == []
        finish_cleanup.set()
        await asyncio.gather(*list(relay.tasks))
        # Let the outgoing task resume from the completed active command.
        while not socket.sent:
            await asyncio.sleep(0)
        assert json.loads(socket.sent[0])["id"] == 1
        assert gateway.registry.acquire("one:browser", "new").owner == "new"
    finally:
        finish_cleanup.set()
        outgoing.cancel()
        await asyncio.gather(outgoing, return_exceptions=True)
        await relay.aclose()
        await gateway.registry.shutdown()
        await service.shutdown()


async def test_channel_detach_failures_are_observable_without_masking_disconnect(
    harness, monkeypatch, caplog
):
    from unittest.mock import AsyncMock

    from pydantic_ai_backends.remote._interactive import EnvironmentStart, InteractiveGateway

    service = harness.app.state.service
    admitted = await service.open_session(wire.CreateSessionRequest(session_id="one"))
    gateway = InteractiveGateway(service)
    await gateway.start("one", "browser", EnvironmentStart(), admitted.token)
    channel = Channel()
    closed = 0

    async def failed_close() -> None:
        nonlocal closed
        closed += 1
        raise OSError("private-cleanup-token")

    async def browser_channel() -> Channel:
        return channel

    class Socket:
        headers = {wire.TOKEN_HEADER: admitted.token}

        async def accept(self) -> None:
            pass

        async def close(self, code: int) -> None:
            pass

    monkeypatch.setattr(channel, "aclose", failed_close)
    monkeypatch.setattr(harness.built["one"], "open_browser_channel", browser_channel)
    monkeypatch.setattr(gateway, "incoming", AsyncMock(side_effect=WebSocketDisconnect(1000)))
    try:
        await gateway.browser_socket(Socket(), "one")
        assert closed == 2
        assert "detach failed" in caplog.text
        assert "private-cleanup-token" not in caplog.text
    finally:
        await gateway.registry.shutdown()
        await service.shutdown()


async def test_channel_detach_deadline_is_bounded_and_logged(harness, monkeypatch, caplog):
    import anyio

    from pydantic_ai_backends.remote import _interactive

    channel = Channel()
    original_scope = anyio.move_on_after

    async def blocked_close() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(channel, "aclose", blocked_close)
    monkeypatch.setattr(
        _interactive.anyio,
        "move_on_after",
        lambda _seconds, *, shield: original_scope(0.001, shield=shield),
    )
    await _interactive.InteractiveGateway(harness.app.state.service)._detach(channel)
    assert "detach timed out" in caplog.text


@pytest.mark.parametrize("send_fails", [False, True])
async def test_already_acknowledged_input_keeps_environment_ready(harness, send_fails):
    from pydantic_ai_backends.remote._interactive import (
        EnvironmentStart,
        InteractiveGateway,
        _BrowserRelay,
    )

    service = harness.app.state.service
    admitted = await service.open_session(wire.CreateSessionRequest(session_id="one"))
    gateway = InteractiveGateway(service)
    await gateway.start("one", "browser", EnvironmentStart(), admitted.token)
    sandbox = harness.built["one"]
    channel = Channel()
    channel.fail_send = send_fails
    relay = _BrowserRelay(gateway, "one", channel)
    acknowledged = asyncio.get_running_loop().create_future()
    acknowledged.set_result(None)
    relay.pending[1] = acknowledged
    try:
        if send_fails:
            with pytest.raises(BrowserChannelError, match="Channel lost"):
                await relay._active(
                    1, acknowledged, '{"id":1,"method":"Input.insertText"}', sandbox, None
                )
        else:
            await relay._active(
                1, acknowledged, '{"id":1,"method":"Input.insertText"}', sandbox, None
            )
        assert gateway.registry.inspect("one:browser").state == "ready"
        assert relay.pending == {}
        lease = gateway.registry.acquire("one:browser", "next-input")
        gateway.registry.release("one:browser", lease.token)
        assert len(channel.sent) == (0 if send_fails else 1)
    finally:
        await relay.aclose()
        await gateway.registry.shutdown()
        await service.shutdown()
