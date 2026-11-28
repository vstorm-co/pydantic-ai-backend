"""Persistent profiles survive explicit sleep while old live identities stay gone."""

import base64
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from pydantic_ai_backends.remote._browser_profiles import BrowserProfileError
from pydantic_ai_backends.remote.server import SandboxdConfig, create_app
from tests.test_browser_profiles import archive
from tests.test_interactive_gateway import SERVICE, BrowserSandbox, Clock

KEY = base64.urlsafe_b64encode(b"p" * 32).decode()
OTHER = base64.urlsafe_b64encode(b"q" * 32).decode()
PATH = "/sessions/profile/environments/browser"


class ProfileSandbox(BrowserSandbox):
    def __init__(self, session_id, runtime):
        super().__init__(session_id, runtime)
        self.profile = archive()
        self.restored = None
        self.exports = []
        self.export_failure = False

    def is_alive_fresh(self):
        return self.alive

    async def export_browser_profile(self, *, graceful):
        self.exports.append(graceful)
        if self.export_failure:
            raise BrowserProfileError("checkpoint refused")
        return self.profile

    async def restore_browser_profile(self, payload):
        self.restored = payload
        self.profile = payload


@contextmanager
def setup(tmp_path):
    built = []

    def factory(session_id, runtime):
        sandbox = ProfileSandbox(session_id, runtime)
        built.append(sandbox)
        return sandbox

    config = SandboxdConfig(
        token=SERVICE["X-Sandbox-Token"],
        runtimes={"browser": "test:image"},
        workspace_root=str(tmp_path),
        persist_browser_profiles=True,
        cleanup_interval=1000,
    )
    app = create_app(config, sandbox_builder=factory)
    clock = Clock()
    app.state.interactive._now = clock
    with TestClient(app) as client:
        opened = client.post(
            "/sessions",
            headers=SERVICE,
            json={"session_id": "profile", "runtime": "browser", "tenant": "tenant-one"},
        )
        assert opened.status_code == 200
        headers = {"X-Sandbox-Token": opened.json()["token"]}
        yield client, headers, built, app, clock


def test_sleep_restores_encrypted_profile_into_new_generation(tmp_path):
    with setup(tmp_path) as (client, headers, built, app, clock):
        first = client.post(PATH, headers=headers, json={"profile_key": KEY}).json()
        assert first["persistent"] and not first["profile_restored"]
        old_generation = first["generation"]
        assert old_generation.startswith("profile-")
        with client.websocket_connect("/sessions/profile/browser/cdp", headers=headers) as ws:
            ws.send_json({"id": 1, "method": "Target.createBrowserContext"})
            assert ws.receive_json()["result"]["browserContextId"] == old_generation
            ws.close()
            assert ws.receive()["type"] == "websocket.close"
        assert client.post(PATH + "/suspend", headers=headers).status_code == 204
        assert client.post(PATH + "/suspend", headers=headers).status_code == 204
        assert built[0].exports == [True]
        assert built[0].stopped == 1
        encrypted = (tmp_path / "profile/private/browser-profile.enc").read_bytes()
        assert b"private-login" not in encrypted and KEY.encode() not in encrypted
        assert client.get(PATH, headers=headers).status_code == 410
        second = client.post(PATH, headers=headers, json={"profile_key": KEY}).json()
        assert second["profile_restored"] and second["profile_saved_at"]
        assert second["generation"] != old_generation
        assert len(built) == 2 and built[-1].restored
        with client.websocket_connect("/sessions/profile/browser/cdp", headers=headers) as ws:
            ws.send_json(
                {
                    "id": 2,
                    "method": "Target.createTarget",
                    "params": {"browserContextId": old_generation, "url": "about:blank"},
                }
            )
            assert "error" in ws.receive_json()
            ws.close()
            assert ws.receive()["type"] == "websocket.close"
        assert client.delete(PATH + "/profile", headers=headers).status_code == 204
        assert not (tmp_path / "profile").exists()
        assert app.state.service.manager.session_count == 0


