"""Persistent supervisors have exclusive authority before any container lifecycle work."""

import os

import pytest
from fastapi.testclient import TestClient

from pydantic_ai_backends.remote._storage_owner import StorageOwner, StorageOwnershipError
from pydantic_ai_backends.remote.server import SandboxdConfig
from tests.test_interactive_gateway import Harness


def test_basic_shared_owners_coexist_but_refuse_persistent_exclusive_owner(tmp_path):
    first = StorageOwner(tmp_path, exclusive=False)
    second = StorageOwner(tmp_path, exclusive=False)
    exclusive = StorageOwner(tmp_path, exclusive=True)
    try:
        first.acquire()
        first.acquire()
        second.acquire()
        with pytest.raises(StorageOwnershipError, match="supervisor"):
            exclusive.acquire()
        first.close()
        with pytest.raises(StorageOwnershipError):
            exclusive.acquire()
        second.close()
        exclusive.acquire()
    finally:
        first.close()
        second.close()
        exclusive.close()
        exclusive.close()


def test_exclusive_owner_refuses_other_owners_and_release_allows_restart(tmp_path):
    first = StorageOwner(tmp_path, exclusive=True)
    first.acquire()
    try:
        for mode in (False, True):
            other = StorageOwner(tmp_path, exclusive=mode)
            with pytest.raises(StorageOwnershipError):
                other.acquire()
            assert other._fd is None
    finally:
        first.close()
    replacement = StorageOwner(tmp_path, exclusive=True)
    replacement.acquire()
    replacement.close()
    assert (tmp_path / ".sandboxd-owner.lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "fifo", "wrong-owner"])
def test_ownership_requires_regular_unlinked_operator_control_file(tmp_path, monkeypatch, kind):
    path = tmp_path / ".sandboxd-owner.lock"
    if kind == "symlink":
        path.symlink_to(tmp_path / "other")
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        original = os.fstat

        def wrong_owner(fd):
            values = list(original(fd))
            values[4] = os.geteuid() + 1
            return os.stat_result(values)

        monkeypatch.setattr(os, "fstat", wrong_owner)
    with pytest.raises((StorageOwnershipError, OSError)):
        StorageOwner(tmp_path, exclusive=True).acquire()


def test_server_rejects_second_owner_before_starting_any_session(tmp_path):
    first, second = Harness(), Harness()
    for harness in (first, second):
        harness.config.workspace_root = str(tmp_path)
        harness.config.persist_browser_profiles = True
    with TestClient(first.app):
        with pytest.raises(StorageOwnershipError), TestClient(second.app):
            pytest.fail("Second storage owner started")
        assert second.built == {}
    with TestClient(second.app):
        assert second.built == {}


async def test_failed_service_startup_releases_ownership_for_retry(tmp_path, monkeypatch):
    harness = Harness()
    service = harness.app.state.service
    service.config.workspace_root = str(tmp_path)
    service.config.persist_browser_profiles = True

    def failed():
        raise RuntimeError("startup failed")

    monkeypatch.setattr(service, "_startup", failed)
    with pytest.raises(RuntimeError, match="startup failed"):
        service.startup()
    assert service._storage_owner is None
    replacement = StorageOwner(tmp_path, exclusive=True)
    replacement.acquire()
    replacement.close()
    await service.shutdown()


def test_persistent_browser_mode_requires_workspace_storage():
    with pytest.raises(ValueError, match="workspace_root"):
        SandboxdConfig(token="secret", persist_browser_profiles=True)


def test_double_startup_does_not_lose_the_owned_lock_descriptor(tmp_path):
    harness = Harness()
    service = harness.app.state.service
    service.config.workspace_root = str(tmp_path)
    with TestClient(harness.app):
        original = service._storage_owner
        with pytest.raises(RuntimeError, match="already started"):
            service.startup()
        assert service._storage_owner is original
        with pytest.raises(StorageOwnershipError):
            StorageOwner(tmp_path, exclusive=True).acquire()
    restarted = StorageOwner(tmp_path, exclusive=True)
    restarted.acquire()
    restarted.close()
