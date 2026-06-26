"""Protocol definitions for backends."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
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


@runtime_checkable
class BackendProtocol(Protocol):
    """Protocol for file storage backends.

    All backends must implement these methods for basic file operations.
    This allows using different storage backends (in-memory, filesystem,
    Docker, cloud storage) interchangeably.

    Example:
        ```python
        from pydantic_ai_backends import BackendProtocol, StateBackend

        def process_files(backend: BackendProtocol) -> None:
            # Works with any backend implementation
            files = backend.ls_info("/")
            for f in files:
                content = backend.read(f["path"])
                print(content)
        ```
    """

    def exists(self, path: str) -> bool:
        """Check whether a file exists at the given path.

        Args:
            path: File path to check.

        Returns:
            True if the path resolves to an existing file, False otherwise.
            Returns False for invalid paths, directories, and permission errors —
            callers must use ls_info() to distinguish directories from missing paths.
        """
        ...

    def ls_info(self, path: str) -> list[FileInfo]:
        """List files and directories at the given path.

        Args:
            path: Directory path to list.

        Returns:
            List of FileInfo objects for each entry.
        """
        ...

    def read_bytes(self, path: str) -> bytes:
        """Read raw bytes from a file.

        Args:
            path: File path to read.

        Returns:
            File content as bytes.
        """
        ...

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read file content with line numbers.

        Args:
            path: File path to read.
            offset: Line number to start reading from (0-indexed).
            limit: Maximum number of lines to read.

        Returns:
            File content as a string with line numbers prefixed.
        """
        ...

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write content to a file.

        Args:
            path: File path to write to.
            content: Content to write (string or bytes).

        Returns:
            WriteResult with path or error.
        """
        ...

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit a file by replacing strings.

        Args:
            path: File path to edit.
            old_string: String to find and replace.
            new_string: Replacement string.
            replace_all: If True, replace all occurrences. Otherwise, replace only first.

        Returns:
            EditResult with path, error, or occurrence count.
        """
        ...

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern (e.g., "**/*.py").
            path: Base directory to search from.

        Returns:
            List of matching FileInfo objects.
        """
        ...

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search for pattern in files.

        Args:
            pattern: Regex pattern to search for.
            path: Specific file or directory to search.
            glob: Glob pattern to filter files.
            ignore_hidden: If True, ignore hidden files.

        Returns:
            List of GrepMatch objects or error string.
        """
        ...


@runtime_checkable
class SandboxProtocol(BackendProtocol, Protocol):
    """Extended protocol for backends that support command execution.

    In addition to file operations, sandbox backends can execute shell commands.
    This is useful for running code, installing packages, or any shell operations.

    Example:
        ```python
        from pydantic_ai_backends import SandboxProtocol, DockerSandbox

        def run_python_script(sandbox: SandboxProtocol, script: str) -> str:
            sandbox.write("/tmp/script.py", script)
            result = sandbox.execute("python /tmp/script.py", timeout=30)
            return result.output
        ```
    """

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Execute a shell command.

        Args:
            command: Command to execute.
            timeout: Maximum execution time in seconds.

        Returns:
            ExecuteResponse with output, exit code, and truncation status.
        """
        ...

    @property
    def id(self) -> str:
        """Unique identifier for this sandbox instance."""
        ...


@runtime_checkable
class AsyncBackendProtocol(Protocol):
    """Async protocol for file storage backends.

    Sync backends can be adapted with :func:`pydantic_ai_backends.ensure_async`.
    """

    async def exists(self, path: str) -> bool: ...
    async def ls_info(self, path: str) -> list[FileInfo]: ...
    async def read_bytes(self, path: str) -> bytes: ...
    async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str: ...
    async def write(self, path: str, content: str | bytes) -> WriteResult: ...
    async def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult: ...
    async def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]: ...
    async def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str: ...


@runtime_checkable
class AsyncSandboxProtocol(AsyncBackendProtocol, Protocol):
    """Async protocol for sandbox backends that support command execution."""

    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse: ...


@runtime_checkable
class BackgroundSandboxProtocol(SandboxProtocol, Protocol):
    """Optional sandbox extension for long-lived (background) processes.

    A backend opts in by implementing these methods; consumers detect support
    with ``isinstance(backend, BackgroundSandboxProtocol)``. Unlike `execute`
    (which runs to completion and reaps the process tree), a background process
    keeps running after the call returns — its output is drained incrementally
    via `read_background` and it is stopped explicitly via `kill_background`.

    Implementations MUST clean up all background processes in
    `kill_all_background` (called on session teardown) so none are orphaned.
    """

    def execute_background(self, command: str) -> BackgroundHandle:
        """Start `command` detached and return a handle immediately."""
        ...

    def read_background(self, shell_id: str) -> BackgroundOutput:
        """Return output produced since the previous read, plus run status."""
        ...

    def kill_background(self, shell_id: str) -> bool:
        """Stop a background process. Returns True if it was running."""
        ...

    def list_background(self) -> list[BackgroundProcessInfo]:
        """Return status for every tracked background process."""
        ...

    def kill_all_background(self) -> None:
        """Stop every background process and release its resources."""
        ...


@runtime_checkable
class AsyncBackgroundSandboxProtocol(AsyncSandboxProtocol, Protocol):
    """Async counterpart to :class:`BackgroundSandboxProtocol`."""

    async def execute_background(self, command: str) -> BackgroundHandle: ...
    async def read_background(self, shell_id: str) -> BackgroundOutput: ...
    async def kill_background(self, shell_id: str) -> bool: ...
    async def list_background(self) -> list[BackgroundProcessInfo]: ...
    async def kill_all_background(self) -> None: ...
