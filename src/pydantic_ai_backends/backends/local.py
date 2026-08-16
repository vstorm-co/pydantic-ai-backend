"""Local filesystem backend with optional shell execution."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai_backends._editing import Replacement, replace_in_content
from pydantic_ai_backends._limits import DEFAULT_READ_LIMIT, MAX_EXECUTE_OUTPUT_BYTES
from pydantic_ai_backends._time import iso_mtime
from pydantic_ai_backends.backends._background import BackgroundProcesses
from pydantic_ai_backends.backends._guard import PermissionGuard
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

if TYPE_CHECKING:
    from pydantic_ai_backends.permissions.checker import (
        AskCallback,
        AskFallback,
        PermissionChecker,
    )
    from pydantic_ai_backends.permissions.types import (
        PermissionOperation,
        PermissionRuleset,
    )

DEFAULT_EXECUTE_TIMEOUT = 120

MAX_READ_OUTPUT_CHARS = 200_000
"""Character ceiling for one `read`.

A default read that exceeds it is truncated to a page; an *explicit*
offset/limit that still exceeds it errors, so the agent narrows its request
instead of flooding the context.
"""

GREP_SKIP_DIRS = frozenset(
    {
        ".eggs",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
"""Directories the Python grep fallback skips.

ripgrep already honors .gitignore; this gives the fallback comparable
"don't search build junk" behavior so a grep without ripgrep does not drown in
node_modules and caches.
"""

GREP_TIMEOUT_SECONDS = 30


def is_ignored_path(parts: tuple[str, ...], ignore_hidden: bool) -> bool:
    """Whether the Python grep fallback should skip a path.

    With `ignore_hidden` (the default) hidden and build/cache directories are
    skipped; without it nothing is, so "search everything" really does.
    """
    if not ignore_hidden:
        return False
    return any(part in GREP_SKIP_DIRS or part.startswith(".") for part in parts)


def shell_argv(command: str) -> list[str]:
    """Wrap a command string for the platform's shell."""
    return ["cmd", "/c", command] if sys.platform == "win32" else ["sh", "-c", command]


