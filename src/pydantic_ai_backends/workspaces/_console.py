"""This library's sandbox protocol over a Pydantic AI workspace.

What lets `ConsoleCapability` run in whatever workspace the run has — one of the
capabilities in this package, or E2B, Modal, a local directory — instead of a
backend of its own. The direction is the reverse of the rest of the package:
there a sandbox of ours becomes a workspace, here any workspace becomes a
sandbox the console tools can use.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic_ai.workspaces import (
    Workspace,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
)

from pydantic_ai_backends._editing import Replacement, replace_in_content
from pydantic_ai_backends._limits import MAX_EXECUTE_OUTPUT_BYTES
from pydantic_ai_backends._text import bytes_to_text
from pydantic_ai_backends.backends.base import AsyncBaseSandbox
from pydantic_ai_backends.types import EditResult, ExecuteResponse, FileInfo, WriteResult

TIMED_OUT_EXIT_CODE = 124
"""What `timeout(1)` and `LocalBackend` report for a command stopped at its deadline."""


def _extension(path: str) -> str:
    return PurePosixPath(path).suffix.lower().lstrip(".")


def _bounded(output: str) -> tuple[str, bool]:
    """`output` cut to the execute ceiling, and whether anything was cut."""
    raw = output.encode("utf-8")
    if len(raw) <= MAX_EXECUTE_OUTPUT_BYTES:
        return output, False
    return raw[:MAX_EXECUTE_OUTPUT_BYTES].decode("utf-8", errors="ignore"), True


class WorkspaceSandbox(AsyncBaseSandbox):
    """A Pydantic AI `Workspace`, behind this library's async sandbox protocol.

    File operations go through the workspace's own file methods, so they work on
    a read-only workspace and on one without commands, and a wrapper's policy —
    `ReadOnlyWorkspace`, or a host's own `WrapperWorkspace` — applies to every
    one of them. `glob_info` and `grep_raw` keep the shell defaults of
    :class:`AsyncBaseSandbox`, so they need a workspace that runs commands.

    Like every backend here it returns failures instead of raising them: a
    workspace error becomes an `Error:` result the model can read, and the run
    goes on.

    Args:
        workspace: The workspace to operate on, usually `ctx.workspace`.
    """

    def __init__(self, workspace: Workspace) -> None:
        ref = workspace.ref
        super().__init__(None if ref is None else f"{ref.provider}:{ref.id}")
        self._workspace = workspace

    @property
    def workspace(self) -> Workspace:
        """The workspace every operation reaches."""
        return self._workspace

    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Run `command` through the workspace's shell, stderr after stdout."""
        self.touch()
        try:
            result = await self._workspace.run(command, shell=True, timeout=timeout)
        except WorkspaceTimeoutError as error:
            partial, _ = _bounded(error.stdout + error.stderr)
            return ExecuteResponse(
                output=f"Error: Command timed out\n{partial}".rstrip("\n"),
                exit_code=TIMED_OUT_EXIT_CODE,
            )
        except WorkspaceOutputLimitError as error:
            partial, _ = _bounded(error.stdout + error.stderr)
            return ExecuteResponse(output=partial, exit_code=1, truncated=True)
        except Exception as error:
            # Every workspace refusal — read-only, no commands, gone — and every
            # provider failure: the protocol returns them, and the model reads
            # the reason instead of the run ending on it.
            return ExecuteResponse(output=f"Error: {error}", exit_code=1)
        output, truncated = _bounded(result.stdout + result.stderr)
        return ExecuteResponse(output=output, exit_code=result.exit_code, truncated=truncated)

    async def exists(self, path: str) -> bool:
        """Whether `path` is a regular file; a directory is not one."""
        try:
            return (await self._workspace.stat(path)).is_dir is False
        except (OSError, WorkspaceError):
            return False

    async def ls_info(self, path: str) -> list[FileInfo]:
        """One directory's entries, or `[]` when it cannot be listed."""
        try:
            entries = await self._workspace.list_dir(path)
        except (OSError, WorkspaceError):
            return []
        return [
            FileInfo(name=entry.name, path=entry.path, is_dir=entry.is_dir, size=entry.size)
            for entry in entries
        ]

    async def read_bytes(self, path: str) -> bytes:
        """A whole file, or `b""` when it cannot be read."""
        try:
            return await self._workspace.read_bytes(path)
        except (OSError, WorkspaceError):
            return b""

    async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """A slice of a text file, numbered by its real line positions."""
        try:
            data = await self._workspace.read_bytes(path)
        except FileNotFoundError:
            return f"Error: File '{path}' not found"
        except (OSError, WorkspaceError) as error:
            return f"Error: {error}"
        try:
            lines = bytes_to_text(_extension(path), data).splitlines()
        except ValueError as error:
            return f"[Error: {error}]"
        if offset >= len(lines) and lines:
            return f"Error: Offset {offset} exceeds file length ({len(lines)} lines)"
        end = min(offset + limit, len(lines))
        numbered = "\n".join(f"{i + 1:>6}\t{lines[i]}" for i in range(offset, end))
        if end < len(lines):
            return f"{numbered}\n\n... ({len(lines) - end} more lines)"
        return numbered

    async def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write a file, creating missing parent directories."""
        data = content.encode("utf-8") if isinstance(content, str) else content
        try:
            await self._workspace.write_bytes(path, data)
        except (OSError, WorkspaceError) as error:
            return WriteResult(error=f"Failed to write '{path}': {error}")
        return WriteResult(path=path)

    async def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Replace a string in a file, read and written through the workspace."""
        try:
            data = await self._workspace.read_bytes(path)
        except FileNotFoundError:
            return EditResult(error=f"File '{path}' not found")
        except (OSError, WorkspaceError) as error:
            return EditResult(error=f"Failed to edit file: {error}")
        try:
            content = bytes_to_text(_extension(path), data)
        except ValueError as error:
            return EditResult(error=str(error))
        outcome = replace_in_content(content, old_string, new_string, replace_all)
        if not isinstance(outcome, Replacement):
            return EditResult(error=outcome)
        written = await self.write(path, outcome.content)
        if written.error:
            return EditResult(error=written.error)
        return EditResult(path=path, occurrences=outcome.occurrences)
