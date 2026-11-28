"""One authoritative persistent-environment supervisor per retained storage root."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class StorageOwnershipError(RuntimeError):
    """Another supervisor owns this storage root, or ownership cannot be verified."""


class StorageOwner:
    """Hold a shared basic-service or exclusive persistent-environment flock."""

    def __init__(self, root: Path, *, exclusive: bool) -> None:
        self.root = root
        self.exclusive = exclusive
        self._fd: int | None = None

    def acquire(self) -> None:
        import fcntl

        if self._fd is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            self.root / ".sandboxd-owner.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise StorageOwnershipError(
                    "Storage ownership requires a regular operator-owned file"
                )
            try:
                fcntl.flock(
                    fd, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
                )
            except BlockingIOError as error:
                raise StorageOwnershipError(
                    "Persistent storage already has an active supervisor; stop it before restarting"
                ) from error
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def close(self) -> None:
        if self._fd is None:
            return
        os.close(self._fd)
        self._fd = None
