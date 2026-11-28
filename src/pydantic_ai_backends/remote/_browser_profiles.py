"""Encrypted durable Chromium profile checkpoints outside public workspace files.

Only ciphertext reaches host disk. Keys are supplied per attachment and never
persisted; authentication binds each snapshot to its tenant and named session.
A checkpoint is a last-known filesystem snapshot, not a live browser/RAM image.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import os
import secrets
import stat
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

import anyio

from .wire import SESSION_ID_PATTERN

MAX_PROFILE_BYTES = 64 * 1024 * 1024
MAX_PROFILE_MEMBERS = 20000
PROFILE_NAME = "sandboxd-browser-profile"
_MAGIC = b"PABP\x01"


class BrowserProfileError(RuntimeError):
    """A profile could not be retained or restored; no empty fallback is allowed."""


@runtime_checkable
class ProfileSandbox(Protocol):
    async def export_browser_profile(self, *, graceful: bool) -> bytes: ...
    async def restore_browser_profile(self, archive: bytes) -> None: ...


def profile_archive(payload: bytes) -> bytes:
    """Validate and normalize Docker tar without extracting anything on the host."""
    if not payload or len(payload) > MAX_PROFILE_BYTES:
        raise BrowserProfileError("Browser profile archive exceeds its bound or is empty")
    output = io.BytesIO()
    try:
        with (
            tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as source,
            tarfile.open(fileobj=output, mode="w:") as target,
        ):
            count = 0
            total = 0
            for member in source:
                count += 1
                total += member.size
                path = PurePosixPath(member.name)
                if (
                    count > MAX_PROFILE_MEMBERS
                    or total > MAX_PROFILE_BYTES
                    or path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or path.parts[0] != PROFILE_NAME
                    or not (member.isfile() or member.isdir() or member.issym())
                ):
                    raise BrowserProfileError(
                        "Browser profile archive is invalid or exceeds its bound"
                    )
                # Chromium's process locks are symlinks; they must never be
                # restored. All other links are refused, not dereferenced.
                if any(part.startswith("Singleton") for part in path.parts):
                    continue
                if member.issym():
                    raise BrowserProfileError("Browser profile archive contains a symbolic link")
                if any(
                    part in {"Cache", "Code Cache", "GPUCache", "ShaderCache", "BrowserMetrics"}
                    for part in path.parts
                ):
                    continue
                member.pax_headers = {}
                member.mode = 0o700 if member.isdir() else 0o600
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
                if output.tell() > MAX_PROFILE_BYTES:
                    raise BrowserProfileError("Browser profile archive exceeds its bound")
    except (tarfile.TarError, OSError, ValueError) as error:
        raise BrowserProfileError("Browser profile archive is invalid") from error
    result = output.getvalue()
    if len(result) > MAX_PROFILE_BYTES:
        raise BrowserProfileError("Browser profile archive exceeds its bound")
    return result


class BrowserProfileStore:
    """One encrypted checkpoint bound to a tenant, session and caller-supplied key."""

    def __init__(self, root: Path, tenant: str, session_id: str, key: str) -> None:
        import re

        if not re.fullmatch(SESSION_ID_PATTERN, session_id) or not tenant:
            raise ValueError("A named browser session and tenant are required")
        try:
            decoded = base64.b64decode(key, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("Browser profile key must encode exactly 32 bytes") from error
        if len(decoded) != 32:
            raise ValueError("Browser profile key must encode exactly 32 bytes")
        self._key = decoded
        self._directory = root / session_id / "private"
        self._path = self._directory / "browser-profile.enc"
        self._aad = json.dumps([1, tenant, session_id], separators=(",", ":")).encode()
        self._lock = asyncio.Lock()
        self.saved_at: float | None = None

    def _prepare(self) -> None:
        if self._directory.parent.is_symlink() or self._directory.is_symlink():
            raise BrowserProfileError("Browser profile directory must not be a symbolic link")
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._directory, 0o700)

    def _read(self) -> bytes | None:
        self._prepare()
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise BrowserProfileError("Browser checkpoint is not a regular file")
            self.saved_at = os.fstat(fd).st_mtime
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_PROFILE_BYTES + 128)
            if len(data) > MAX_PROFILE_BYTES + 64 or not data.startswith(_MAGIC):
                raise BrowserProfileError("Browser checkpoint is invalid or exceeds its bound")
            return data
        finally:
            os.close(fd)

    def _write(self, data: bytes) -> None:
        self._prepare()
        fd, temporary = tempfile.mkstemp(prefix=".browser-", dir=self._directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            parent = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def restore(self, sandbox: object) -> bool:
        """Restore the last checkpoint before Chromium starts; missing alone returns False."""
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not isinstance(sandbox, ProfileSandbox):
            raise BrowserProfileError("This sandbox does not support browser profile persistence")
        async with self._lock:
            try:
                data = await anyio.to_thread.run_sync(self._read)
                if data is None:
                    return False
                plain = AESGCM(self._key).decrypt(data[5:17], data[17:], self._aad)
                archive = await anyio.to_thread.run_sync(profile_archive, plain)
                await sandbox.restore_browser_profile(archive)
            except (InvalidTag, OSError, ValueError) as error:
                raise BrowserProfileError("The browser checkpoint could not be restored") from error
            return True

    async def checkpoint(self, sandbox: object, *, graceful: bool) -> None:
        """Atomically replace the encrypted snapshot after a successful bounded capture."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not isinstance(sandbox, ProfileSandbox):
            raise BrowserProfileError("This sandbox does not support browser profile persistence")
        async with self._lock:
            archive = await sandbox.export_browser_profile(graceful=graceful)
            plain = await anyio.to_thread.run_sync(profile_archive, archive)
            nonce = secrets.token_bytes(12)
            encrypted = _MAGIC + nonce + AESGCM(self._key).encrypt(nonce, plain, self._aad)
            try:
                await anyio.to_thread.run_sync(self._write, encrypted)
                self.saved_at = time.time()
            except OSError as error:
                raise BrowserProfileError("The browser checkpoint could not be saved") from error

    async def purge(self) -> None:
        """Delete only this profile's ciphertext, leaving unrelated workspace files alone."""
        async with self._lock:
            await anyio.to_thread.run_sync(lambda: self._path.unlink(missing_ok=True))
