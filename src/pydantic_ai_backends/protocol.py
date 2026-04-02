"""Protocol definitions for backends."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pydantic_ai_backends.types import (
        EditResult,
        ExecuteResponse,
        FileInfo,
        GrepMatch,
        RuntimeConfig,
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

    def ls_info(self, path: str) -> list[FileInfo]:
        """List files and directories at the given path.

        Args:
            path: Directory path to list.

        Returns:
            List of FileInfo objects for each entry.
        """
        ...

    def _read_bytes(self, path: str) -> bytes:
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
class SandboxSessionManager(Protocol):
    """Session management for SandboxProtocol sandboxes."""

    @property
    def sessions(self) -> dict[str, SandboxProtocol]:
        """Active sessions dictionary (read-only access)."""
        ...

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        ...

    async def get_or_create(
        self,
        session_id: str,
        runtime: RuntimeConfig | str | None = None,
    ) -> SandboxProtocol:
        """Get an existing sandbox or create a new one.

        If a sandbox exists for the session_id and is still alive,
        it will be returned. Otherwise, a new sandbox will be created.

        Args:
            session_id: Unique identifier for the session.
            runtime: RuntimeConfig or name to use (defaults a runtime
            configuration specified in the manager instance).

        Returns:
            SandboxProtocol instance for the session.

        Raises:
            ValueError: If no runtime specified and no default runtime set.
        """
        ...

    async def release(self, session_id: str) -> bool:
        """Release a session and stop its container.

        Args:
            session_id: Session identifier to release.

        Returns:
            True if session was found and released, False otherwise.
        """
        ...

    async def cleanup_idle(self, max_idle: int | None = None) -> int:
        """Clean up idle sessions.

        Removes and stops sandboxes that have been idle for longer than
        the specified time.

        Args:
            max_idle: Maximum idle time in seconds. Uses default if
            not specified.

        Returns:
            Number of sessions cleaned up.
        """
        ...

    def start_cleanup_loop(self, interval: int = 300) -> None:
        """Start background cleanup loop.

        Periodically cleans up idle sessions.

        Args:
            interval: Cleanup interval in seconds (default: 5 minutes).
        """
        ...

    def stop_cleanup_loop(self) -> None:
        """Stop the background cleanup loop."""
        ...

    async def shutdown(self) -> int:
        """Shutdown all sessions and stop cleanup loop.

        Returns:
            Number of sessions that were stopped.
        """
        ...

    def __contains__(self, session_id: str) -> bool:
        """Check if a session exists."""
        ...

    def __len__(self) -> int:
        """Return number of active sessions."""
        ...
