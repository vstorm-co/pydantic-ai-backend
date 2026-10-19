"""A live session's files, read and written on the host without the sandbox.

Every file operation a Pydantic AI workspace makes without a native filesystem is
a shell script run in the container: a read is a `stat` and a `base64`, a write
two or three commands more. Through `sandboxd` that is one `docker exec` each,
tens of milliseconds apiece and hundreds for a file of any size. The files are
already on the host - the work directory is a bind mount of the session's
workspace - so the service can do the same operation itself in a millisecond.

**The sandbox writes this directory, and it is untrusted.** Code inside the
container can turn any path component into a symlink at any moment, so a path
checked and then opened by name could be swapped between the two and read or
write the host's file instead. Nothing here opens a path by name: each component
is opened relative to its parent's descriptor with `O_NOFOLLOW`, so a symlink
anywhere on the way fails the open rather than being followed. A symlink, a FIFO,
a device or a path outside the work directory is not an error either - the
container resolves those in its own view, so they raise :class:`NotNative` and
the client takes its shell path for that one call.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat as stat_module
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
"""`O_NONBLOCK` so a FIFO the sandbox planted cannot hold the open; it is then
refused as not a regular file, and the flag means nothing for one that is."""

_FOLLOWED_A_LINK = (errno.ELOOP, errno.EMLINK)
"""What `O_NOFOLLOW` answers for a symlink: `ELOOP` on Linux, `EMLINK` on BSDs."""


class NotNative(Exception):
    """This path has to be resolved by the sandbox, not on the host."""


class TooLarge(NotNative):
    """A file over the read ceiling: the shell reads it in chunks instead."""

    def __init__(self, name: str, size: int, limit: int) -> None:
        super().__init__(f"{name} is {size} bytes, over the {limit}-byte read limit")
        self.size = size
        self.limit = limit


@dataclass(frozen=True)
class Entry:
    """One file or directory, as the workspace protocol describes it."""

    name: str
    path: str
    is_dir: bool
    size: int | None


def parts_of(path: str, work_dir: str) -> list[str]:
    """The components of `path` below `work_dir`.

    Raises:
        NotNative: The path is outside the work directory, or not normalized -
            a `.` or `..` the container would resolve against its own links.
        ValueError: The path is not absolute.
    """
    requested = PurePosixPath(path)
    if not requested.is_absolute():
        raise ValueError(f"path must be absolute: {path!r}")
    try:
        relative = requested.relative_to(PurePosixPath(work_dir))
    except ValueError as exc:
        raise NotNative(f"{path} is outside the work directory") from exc
    parts = list(relative.parts)
    if ".." in parts:
        raise NotNative(f"{path} climbs out through ..")
    return parts


@contextlib.contextmanager
def opened_dir(root: Path, parts: list[str], *, create: bool = False) -> Iterator[int]:
    """A descriptor for the directory `parts` names under `root`, never through a link.

    `root` is the operator's own path to the session's workspace; everything
    below it is the sandbox's and is walked one component at a time.
    """
    fd = os.open(root, _DIR_FLAGS)
    try:
        for part in parts:
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o777, dir_fd=fd)
                child = _open_child_dir(part, fd)
            except OSError as exc:
                raise _translated(exc, part, fd) from exc
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _open_child_dir(part: str, parent: int) -> int:
    try:
        return os.open(part, _DIR_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise _translated(exc, part, parent) from exc


def _translated(exc: OSError, part: str, parent: int) -> OSError | NotNative:
    """A symlink met on the way is the sandbox's to resolve; anything else stands."""
    if exc.errno in _FOLLOWED_A_LINK or (exc.errno == errno.ENOTDIR and _is_link(part, parent)):
        return NotNative(f"{part} is a symbolic link")
    return exc


def _is_link(name: str, parent: int) -> bool:
    try:
        return stat_module.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _leaf(parts: list[str]) -> tuple[list[str], str]:
    if not parts:
        raise IsADirectoryError("the work directory is a directory")
    return parts[:-1], parts[-1]


def read(root: Path, parts: list[str], *, max_bytes: int) -> bytes:
    """A regular file's bytes.

    Raises:
        NotNative: A link, a special file, or a file over `max_bytes` - the
            shell path reads that one in chunks.
        FileNotFoundError, IsADirectoryError, PermissionError: As the protocol says.
    """
    directory, name = _leaf(parts)
    with opened_dir(root, directory) as parent:
        try:
            fd = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=parent)
        except OSError as exc:
            raise _translated(exc, name, parent) from exc
        try:
            info = os.fstat(fd)
            if stat_module.S_ISDIR(info.st_mode):
                raise IsADirectoryError(f"{name} is a directory")
            if not stat_module.S_ISREG(info.st_mode):
                raise NotNative(f"{name} is not a regular file")
            if info.st_size > max_bytes:
                raise TooLarge(name, info.st_size, max_bytes)
            # Counted as read, not trusted from the `fstat`: the sandbox can grow
            # the file between the two, and the ceiling is the service's memory.
            data = bytearray()
            while chunk := os.read(fd, min(1 << 20, max_bytes + 1 - len(data))):
                data += chunk
                if len(data) > max_bytes:
                    raise TooLarge(name, len(data), max_bytes)
            return bytes(data)
        finally:
            os.close(fd)


