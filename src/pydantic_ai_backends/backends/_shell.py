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

import base64
import binascii
import shlex
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

    # The *unquoted* path: a listing's rows are handed to a model, which sends
    # them back to `read` or `edit`, which quote for the shell themselves. Built
    # from the quoted form, a directory with a space in it produced
    # `'/my work'/notes.md` — a path that does not exist and cannot be recovered
    # from. Plain paths quote to themselves, which is why it went unnoticed.
    root = path.rstrip("/")
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
                path=f"{root}/{name}",
                is_dir=parts[0].startswith("d"),
                size=int(parts[4]) if parts[4].isdigit() else None,
            )
        )

    return sorted(entries, key=lambda x: (not x["is_dir"], x["name"]))


def read_bytes_command(path: str) -> str:
    """Read a whole file, base64-encoded so arbitrary bytes survive the trip.

    A plain `cat` could not carry them. Command output crosses back as text — a
    sandbox decodes its exec stream with `errors="replace"` — so every byte that
    is not UTF-8 arrived as U+FFFD. A PNG written by `write_command` and read
    back came out a different file, silently and with no error, which is exactly
    what the toolset's `image_support` does on a shell-derived sandbox.

    Requires `base64` in the image, as `write_command` already does.
    """
    return f"base64 {shlex.quote(path)}"


def parse_read_bytes(result: ExecuteResponse) -> bytes:
    """Content of a base64 read, or `b""` on any failure.

    An error message encoded into the payload would leave the caller unable to
    tell it from real file content, so failure is empty rather than described.
    A *truncated* payload is a failure too: base64 cut short decodes to bytes
    that are not the file's, and a wrong answer is worse here than no answer,
    since the caller cannot tell one from the other. `read` returns a partial
    slice for the oversized case; this returns nothing.
    """
    if result.exit_code != 0 or result.truncated:
        return b""
    try:
        # `validate=False` (the default) discards the newlines `base64` wraps at.
        return base64.b64decode(result.output)
    except (binascii.Error, ValueError):
        return b""


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


def write_command(path: str, content: str | bytes) -> str:
    """Write a file, carrying the content base64-encoded.

    Base64 rather than the quoted heredoc this used to be, for two reasons the
    heredoc could not fix. It cannot carry arbitrary bytes: `content` is typed
    `str | bytes` by the protocol, and interpolating `bytes` into the body wrote
    the Python repr — `b'\\x89PNG\\r\\n'` — into the file with no error. And a
    heredoc's terminator must start a line, so writing `"x\\n"` produced `"x\\n\\n"`;
    `LocalBackend` and `StateBackend` both store exactly what they are given.

    The payload needs no quoting of its own, base64 being `[A-Za-z0-9+/=]`.
    Requires `base64` in the image, which GNU coreutils and BusyBox both provide.
    """
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    payload = base64.b64encode(raw).decode("ascii")
    quoted_path = shlex.quote(path)
    return f"mkdir -p $(dirname {quoted_path}) && printf %s {payload} | base64 -d > {quoted_path}"


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
    """Search file contents with `grep`.

    The pattern and the glob are quoted like every other value here. Wrapping
    them in literal single quotes instead let one of their own close the quoting:
    a search for `don't` produced an unterminated command, and a crafted pattern
    ran whatever followed it. `-e` for the same reason a pattern is quoted — a
    pattern starting with `-` is a pattern, not an option.
    """
    options = ["-rn"]
    if ignore_hidden:
        # Quoted, or the shell expands `.*` against the working directory before
        # grep ever sees it.
        hidden = shlex.quote(".*")
        options.extend([f"--exclude={hidden}", f"--exclude-dir={hidden}"])
    if glob:
        options.append(f"--include={shlex.quote(glob)}")
    return f"grep {' '.join(options)} -e {shlex.quote(pattern)} {shlex.quote(path or '.')}"


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
