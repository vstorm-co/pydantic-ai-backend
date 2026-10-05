"""What the console tools need of a workspace, built on Pydantic AI's `Workspace`.

The tools speak in operations a model reasons about — read a numbered slice,
edit a string, grep a tree — and a workspace offers the primitives under them:
bytes, entries, a command. :class:`WorkspaceOps` is the translation.

A path that is missing, a directory or not permitted is an answer about that
path, and comes back as a result the model can act on. A workspace that cannot
answer at all — none attached, its environment gone, a read that timed out —
raises instead. Folding that into "empty" or "not found" would send the model on
in an environment that is not there, the one thing a workspace refuses to do;
the console tools report it as the call's error.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath
from typing import Protocol

from pydantic_ai.workspaces import (
    SupportsCommands,
    Workspace,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
)
from wcmatch import glob as wcglob

from pydantic_ai_backends._confined import confinement
from pydantic_ai_backends._editing import Replacement, replace_in_content
from pydantic_ai_backends._limits import MAX_EXECUTE_OUTPUT_BYTES
from pydantic_ai_backends._text import bytes_to_text
from pydantic_ai_backends.toolsets import _shell
from pydantic_ai_backends.types import (
    EditResult,
    ExecuteResponse,
    FileInfo,
    GrepMatch,
    WriteResult,
)

TIMED_OUT_EXIT_CODE = 124
"""What `timeout(1)` reports for a command stopped at its deadline."""

SEARCH_TIMEOUT = 120
"""Ceiling on `glob_info` and `grep_raw`, which walk a tree rather than one file.

A command with no deadline is one nothing reclaims, and a legitimate grep over a
large repository is slow in a way one file's read never is."""


class FileOps(Protocol):
    """The operations every console tool reaches a workspace through."""

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
    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse: ...


def _extension(path: str) -> str:
    return PurePosixPath(path).suffix.lower().lstrip(".")


def _bounded(output: str) -> tuple[str, bool]:
    """`output` cut to the execute ceiling, and whether anything was cut."""
    raw = output.encode("utf-8")
    if len(raw) <= MAX_EXECUTE_OUTPUT_BYTES:
        return output, False
    return raw[:MAX_EXECUTE_OUTPUT_BYTES].decode("utf-8", errors="ignore"), True