def test_idle_and_external_hibernation_checkpoint_before_stopping(tmp_path):
    with setup(tmp_path) as (client, headers, built, app, clock):
        assert (
            client.post(
                PATH, headers=headers, json={"profile_key": KEY, "idle_seconds": 3}
            ).status_code
            == 200
        )
        clock.now = 4
        assert client.portal.call(app.state.interactive.sweep) == []
        assert built[0].exports == [True] and built[0].stopped == 1
        assert client.post(PATH, headers=headers, json={"profile_key": KEY}).status_code == 200
        client.portal.call(app.state.service.hibernate, "profile")
        assert built[-1].exports == [True]
        # Explicit Start can replace confirmed-lost processes in one request.
        result = client.post(PATH, headers=headers, json={"profile_key": KEY})
        assert result.status_code == 200 and result.json()["profile_restored"]


def test_wrong_key_and_corrupt_checkpoint_never_create_blank_profile(tmp_path):
    with setup(tmp_path) as (client, headers, built, app, clock):
        assert client.post(PATH, headers=headers, json={"profile_key": KEY}).status_code == 200
        assert client.post(PATH, headers=headers, json={"profile_key": OTHER}).status_code == 409
        assert client.post(PATH + "/suspend", headers=headers).status_code == 204
        path = tmp_path / "profile/private/browser-profile.enc"
        original = path.read_bytes()
        assert client.post(PATH, headers=headers, json={"profile_key": OTHER}).status_code == 409
        assert path.read_bytes() == original
        assert built[-1].stopped == 1
        path.write_bytes(b"corrupt")
        result = client.post(PATH, headers=headers, json={"profile_key": KEY})
        assert result.status_code == 409
        assert KEY not in result.text and "corrupt" not in result.text


def test_failed_checkpoint_is_visible_and_retains_runtime_for_retry(tmp_path):
    with setup(tmp_path) as (client, headers, built, app, clock):
        assert client.post(PATH, headers=headers, json={"profile_key": KEY}).status_code == 200
        built[0].export_failure = True
        result = client.post(PATH + "/suspend", headers=headers)
        assert result.status_code == 503 and built[0].stopped == 0
        assert not (tmp_path / "profile/private/browser-profile.enc").exists()
        built[0].export_failure = False
        assert client.post(PATH + "/suspend", headers=headers).status_code == 204
        assert (tmp_path / "profile/private/browser-profile.enc").exists()


def test_live_context_uses_operator_proxy_and_generation_on_replies(tmp_path):
    with setup(tmp_path) as (client, headers, built, app, clock):
        state = client.post(PATH, headers=headers, json={"profile_key": KEY}).json()
        with client.websocket_connect("/sessions/profile/browser/cdp", headers=headers) as ws:
            ws.send_json({"id": 1, "method": "Target.getBrowserContexts"})
            assert ws.receive_json()["result"]["browserContextIds"] == [state["generation"]]
            ws.send_json(
                {
                    "id": 2,
                    "method": "Target.createBrowserContext",
                    "params": {"proxyServer": "untrusted"},
                }
            )
            assert "error" in ws.receive_json()
            ws.send_json(
                {
                    "id": 3,
                    "method": "Target.createTarget",
                    "params": {"browserContextId": state["generation"], "url": "about:blank"},
                }
            )
            assert ws.receive_json()["id"] == 3
            ws.close()
            assert ws.receive()["type"] == "websocket.close"
        sent = built[0].channels[-1].sent[-1]
        assert "browserContextId" not in sent["params"]
        assert KEY not in json.dumps(built[0].commands)


def test_purge_waits_for_checkpoint_and_never_recreates_deleted_ciphertext(tmp_path):
    import asyncio
    from functools import partial

    with setup(tmp_path) as (client, headers, built, app, clock):
        assert client.post(PATH, headers=headers, json={"profile_key": KEY}).status_code == 200
        resource = client.portal.call(app.state.interactive.resource, "profile:browser")
        captured = asyncio.Event()
        release_capture = asyncio.Event()
        purge_entered = asyncio.Event()

        async def delayed_capture(*, graceful):
            captured.set()
            await release_capture.wait()
            return archive()

        async def purge():
            purge_entered.set()
            await resource.owner.purge_profile("profile", "browser", headers["X-Sandbox-Token"])

        built[0].export_browser_profile = delayed_capture
        capturing = client.portal.start_task_soon(partial(resource.checkpoint, graceful=False))
        client.portal.call(captured.wait)
        purging = client.portal.start_task_soon(purge)
        client.portal.call(purge_entered.wait)
        assert not purging.done()
        client.portal.call(release_capture.set)
        capturing.result(timeout=5)
        purging.result(timeout=5)
        assert not (tmp_path / "profile").exists()
        client.portal.call(partial(resource.checkpoint, graceful=False))
        assert not (tmp_path / "profile").exists()


