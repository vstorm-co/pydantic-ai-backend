"""Immutable ownership of retained named-session files, independent of live tokens."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .wire import SESSION_ID_PATTERN


class RetainedSessionConflict(ValueError):
    """A named session's retained files belong to another tenant or runtime."""


class RetainedSessionUnavailable(RuntimeError):
    """Retained scope could not be verified; admission must remain closed."""


def bind_retained_session(root: Path, session_id: str, tenant: str | None, runtime: str) -> None:
    if not re.fullmatch(SESSION_ID_PATTERN, session_id) or not runtime:
        raise RetainedSessionConflict("A valid named session and runtime are required")
    try:
        _bind(root, session_id, tenant, runtime)
    except (OSError, ValueError) as error:
        if isinstance(error, RetainedSessionConflict):
            raise
        raise RetainedSessionUnavailable("Retained session scope could not be verified") from error


def _bind(root: Path, session_id: str, tenant: str | None, runtime: str) -> None:
    """Atomically reserve/verify scope; never serialize transient session credentials."""
    import fcntl

    directory = root / session_id
    if directory.is_symlink():
        raise RetainedSessionConflict("Retained session directory must not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True)
    lock = os.open(directory / ".scope.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        expected = {"version": 1, "session_id": session_id, "tenant": tenant, "runtime": runtime}
        path = directory / ".scope.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, "r") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RetainedSessionUnavailable("Retained session scope is not a regular file")
                actual: object = json.loads(stream.read(4096))
            if actual != expected:
                raise RetainedSessionConflict("Retained session scope does not match")
            return
        fd, temporary = tempfile.mkstemp(prefix=".scope-", dir=directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(expected, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
    finally:
        os.close(lock)
