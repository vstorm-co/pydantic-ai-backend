"""Reading a session's files off the host volume, with no container running.

A workspace outlives the sandbox that wrote it, so listing and reading it should
not cost a container start. Opening a conversation from last week just to see
what the agent produced would otherwise boot a container, wait for it, and reap
it again minutes later.

Everything here reads the host filesystem directly, which makes path containment
the only thing standing between a caller and the rest of the machine. Two ways in
have to be closed:

- `..` in a requested path, refused before anything is opened.
- A **symlink the sandbox itself created**. Untrusted code inside a container can
  run `ln -s /etc/shadow notes.txt`; in the container that points at the
  container's own file, but read from the host side it would resolve to the
  host's. Resolving a path and then opening it is not enough: a live sandbox can
  swap a directory for a link between the two. So nothing is opened by name -
  every component is opened from its parent's descriptor with `O_NOFOLLOW`
  (`_hostfs`), a link is never followed, and a listing leaves links out.
"""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path, PurePosixPath

from pydantic_ai_backends._text import bytes_to_text
from pydantic_ai_backends._time import iso_mtime
from pydantic_ai_backends.remote import _hostfs as hostfs
from pydantic_ai_backends.types import FileInfo


class WorkspacePathError(Exception):
    """Raised when a requested path resolves outside the workspace."""


def workspace_root_for(root: Path, session_id: str, work_dir_name: str = "workspace") -> Path:
    """Directory holding one session's files on the host.

    Args:
        root: The service's `workspace_root`.
        session_id: Session whose files are wanted. Already pattern-checked, so
            it cannot contribute a traversal of its own.
        work_dir_name: Subdirectory the sandbox's work directory is mounted from.
    """
    return root / session_id / work_dir_name


def relative_request_path(path: str, work_dir: str) -> str:
    """Interpret a client-supplied path as relative to the workspace.

    Callers pass whatever a listing gave them, and a listing through a live
    session returns absolute in-container paths (`/workspace/notes.md`). Those
    resolve here to the same file as `notes.md`, so a UI can hand back exactly
    what it was shown.

    An absolute path outside the work directory keeps its segments and is
    resolved inside the workspace anyway, where it simply will not exist —
    `/etc/passwd` becomes `etc/passwd`, not the host's file.
    """
    requested = PurePosixPath(path)
    if not requested.is_absolute():
        return str(requested)
    try:
        return str(requested.relative_to(PurePosixPath(work_dir)))
    except ValueError:
        return str(requested).lstrip("/")


def _parts(path: str) -> list[str]:
    """The components of a relative workspace path, refusing a climb out of it."""
    parts = [part for part in PurePosixPath(path).parts if part not in (".", "/")]
    if ".." in parts:
        raise WorkspacePathError(f"Path '{path}' is outside the workspace")
    return parts


def list_workspace(root: Path, path: str) -> list[FileInfo]:
    """List one directory of a workspace.

    A link is left out rather than described - one out of the workspace, one
    pointing at nothing, and one pointing inside it alike. Describing it would
    mean following it on the host, which is the one thing a link planted by the
    sandbox must not get.

    Args:
        root: The session's workspace directory.
        path: Directory to list, relative to `root`.

    Raises:
        WorkspacePathError: If `path` escapes the workspace or runs through a link.
        FileNotFoundError: If it is not a directory.
    """
    parts = _parts(path)
    try:
        with hostfs.opened_dir(root, parts) as fd, os.scandir(fd) as scanned:
            entries: list[FileInfo] = []
            for item in scanned:
                info = item.stat(follow_symlinks=False)
                if stat_module.S_ISLNK(info.st_mode):
                    continue
                is_dir = stat_module.S_ISDIR(info.st_mode)
                entries.append(
                    FileInfo(
                        name=item.name,
                        path="/".join([*parts, item.name]),
                        is_dir=is_dir,
                        size=None if is_dir else info.st_size,
                        modified_at=None if is_dir else iso_mtime(info.st_mtime),
                    )
                )
    except hostfs.NotNative as exc:
        raise WorkspacePathError(f"Path '{path}' is outside the workspace") from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise FileNotFoundError(f"No such directory: {path}") from exc
    return sorted(entries, key=lambda item: (not item["is_dir"], item["name"]))