def test_operator_can_purge_orphan_without_starting_a_container(tmp_path):
    with setup(tmp_path) as (client, headers, built, app, clock):
        assert client.post(PATH, headers=headers, json={"profile_key": KEY}).status_code == 200
        assert client.post(PATH + "/suspend", headers=headers).status_code == 204
        assert client.delete("/sessions/profile", headers=headers).status_code == 204
        count = len(built)
        assert client.delete(PATH + "/profile", headers=headers).status_code == 401
        assert client.delete(PATH + "/profile", headers=SERVICE).status_code == 204
        assert len(built) == count and not (tmp_path / "profile").exists()


@pytest.mark.parametrize(
    "context", ["native", None, "fallback", "malformed", "invalid-id", "nonobject", "empty"]
)
async def test_default_context_discovery_accepts_provider_versions_and_refuses_bad_replies(context):
    import asyncio

    from fastapi import HTTPException

    from pydantic_ai_backends.backends.docker._browser_bridge import BrowserChannelError
    from pydantic_ai_backends.remote._interactive import InteractiveGateway
    from tests.test_interactive_gateway import Harness

    class Discovery:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.closed = False

        async def send(self, message):
            request = json.loads(message)
            await self.queue.put(json.dumps({"method": "Target.event"}))
            if request["id"] == 1:
                result = (
                    []
                    if context == "malformed"
                    else {
                        "defaultBrowserContextId": 12
                        if context == "invalid-id"
                        else ("native" if context == "native" else None)
                    }
                )
            elif context == "nonobject":
                result = []
            else:
                result = (
                    {
                        "targetInfos": [
                            None,
                            {"type": "worker"},
                            {"type": "page", "browserContextId": None},
                            {"type": "page", "browserContextId": "fallback"},
                        ]
                    }
                    if context == "fallback"
                    else {"targetInfos": []}
                    if context == "empty"
                    else {}
                )
            await self.queue.put(json.dumps({"id": request["id"], "result": result}))

        async def receive(self):
            return await self.queue.get()

        async def aclose(self):
            self.closed = True

    class Provider:
        async def open_browser_channel(self):
            return channel

    channel = Discovery()
    gateway = InteractiveGateway(Harness().app.state.service)
    if context in ("malformed", "invalid-id"):
        with pytest.raises(BrowserChannelError):
            await gateway._profile_context(Provider())
    else:
        profile = await gateway._profile_context(Provider())
        assert profile.default_context_id == (
            context if context in ("native", "fallback") else None
        )
    assert channel.closed
    with pytest.raises(HTTPException) as error:
        await gateway._profile_context(object())
    assert error.value.status_code == 501


def test_profile_start_refuses_invalid_policy_and_explicit_start_retires_expired_generation(
    tmp_path,
):
    with setup(tmp_path) as (client, headers, built, app, clock):
        assert (
            client.post(PATH, headers=headers, json={"profile_key": "bad-key"}).status_code == 422
        )
        first = client.post(
            PATH, headers=headers, json={"profile_key": KEY, "idle_seconds": 3}
        ).json()
        assert client.post(PATH, headers=headers, json={}).status_code == 409
        assert (
            client.post(
                "/sessions/profile/environments/desktop", headers=headers, json={}
            ).status_code
            == 409
        )
        clock.now = 4
        second = client.post(PATH, headers=headers, json={"profile_key": KEY, "idle_seconds": 3})
        assert second.status_code == 200
        assert second.json()["generation"] != first["generation"]
        assert second.json()["profile_restored"]


