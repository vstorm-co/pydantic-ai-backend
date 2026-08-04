"""Abstract sandboxes with shell-based defaults for every file operation.

A subclass only has to implement `execute` and `edit`. Everything else is
derived from shell commands, which is enough for any sandbox that offers a
shell; subclasses with a native file API override the methods it covers.

Two variants, one implementation. :class:`BaseSandbox` is for a sandbox reached
synchronously — a Docker socket, a subprocess. :class:`AsyncBaseSandbox` is for
one that is natively asynchronous — asyncssh, an async HTTP SDK — and is the
class to subclass there rather than writing a synchronous facade over async code:
the facade has to hop back onto the event loop from a worker thread, and a
sandbox that reprovisions itself can then deadlock against its own thread pool.
Both derive their file operations from :mod:`._shell`, so neither can drift.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod

from pydantic_ai_backends.backends import _shell
from pydantic_ai_backends.types import (
    EditResult,
    ExecuteResponse,
    FileInfo,
    GrepMatch,
    WriteResult,
)

LS_FIELD_COUNT = _shell.LS_FIELD_COUNT
"""Re-exported from `_shell`, where the parsing that uses it lives."""

FILE_OP_TIMEOUT = 30
"""Ceiling on the shell commands `ls`, `read`, `read_bytes` and `write` derive from.

Every operation here runs a command, and a command with no timeout is one
nothing can reclaim: `DockerSandbox.execute` only wraps the `timeout` utility
around a command it was given a limit for, and `exec_run` has no deadline of its
own. A single uncapped operation therefore pins a worker thread for good, which
is how a handful of requests wedged a whole sandbox service. A listing or a read
that has not answered in thirty seconds is not going to.
"""

SEARCH_TIMEOUT = 120
"""Ceiling on `glob_info` and `grep_raw`, which walk a tree rather than one file.