def walk_workspace(
    root: Path, path: str, *, max_depth: int, max_entries: int
) -> tuple[list[FileInfo], bool]:
    """List a directory and the directories under it, breadth first.

    One request for what a file browser otherwise asks for one directory at a
    time - each of those a round trip, and a workspace three deep is the usual
    shape. Each directory is listed by :func:`list_workspace`, so links are left
    out here exactly as they are there.

    Args:
        root: The session's workspace directory.
        path: Directory to start from, relative to `root`.
        max_depth: Levels to list; 1 is `path` alone.
        max_entries: Most entries to return.

    Returns:
        The entries, each directory's before its children's, and whether the
        listing is incomplete: a directory left unopened at `max_depth`, one
        that could not be read, or `max_entries` reached with more to list.

    Raises:
        WorkspacePathError: If `path` escapes the workspace or runs through a link.
        FileNotFoundError: If `path` is not a directory. Below it, a directory
            that vanished or turned into a link marks the listing truncated
            instead, since the sandbox may be changing the tree as it is read.
    """
    found: list[FileInfo] = []
    truncated = False
    queue = [(path, 1)]
    while queue:
        directory, depth = queue.pop(0)
        try:
            entries = list_workspace(root, directory)
        except (WorkspacePathError, FileNotFoundError, PermissionError):
            if depth == 1:
                raise
            truncated = True
            continue
        for entry in entries:
            if len(found) == max_entries:
                return found, True
            found.append(entry)
            if not entry["is_dir"]:
                continue
            if depth < max_depth:
                queue.append((entry["path"], depth + 1))
            else:
                truncated = True
    return found, truncated


def read_workspace(root: Path, path: str, offset: int, limit: int, max_bytes: int) -> str:
    """Read a slice of a workspace file as text.

    Decoded the same way a live session would decode it, so the archive and the
    sandbox do not disagree about what a file says - encoding detection included,
    and a PDF is extracted rather than returned as noise.

    Args:
        root: The session's workspace directory.
        path: File to read, relative to `root`.
        offset: First line to return, 0-indexed.
        limit: Most lines to return.
        max_bytes: Largest file to read into memory.

    Raises:
        WorkspacePathError: If `path` escapes the workspace or is a link.
        FileNotFoundError: If it is not a regular file.
        ValueError: If the file is over `max_bytes`, or holds no readable text.
    """
    raw = read_workspace_bytes(root, path, max_bytes)
    extension = PurePosixPath(path).suffix.lower().lstrip(".")
    lines = bytes_to_text(extension, raw).splitlines()

    if offset >= len(lines):
        return "[End of file]"

    end = offset + limit
    chunk = "\n".join(lines[offset:end])
    if end >= len(lines):
        return chunk
    remaining = len(lines) - end
    return f"{chunk}\n\n[... {remaining} more lines. Use offset={end} to read more.]"


def read_workspace_bytes(root: Path, path: str, max_bytes: int) -> bytes:
    """Read a whole workspace file as bytes.

    The sibling of :func:`read_workspace`, and the reason it exists is that
    decoding is not always wanted. A chart, a rendered PDF, an image an agent
    fetched - the most common things it actually produces - are not text, and
    reading them as text then re-encoding yields a corrupt file that downloads
    successfully. That is the worst available outcome, so a consumer with only
    `read` had to refuse the whole class of file instead.

    Whole rather than sliced: a byte range is meaningless for the formats this
    exists to serve, and the listing already carries `size`, so a caller that
    needs to bound a read can look before making it.

    Args:
        root: The session's workspace directory.
        path: File to read, relative to `root`.
        max_bytes: Largest file to read into memory.

    Raises:
        WorkspacePathError: If `path` escapes the workspace or is a link.
        FileNotFoundError: If it is not a regular file.
        ValueError: If the file is over `max_bytes`.
    """
    try:
        return hostfs.read(root, _parts(path), max_bytes=max_bytes)
    except hostfs.TooLarge as exc:
        raise ValueError(f"File is {exc.size} bytes, over the {exc.limit}-byte read limit") from exc
    except hostfs.NotNative as exc:
        raise WorkspacePathError(f"Path '{path}' is outside the workspace") from exc
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
        raise FileNotFoundError(f"No such file: {path}") from exc
