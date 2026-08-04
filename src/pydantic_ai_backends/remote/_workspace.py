"""Reading a session's files off the host volume, with no container running.

A workspace outlives the sandbox that wrote it, so listing and reading it should
not cost a container start. Opening a conversation from last week just to see
what the agent produced would otherwise boot a container, wait for it, and reap
it again minutes later.

Everything here reads the host filesystem directly, which makes path containment
the only thing standing between a caller and the rest of the machine. Two ways in
have to be closed, and both are closed by resolving first and checking after:

- `..` in a requested path.
- A **symlink the sandbox itself created**. Untrusted code inside a container can
  run `ln -s /etc/shadow notes.txt`; in the container that points at the
  container's own file, but read from the host side it would resolve to the
  host's. So a resolved path outside the workspace is refused even when the link
  lives inside it.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from pydantic_ai_backends._text import bytes_to_text
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


def resolve_within(root: Path, path: str) -> Path:
    """Resolve `path` inside `root`, refusing anything that escapes.

    Raises:
        WorkspacePathError: If the resolved path is not the root or under it —
            whether it got there through `..` or through a symlink.
    """
    resolved_root = root.resolve()
    candidate = (resolved_root / path).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise WorkspacePathError(f"Path '{path}' is outside the workspace")
    return candidate


def list_workspace(root: Path, path: str) -> list[FileInfo]:
    """List one directory of a workspace.

    An entry the sandbox left in a state we cannot describe is omitted rather
    than reported — a symlink out of the workspace, or one pointing at nothing.
    Either would otherwise turn one planted link into a failure for the whole
    directory, and untrusted code inside the container can plant both.

    Args:
        root: The session's workspace directory.
        path: Directory to list, relative to `root`.

    Raises:
        WorkspacePathError: If `path` escapes the workspace.
        FileNotFoundError: If it is not a directory.
    """
    directory = resolve_within(root, path)
    if not directory.is_dir():
        raise FileNotFoundError(f"No such directory: {path}")

    resolved_root = root.resolve()
    entries: list[FileInfo] = []
    for entry in directory.iterdir():
        try:
            relative = str(entry.relative_to(resolved_root))
            target = resolve_within(root, relative)
            is_dir = target.is_dir()
            size = None if is_dir else target.stat().st_size
        except (WorkspacePathError, ValueError, OSError):
            continue
        entries.append(FileInfo(name=entry.name, path=relative, is_dir=is_dir, size=size))
    return sorted(entries, key=lambda item: (not item["is_dir"], item["name"]))


def read_workspace(root: Path, path: str, offset: int, limit: int, max_bytes: int) -> str:
    """Read a slice of a workspace file as text.

    Decoded the same way a live session would decode it, so the archive and the
    sandbox do not disagree about what a file says — encoding detection included,
    and a PDF is extracted rather than returned as noise.

    Args:
        root: The session's workspace directory.
        path: File to read, relative to `root`.
        offset: First line to return, 0-indexed.
        limit: Most lines to return.
        max_bytes: Largest file to read into memory.

    Raises:
        WorkspacePathError: If `path` escapes the workspace.
        FileNotFoundError: If it is not a regular file.
        ValueError: If the file is over `max_bytes`, or holds no readable text.
    """
    target = resolve_within(root, path)
    if not target.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    size = target.stat().st_size
    if size > max_bytes:
        raise ValueError(f"File is {size} bytes, over the {max_bytes}-byte read limit")

    extension = target.suffix.lower().lstrip(".")
    lines = bytes_to_text(extension, target.read_bytes()).splitlines()

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
    fetched — the most common things it actually produces — are not text, and
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
        WorkspacePathError: If `path` escapes the workspace.
        FileNotFoundError: If it is not a regular file.
        ValueError: If the file is over `max_bytes`.
    """
    target = resolve_within(root, path)
    if not target.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    size = target.stat().st_size
    if size > max_bytes:
        raise ValueError(f"File is {size} bytes, over the {max_bytes}-byte read limit")

    return target.read_bytes()