Separate from :data:`FILE_OP_TIMEOUT` because a legitimate `grep` over a large
repository is slow in a way a `ls` never is, and capping both at the same number
would mean choosing between a useless search and an unbounded listing.
"""


class _SandboxIdentity:
    """The identity and idle bookkeeping both base sandboxes share.

    Args:
        sandbox_id: Unique identifier for this sandbox. Generated when omitted.
    """

    def __init__(self, sandbox_id: str | None = None):
        self._id = sandbox_id or str(uuid.uuid4())
        self._last_activity = time.time()

    @property
    def id(self) -> str:
        """Unique identifier for this sandbox."""
        return self._id

    @property
    def last_activity(self) -> float:
        """Wall clock of the last operation, which idle cleanup reaps against."""
        return self._last_activity

    def touch(self) -> None:
        """Record activity, so idle cleanup does not reap a sandbox in use."""
        self._last_activity = time.time()


class BaseSandbox(_SandboxIdentity, ABC):
    """Base class for synchronous sandboxes that expose a shell.

    Args:
        sandbox_id: Unique identifier for this sandbox. Generated when omitted.
    """

    def start(self) -> None:
        """Start the sandbox eagerly.

        The default is a no-op, since sandboxes start on first use.
        """

    def is_alive(self) -> bool:
        """Whether the sandbox is running and responsive.

        **Override this.** The default answer is "no", and `SessionManager` reads
        it as "replace the sandbox" — so a subclass that never overrides it has a
        sandbox built, stopped and rebuilt on every single operation. It defaults
        to `False` rather than `True` because a sandbox wrongly believed alive is
        the harder failure to diagnose, but neither default is right for a real
        sandbox: answer it.
        """
        return False

    def stop(self, purge: bool = False) -> None:
        """Stop and clean up the sandbox.

        Args:
            purge: Also discard what the sandbox accumulated — its filesystem,
                and any workspace kept for it outside the container. Left off,
                the sandbox ends while its files survive for the next attach;
                turned on, the thing the sandbox belonged to is gone for good.

                Accepted by every sandbox so one call site can end any of them,
                and the subclasses that cannot tell the two apart say so: a
                Daytona sandbox and a Kubernetes pod are deleted either way,
                because neither keeps a filesystem this library can reattach to.
        """

    @abstractmethod
    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Run a command in the sandbox.

        Args:
            command: Command to execute.
            timeout: Maximum execution time in seconds.
        """
        ...

    @abstractmethod
    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit a file by replacing a string.

        Args:
            path: File path to edit.
            old_string: String to find and replace.
            new_string: Replacement string.
            replace_all: Replace every occurrence instead of only the first.
        """
        ...

    def exists(self, path: str) -> bool:
        """Whether `path` is a regular file, via `test -f`."""
        return self.execute(_shell.exists_command(path), timeout=5).exit_code == 0

    def ls_info(self, path: str) -> list[FileInfo]:
        """List one directory using `ls -la`."""
        command = _shell.ls_command(path)
        return _shell.parse_ls(self.execute(command, timeout=FILE_OP_TIMEOUT), path)

    def read_bytes(self, path: str) -> bytes:
        """Read a whole file with `cat`, or `b""` on any failure."""
        command = _shell.read_bytes_command(path)
        return _shell.parse_read_bytes(self.execute(command, timeout=FILE_OP_TIMEOUT))

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a slice of a file, numbered by its real line positions."""
        command = _shell.read_command(path, offset, limit)
        return _shell.parse_read(self.execute(command, timeout=FILE_OP_TIMEOUT))

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write a file, carrying the content base64-encoded."""
        command = _shell.write_command(path, content)
        return _shell.parse_write(self.execute(command, timeout=FILE_OP_TIMEOUT), path)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Match files with `find`."""
        command = _shell.glob_command(pattern, path)
        return _shell.parse_glob(self.execute(command, timeout=SEARCH_TIMEOUT))

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search file contents with `grep`."""
        command = _shell.grep_command(pattern, path, glob, ignore_hidden)
        return _shell.parse_grep(self.execute(command, timeout=SEARCH_TIMEOUT))


class AsyncBaseSandbox(_SandboxIdentity, ABC):
    """Base class for natively asynchronous sandboxes that expose a shell.

    Subclass this when the sandbox is reached over an async transport — asyncssh,
    an async HTTP client, any async SDK. Implement `execute` and `edit` as
    coroutines and every other operation is derived from shell commands, exactly
    as the synchronous base does.

    Subclassing this rather than wrapping async code in a synchronous facade is
    not a style preference. `ensure_async` cannot see through a facade: it
    wraps the facade in a thread adapter, so each call runs on a worker thread
    that has to hop back onto the event loop to reach the real async code. A
    sandbox whose own recovery path also needs a thread — reprovisioning a dead
    container, say — then waits for a thread that is waiting for the loop, and
    starves the pool for every other agent sharing it. Being async all the way
    down means `ensure_async` passes the backend through untouched and the
    toolset awaits it directly.

    Recognised by `ensure_async` through this base class, so no method-shape
    sniffing is involved:

    ```python
    from pydantic_ai_backends import AsyncBaseSandbox, ExecuteResponse


    class SSHSandbox(AsyncBaseSandbox):
        async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
            result = await self._connection.run(command, timeout=timeout)
            return ExecuteResponse(output=result.stdout, exit_code=result.exit_status)

        async def edit(self, path, old_string, new_string, replace_all=False) -> EditResult:
            ...
    ```

    Failures are returned, never raised — see
    :class:`~pydantic_ai_backends.protocol.AsyncBackendProtocol` for the contract
    every method here is held to.

    Args:
        sandbox_id: Unique identifier for this sandbox. Generated when omitted.
    """

    async def start(self) -> None:
        """Start the sandbox eagerly.

        The default is a no-op, since sandboxes start on first use.
        """

    async def is_alive(self) -> bool:
        """Whether the sandbox is running and responsive.

        **Override this.** The default answer is "no", and `SessionManager` reads
        it as "replace the sandbox" — so a subclass that never overrides it has a
        sandbox built, stopped and rebuilt on every single operation. It defaults
        to `False` rather than `True` because a sandbox wrongly believed alive is
        the harder failure to diagnose, but neither default is right for a real
        sandbox: answer it.
        """
        return False

    async def stop(self, purge: bool = False) -> None:
        """Stop and clean up the sandbox.

        Args:
            purge: Also discard what the sandbox accumulated. See
                :meth:`BaseSandbox.stop`.
        """

    @abstractmethod
    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Run a command in the sandbox.

        Args:
            command: Command to execute.
            timeout: Maximum execution time in seconds.
        """
        ...

    @abstractmethod
    async def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit a file by replacing a string.

        Args:
            path: File path to edit.
            old_string: String to find and replace.
            new_string: Replacement string.
            replace_all: Replace every occurrence instead of only the first.
        """
        ...

    async def exists(self, path: str) -> bool:
        """Whether `path` is a regular file, via `test -f`."""
        result = await self.execute(_shell.exists_command(path), timeout=5)
        return result.exit_code == 0

    async def ls_info(self, path: str) -> list[FileInfo]:
        """List one directory using `ls -la`."""
        result = await self.execute(_shell.ls_command(path), timeout=FILE_OP_TIMEOUT)
        return _shell.parse_ls(result, path)

    async def read_bytes(self, path: str) -> bytes:
        """Read a whole file with `cat`, or `b""` on any failure."""
        command = _shell.read_bytes_command(path)
        return _shell.parse_read_bytes(await self.execute(command, timeout=FILE_OP_TIMEOUT))

    async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a slice of a file, numbered by its real line positions."""
        command = _shell.read_command(path, offset, limit)
        return _shell.parse_read(await self.execute(command, timeout=FILE_OP_TIMEOUT))

    async def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write a file, carrying the content base64-encoded."""
        result = await self.execute(_shell.write_command(path, content), timeout=FILE_OP_TIMEOUT)
        return _shell.parse_write(result, path)

    async def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Match files with `find`."""
        command = _shell.glob_command(pattern, path)
        return _shell.parse_glob(await self.execute(command, timeout=SEARCH_TIMEOUT))

    async def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search file contents with `grep`."""
        command = _shell.grep_command(pattern, path, glob, ignore_hidden)
        return _shell.parse_grep(await self.execute(command, timeout=SEARCH_TIMEOUT))
