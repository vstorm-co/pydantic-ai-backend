"""Disk retention cannot be reclaimed under a different session scope."""

import json

import pytest

from pydantic_ai_backends.remote._retained_sessions import (
    RetainedSessionConflict,
    bind_retained_session,
)
from pydantic_ai_backends.remote.server import SandboxdConfig, SandboxRuntime, _default_builder


def test_scope_survives_service_memory_and_refuses_tenant_or_runtime_change(tmp_path):
    bind_retained_session(tmp_path, "one", "tenant", "browser")
    bind_retained_session(tmp_path, "one", "tenant", "browser")
    for tenant, runtime in [("other", "browser"), ("tenant", "desktop")]:
        with pytest.raises(RetainedSessionConflict):
            bind_retained_session(tmp_path, "one", tenant, runtime)
    stored = json.loads((tmp_path / "one/.scope.json").read_text())
    assert stored == {"version": 1, "session_id": "one", "tenant": "tenant", "runtime": "browser"}
    assert (tmp_path / "one/.scope.json").stat().st_mode & 0o777 == 0o600


def test_scope_refuses_session_symlink(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (tmp_path / "one").symlink_to(other, target_is_directory=True)
    with pytest.raises(RetainedSessionConflict):
        bind_retained_session(tmp_path, "one", "tenant", "browser")


@pytest.mark.parametrize("home", ["relative", "/", "/tmp/a", "/home/a/../b"])
def test_home_operator_path_validation(home):
    with pytest.raises(ValueError, match="home_path"):
        SandboxRuntime(image="desktop:test", home_path=home)


def test_persistent_home_requires_durable_storage_and_known_custom_image_path(tmp_path):
    with pytest.raises(ValueError, match="workspace_root"):
        SandboxdConfig(token="secret", persist_desktop_home=True)
    with pytest.raises(ValueError, match="home_path"):
        SandboxdConfig(
            token="secret",
            workspace_root=str(tmp_path),
            persist_desktop_home=True,
            runtimes={"desktop": "desktop:test"},
        )


@pytest.mark.parametrize("uid,home", [(None, "/root"), (10001, "/home/agent")])
def test_builtin_desktop_home_is_separate_from_public_workspace(tmp_path, monkeypatch, uid, home):
    from pydantic_ai_backends.remote import server

    monkeypatch.setattr(server, "_hand_workspace_to", lambda _path, _uid: None)
    config = SandboxdConfig(
        token="secret", workspace_root=str(tmp_path), persist_desktop_home=True, sandbox_uid=uid
    )
    sandbox = _default_builder(config)("one", SandboxRuntime(runtime="desktop"))
    assert sandbox._volumes[str((tmp_path / "one/home").resolve())] == home
    assert sandbox._volumes[str((tmp_path / "one/workspace").resolve())] == "/workspace"
    sandbox._container = None


def test_custom_desktop_home_refuses_workspace_overlap_and_symlink(tmp_path):
    runtime = SandboxRuntime(image="desktop:test", home_path="/home/person")
    config = SandboxdConfig(
        token="secret",
        workspace_root=str(tmp_path),
        persist_desktop_home=True,
        runtimes={"desktop": runtime},
    )
    sandbox = _default_builder(config)("one", runtime)
    assert sandbox._volumes[str((tmp_path / "one/home").resolve())] == "/home/person"
    sandbox._container = None
    config.work_dir = "/home/person/work"
    with pytest.raises(ValueError, match="overlap"):
        _default_builder(config)("one", runtime)
    config.work_dir = "/workspace"
    (tmp_path / "one/home").rmdir()
    (tmp_path / "one/home").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        _default_builder(config)("one", runtime)


@pytest.mark.parametrize("kind", ["corrupt", "symlink", "fifo"])
def test_retained_scope_storage_errors_are_not_new_empty_sessions(tmp_path, kind):
    import os

    from pydantic_ai_backends.remote._retained_sessions import RetainedSessionUnavailable

    directory = tmp_path / "one"
    directory.mkdir()
    path = directory / ".scope.json"
    if kind == "corrupt":
        path.write_text("invalid")
    elif kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        os.mkfifo(path)
    with pytest.raises(RetainedSessionUnavailable):
        bind_retained_session(tmp_path, "one", "tenant", "browser")


def test_invalid_session_cannot_escape_retained_scope_root(tmp_path):
    with pytest.raises(RetainedSessionConflict):
        bind_retained_session(tmp_path, "../escape", "tenant", "browser")


@pytest.mark.parametrize("change", ["tenant", "runtime", "corrupt"])
def test_service_reopen_checks_durable_scope_before_any_container_start(tmp_path, change):
    from fastapi.testclient import TestClient

    from tests.test_interactive_gateway import SERVICE, Harness

    original = Harness()
    original.config.workspace_root = str(tmp_path)
    with TestClient(original.app) as client:
        response = client.post(
            "/sessions",
            headers=SERVICE,
            json={"session_id": "one", "tenant": "tenant", "runtime": "browser"},
        )
        assert response.status_code == 200
    fresh = Harness()
    fresh.config.workspace_root = str(tmp_path)
    body = {
        "session_id": "one",
        "tenant": "other" if change == "tenant" else "tenant",
        "runtime": "desktop" if change == "runtime" else "browser",
    }
    if change == "corrupt":
        (tmp_path / "one/.scope.json").write_text("invalid")
    with TestClient(fresh.app) as client:
        response = client.post("/sessions", headers=SERVICE, json=body)
        assert response.status_code == (503 if change == "corrupt" else 409)
        assert fresh.built == {}


def test_default_runtime_omission_on_live_reuse_preserves_bound_runtime(tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_interactive_gateway import SERVICE, Harness

    harness = Harness()
    harness.config.workspace_root = str(tmp_path)
    with TestClient(harness.app) as client:
        body = {"session_id": "one", "tenant": "tenant", "runtime": "desktop"}
        assert client.post("/sessions", headers=SERVICE, json=body).status_code == 200
        assert (
            client.post(
                "/sessions",
                headers=SERVICE,
                json={"session_id": "one", "tenant": "tenant", "reuse": True},
            ).status_code
            == 200
        )


def test_persistent_home_uses_effective_built_runtime_uid(tmp_path, monkeypatch):
    from pydantic_ai_backends.backends.docker.runtimes import get_runtime
    from pydantic_ai_backends.remote import server

    ownership = []
    monkeypatch.setattr(
        server, "_hand_workspace_to", lambda path, uid: ownership.append((path, uid))
    )
    runtime = get_runtime("desktop").model_copy(update={"run_as_uid": 10002})
    config = SandboxdConfig(token="secret", workspace_root=str(tmp_path), persist_desktop_home=True)
    sandbox = _default_builder(config)("one", SandboxRuntime(runtime=runtime))
    home = tmp_path / "one/home"
    assert sandbox._volumes[str(home.resolve())] == "/home/agent"
    assert (home, 10002) in ownership
    assert config.sandbox_uid is None
    sandbox._container = None


async def test_purge_racing_hibernation_revokes_the_original_scoped_token():
    import asyncio

    from fastapi import HTTPException

    from pydantic_ai_backends.remote import wire
    from tests.test_interactive_gateway import Harness

    service = Harness().app.state.service
    opened = await service.open_session(wire.CreateSessionRequest(session_id="one"))
    entered, unblock, hibernating = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def checkpoint(_id):
        entered.set()
        await unblock.wait()

    service.manager.before_release = checkpoint
    closing = asyncio.create_task(service.close_session("one", purge=True))
    await entered.wait()
    release = service.manager.release

    async def tracked_release(session_id):
        if session_id in service._hibernating:
            hibernating.set()
        return await release(session_id)

    service.manager.release = tracked_release
    sleeping = asyncio.create_task(service.hibernate("one"))
    try:
        await hibernating.wait()
        with pytest.raises(HTTPException) as blocked:
            await service.open_session(wire.CreateSessionRequest(session_id="one", reuse=True))
        assert blocked.value.status_code == 409
        with pytest.raises(HTTPException):
            await service.sandbox("one")
        with pytest.raises(HTTPException):
            await service.close_session("one", purge=True)
        unblock.set()
        await asyncio.gather(closing, sleeping)
        assert "one" not in service._sessions
        with pytest.raises(HTTPException) as revoked:
            service.check_session_token("one", opened.token)
        assert revoked.value.status_code == 401
    finally:
        unblock.set()
        await asyncio.gather(closing, sleeping, return_exceptions=True)
        await service.shutdown()


async def test_close_preserves_a_concurrently_replaced_record_and_its_files(tmp_path):
    from fastapi import HTTPException

    from pydantic_ai_backends.remote import wire
    from pydantic_ai_backends.remote.server import _Session
    from tests.test_interactive_gateway import Harness

    harness = Harness()
    service = harness.app.state.service
    service.config.workspace_root = str(tmp_path)
    await service.open_session(wire.CreateSessionRequest(session_id="one"))
    replacement = _Session(runtime="browser", token="replacement", created_at=0, tenant=None)

    async def replace(_id):
        service._sessions["one"] = replacement
        return True

    service.manager.release = replace
    marker = tmp_path / "one/private/data"
    marker.parent.mkdir(parents=True)
    marker.write_text("new-state")
    try:
        with pytest.raises(HTTPException) as conflict:
            await service.close_session("one", purge=True)
        assert conflict.value.status_code == 409
        assert service._sessions["one"] is replacement
        assert marker.read_text() == "new-state"
        assert "one" not in service._closing
    finally:
        await service.shutdown()


def test_missing_attach_does_not_create_or_reserve_retained_scope(tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_interactive_gateway import SERVICE, Harness

    harness = Harness()
    harness.config.workspace_root = str(tmp_path)
    with TestClient(harness.app) as client:
        missing = client.post(
            "/sessions",
            headers=SERVICE,
            json={"session_id": "missing", "tenant": "original", "attach": True},
        )
        assert missing.status_code == 404
        assert not (tmp_path / "missing").exists()
        assert harness.built == {}
        created = client.post(
            "/sessions",
            headers=SERVICE,
            json={"session_id": "missing", "tenant": "different"},
        )
        assert created.status_code == 200