def test_missing_workspace_or_tenant_never_admits_a_durable_profile(tmp_path):
    from tests.test_interactive_gateway import Harness, opened

    harness = Harness()
    with TestClient(harness.app) as client:
        headers = opened(client)
        assert (
            client.post(
                "/sessions/one/environments/browser", headers=headers, json={"profile_key": KEY}
            ).status_code
            == 409
        )
    harness = Harness()
    harness.config.workspace_root = str(tmp_path)
    harness.config.persist_browser_profiles = True
    with TestClient(harness.app) as client:
        headers = opened(client)
        assert (
            client.post(
                "/sessions/one/environments/browser", headers=headers, json={"profile_key": KEY}
            ).status_code
            == 409
        )


async def test_completed_or_dead_profile_does_not_overwrite_last_good_checkpoint(tmp_path):
    import asyncio

    from pydantic_ai_backends.remote._browser_profiles import BrowserProfileStore
    from pydantic_ai_backends.remote._interactive import InteractiveResource
    from pydantic_ai_backends.remote.server import SandboxRuntime
    from tests.test_interactive_gateway import Harness

    service = Harness().app.state.service
    sandbox = ProfileSandbox("profile", SandboxRuntime(image="test"))
    sandbox.start()
    store = BrowserProfileStore(tmp_path, "tenant-one", "profile", KEY)
    resource = InteractiveResource(service, "profile", sandbox, "browser", profile=store)
    resource.last_checkpoint = 0
    await resource.checkpoint_due()
    assert sandbox.exports == [False] and store.saved_at
    original = (tmp_path / "profile/private/browser-profile.enc").read_bytes()
    sandbox.alive = False
    await resource.checkpoint(graceful=True)
    assert (tmp_path / "profile/private/browser-profile.enc").read_bytes() == original
    assert resource.checkpointed
    resource.checkpointed = False
    await resource.profile_lock.acquire()
    waiting = asyncio.create_task(resource.checkpoint(graceful=False))
    await asyncio.sleep(0)
    resource.checkpointed = True
    resource.profile_lock.release()
    await waiting
    assert sandbox.exports == [False]


def test_failed_end_and_expired_restart_keep_checkpoint_failure_visible(tmp_path):
    with setup(tmp_path) as (client, headers, built, app, clock):
        assert (
            client.post(
                PATH, headers=headers, json={"profile_key": KEY, "idle_seconds": 3}
            ).status_code
            == 200
        )
        built[0].export_failure = True
        assert client.delete(PATH, headers=headers).status_code == 503
        assert built[0].stopped == 0
        clock.now = 4
        assert client.post(PATH, headers=headers, json={"profile_key": KEY}).status_code == 503
        built[0].export_failure = False
        assert client.delete(PATH, headers=headers).status_code == 204


def test_sleep_and_purge_refuse_unsupported_resources(tmp_path):
    with setup(tmp_path) as (client, headers, built, app, clock):
        assert client.post(PATH + "/suspend", headers=headers).status_code == 409
        assert client.post(PATH, headers=headers, json={}).status_code == 200
        assert client.post(PATH + "/suspend", headers=headers).status_code == 409


async def test_internal_purge_validation_and_stale_release_preserve_new_generation(tmp_path):
    from fastapi import HTTPException

    from pydantic_ai_backends.remote._interactive import InteractiveGateway, InteractiveResource
    from pydantic_ai_backends.remote.server import SandboxRuntime
    from tests.test_interactive_gateway import Harness

    service = Harness().app.state.service
    gateway = InteractiveGateway(service)
    with pytest.raises(HTTPException, match="422"):
        await gateway.purge_profile("../outside", "browser")
    with pytest.raises(HTTPException, match="409"):
        await gateway._purge_profile("profile", "desktop")
    sandbox = ProfileSandbox("profile", SandboxRuntime(image="test"))
    old = InteractiveResource(service, "profile", sandbox, "desktop")
    newer = InteractiveResource(service, "profile", sandbox, "desktop")
    gateway.resources["profile"] = newer
    gateway._released(old)
    assert gateway.resources["profile"] is newer
    await old.aclose()
    await old.checkpoint_due()


