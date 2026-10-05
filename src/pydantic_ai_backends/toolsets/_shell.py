"""The `find` and `grep` commands the console's `glob` and `grep` run in a workspace.

Pure functions of their arguments — a command to run, or the parsing of what one
printed. The names are public inside this private module so call sites read as
prose: `command = grep_command(pattern)`, `return parse_grep(result)`.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pydantic_ai_backends.types import FileInfo, GrepMatch

if TYPE_CHECKING:
    from pydantic_ai_backends.types import ExecuteResponse

ROOT_SPELLINGS = frozenset({"", "/", "."})
"""The three ways a caller spells "the root of this workspace".

A workspace has a real filesystem under it and a working directory inside it,
so all three mean that directory and become `.` - see :func:`glob_command` for
what passing `/` through cost.
"""


def glob_command(pattern: str, path: str) -> str:
    """Match files with `find`, rooted where the caller's path means.

    Two things to get right, and this got both wrong.

    **The root.** A path naming the backend's root means the session's working
    directory here, not the filesystem's. Passed through, `find /` read it as the
    machine: a glob of `*` in a container answered 2540 paths from `/proc` and
    `/usr` and not one from the workspace. So the agent's own search tool read
    the image it was running on - into its context - and a channel's "what did
    the agent write this turn" snapshot diffed two photographs of `/proc`.
    Anything genuinely absolute is still passed through: `/etc` is a root a
    caller may mean.

    **Globstar.** `find -path` matches with fnmatch, where `**` is no different
    from `*` and every `/` in the pattern must be present in the path. So `**/*`
    - what anything walking a tree reaches for, including that snapshot - needed
    two slashes and therefore missed every file at the top level. A leading
    `**/` means "at any depth", which is exactly what the `*/` prefix below
    already provides, so it is dropped rather than stacked.

    The pattern is matched as `-path '*/{pattern}'` so a basename glob like
    `*.py` matches files anywhere under the root, since `find -path` tests the
    whole pathname.
    """
    root = "." if path.strip() in ROOT_SPELLINGS else path
    anywhere = pattern[3:] if pattern.startswith("**/") else pattern
    quoted_path = shlex.quote(root)
    quoted_pattern = shlex.quote(f"*/{anywhere}")
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
    # `-H` because GNU grep leaves the file name out when it is given a single
    # file, and `parse_grep` then reads no match at all.
    options = ["-rnH"]
    if ignore_hidden:
        # Directories only, and `.[!.]*` rather than `.*`. BSD grep matches both
        # excludes against the path as it walks it - `./notes.txt` - so `.*`
        # excluded the starting directory and a file exclude excluded every
        # file, and a search of the working directory found nothing on macOS.
        # Hidden files are dropped by `hidden_match` instead. Quoted, or the
        # shell expands the pattern against the working directory.
        options.append(f"--exclude-dir={shlex.quote('.[!.]*')}")
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


def hidden_match(match_path: str, root: str | None) -> bool:
    """Whether a grep hit lies in a hidden file or directory below `root`.

    Measured from the search root, so an explicitly named hidden directory -
    `.github` - is still searched, as GNU grep's command-line arguments are.
    """
    base = (root or ".").rstrip("/") or "/"
    relative = match_path[len(base) :] if match_path.startswith(base) else match_path
    return any(
        part.startswith(".") and part not in {".", ".."} for part in relative.split("/") if part
    )
