"""Async adapters for sync backend implementations."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pydantic_ai_backends.protocol import (
        AsyncBackendProtocol,
        BackendProtocol,
        SandboxProtocol,
    )
    from pydantic_ai_backends.types import (
        EditResult,
        ExecuteResponse,
        FileInfo,
        GrepMatch,
        WriteResult,
    )


class AsyncBackendAdapter:
    """Wrap a sync :class:`BackendProtocol` with async methods."""

    def __init__(self, backend: BackendProtocol) -> None:
        self._backend = backend

    def unwrap(self) -> BackendProtocol:
        """Return the wrapped sync backend."""
        return self._backend

    async def exists(self, path: str) -> bool:
        return await asyncio.to_thread(self._backend.exists, path)

    async def ls_info(self, path: str) -> list[FileInfo]:
        return await asyncio.to_thread(self._backend.ls_info, path)

    async def read_bytes(self, path: str) -> bytes:
        reader = getattr(self._backend, "read_bytes", None)
        if reader is None:
            reader = self._backend._read_bytes  # type: ignore[attr-defined]
        return await asyncio.to_thread(reader, path)

    async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        return await asyncio.to_thread(self._backend.read, path, offset, limit)

    async def write(self, path: str, content: str | bytes) -> WriteResult:
        return await asyncio.to_thread(self._backend.write, path, content)

    async def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return await asyncio.to_thread(
            self._backend.edit, path, old_string, new_string, replace_all
        )

    async def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        return await asyncio.to_thread(self._backend.glob_info, pattern, path)

    async def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        return await asyncio.to_thread(self._backend.grep_raw, pattern, path, glob, ignore_hidden)


class AsyncSandboxAdapter(AsyncBackendAdapter):
    """Wrap a sync :class:`SandboxProtocol` with async sandbox methods."""

    def __init__(self, backend: SandboxProtocol) -> None:
        super().__init__(backend)

    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        sandbox = cast("SandboxProtocol", self._backend)
        async_execute = getattr(sandbox, "async_execute", None)
        if inspect.iscoroutinefunction(async_execute):
            async_execute_fn = cast(
                "Callable[[str, int | None], Awaitable[ExecuteResponse]]",
                async_execute,
            )
            return await async_execute_fn(command, timeout)
        return await asyncio.to_thread(sandbox.execute, command, timeout)


def _is_async_backend(backend: Any) -> bool:
    return inspect.iscoroutinefunction(getattr(backend, "read_bytes", None))


def ensure_async(backend: BackendProtocol | AsyncBackendProtocol) -> AsyncBackendProtocol:
    """Return an async backend, wrapping sync backends when needed."""
    if isinstance(backend, AsyncBackendAdapter):
        return backend
    if _is_async_backend(backend):
        return cast("AsyncBackendProtocol", backend)
    if hasattr(backend, "execute"):
        return AsyncSandboxAdapter(cast("SandboxProtocol", backend))
    return AsyncBackendAdapter(cast("BackendProtocol", backend))
