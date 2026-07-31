"""Tracking what the agent has read, so an edit cannot apply to stale content.

An agent constructs an edit from what it last read. If the file changed on disk
in between — a formatter, a pre-commit hook, another agent — applying the edit
anyway would either fail confusingly or corrupt the file. Every read and write
records a fingerprint, and an edit to a tracked file is refused when the
fingerprint no longer matches.

State is keyed by backend in a `WeakKeyDictionary`, so it disappears with the
backend rather than growing for the life of the process.
"""

from __future__ import annotations

import asyncio
import hashlib
import weakref
from typing import Any

from pydantic_ai_backends.protocol import AsyncBackendProtocol

read_fingerprints: weakref.WeakKeyDictionary[Any, dict[str, str]] = weakref.WeakKeyDictionary()
"""Per-backend fingerprint of the content the agent last saw at each path."""

_edit_locks: weakref.WeakKeyDictionary[Any, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()


def fingerprint(data: bytes) -> str:
    """Content fingerprint used to detect a change between read and edit."""
    return hashlib.sha256(data).hexdigest()


def edit_lock(backend: Any, path: str) -> asyncio.Lock:
    """The lock serializing edits to one path on one backend."""
    per_path = _edit_locks.setdefault(backend, {})
    return per_path.setdefault(path, asyncio.Lock())


def record_read(backend: Any, path: str, data: bytes) -> None:
    """Remember the content the agent just saw at `path`."""
    read_fingerprints.setdefault(backend, {})[path] = fingerprint(data)


async def record_path_read(
    backend_async: AsyncBackendProtocol, backend: Any, path: str
) -> None:  # pragma: no cover - glue, exercised through the file tools
    """Fetch `path` and record its fingerprint, best effort."""
    try:
        data = await backend_async.read_bytes(path)
    except Exception:
        return
    record_read(backend, path, data)


async def staleness_error(
    backend_async: AsyncBackendProtocol, backend: Any, path: str
) -> str | None:
    """Why `path` must be re-read before editing, or `None` when it is current.

    Only files already seen through the tools are guarded — an edit to a file
    that was never read proceeds, since the edit's own match check still applies.
    """
    recorded = read_fingerprints.get(backend, {}).get(path)
    if recorded is None:
        return None

    try:
        current = await backend_async.read_bytes(path)
    except Exception:  # pragma: no cover - defensive; the edit itself will report
        return None

    if fingerprint(current) == recorded:
        return None
    return (
        f"Error: '{path}' changed since you last read it. Read it again "
        "before editing so your change applies to the current contents."
    )
