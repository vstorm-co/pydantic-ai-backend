"""Abstract sandbox with shell-based defaults for every file operation.

A subclass only has to implement `execute` and `edit`. Everything else is
derived from shell commands, which is enough for any sandbox that offers a
shell; subclasses with a native file API override the methods it covers.
"""

from __future__ import annotations

import shlex
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import PurePosixPath

from pydantic_ai_backends.types import (
    EditResult,
    ExecuteResponse,
    FileInfo,
    GrepMatch,
    WriteResult,
)

LS_FIELD_COUNT = 9
"""Fields in a `ls -la` line before the name; fewer means it is not an entry."""


class BaseSandbox(ABC):
    """Base class for sandboxes that expose a shell.

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

    def start(self) -> None:  # pragma: no cover  # noqa: B027
        """Start the sandbox eagerly.

        The default is a no-op, since sandboxes start on first use.
        """

    def is_alive(self) -> bool:  # pragma: no cover
        """Whether the sandbox is running and responsive."""
        return False

    def stop(self) -> None:  # pragma: no cover  # noqa: B027
        """Stop and clean up the sandbox."""

    @abstractmethod
    def execute(
        self, command: str, timeout: int | None = None
    ) -> ExecuteResponse:  # pragma: no cover
        """Run a command in the sandbox.

        Args:
            command: Command to execute.
            timeout: Maximum execution time in seconds.
        """
        ...

    @abstractmethod
    def edit(  # pragma: no cover
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

    def exists(self, path: str) -> bool:  # pragma: no cover
        """Whether `path` is a regular file, via `test -f`."""
        return self.execute(f"test -f {shlex.quote(path)}", timeout=5).exit_code == 0

    def ls_info(self, path: str) -> list[FileInfo]:  # pragma: no cover
        """List one directory using `ls -la`."""
        quoted_path = shlex.quote(path)
        result = self.execute(f"ls -la {quoted_path}")
        if result.exit_code != 0:
            return []

        entries: list[FileInfo] = []
        for line in result.output.strip().split("\n")[1:]:  # The first line is the total.
            parts = line.split()
            if len(parts) < LS_FIELD_COUNT:
                continue

            name = " ".join(parts[8:])
            if name in (".", ".."):
                continue

            entries.append(
                FileInfo(
                    name=name,
                    path=f"{quoted_path.rstrip('/')}/{name}",
                    is_dir=parts[0].startswith("d"),
                    size=int(parts[4]) if parts[4].isdigit() else None,
                )
            )

        return sorted(entries, key=lambda x: (not x["is_dir"], x["name"]))

    def read_bytes(self, path: str) -> bytes:  # pragma: no cover
        """Read a whole file with `cat`.

        Returns:
            The content, or `b""` on any failure — matching `LocalBackend` and
            `StateBackend`. Encoding an error message into the payload would
            leave the caller unable to tell it from real file content.
        """
        result = self.execute(f"cat {shlex.quote(path)}")
        if result.exit_code != 0:
            return b""
        return result.output.encode("utf-8", errors="replace")

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:  # pragma: no cover
        """Read a slice of a file, numbered by its real line positions.

        `awk` rather than `sed | cat -n`, because the latter renumbers the slice
        from 1 and would be off by `offset`.
        """
        start = offset + 1
        end = offset + limit
        result = self.execute(
            f"awk 'NR>={start} && NR<={end} {{ printf \"%6d\\t%s\\n\", NR, $0 }}' "
            f"{shlex.quote(path)}"
        )

        if result.exit_code != 0:
            return f"Error: {result.output}"
        if result.truncated:
            return result.output + "\n\n... (output truncated)"
        return result.output

    def write(self, path: str, content: str) -> WriteResult:  # pragma: no cover
        """Write a file with `cat` and a quoted heredoc.

        The delimiter is quoted (`<< 'DELIM'`) so the shell expands nothing
        inside the body and the content lands verbatim. Pre-escaping
        backslashes or `$` here would corrupt it instead.
        """
        # Random delimiter so it cannot collide with a line of the content.
        delimiter = f"EOF_{uuid.uuid4().hex[:8]}"
        quoted_path = shlex.quote(path)

        result = self.execute(
            f"mkdir -p $(dirname {quoted_path}) && cat > {quoted_path} << '{delimiter}'\n"
            f"{content}\n"
            f"{delimiter}"
        )
        if result.exit_code != 0:
            return WriteResult(error=result.output)
        return WriteResult(path=path)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:  # pragma: no cover
        """Match files with `find`.

        The pattern is matched as `-path '*/{pattern}'` so a basename glob like
        `*.py` matches files anywhere under the root, since `find -path` tests
        the whole pathname.
        """
        quoted_path = shlex.quote(path)
        quoted_pattern = shlex.quote(f"*/{pattern}")
        result = self.execute(f"find {quoted_path} -path {quoted_pattern} -type f 2>/dev/null")

        if result.exit_code != 0:
            return []

        entries = [
            FileInfo(name=file.name, path=str(file), is_dir=False, size=None)
            for file in (PurePosixPath(line) for line in result.output.splitlines())
        ]
        return sorted(entries, key=lambda x: x["path"])

    def grep_raw(  # pragma: no cover
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search file contents with `grep`."""
        options = ["-rn"]
        if ignore_hidden:
            options.extend(["--exclude='.*'", "--exclude-dir='.*'"])
        if glob:
            options.append(f"--include='{glob}'")

        result = self.execute(f"grep {' '.join(options)} '{pattern}' {shlex.quote(path or '.')}")

        if result.exit_code == 1:  # grep exits 1 when nothing matched.
            return []
        if result.exit_code != 0:
            return f"Error: {result.output}"

        matches: list[GrepMatch] = []
        for line in result.output.strip().split("\n"):
            # grep prints file:line:content.
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            try:
                line_number = int(parts[1])
            except ValueError:
                continue
            matches.append(GrepMatch(path=parts[0], line_number=line_number, line=parts[2]))

        return matches