def write(root: Path, parts: list[str], data: bytes) -> None:
    """Write a regular file, creating it and its parents as `mkdir -p` would.

    Atomic: the bytes go to a temporary file beside the target, which then
    replaces it with one `rename`. A write that fails part-way - the disk full,
    the service stopped - leaves the old file as it was, and a reader sees the
    old content or the new, never half of it. An existing file's permission
    bits are carried over, so an executable script stays executable; a new one
    gets the mode the service's umask gives it. The rename is not followed by an
    `fsync`: a power cut can still lose the new content, never the old file's
    integrity. A hard link to the old file keeps the old content.

    Raises:
        NotNative: The file is a link or a special file - written through by the
            shell, in the sandbox's view.
        IsADirectoryError, NotADirectoryError, PermissionError: As the protocol says.
    """
    directory, name = _leaf(parts)
    with opened_dir(root, directory, create=True) as parent:
        try:
            existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            mode = None
        else:
            if stat_module.S_ISDIR(existing.st_mode):
                raise IsADirectoryError(f"{name} is a directory")
            if stat_module.S_ISLNK(existing.st_mode):
                raise NotNative(f"{name} is a symbolic link")
            if not stat_module.S_ISREG(existing.st_mode):
                raise NotNative(f"{name} is not a regular file")
            mode = stat_module.S_IMODE(existing.st_mode)
        temporary = f".{name}.{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o666,
                dir_fd=parent,
            )
        except OSError as exc:
            if exc.errno == errno.ENAMETOOLONG:
                # The name fits and its temporary sibling does not.
                raise NotNative(f"{name} is too long to write beside") from exc
            raise
        try:
            try:
                if mode is not None:
                    os.fchmod(fd, mode)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
            finally:
                os.close(fd)
            # `rename` replaces whatever is at `name` now, link or not, without
            # following it - so a link planted after the check above is itself
            # replaced, and nothing outside the workspace is written.
            os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=parent)
            raise


def stat(root: Path, parts: list[str], path: str) -> Entry:
    """What `path` is. A link is the sandbox's to describe, since it follows links."""
    name = PurePosixPath(path).name
    if not parts:
        return Entry(name=name, path=path, is_dir=True, size=None)
    directory, leaf = _leaf(parts)
    with opened_dir(root, directory) as parent:
        info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    return _entry(name, path, info)


def _entry(name: str, path: str, info: os.stat_result) -> Entry:
    if stat_module.S_ISLNK(info.st_mode):
        raise NotNative(f"{name} is a symbolic link")
    is_dir = stat_module.S_ISDIR(info.st_mode)
    return Entry(name=name, path=path, is_dir=is_dir, size=None if is_dir else info.st_size)


def list_dir(root: Path, parts: list[str], path: str) -> list[Entry]:
    """A directory's entries, not recursive.

    A directory holding a link is listed by the sandbox instead: the protocol
    reports a link by what it points at, which only the container can see.
    """
    with opened_dir(root, parts) as fd, os.scandir(fd) as scanned:
        entries = []
        for item in scanned:
            info = item.stat(follow_symlinks=False)
            child = str(PurePosixPath(path) / item.name)
            entries.append(_entry(item.name, child, info))
    return sorted(entries, key=lambda entry: entry.name)


def realpath(root: Path, parts: list[str], work_dir: str) -> str:
    """The path with every link resolved, as `os.path.realpath(strict=False)` in the sandbox.

    Answered here only when no component is a link: then the path already is
    its own real path, and saying so takes a few `stat`s instead of a `docker
    exec`. A link is the sandbox's to resolve - its target means something in
    the container's view, not the host's. A missing component, or anything below
    a file, is kept as written, as `realpath` keeps it.

    Raises:
        NotNative: A component is a link.
        PermissionError: A directory on the way cannot be searched by this service.
    """
    fd = os.open(root, _DIR_FLAGS)
    try:
        for part in parts:
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                break
            if stat_module.S_ISLNK(info.st_mode):
                raise NotNative(f"{part} is a symbolic link")
            if not stat_module.S_ISDIR(info.st_mode):
                break
            child = _open_child_dir(part, fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)
    return str(PurePosixPath(work_dir, *parts))


def make_dir(root: Path, parts: list[str]) -> None:
    """`mkdir -p`. A file in the way raises, as it would in the shell.

    The last component is made apart from its parents, because the two fail
    differently there: a file where the new directory goes is `FileExistsError`,
    one on the way to it `NotADirectoryError`.
    """
    if not parts:
        return
    directory, name = parts[:-1], parts[-1]
    with opened_dir(root, directory, create=True) as parent:
        try:
            os.mkdir(name, 0o777, dir_fd=parent)
        except FileExistsError:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat_module.S_ISLNK(info.st_mode):
                raise NotNative(f"{name} is a symbolic link") from None
            if not stat_module.S_ISDIR(info.st_mode):
                raise


def remove(root: Path, parts: list[str]) -> None:
    """Remove a file, a link itself, or a directory and everything in it.

    Raises:
        ValueError: For the work directory itself.
        NotNative: A directory in the tree turned into a link while it was being
            removed; the shell finishes the job in the sandbox's view.
    """
    if not parts:
        raise ValueError("refusing to remove the working directory")
    directory, name = _leaf(parts)
    with opened_dir(root, directory) as parent:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat_module.S_ISDIR(info.st_mode):
            _remove_tree(parent, name)
        else:
            os.unlink(name, dir_fd=parent)


def _remove_tree(parent: int, name: str) -> None:
    """Remove a directory and its contents through descriptors, following no link.

    `shutil.rmtree` does this from Python 3.11 with `dir_fd`; this library still
    runs on 3.10. A link is unlinked as itself, and a directory swapped for a
    link mid-way fails its `O_NOFOLLOW` open rather than being descended into.
    """
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise _translated(exc, name, parent) from exc
    try:
        with os.scandir(fd) as scanned:
            children = [(item.name, item.is_dir(follow_symlinks=False)) for item in scanned]
        for child, is_dir in children:
            if is_dir:
                _remove_tree(fd, child)
            else:
                os.unlink(child, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)