class WorkspaceOps:
    """The console tools' operations on one Pydantic AI `Workspace`.

    File operations use the workspace's file methods, so they work on a
    read-only workspace and on one without commands, and a policy wrapped around
    the workspace applies to each. `glob_info` and `grep_raw` run `find` and
    `grep` where the workspace runs commands, and walk its files where it does
    not.

    Args:
        workspace: The workspace every operation reaches, usually `ctx.workspace`.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    @property
    def workspace(self) -> Workspace:
        """The workspace every operation reaches."""
        return self._workspace

    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Run `command` through the workspace's shell, stderr after stdout.

        A workspace's own refusals — read-only, no commands, gone — come back as a
        failed command the model can read. Anything else raises: a provider's
        transport error, and the exceptions that steer a run, such as the
        `ApprovalRequired` a policy wrapped around the workspace raises to have a
        human approve the command.
        """
        if not isinstance(self._workspace.backend, SupportsCommands):
            # Checked here because `Workspace.run` reports it as `UserError`, which
            # the tools let through as misuse - and a model calling `execute` in a
            # files-only workspace has not misused anything.
            return ExecuteResponse(
                output="Error: This workspace does not support command execution.", exit_code=1
            )
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
        except (OSError, WorkspaceError) as error:
            return ExecuteResponse(output=f"Error: {error}", exit_code=1)
        output, truncated = _bounded(result.stdout + result.stderr)
        return ExecuteResponse(output=output, exit_code=result.exit_code, truncated=truncated)

    async def exists(self, path: str) -> bool:
        """Whether `path` is a regular file; a directory is not one."""
        try:
            return (await self._workspace.stat(path)).is_dir is False
        except OSError:
            return False

    async def ls_info(self, path: str) -> list[FileInfo]:
        """One directory's entries, or `[]` when the path is not a listable directory."""
        try:
            entries = await self._workspace.list_dir(path)
        except OSError:
            return []
        listed = [
            FileInfo(name=entry.name, path=entry.path, is_dir=entry.is_dir, size=entry.size)
            for entry in entries
        ]
        return sorted(listed, key=lambda entry: (not entry["is_dir"], entry["name"]))

    async def read_bytes(self, path: str) -> bytes:
        """A whole file, or `b""` when the path is not a readable file.

        Empty rather than an error message, because a caller cannot tell a
        message from content: a probe staging a screenshot would show the text.
        """
        try:
            return await self._workspace.read_bytes(path)
        except (OSError, ValueError):
            return b""

    async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """A slice of a text file, numbered by its real line positions."""
        try:
            data = await self._workspace.read_bytes(path)
        except FileNotFoundError:
            return f"Error: File '{path}' not found"
        except (OSError, WorkspaceError, ValueError) as error:
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
        except (OSError, WorkspaceError, ValueError) as error:
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

    def _runs_commands(self) -> bool:
        """Whether this workspace will run `find` and `grep` for a search."""
        return not self._workspace.read_only and isinstance(
            self._workspace.backend, SupportsCommands
        )

    async def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Files matching `pattern` under `path`.

        Through `find` where the workspace runs commands, and by walking its
        directories otherwise, so a read-only or file-only workspace can search.

        Raises:
            WorkspacePathError: The workspace is confined and `path` leads out of it.
        """
        await self._check_search_root(path)
        if self._runs_commands():
            return _shell.parse_glob(await self._search(_shell.glob_command(pattern, path)))
        root = "." if path.strip() in _shell.ROOT_SPELLINGS else path
        anywhere = pattern[3:] if pattern.startswith("**/") else pattern
        base = await self._workspace.resolve(root)
        matches = [
            FileInfo(name=posixpath.basename(file), path=file, is_dir=False, size=None)
            for file in await self._files_under(base)
            if wcglob.globmatch(
                posixpath.relpath(file, base), f"**/{anywhere}", flags=wcglob.GLOBSTAR
            )
        ]
        return sorted(matches, key=lambda entry: entry["path"])

    async def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Lines matching `pattern`.

        Through `grep` where the workspace runs commands, and by reading its files
        otherwise. Either way a binary file is skipped rather than matched.
        """
        try:
            await self._check_search_root(path)
        except WorkspaceError as error:
            return f"Error: {error}"
        if self._runs_commands():
            command = _shell.grep_command(pattern, path, glob, ignore_hidden)
            found = _shell.parse_grep(await self._search(command))
            if isinstance(found, str) or not ignore_hidden:
                return found
            return [match for match in found if not _shell.hidden_match(match["path"], path)]
        try:
            regex = re.compile(pattern)
        except re.error as error:
            return f"Error: Invalid regex pattern: {error}"
        base = await self._workspace.resolve(path or ".")
        try:
            is_file = not (await self._workspace.stat(base)).is_dir
        except (OSError, WorkspaceError) as error:
            return f"Error: {error}"
        candidates = [base] if is_file else await self._files_under(base)
        matches: list[GrepMatch] = []
        for file in candidates:
            relative = posixpath.relpath(file, base)
            if (
                not is_file
                and ignore_hidden
                and any(part.startswith(".") for part in relative.split("/"))
            ):
                continue
            if glob and not wcglob.globmatch(posixpath.basename(file), glob, flags=wcglob.BRACE):
                continue
            try:
                text = (await self._workspace.read_bytes(file)).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            matches.extend(
                GrepMatch(path=file, line_number=number, line=line)
                for number, line in enumerate(text.splitlines(), start=1)
                if regex.search(line)
            )
        return matches

    async def _check_search_root(self, path: str | None) -> None:
        """Refuse a search rooted outside a `ConfinedWorkspace`.

        `find` and `grep` run as commands, which the confinement leaves alone, so
        the root is checked here. Neither follows a symlink below the root - `find`
        never does, `grep -r` only for one named on its command line, which is the
        root itself - so the root is the one path that needs it.
        """
        confined = confinement(self._workspace)
        if confined is not None:
            await confined.check(
                "." if path is None or path.strip() in _shell.ROOT_SPELLINGS else path
            )

    async def _search(self, command: str) -> ExecuteResponse:
        """Run a `find` or `grep`, letting a workspace failure raise.

        Not through :meth:`execute`, which reports a failure as exit status 1 -
        exactly what `grep` answers for no match, so a dropped connection would
        reach the model as "No matches" instead of an error.
        """
        result = await self._workspace.run(command, shell=True, timeout=SEARCH_TIMEOUT)
        return ExecuteResponse(output=result.stdout + result.stderr, exit_code=result.exit_code)

    async def _files_under(self, root: str) -> list[str]:
        """Every file below `root`, found by listing directories; unlistable ones skipped."""
        files: list[str] = []
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = await self._workspace.list_dir(directory)
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir:
                    pending.append(entry.path)
                else:
                    files.append(entry.path)
        return files
