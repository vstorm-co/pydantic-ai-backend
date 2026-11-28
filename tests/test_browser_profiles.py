"""Encrypted full-profile persistence never substitutes empty state on a bad key."""

import base64
import io
import os
import tarfile

import pytest

from pydantic_ai_backends.remote import _browser_profiles as profiles

KEY = base64.urlsafe_b64encode(b"a" * 32).decode()


def archive(entries=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        for name, payload, kind in entries or [("Default/IndexedDB/db", b"private-login", "file")]:
            member = tarfile.TarInfo(f"{profiles.PROFILE_NAME}/{name}")
            if kind == "file":
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))
            else:
                member.type = tarfile.SYMTYPE if kind == "link" else tarfile.DIRTYPE
                member.linkname = "/sensitive"
                tar.addfile(member)
    return data.getvalue()


class Sandbox:
    def __init__(self):
        self.payload = archive()
        self.restored = None
        self.graceful = None

    async def export_browser_profile(self, *, graceful):
        self.graceful = graceful
        return self.payload

    async def restore_browser_profile(self, payload):
        self.restored = payload


async def test_roundtrip_preserves_full_profile_but_only_writes_ciphertext(tmp_path):
    sandbox = Sandbox()
    store = profiles.BrowserProfileStore(tmp_path, "tenant-one", "session", KEY)
    assert await store.restore(sandbox) is False
    assert store.saved_at is None
    await store.checkpoint(sandbox, graceful=True)
    path = tmp_path / "session/private/browser-profile.enc"
    ciphertext = path.read_bytes()
    assert b"private-login" not in ciphertext
    assert b"IndexedDB" not in ciphertext
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert store.saved_at
    reopened = profiles.BrowserProfileStore(tmp_path, "tenant-one", "session", KEY)
    assert await reopened.restore(sandbox)
    assert sandbox.restored == profiles.profile_archive(sandbox.payload)
    assert sandbox.graceful is True
    assert KEY not in repr(store)
    await reopened.purge()
    await reopened.purge()
    assert not path.exists()


@pytest.mark.parametrize("mismatch", ["tenant", "session", "key", "corrupt", "truncated"])
async def test_wrong_scope_key_or_corrupt_snapshot_never_starts_blank(tmp_path, mismatch):
    sandbox = Sandbox()
    store = profiles.BrowserProfileStore(tmp_path, "one", "session", KEY)
    await store.checkpoint(sandbox, graceful=False)
    path = tmp_path / "session/private/browser-profile.enc"
    tenant, session, key = "one", "session", KEY
    if mismatch == "tenant":
        tenant = "two"
    elif mismatch == "session":
        session = "other"
        target = tmp_path / "other/private"
        target.mkdir(parents=True)
        (target / path.name).write_bytes(path.read_bytes())
    elif mismatch == "key":
        key = base64.urlsafe_b64encode(b"b" * 32).decode()
    elif mismatch == "corrupt":
        path.write_bytes(b"invalid")
    else:
        path.write_bytes(profiles._MAGIC)
    with pytest.raises(profiles.BrowserProfileError):
        await profiles.BrowserProfileStore(tmp_path, tenant, session, key).restore(sandbox)
    assert sandbox.restored is None


@pytest.mark.parametrize(
    "session,tenant,key",
    [("../escape", "x", KEY), ("ok", "", KEY), ("ok", "x", "bad"), ("ok", "x", "YWJj")],
)
def test_invalid_store_identity_and_keys_are_refused(tmp_path, session, tenant, key):
    with pytest.raises(ValueError):
        profiles.BrowserProfileStore(tmp_path, tenant, session, key)


async def test_unsupported_sandbox_and_io_failures_remain_explicit(tmp_path, monkeypatch):
    store = profiles.BrowserProfileStore(tmp_path, "one", "session", KEY)
    with pytest.raises(profiles.BrowserProfileError, match="support"):
        await store.restore(object())
    with pytest.raises(profiles.BrowserProfileError, match="support"):
        await store.checkpoint(object(), graceful=True)

    def failed(_data):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "_write", failed)
    with pytest.raises(profiles.BrowserProfileError, match="saved"):
        await store.checkpoint(Sandbox(), graceful=True)


@pytest.mark.parametrize("kind", ["parent", "private", "checkpoint", "fifo"])
async def test_profile_paths_refuse_links_and_nonregular_files(tmp_path, kind):
    store = profiles.BrowserProfileStore(tmp_path, "one", "session", KEY)
    other = tmp_path / "other"
    other.mkdir()
    if kind == "parent":
        (tmp_path / "session").symlink_to(other, target_is_directory=True)
    elif kind == "private":
        (tmp_path / "session").mkdir()
        (tmp_path / "session/private").symlink_to(other, target_is_directory=True)
    else:
        path = tmp_path / "session/private"
        path.mkdir(parents=True)
        if kind == "fifo":
            os.mkfifo(path / "browser-profile.enc")
        else:
            (path / "browser-profile.enc").symlink_to(other)
    with pytest.raises(profiles.BrowserProfileError):
        await store.restore(Sandbox())


@pytest.mark.parametrize("entry", ["../outside", "Default/../../outside"])
def test_archive_rejects_traversal(entry):
    with pytest.raises(profiles.BrowserProfileError):
        profiles.profile_archive(archive([(entry, b"x", "file")]))


def test_archive_excludes_process_locks_and_caches_but_keeps_database():
    payload = archive(
        [
            ("SingletonLock", b"", "link"),
            ("Default/Cache/item", b"cache", "file"),
            ("Default/IndexedDB/db", b"saved", "file"),
            ("Default", b"", "dir"),
        ]
    )
    with tarfile.open(fileobj=io.BytesIO(profiles.profile_archive(payload))) as tar:
        assert tar.getnames() == [
            "sandboxd-browser-profile/Default/IndexedDB/db",
            "sandboxd-browser-profile/Default",
        ]
    with pytest.raises(profiles.BrowserProfileError, match="symbolic"):
        profiles.profile_archive(archive([("unsafe", b"", "link")]))


def test_invalid_and_bounded_archives(monkeypatch):
    for payload in (b"", b"not tar"):
        with pytest.raises(profiles.BrowserProfileError):
            profiles.profile_archive(payload)
    monkeypatch.setattr(profiles, "MAX_PROFILE_MEMBERS", 0)
    with pytest.raises(profiles.BrowserProfileError):
        profiles.profile_archive(archive())
    monkeypatch.setattr(profiles, "MAX_PROFILE_BYTES", 1)
    with pytest.raises(profiles.BrowserProfileError):
        profiles.profile_archive(archive())


def test_archive_size_bound_includes_tar_header_expansion_and_final_padding(monkeypatch):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w", format=tarfile.GNU_FORMAT) as tar:
        member = tarfile.TarInfo(f"{profiles.PROFILE_NAME}/database")
        member.uid = 999999999999
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    compact = data.getvalue()[:1024]
    monkeypatch.setattr(profiles, "MAX_PROFILE_BYTES", len(compact))
    with pytest.raises(profiles.BrowserProfileError, match="bound"):
        profiles.profile_archive(compact)
    normal = archive()[:1024]
    with pytest.raises(profiles.BrowserProfileError, match="bound"):
        profiles.profile_archive(normal)