@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_profile_start_cleanup_does_not_leak_provider_error(failure, monkeypatch, caplog):
    import asyncio
    from unittest.mock import AsyncMock

    import anyio

    from pydantic_ai_backends.remote import _interactive
    from tests.test_interactive_gateway import Harness

    service = Harness().app.state.service
    gateway = _interactive.InteractiveGateway(service)
    original_scope = anyio.move_on_after

    async def failed_hibernate(_session_id):
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("private-provider-details")

    monkeypatch.setattr(service, "hibernate", AsyncMock(side_effect=failed_hibernate))
    monkeypatch.setattr(
        _interactive.anyio,
        "move_on_after",
        lambda seconds, *, shield: original_scope(0.001, shield=shield),
    )
    await gateway._failed_profile_start("profile")
    assert "cleanup" in caplog.text
    assert "private-provider-details" not in caplog.text
    assert ("timed out" in caplog.text) == (failure == "timeout")


async def test_maintenance_reports_checkpoint_failure_and_still_sweeps(monkeypatch, caplog):
    import asyncio

    from pydantic_ai_backends.remote import _interactive
    from pydantic_ai_backends.remote._environments import ManagedEnvironmentRegistry

    class Resource:
        session_id = "profile"
        closed = False

        async def checkpoint_due(self):
            raise BrowserProfileError("private-cookie-payload")

        async def aclose(self):
            self.closed = True

    resource = Resource()
    registry = ManagedEnvironmentRegistry()

    async def provision():
        return resource

    await registry.ensure("profile", kind="browser", open_resource=provision)
    registry.mark_gone("profile")
    calls = 0

    async def interval(seconds):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(_interactive.asyncio, "sleep", interval)
    with pytest.raises(asyncio.CancelledError):
        await _interactive.maintain_interactive(registry)
    assert resource.closed
    assert "checkpoint failed: profile" in caplog.text
    assert "private-cookie-payload" not in caplog.text


async def test_profile_checkpoint_supports_provider_without_fresh_probe_and_lost_reservation(
    tmp_path,
):
    from pydantic_ai_backends.remote._browser_profiles import BrowserProfileStore
    from pydantic_ai_backends.remote._interactive import InteractiveResource
    from pydantic_ai_backends.remote.server import SandboxRuntime
    from tests.test_interactive_gateway import Harness

    sandbox = ProfileSandbox("profile", SandboxRuntime(image="test"))

    class Provider:
        async def export_browser_profile(self, *, graceful):
            return await sandbox.export_browser_profile(graceful=graceful)

        async def restore_browser_profile(self, payload):
            await sandbox.restore_browser_profile(payload)

    service = Harness().app.state.service
    store = BrowserProfileStore(tmp_path, "tenant-one", "profile", KEY)
    resource = InteractiveResource(service, "profile", Provider(), "browser", profile=store)
    await resource.checkpoint(graceful=True)
    assert sandbox.exports == [True]
    resource.reserved = True
    await resource.aclose()
    assert not resource.reserved and service._inflight.get("profile", 0) == 0


