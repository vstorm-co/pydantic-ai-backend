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
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic_ai_backends.types import (
    EditResult,
    ExecuteResponse,
    FileInfo,
    GrepMatch,
    WriteResult,
)

if TYPE_CHECKING:
    from pydantic_ai_backends.permissions.checker import PermissionChecker
    from pydantic_ai_backends.permissions.types import (
        PermissionOperation,
        PermissionRuleset,
    )

# Callback type for asking user permission
AskCallback = Callable[["PermissionOperation", str, str], Awaitable[bool]]

# Fallback behavior when ask_callback is not provided
AskFallback = Literal["deny", "error"]

MAX_EXECUTE_OUTPUT = 100_000


def _normalize_newlines(content: str) -> str:
    """Collapse carriage returns so text-mode writes don't double them.

    ``Path.write_text`` opens in text mode, where only ``\\n`` is translated to
    ``os.linesep`` on write while existing ``\\r`` is left untouched. Content
    that already contains ``\\r\\n`` would become ``\\r\\r\\n`` on Windows.
    Stripping ``\\r`` lets text mode re-add clean platform-native endings.
    """
    return content.replace("\r", "")


class LocalBackend:
    """Local filesystem backend with optional shell execution.

    Combines file operations (Python native) with optional shell command
    execution (subprocess). File operations can be restricted to specific
    directories using `allowed_directories`.

    Example:
        ```python
        from pydantic_ai_backends import LocalBackend

        # Full access with shell execution
        backend = LocalBackend(root_dir="/workspace")
        backend.write("/src/app.py", "print('hello')")
        result = backend.execute("python /src/app.py")

        # Restricted directories, no shell
        backend = LocalBackend(
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
            root_dir: Base directory for file operations. If not provided,
                uses first allowed_directory or current working directory.
            allowed_directories: List of directories that file operations are
                restricted to. If None, only root_dir is accessible.
                Paths are resolved to absolute paths.
            enable_execute: Whether shell execution is enabled. Default True.
            sandbox_id: Unique identifier for this backend instance.
            permissions: Optional permission ruleset for fine-grained access control.
                If provided, operations are checked against this ruleset after
                the allowed_directories check passes.
            ask_callback: Async callback for "ask" permission actions. Receives
                (operation, target, reason) and returns True to allow.
            ask_fallback: What to do when ask_callback is None but needed.
                "deny" denies the operation, "error" raises PermissionError.
        """
        self._id = sandbox_id or str(uuid.uuid4())
        self._enable_execute = enable_execute
        self._permissions = permissions
        self._ask_callback = ask_callback
        self._ask_fallback = ask_fallback
        self._permission_checker: PermissionChecker | None = None

        # Initialize permission checker if ruleset provided
        if permissions is not None:
            from pydantic_ai_backends.permissions.checker import PermissionChecker

            self._permission_checker = PermissionChecker(
                ruleset=permissions,
                ask_callback=ask_callback,
                ask_fallback=ask_fallback,
            )

        # Resolve allowed directories
        self._allowed_directories: list[Path] | None = None
        if allowed_directories is not None:
            self._allowed_directories = [Path(d).resolve() for d in allowed_directories]
            # Create directories if they don't exist
            for d in self._allowed_directories:
                d.mkdir(parents=True, exist_ok=True)

        # Resolve root directory
        if root_dir is not None:
            self._root = Path(root_dir).resolve()
        elif self._allowed_directories:
            self._root = self._allowed_directories[0]
        else:
            self._root = Path.cwd()  # pragma: no cover

        # Ensure root exists
        self._root.mkdir(parents=True, exist_ok=True)

        # If no allowed_directories specified, restrict to root_dir only
        if self._allowed_directories is None:
            self._allowed_directories = [self._root]

    @staticmethod
    def _shell_cmd(command: str) -> list[str]:
        return ["cmd", "/c", command] if sys.platform == "win32" else ["sh", "-c", command]

    @property
    def id(self) -> str:
        """Unique identifier for this backend."""
        return self._id

    @property
    def root_dir(self) -> Path:
        """Get the root directory."""
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
        return self._permission_checker

    def _check_permission_sync(self, operation: PermissionOperation, target: str) -> str | None:
        """Check permission synchronously.

        Returns None if allowed, or an error message if denied.
        For "ask" actions, returns an error since this is sync.
        """
        if self._permission_checker is None:
            return None

        from pydantic_ai_backends.permissions.checker import (
            PermissionAskError,
        )

        action = self._permission_checker.check_sync(operation, target)
        if action == "allow":
            return None
        if action == "deny":
            rule = self._permission_checker._find_matching_rule(operation, target)
            if rule and rule.description:
                return f"Permission denied: {rule.description}"
            return f"Permission denied for {operation} on '{target}'"
        # action == "ask" - in sync context, we can't ask
        if self._ask_fallback == "deny":
            return f"Permission denied for {operation} on '{target}' (approval required)"
        # ask_fallback == "error"
        raise PermissionAskError(operation, target, "Approval required but no callback")

    def _validate_path(self, path: str) -> Path:
        """Validate and resolve path within allowed directories.

        Args:
            path: Path to validate (absolute or relative to root).

        Returns:
            Resolved absolute Path.

        Raises:
            PermissionError: If path is outside allowed directories.
        """
        # Handle relative paths
        if not Path(path).is_absolute():
            resolved = (self._root / path).resolve()
        else:
            resolved = Path(path).resolve()

        # Check against allowed directories
        assert self._allowed_directories is not None
        for allowed in self._allowed_directories:
            try:
                resolved.relative_to(allowed)
                return resolved
            except ValueError:
                continue

        # Path not in any allowed directory
        allowed_str = ", ".join(str(d) for d in self._allowed_directories)
        raise PermissionError(
            f"Access denied: '{path}' is outside allowed directories ({allowed_str})"
        )

    def exists(self, path: str) -> bool:
        """Check whether a file exists on the filesystem.

        Returns `False` for: missing files, directories, paths outside
        the allowed directory set, and any other invalid path the
        filesystem refuses to stat. The protocol contract is "invalid
        paths, directories, and permission errors all return False" —
        callers needing to distinguish those reasons should use
        `ls_info()`.

        Exception sources:
        - `PermissionError` from `_validate_path` when the resolved
          path escapes the allowed directory set.
        - `ValueError` from `Path.is_file()` on paths the OS rejects
          before it can stat them (notably embedded null bytes — POSIX
          rejects them at the syscall boundary).
        - `OSError` covers the remaining edge cases (filename too long,
          ELOOP on a symlink cycle, etc.). `Path.is_file()` swallows
          most of these already; the explicit catch is belt-and-braces.
        """
        try:
            validated = self._validate_path(path)
            return validated.is_file()
        except (PermissionError, ValueError, OSError):
            return False

    def ls_info(self, path: str) -> list[FileInfo]:
        """List files and directories at the given path."""
        try:
            full_path = self._validate_path(path)
        except PermissionError:  # pragma: no cover
            return []

        if not full_path.exists():  # pragma: no cover
            return []

        if full_path.is_file():  # pragma: no cover
            return [
                FileInfo(
                    name=full_path.name,
                    path=str(full_path),
                    is_dir=False,
                    size=full_path.stat().st_size,
                )
            ]

        results: list[FileInfo] = []
        try:
            for entry in full_path.iterdir():
                try:
                    # Validate each entry is within allowed dirs
                    self._validate_path(str(entry))
                    results.append(
                        FileInfo(
                            name=entry.name,
                            path=str(entry),
                            is_dir=entry.is_dir(),
                            size=entry.stat().st_size if entry.is_file() else None,
                        )
                    )
                except PermissionError:  # pragma: no cover
                    continue  # Skip entries outside allowed directories
        except PermissionError:  # pragma: no cover
            return []

        return sorted(results, key=lambda x: (not x["is_dir"], x["name"]))

    def read_bytes(self, path: str) -> bytes:  # pragma: no cover
        """Read raw bytes from a file."""
        try:
            full_path = self._validate_path(path)
        except PermissionError:
            return b""

        if not full_path.exists() or not full_path.is_file():
            return b""

        try:
            return full_path.read_bytes()
        except (PermissionError, OSError):
            return b""

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read file content with line numbers."""
        try:
            full_path = self._validate_path(path)
        except PermissionError as e:
            return f"Error: {e}"

        # Check permissions
        perm_error = self._check_permission_sync("read", str(full_path))
        if perm_error:
            return f"Error: {perm_error}"

        if not full_path.exists():
            return f"Error: File '{path}' not found"

        if full_path.is_dir():  # pragma: no cover
            return f"Error: '{path}' is a directory"

        try:
            with open(full_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except PermissionError:  # pragma: no cover
            return f"Error: Permission denied for '{path}'"
        except OSError as e:  # pragma: no cover
            return f"Error: {e}"

        total_lines = len(lines)

        if offset >= total_lines:  # pragma: no cover
            return f"Error: Offset {offset} exceeds file length ({total_lines} lines)"

        end = min(offset + limit, total_lines)
        result_lines = []

        for i in range(offset, end):
            line_num = i + 1
            line = lines[i].rstrip("\n\r")
            result_lines.append(f"{line_num:>6}\t{line}")

        result = "\n".join(result_lines)

        if end < total_lines:
            result += f"\n\n... ({total_lines - end} more lines)"

        return result

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write content to a file."""
        try:
            full_path = self._validate_path(path)
        except PermissionError as e:
            return WriteResult(error=str(e))

        # Check permissions
        perm_error = self._check_permission_sync("write", str(full_path))
        if perm_error:
            return WriteResult(error=perm_error)

        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)

            if isinstance(content, bytes):  # pragma: no cover
                full_path.write_bytes(content)
            else:
                full_path.write_text(_normalize_newlines(content), encoding="utf-8")

            return WriteResult(path=str(full_path))
        except PermissionError:  # pragma: no cover
            return WriteResult(error=f"Permission denied for '{path}'")
        except OSError as e:  # pragma: no cover
            return WriteResult(error=str(e))

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit a file by replacing strings."""
        try:
            full_path = self._validate_path(path)
        except PermissionError as e:  # pragma: no cover
            return EditResult(error=str(e))

        # Check permissions
        perm_error = self._check_permission_sync("edit", str(full_path))
        if perm_error:
            return EditResult(error=perm_error)

        if not full_path.exists():
            return EditResult(error=f"File '{path}' not found")

        try:
            content = full_path.read_text(encoding="utf-8")
        except PermissionError:  # pragma: no cover
            return EditResult(error=f"Permission denied for '{path}'")
        except OSError as e:  # pragma: no cover
            return EditResult(error=str(e))

        occurrences = content.count(old_string)

        if occurrences == 0:  # pragma: no cover
            return EditResult(error=f"String '{old_string}' not found in file")

        if occurrences > 1 and not replace_all:  # pragma: no cover
            return EditResult(
                error=f"String '{old_string}' found {occurrences} times. "
                "Use replace_all=True to replace all, or provide more context."
            )

        if replace_all:  # pragma: no cover
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        try:
            full_path.write_text(_normalize_newlines(new_content), encoding="utf-8")
            return EditResult(path=str(full_path), occurrences=occurrences if replace_all else 1)
        except PermissionError:  # pragma: no cover
            return EditResult(error=f"Permission denied for '{path}'")
        except OSError as e:  # pragma: no cover
            return EditResult(error=str(e))

    def glob_info(self, pattern: str, path: str = ".") -> list[FileInfo]:
        """Find files matching a glob pattern."""
        try:
            base_path = self._validate_path(path)
        except PermissionError:  # pragma: no cover
            return []

        if not base_path.exists():  # pragma: no cover
            return []

        results: list[FileInfo] = []

        try:
            for match in base_path.glob(pattern):  # pragma: no branch
                if match.is_file():
                    try:
                        self._validate_path(str(match))
                        results.append(
                            FileInfo(
                                name=match.name,
                                path=str(match),
                                is_dir=False,
                                size=match.stat().st_size,
                            )
                        )
                    except PermissionError:  # pragma: no cover
                        continue
        except (PermissionError, OSError):  # pragma: no cover
            pass

        return sorted(results, key=lambda x: x["path"])

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search for pattern in files.

        Uses ripgrep if available, falls back to Python regex.
        """
        search_path = path or str(self._root)

        try:
            validated_path = self._validate_path(search_path)
        except PermissionError as e:  # pragma: no cover
            return str(e)

        # Try ripgrep first when searching directories for better performance
        use_ripgrep = shutil.which("rg") is not None and not validated_path.is_file()
        if use_ripgrep:  # pragma: no cover
            return self._grep_ripgrep(pattern, validated_path, glob, ignore_hidden)

        return self._grep_python(pattern, validated_path, glob, ignore_hidden)  # pragma: no cover

    def _grep_ripgrep(  # pragma: no cover
        self, pattern: str, search_path: Path, glob: str | None = None, ignore_hidden: bool = True
    ) -> list[GrepMatch] | str:
        """Use ripgrep for fast searching."""
        cmd = ["rg", "--line-number", "--no-heading", pattern]

        if glob:
            cmd.extend(["--glob", glob])

        if not ignore_hidden:
            cmd.append("--hidden")

        cmd.append(".")

        try:
            result = subprocess.run(
                cmd,
                cwd=search_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return "Error: Search timed out"
        except OSError as e:
            return f"Error: {e}"

        results: list[GrepMatch] = []

        for line in result.stdout.strip().split("\n"):
            if not line:
                continue

            parts = line.split(":", 2)
            if len(parts) >= 3:
                file_path = parts[0]
                try:
                    line_num = int(parts[1])
                except ValueError:
                    continue
                content = parts[2]

                # Convert to absolute path and validate
                try:
                    base_path = search_path.parent if search_path.is_file() else search_path
                    full_path = (base_path / file_path).resolve()
                    self._validate_path(str(full_path))
                    results.append(
                        GrepMatch(
                            path=str(full_path),
                            line_number=line_num,
                            line=content,
                        )
                    )
                except PermissionError:
                    continue

        return results

    def _grep_python(  # pragma: no cover
        self,
        pattern: str,
        search_path: Path,
        glob_pattern: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Use Python regex for searching (fallback)."""
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: Invalid regex pattern: {e}"

        if not search_path.exists():
            return f"Error: Path '{search_path}' not found"

        results: list[GrepMatch] = []

        if search_path.is_file():
            files = [search_path]
        else:
            if glob_pattern:
                files = list(search_path.glob(glob_pattern))
            else:
                files = list(search_path.rglob("*"))
            if ignore_hidden:
                files = [f for f in files if not any(part.startswith(".") for part in f.parts)]

        for file_path in files:
            if not file_path.is_file():
                continue

            try:
                self._validate_path(str(file_path))
            except PermissionError:
                continue

            try:
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f):
                        if regex.search(line):
                            results.append(
                                GrepMatch(
                                    path=str(file_path),
                                    line_number=i + 1,
                                    line=line.rstrip("\n\r"),
                                )
                            )
            except (PermissionError, OSError):
                continue

        return results

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Execute a shell command.

        Args:
            command: Command to execute.
            timeout: Maximum execution time in seconds (default 120).

        Returns:
            ExecuteResponse with output, exit code, and truncation status.

        Raises:
            RuntimeError: If execute is disabled for this backend.
        """
        if not self._enable_execute:
            raise RuntimeError(
                "Shell execution is disabled for this backend. "
                "Initialize with enable_execute=True to enable."
            )

        # Check permissions
        perm_error = self._check_permission_sync("execute", command)
        if perm_error:
            return ExecuteResponse(
                output=f"Error: {perm_error}",
                exit_code=1,
                truncated=False,
            )

        try:
            result = subprocess.run(
                self._shell_cmd(command),
                cwd=self._root,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else 120,
            )

            output = result.stdout + result.stderr

            # Truncate if too long
            truncated = len(output) > MAX_EXECUTE_OUTPUT
            if truncated:  # pragma: no cover
                output = output[:MAX_EXECUTE_OUTPUT]

            return ExecuteResponse(
                output=output,
                exit_code=result.returncode,
                truncated=truncated,
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output="Error: Command timed out",
                exit_code=124,
                truncated=False,
            )
        except Exception as e:  # pragma: no cover
            return ExecuteResponse(
                output=f"Error: {e}",
                exit_code=1,
                truncated=False,
            )

    async def async_execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Async, cancellable version of execute().

        Uses `asyncio.create_subprocess_exec` so that cancelling the calling
        task immediately kills the subprocess rather than waiting for a thread
        to finish. On Unix, the subprocess runs in its own session so the entire
        process tree (including any grandchildren the shell forked) is reaped
        on cancellation or timeout. Defaults to a 120-second timeout when
        `timeout` is `None`.
        """
        if not self._enable_execute:
            raise RuntimeError(
                "Shell execution is disabled for this backend. "
                "Initialize with enable_execute=True to enable."
            )

        perm_error = self._check_permission_sync("execute", command)
        if perm_error:
            return ExecuteResponse(output=f"Error: {perm_error}", exit_code=1, truncated=False)

        try:
            proc = await asyncio.create_subprocess_exec(
                *self._shell_cmd(command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._root,
                # New session so we can kill the entire process group on Unix.
                # Windows kills the process tree via the cmd /c lifecycle.
                start_new_session=(sys.platform != "win32"),
            )

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout if timeout is not None else 120
                )
            except asyncio.CancelledError:
                self._kill_proc_tree(proc)
                # Shield cleanup so a second cancel can't leave pipes dangling.
                with contextlib.suppress(BaseException):
                    await asyncio.shield(asyncio.ensure_future(proc.communicate()))
                raise
            except asyncio.TimeoutError:
                self._kill_proc_tree(proc)
                with contextlib.suppress(BaseException):
                    await proc.communicate()
                return ExecuteResponse(
                    output="Error: Command timed out",
                    exit_code=124,
                    truncated=False,
                )

            output = stdout_b.decode("utf-8", errors="replace") + stderr_b.decode(
                "utf-8", errors="replace"
            )

            truncated = len(output) > MAX_EXECUTE_OUTPUT
            if truncated:  # pragma: no cover
                output = output[:MAX_EXECUTE_OUTPUT]

            return ExecuteResponse(
                output=output,
                exit_code=proc.returncode if proc.returncode is not None else 1,
                truncated=truncated,
            )
        except Exception as e:  # pragma: no cover
            return ExecuteResponse(output=f"Error: {e}", exit_code=1, truncated=False)

    @staticmethod
    def _kill_proc_tree(proc: asyncio.subprocess.Process) -> None:
        """Kill the subprocess and (on Unix) every grandchild it forked.

        On Unix the process is launched with `start_new_session=True`, so we
        can `killpg` the whole tree. On Windows `proc.kill()` is sufficient
        because `cmd /c` already terminates child processes when it dies.
        """
        if sys.platform == "win32":
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