class LocalBackend:
    """Local filesystem backend with optional shell execution.

    File operations are native Python; `execute` shells out. Both are confined
    to `allowed_directories`, and can be narrowed further with a permission
    ruleset.

    Example:
        ```python
        from pydantic_ai_backends import LocalBackend

        backend = LocalBackend(root_dir="/workspace")
        backend.write("/src/app.py", "print('hello')")
        result = backend.execute("python /src/app.py")

        readonly = LocalBackend(
            allowed_directories=["/home/user/project"],
            enable_execute=False,
        )
        ```
    """

    def __init__(
        self,
        root_dir: str | Path | None = None,
        allowed_directories: list[str] | None = None,
        enable_execute: bool = True,
        sandbox_id: str | None = None,
        permissions: PermissionRuleset | None = None,
        ask_callback: AskCallback | None = None,
        ask_fallback: AskFallback = "error",
    ):
        """Initialize the backend.

        Args:
            root_dir: Base directory for file operations. Defaults to the first
                allowed directory, or the current working directory.
            allowed_directories: Directories file operations are confined to,
                resolved to absolute paths and created when missing. When
                omitted, only `root_dir` is reachable.
            enable_execute: Whether shell execution is available.
            sandbox_id: Unique identifier for this backend instance.
            permissions: Optional ruleset applied after the allowed-directory
                check passes.
            ask_callback: Async callback for "ask" actions, receiving
                `(operation, target, reason)` and returning whether to allow.
            ask_fallback: What an unanswerable "ask" does — `"deny"` refuses the
                operation, `"error"` raises.
        """
        self._id = sandbox_id or str(uuid.uuid4())
        self._enable_execute = enable_execute
        self._permissions = permissions

        self._allowed_directories = [Path(d).resolve() for d in allowed_directories or []]
        for directory in self._allowed_directories:
            directory.mkdir(parents=True, exist_ok=True)

        if root_dir is not None:
            self._root = Path(root_dir).resolve()
        elif self._allowed_directories:
            self._root = self._allowed_directories[0]
        else:
            self._root = Path.cwd()
        self._root.mkdir(parents=True, exist_ok=True)

        if not self._allowed_directories:
            self._allowed_directories = [self._root]

        self._guard = (
            PermissionGuard(permissions, self._root, ask_callback, ask_fallback)
            if permissions is not None
            else None
        )
        self._background = BackgroundProcesses(self._root)

    @property
    def id(self) -> str:
        """Unique identifier for this backend."""
        return self._id

    @property
    def root_dir(self) -> Path:
        """Directory relative paths resolve against, and commands run in."""
        return self._root

    @property
    def execute_enabled(self) -> bool:
        """Whether shell execution is enabled."""
        return self._enable_execute

    @property
    def permissions(self) -> PermissionRuleset | None:
        """The permission ruleset for this backend, if any."""
        return self._permissions

    @property
    def permission_checker(self) -> PermissionChecker | None:
        """The permission checker for this backend, if any."""
        return self._guard.checker if self._guard else None

    def _denial_reason(self, operation: PermissionOperation, target: str) -> str | None:
        return self._guard.denial_reason(operation, target) if self._guard else None

    def _is_denied(self, operation: PermissionOperation, target: str) -> bool:
        return self._guard is not None and self._guard.is_denied(operation, target)

    def _resolve(self, path: str) -> Path:
        """Resolve `path` inside the allowed directories.

        Args:
            path: Absolute path, or one relative to the root directory.

        Raises:
            PermissionError: If the resolved path escapes every allowed
                directory — including via `..` or a symlink, since resolution
                happens before the check.
        """
        candidate = Path(path)
        resolved = candidate.resolve() if candidate.is_absolute() else (self._root / path).resolve()

        for allowed in self._allowed_directories:
            if resolved == allowed or allowed in resolved.parents:
                return resolved

        allowed_str = ", ".join(str(d) for d in self._allowed_directories)
        raise PermissionError(
            f"Access denied: '{path}' is outside allowed directories ({allowed_str})"
        )

    # ── Files ──────────────────────────────────────────────────────────

    def exists(self, path: str) -> bool:
        """Whether `path` is a regular file inside the allowed directories.

        Missing files, directories, paths outside the allowed set and paths the
        filesystem refuses to stat at all (embedded null bytes, `ELOOP`, a name
        that is too long) are all `False`. Use `ls_info` when the reason matters.
        """
        try:
            return self._resolve(path).is_file()
        except (PermissionError, ValueError, OSError):
            return False

    def ls_info(self, path: str) -> list[FileInfo]:
        """List one directory, omitting entries denied for "ls".

        An "ask" counts as visible, since a listing cannot prompt.
        """
        try:
            full_path = self._resolve(path)
        except PermissionError:
            return []

        if self._is_denied("ls", str(full_path)) or not full_path.exists():
            return []

        if full_path.is_file():
            return [_entry_info(full_path)]

        results: list[FileInfo] = []
        try:
            for entry in full_path.iterdir():
                try:
                    self._resolve(str(entry))
                    if self._is_denied("ls", str(entry)):
                        continue
                    info = _entry_info(entry)
                except PermissionError:
                    continue
                except OSError:
                    # Per entry, not per listing, matching `glob_info`: a file
                    # that vanished or cannot be stat'd between `iterdir` and
                    # `_entry_info` used to abort the whole loop and take the
                    # directory's other rows with it.
                    continue
                results.append(info)
        except (PermissionError, OSError):
            # The walk itself failed, so there is nothing left to collect.
            return []

        return sorted(results, key=lambda x: (not x["is_dir"], x["name"]))

    def read_bytes(self, path: str) -> bytes:
        """Read a whole file as bytes, or `b""` when it cannot be read.

        The same "read" rules as :meth:`read` apply; a denied path is `b""`.
        """
        try:
            full_path = self._resolve(path)
        except PermissionError:
            return b""

        if self._denial_reason("read", str(full_path)) is not None:
            return b""
        if not full_path.is_file():
            return b""

        try:
            return full_path.read_bytes()
        except (PermissionError, OSError):
            return b""

    def read(self, path: str, offset: int = 0, limit: int = DEFAULT_READ_LIMIT) -> str:
        """Read a slice of a file with line numbers."""
        try:
            full_path = self._resolve(path)
        except PermissionError as e:
            return f"Error: {e}"

        denial = self._denial_reason("read", str(full_path))
        if denial:
            return f"Error: {denial}"

        if not full_path.exists():
            return f"Error: File '{path}' not found"
        if full_path.is_dir():
            return f"Error: '{path}' is a directory"

        try:
            with open(full_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except PermissionError:
            return f"Error: Permission denied for '{path}'"
        except OSError as e:
            return f"Error: {e}"

        if offset >= len(lines):
            return f"Error: Offset {offset} exceeds file length ({len(lines)} lines)"

        end = min(offset + limit, len(lines))
        result = "\n".join(_numbered_line(i + 1, lines[i]) for i in range(offset, end))
        if end < len(lines):
            result += f"\n\n... ({len(lines) - end} more lines)"

        return _within_read_ceiling(result, explicit=offset != 0 or limit != DEFAULT_READ_LIMIT)

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write a file, creating parent directories as needed."""
        try:
            full_path = self._resolve(path)
        except PermissionError as e:
            return WriteResult(error=str(e))

        denial = self._denial_reason("write", str(full_path))
        if denial:
            return WriteResult(error=denial)

        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                full_path.write_bytes(content)
            else:
                full_path.write_text(_normalize_newlines(content), encoding="utf-8")
            return WriteResult(path=str(full_path))
        except PermissionError:
            return WriteResult(error=f"Permission denied for '{path}'")
        except OSError as e:
            return WriteResult(error=str(e))

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit a file by replacing a string."""
        try:
            full_path = self._resolve(path)
        except PermissionError as e:
            return EditResult(error=str(e))

        denial = self._denial_reason("edit", str(full_path))
        if denial:
            return EditResult(error=denial)

        if not full_path.exists():
            return EditResult(error=f"File '{path}' not found")

        try:
            content = full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Refused rather than decoded with replacement characters: writing the
            # result back would substitute every undecodable byte for U+FFFD and
            # destroy the file. `read` can afford `errors="replace"` because it
            # only displays the content; `edit` stores it again.
            return EditResult(error=f"'{path}' is not valid UTF-8 text and cannot be edited")
        except PermissionError:
            return EditResult(error=f"Permission denied for '{path}'")
        except OSError as e:
            return EditResult(error=str(e))

        outcome = replace_in_content(content, old_string, new_string, replace_all)
        if not isinstance(outcome, Replacement):
            return EditResult(error=outcome)

        try:
            full_path.write_text(_normalize_newlines(outcome.content), encoding="utf-8")
        except PermissionError:
            return EditResult(error=f"Permission denied for '{path}'")
        except OSError as e:
            return EditResult(error=str(e))

        return EditResult(path=str(full_path), occurrences=outcome.occurrences)

    def glob_info(self, pattern: str, path: str = ".") -> list[FileInfo]:
        """Match files by glob, most recently modified first.

        That ordering matches ripgrep and Claude Code, and is usually what an
        agent wants; the path breaks ties so the order is stable. Matches denied
        for "glob" are omitted, and "ask" counts as visible.
        """
        try:
            base_path = self._resolve(path)
        except PermissionError:
            return []

        if self._is_denied("glob", str(base_path)) or not base_path.exists():
            return []

        collected: list[tuple[float, str, FileInfo]] = []
        try:
            for match in base_path.glob(pattern):  # pragma: no branch
                try:
                    if not match.is_file():
                        continue
                    self._resolve(str(match))
                    if self._is_denied("glob", str(match)):
                        continue
                    stat = match.stat()
                except PermissionError:
                    continue
                except OSError:
                    # Per entry, not per walk. One file that vanished or cannot
                    # be stat'd between the glob and the stat used to abort the
                    # whole loop and return whatever had been collected — a
                    # silently short answer, which is worse than a missing row
                    # and worse than an error. `ls_info` and grep both skip and
                    # carry on; this now matches them.
                    continue
                info = FileInfo(
                    name=match.name,
                    path=str(match),
                    is_dir=False,
                    size=stat.st_size,
                    modified_at=iso_mtime(stat.st_mtime),
                )
                collected.append((stat.st_mtime, str(match), info))
        except (PermissionError, OSError):
            # The walk itself failed, so there is nothing left to collect.
            pass

        collected.sort(key=lambda item: (-item[0], item[1]))
        return [info for _mtime, _path, info in collected]

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search file contents, using ripgrep when it is installed.

        Files denied for "grep" — or for "read", since a match carries content —
        never contribute results. An explicit "grep" deny on the search path
        errors the whole search.
        """
        search_path = path or str(self._root)

        try:
            validated = self._resolve(search_path)
        except PermissionError as e:
            return str(e)

        if self._is_denied("grep", str(validated)):
            return f"Error: Permission denied for grep on '{search_path}'"

        if shutil.which("rg") is not None and not validated.is_file():
            return self._grep_ripgrep(pattern, validated, glob, ignore_hidden)
        return self._grep_python(pattern, validated, glob, ignore_hidden)

    def _grep_ripgrep(
        self, pattern: str, search_path: Path, glob: str | None, ignore_hidden: bool
    ) -> list[GrepMatch] | str:
        argv = ["rg", "--line-number", "--no-heading", pattern]
        if glob:
            argv.extend(["--glob", glob])
        if not ignore_hidden:
            argv.append("--hidden")
        argv.append(".")

        try:
            result = subprocess.run(
                argv,
                cwd=search_path,
                capture_output=True,
                text=True,
                timeout=GREP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return "Error: Search timed out"
        except OSError as e:
            return f"Error: {e}"

        base_path = search_path.parent if search_path.is_file() else search_path
        matches: list[GrepMatch] = []
        for relative_path, line_number, line in _parse_grep_lines(result.stdout):
            full_path = (base_path / relative_path).resolve()
            try:
                self._resolve(str(full_path))
            except PermissionError:
                continue
            if self._hidden_from_grep(str(full_path)):
                continue
            matches.append(GrepMatch(path=str(full_path), line_number=line_number, line=line))
        return matches

    def _grep_python(
        self, pattern: str, search_path: Path, glob: str | None, ignore_hidden: bool
    ) -> list[GrepMatch] | str:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: Invalid regex pattern: {e}"

        if not search_path.exists():
            return f"Error: Path '{search_path}' not found"

        if search_path.is_file():
            files = [search_path]
        else:
            candidates = search_path.glob(glob) if glob else search_path.rglob("*")
            files = [f for f in candidates if not is_ignored_path(f.parts, ignore_hidden)]

        matches: list[GrepMatch] = []
        for file_path in files:
            if not file_path.is_file():
                continue
            try:
                self._resolve(str(file_path))
            except PermissionError:
                continue
            if self._hidden_from_grep(str(file_path)):
                continue

            try:
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f):
                        if regex.search(line):
                            matches.append(
                                GrepMatch(
                                    path=str(file_path),
                                    line_number=i + 1,
                                    line=line.rstrip("\n\r"),
                                )
                            )
            except (PermissionError, OSError):
                continue

        return matches

    def _hidden_from_grep(self, path: str) -> bool:
        return self._guard is not None and self._guard.hides_from_grep(path)

    # ── Commands ───────────────────────────────────────────────────────

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Run a shell command in the root directory.

        Args:
            command: Command to execute.
            timeout: Maximum execution time in seconds. Defaults to
                `DEFAULT_EXECUTE_TIMEOUT`.

        Raises:
            RuntimeError: If execution is disabled for this backend.
        """
        denial = self._execute_denial(command)
        if denial is not None:
            return ExecuteResponse(output=f"Error: {denial}", exit_code=1, truncated=False)

        try:
            result = subprocess.run(
                shell_argv(command),
                cwd=self._root,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else DEFAULT_EXECUTE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output="Error: Command timed out", exit_code=124, truncated=False
            )
        except Exception as e:
            return ExecuteResponse(output=f"Error: {e}", exit_code=1, truncated=False)

        return _execute_response(result.stdout + result.stderr, result.returncode)

    async def async_execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Cancellable version of :meth:`execute`.

        Cancelling the calling task kills the subprocess immediately instead of
        waiting for a thread to finish. On Unix the process gets its own session
        so the whole tree — including grandchildren the shell forked — is reaped
        on cancellation or timeout.
        """
        denial = self._execute_denial(command)
        if denial is not None:
            return ExecuteResponse(output=f"Error: {denial}", exit_code=1, truncated=False)

        try:
            process = await asyncio.create_subprocess_exec(
                *shell_argv(command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._root,
                # New session so the whole group can be killed on Unix; on
                # Windows the `cmd /c` lifecycle already takes the tree down.
                start_new_session=(sys.platform != "win32"),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout if timeout is not None else DEFAULT_EXECUTE_TIMEOUT,
                )
            except asyncio.CancelledError:
                _kill_process_tree(process)
                # Shielded so a second cancel cannot leave the pipes dangling.
                with contextlib.suppress(BaseException):
                    await asyncio.shield(asyncio.ensure_future(process.communicate()))
                raise
            except asyncio.TimeoutError:
                _kill_process_tree(process)
                with contextlib.suppress(BaseException):
                    await process.communicate()
                return ExecuteResponse(
                    output="Error: Command timed out", exit_code=124, truncated=False
                )

            output = stdout.decode("utf-8", errors="replace")
            output += stderr.decode("utf-8", errors="replace")
            return _execute_response(output, process.returncode)
        except Exception as e:
            return ExecuteResponse(output=f"Error: {e}", exit_code=1, truncated=False)

    def _execute_denial(self, command: str) -> str | None:
        """Why the command may not run, or `None` when it may.

        Raises:
            RuntimeError: If execution is disabled for this backend, which is a
                misconfiguration rather than a refused command.
        """
        if not self._enable_execute:
            raise RuntimeError(
                "Shell execution is disabled for this backend. "
                "Initialize with enable_execute=True to enable."
            )
        return self._guard.execute_denial_reason(command) if self._guard else None

    # ── Background processes ───────────────────────────────────────────

    def execute_background(self, command: str) -> BackgroundHandle:
        """Start `command` as a detached, long-lived process.

        Returns immediately. The process keeps running after this call — drain
        its output with :meth:`read_background` and stop it with
        :meth:`kill_background`.

        Raises:
            RuntimeError: If execution is disabled for this backend.
            PermissionError: If the ruleset refuses the command.
        """
        denial = self._execute_denial(command)
        if denial is not None:
            raise PermissionError(denial)
        return self._background.start(shell_argv(command), command)

    def read_background(self, shell_id: str) -> BackgroundOutput:
        """Return output produced since the previous read, plus run status."""
        return self._background.read(shell_id)

    def kill_background(self, shell_id: str) -> bool:
        """Stop a background process. Returns whether it was still running."""
        return self._background.kill(shell_id)

    def list_background(self) -> list[BackgroundProcessInfo]:
        """Status of every tracked background process."""
        return self._background.list()

    def kill_all_background(self) -> None:
        """Stop every background process and remove its on-disk output."""
        self._background.kill_all()

    def __del__(self) -> None:  # pragma: no cover - best-effort GC cleanup
        with contextlib.suppress(Exception):
            if self._background:
                self._background.kill_all()


def _numbered_line(number: int, line: str) -> str:
    """Render one `read` line, keeping trailing whitespace that is not a newline."""
    stripped = line.rstrip("\n\r")
    return f"{number:>6}\t{stripped}"


def _entry_info(path: Path) -> FileInfo:
    stat = path.stat()
    is_file = path.is_file()
    return FileInfo(
        name=path.name,
        path=str(path),
        is_dir=path.is_dir(),
        size=stat.st_size if is_file else None,
        modified_at=iso_mtime(stat.st_mtime) if is_file else None,
    )


def _normalize_newlines(content: str) -> str:
    r"""Drop carriage returns so text-mode writes do not double them.

    `Path.write_text` opens in text mode, where only `\n` is translated to
    `os.linesep` while an existing `\r` is left alone — content that already
    holds `\r\n` would become `\r\r\n` on Windows.
    """
    return content.replace("\r", "")


def _within_read_ceiling(result: str, *, explicit: bool) -> str:
    """Keep one read from flooding the agent's context.

    An explicit range that is still too large errors so the agent narrows it; a
    default read is truncated to a page, so a plain `read_file(path)` never
    hard-fails.
    """
    if len(result) <= MAX_READ_OUTPUT_CHARS:
        return result

    if explicit:
        return (
            f"Error: The requested range is too large to return "
            f"({len(result):,} chars, limit {MAX_READ_OUTPUT_CHARS:,}). "
            "Read a smaller slice with a lower `limit`, or use `grep` "
            "to locate the part you need."
        )
    return (
        result[:MAX_READ_OUTPUT_CHARS]
        + f"\n\n... (truncated at {MAX_READ_OUTPUT_CHARS:,} chars — pass a smaller "
        "`limit`/`offset` or use `grep` to read the rest)"
    )


def _execute_response(output: str, returncode: int | None) -> ExecuteResponse:
    truncated = len(output) > MAX_EXECUTE_OUTPUT_BYTES
    if truncated:
        output = output[:MAX_EXECUTE_OUTPUT_BYTES]
    return ExecuteResponse(
        output=output,
        exit_code=returncode if returncode is not None else 1,
        truncated=truncated,
    )


def _parse_grep_lines(stdout: str) -> list[tuple[str, int, str]]:
    """Parse ripgrep's `path:line:content` output, skipping malformed rows."""
    rows: list[tuple[str, int, str]] = []
    for line in stdout.strip().split("\n"):
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((parts[0], int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Kill the subprocess and, on Unix, every grandchild it forked."""
    if sys.platform == "win32":
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