async def test_failed_native_context_admission_hibernates_without_reserving_profile(
    tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock

    from pydantic_ai_backends.backends.docker._browser_bridge import BrowserChannelError
    from pydantic_ai_backends.remote import wire
    from pydantic_ai_backends.remote._interactive import EnvironmentStart, InteractiveGateway
    from pydantic_ai_backends.remote.server import SandboxRuntime, _Service

    sandbox = ProfileSandbox("profile", SandboxRuntime(image="test"))
    config = SandboxdConfig(
        token=SERVICE["X-Sandbox-Token"],
        runtimes={"browser": "test:image"},
        workspace_root=str(tmp_path),
        persist_browser_profiles=True,
    )
    service = _Service(config, build_sandbox=lambda session_id, runtime: sandbox)
    await service.open_session(
        wire.CreateSessionRequest(session_id="profile", runtime="browser", tenant="tenant-one")
    )
    gateway = InteractiveGateway(service)
    monkeypatch.setattr(
        gateway,
        "_profile_context",
        AsyncMock(side_effect=BrowserChannelError("context unavailable")),
    )
    try:
        with pytest.raises(BrowserChannelError, match="context unavailable"):
            await gateway.open_resource("profile", "browser", EnvironmentStart(profile_key=KEY))
        assert sandbox.stopped == 1
        assert service._inflight.get("profile", 0) == 0
        assert "profile" not in gateway.resources
    finally:
        await service.shutdown()


@pytest.mark.parametrize("operation", ["end", "suspend", "expired-start"])
@pytest.mark.parametrize("failure", ["unavailable", "probe"])
def test_provider_retirement_refusal_is_retryable_not_success(
    tmp_path, monkeypatch, operation, failure
):
    from unittest.mock import AsyncMock

    from pydantic_ai_backends.protocol import SandboxProbeError, SandboxUnavailableError

    with setup(tmp_path) as (client, headers, built, app, clock):
        assert (
            client.post(
                PATH, headers=headers, json={"profile_key": KEY, "idle_seconds": 3}
            ).status_code
            == 200
        )
        hibernate = app.state.service.hibernate
        error = SandboxUnavailableError if failure == "unavailable" else SandboxProbeError
        refused = AsyncMock(side_effect=error("private-docker-error"))
        monkeypatch.setattr(app.state.service, "hibernate", refused)
        if operation == "end":
            result = client.delete(PATH, headers=headers)
        elif operation == "suspend":
            result = client.post(PATH + "/suspend", headers=headers)
        else:
            clock.now = 4
            result = client.post(PATH, headers=headers, json={"profile_key": KEY})
        assert result.status_code == 503 and "private-docker-error" not in result.text
        assert built[0].stopped == 0
        monkeypatch.setattr(app.state.service, "hibernate", hibernate)
        assert client.delete(PATH, headers=headers).status_code == 204
        assert built[0].stopped == 1


def test_repeated_end_preserves_saved_profile_and_session_authority(tmp_path):
    with setup(tmp_path) as (client, headers, built, app, clock):
        assert client.post(PATH, headers=headers, json={"profile_key": KEY}).status_code == 200
        assert client.delete(PATH, headers=headers).status_code == 204
        assert client.delete(PATH, headers=headers).status_code == 204
        assert client.get("/sessions/profile", headers=headers).status_code == 200
        resumed = client.post(PATH, headers=headers, json={"profile_key": KEY})
        assert resumed.status_code == 200 and resumed.json()["profile_restored"]
        assert len(built) == 2


def test_end_hibernated_unstarted_browser_without_profile_closes_session(tmp_path):
    with setup(tmp_path) as (client, headers, built, app, clock):
        client.portal.call(app.state.service.hibernate, "profile")
        assert client.delete(PATH, headers=headers).status_code == 204
        assert client.get("/sessions/profile", headers=headers).status_code == 401
        assert not (tmp_path / "profile/private/browser-profile.enc").exists()


def test_initial_profile_discovery_is_busy_until_admission_completes(tmp_path, monkeypatch):
    import asyncio
    import time
    from functools import partial

    from pydantic_ai_backends.remote._interactive import EnvironmentStart, InteractiveGateway

    with setup(tmp_path) as (client, headers, built, app, clock):
        gateway = InteractiveGateway(app.state.service)
        entered, release = asyncio.Event(), asyncio.Event()
        discover = gateway._profile_context

        async def delayed_discovery(sandbox):
            entered.set()
            await release.wait()
            return await discover(sandbox)

        monkeypatch.setattr(gateway, "_profile_context", delayed_discovery)
        start = client.portal.start_task_soon(
            partial(
                gateway.start,
                "profile",
                "browser",
                EnvironmentStart(profile_key=KEY),
                headers["X-Sandbox-Token"],
            )
        )
        try:
            client.portal.call(entered.wait)
            built[0]._last_activity = time.time() - 100
            assert client.portal.call(app.state.service.manager.cleanup_idle, 1) == 0
            assert built[0].stopped == 0
        finally:
            client.portal.call(release.set)
        assert start.result(timeout=5).state == "ready"
        assert app.state.service._inflight["profile"] == 1
        client.portal.call(gateway.end, "profile", "browser", headers["X-Sandbox-Token"])
        assert app.state.service._inflight.get("profile", 0) == 0
