"""Async adapters for sync backend implementations."""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    from concurrent.futures import Executor

    from pydantic_ai_backends.protocol import (
        AsyncBackendProtocol,
        BackendProtocol,
        BackgroundSandboxProtocol,
        SandboxProtocol,
    )
    from pydantic_ai_backends.types import (
        BackgroundHandle,
        BackgroundOutput,
        BackgroundProcessInfo,
        EditResult,
        ExecuteResponse,
        FileInfo,
        GrepMatch,
        WriteResult,
    )

_T = TypeVar("_T")

LEGACY_READ_BYTES = "_read_bytes"
"""Attribute backends used before `read_bytes` became part of the protocol."""


class AsyncBackendAdapter:
    """Wrap a sync :class:`BackendProtocol` with async methods.

    Args:
        backend: The sync backend to wrap.
        executor: Thread pool the blocking calls run on. Without one, asyncio's
            default pool is used — see :meth:`_offload` for why a busy sandbox
            host wants its own.

    Example:
        ```python
        from concurrent.futures import ThreadPoolExecutor

        from pydantic_ai_backends import AsyncSandboxAdapter, DockerSandbox

        # Sized for the number of sandbox commands expected in flight at once.
        backend = AsyncSandboxAdapter(DockerSandbox(), executor=ThreadPoolExecutor(64))
        ```
    """

    def __init__(self, backend: BackendProtocol, *, executor: Executor | None = None) -> None:
        self._backend = backend
        self._executor = executor

    def unwrap(self) -> BackendProtocol:
        """Return the wrapped sync backend."""
        return self._backend

    async def _offload(self, fn: Callable[..., _T], /, *args: object) -> _T:
        """Run one blocking backend call off the event loop.

        Without an explicit `executor` this uses asyncio's default thread pool,
        which holds `min(32, cpu_count + 4)` workers and is shared with every
        other `to_thread` caller in the process. Sandbox commands are not short:
        a handful of concurrent `npm install`-style calls fills that pool, and
        unrelated reads and writes then queue behind them.
        """
        if self._executor is None:
            return await asyncio.to_thread(fn, *args)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args))

    async def exists(self, path: str) -> bool:
        return await self._offload(self._backend.exists, path)

    async def ls_info(self, path: str) -> list[FileInfo]:
        return await self._offload(self._backend.ls_info, path)

    async def read_bytes(self, path: str) -> bytes:
        reader: Any = getattr(self._backend, "read_bytes", None)
        if reader is None:
            reader = getattr(self._backend, LEGACY_READ_BYTES)
        data: bytes = await self._offload(reader, path)
        return data

    async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        return await self._offload(self._backend.read, path, offset, limit)

    async def write(self, path: str, content: str | bytes) -> WriteResult:
        return await self._offload(self._backend.write, path, content)

    async def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return await self._offload(self._backend.edit, path, old_string, new_string, replace_all)

    async def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        return await self._offload(self._backend.glob_info, pattern, path)

    async def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        return await self._offload(self._backend.grep_raw, pattern, path, glob, ignore_hidden)


class AsyncSandboxAdapter(AsyncBackendAdapter):
    """Wrap a sync :class:`SandboxProtocol` with async sandbox methods."""

    def __init__(self, backend: SandboxProtocol, *, executor: Executor | None = None) -> None:
        super().__init__(backend, executor=executor)

    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Run a command, preferring the backend's own async implementation."""
        sandbox = cast("SandboxProtocol", self._backend)
        async_execute = getattr(sandbox, "async_execute", None)
        if inspect.iscoroutinefunction(async_execute):
            native = cast("Callable[[str, int | None], Awaitable[ExecuteResponse]]", async_execute)
            return await native(command, timeout)
        return await self._offload(sandbox.execute, command, timeout)


class AsyncBackgroundSandboxAdapter(AsyncSandboxAdapter):
    """Wrap a sync :class:`BackgroundSandboxProtocol` with async methods.

    The registry operations (poll, drain a file by offset, killpg) are quick and
    non-blocking, but they are bridged through the executor anyway for
    consistency with the rest of the adapter.
    """

    @property
    def _sandbox(self) -> BackgroundSandboxProtocol:
        # The constructor only accepts SandboxProtocol, so the background
        # methods are established by `ensure_async` rather than by the type.
        return cast("BackgroundSandboxProtocol", self._backend)

    async def execute_background(self, command: str) -> BackgroundHandle:
        return await self._offload(self._sandbox.execute_background, command)

    async def read_background(self, shell_id: str) -> BackgroundOutput:
        return await self._offload(self._sandbox.read_background, shell_id)

    async def kill_background(self, shell_id: str) -> bool:
        return await self._offload(self._sandbox.kill_background, shell_id)

    async def list_background(self) -> list[BackgroundProcessInfo]:
        return await self._offload(self._sandbox.list_background)

    async def kill_all_background(self) -> None:
        return await self._offload(self._sandbox.kill_all_background)


def ensure_async(
    backend: BackendProtocol | AsyncBackendProtocol,
    *,
    executor: Executor | None = None,
) -> AsyncBackendProtocol:
    """Return an async backend, wrapping sync ones as needed.

    Args:
        backend: Sync or async backend.
        executor: Thread pool for a newly created adapter. Ignored when
            `backend` is already async or already adapted — this function is
            idempotent on adapters, so wrap once yourself
            (`AsyncSandboxAdapter(backend, executor=...)`) and pass the adapter
            around when every call site should share one pool.

    Returns:
        An async view of `backend`.
    """
    if isinstance(backend, AsyncBackendAdapter):
        return backend

    candidate: Any = backend
    if inspect.iscoroutinefunction(getattr(candidate, "read_bytes", None)):
        return cast("AsyncBackendProtocol", backend)
    if hasattr(candidate, "execute_background"):
        return AsyncBackgroundSandboxAdapter(cast("SandboxProtocol", backend), executor=executor)
    if hasattr(candidate, "execute"):
        return AsyncSandboxAdapter(cast("SandboxProtocol", backend), executor=executor)
    return AsyncBackendAdapter(cast("BackendProtocol", backend), executor=executor)
