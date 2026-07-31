"""Shell commands the sandbox base classes derive file operations from.

Split out because the same derivation serves both :class:`BaseSandbox` and
:class:`AsyncBaseSandbox`, and the only difference between them is whether
`execute` is awaited. Everything here is a pure function of its arguments — a
command to run, or the parsing of what one printed — so the two classes share one
implementation instead of two that drift.

The names are public inside this private module so call sites read as prose:
`command = ls_command(path)`, `return parse_ls(result, path)`.
"""

from __future__ import annotations

import shlex
import uuid
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pydantic_ai_backends.types import FileInfo, GrepMatch, WriteResult

if TYPE_CHECKING:
    from pydantic_ai_backends.types import ExecuteResponse

LS_FIELD_COUNT = 9
"""Fields in a `ls -la` line before the name; fewer means it is not an entry."""


def exists_command(path: str) -> str:
    """Test whether a path is a regular file."""
    return f"test -f {shlex.quote(path)}"


def ls_command(path: str) -> str:
    """List one directory in long form."""
    return f"ls -la {shlex.quote(path)}"


def parse_ls(result: ExecuteResponse, path: str) -> list[FileInfo]:
    """Rows of a `ls -la` listing, directories first."""
    if result.exit_code != 0:
        return []

    quoted_path = shlex.quote(path)
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


def read_bytes_command(path: str) -> str:
    """Read a whole file."""
    return f"cat {shlex.quote(path)}"


def parse_read_bytes(result: ExecuteResponse) -> bytes:
    """Content of a `cat`, or `b""` on any failure.

    An error message encoded into the payload would leave the caller unable to
    tell it from real file content, so failure is empty rather than described.
    """
    if result.exit_code != 0:
        return b""
    return result.output.encode("utf-8", errors="replace")


def read_command(path: str, offset: int, limit: int) -> str:
    """Read a slice of a file, numbered by its real line positions.

    `awk` rather than `sed | cat -n`, because the latter renumbers the slice from
    1 and would be off by `offset`.
    """
    start = offset + 1
    end = offset + limit
    return (
        f"awk 'NR>={start} && NR<={end} {{ printf \"%6d\\t%s\\n\", NR, $0 }}' {shlex.quote(path)}"
    )


def parse_read(result: ExecuteResponse) -> str:
    """Text of a numbered read, or an `Error: ` string."""
    if result.exit_code != 0:
        return f"Error: {result.output}"
    if result.truncated:
        return result.output + "\n\n... (output truncated)"
    return result.output


def write_command(path: str, content: str) -> str:
    """Write a file with `cat` and a quoted heredoc.

    The delimiter is quoted (`<< 'DELIM'`) so the shell expands nothing inside
    the body and the content lands verbatim. Pre-escaping backslashes or `$` here
    would corrupt it instead. The delimiter is random so it cannot collide with a
    line of the content.
    """
    delimiter = f"EOF_{uuid.uuid4().hex[:8]}"
    quoted_path = shlex.quote(path)
    return (
        f"mkdir -p $(dirname {quoted_path}) && cat > {quoted_path} << '{delimiter}'\n"
        f"{content}\n"
        f"{delimiter}"
    )


def parse_write(result: ExecuteResponse, path: str) -> WriteResult:
    """Outcome of a heredoc write."""
    if result.exit_code != 0:
        return WriteResult(error=result.output)
    return WriteResult(path=path)


def glob_command(pattern: str, path: str) -> str:
    """Match files with `find`.

    The pattern is matched as `-path '*/{pattern}'` so a basename glob like
    `*.py` matches files anywhere under the root, since `find -path` tests the
    whole pathname.
    """
    quoted_path = shlex.quote(path)
    quoted_pattern = shlex.quote(f"*/{pattern}")
    return f"find {quoted_path} -path {quoted_pattern} -type f 2>/dev/null"


def parse_glob(result: ExecuteResponse) -> list[FileInfo]:
    """Paths a `find` printed, sorted."""
    if result.exit_code != 0:
        return []

    entries = [
        FileInfo(name=file.name, path=str(file), is_dir=False, size=None)
        for file in (PurePosixPath(line) for line in result.output.splitlines())
    ]
    return sorted(entries, key=lambda x: x["path"])


def grep_command(
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    ignore_hidden: bool = True,
) -> str:
    """Search file contents with `grep`."""
    options = ["-rn"]
    if ignore_hidden:
        options.extend(["--exclude='.*'", "--exclude-dir='.*'"])
    if glob:
        options.append(f"--include='{glob}'")
    return f"grep {' '.join(options)} '{pattern}' {shlex.quote(path or '.')}"


def parse_grep(result: ExecuteResponse) -> list[GrepMatch] | str:
    """Hits a `grep` printed, `[]` for no match, or an `Error: ` string."""
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
