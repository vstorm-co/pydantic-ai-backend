"""A filesystem kept as a JSON document, for a host that stores the document itself."""

from __future__ import annotations

import base64
import binascii
import posixpath
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Literal

from pydantic_ai_backends.types import FileData


class StateBackend:
    """A filesystem kept as a JSON document: files, and the directories created.

    What :class:`~pydantic_ai_backends.workspaces.StateWorkspace` serves as a
    Pydantic AI workspace, and what a host persists between runs — `files` and
    `directories` are both plain JSON, so they fit a PostgreSQL `jsonb` column.

    The operations follow a filesystem's rules and raise its errors, because the
    workspace contract asks for exactly that: a missing path is
    `FileNotFoundError`, reading a directory `IsADirectoryError`, a file standing
    where a directory is expected `NotADirectoryError`.

    Paths are absolute POSIX paths; `.` and `..` are collapsed as text, since
    there are no symlinks here for `..` to climb out of. `directories` records
    every directory created, by `make_dir` or as the parent of a written file, so
    one stays when the last thing in it is removed, as it would on disk. A
    directory something is stored under exists too, which is how a document
    written before `directories` existed keeps its tree.

    Text is stored as lines and anything that is not UTF-8 as base64, so the
    document is always JSON and `read_bytes` returns exactly what was written.

    Example:
        ```python
        import json

        from pydantic_ai_backends import StateBackend

        state = StateBackend()
        state.write_bytes("/src/app.py", b"print('hello')")
        state.make_dir("/data")
        saved = json.dumps({"files": state.files, "directories": sorted(state.directories)})

        loaded = json.loads(saved)
        restored = StateBackend(files=loaded["files"], directories=loaded["directories"])
        ```
    """

    def __init__(
        self,
        files: dict[str, FileData] | None = None,
        directories: Iterable[str] | None = None,
    ) -> None:
        """Load a document, or start an empty one.

        Args:
            files: Files by path. A document a previous instance produced loads
                unchanged, including one written before `encoding` existed.
            directories: Directories created, by path. Any directory holding a
                file exists anyway, so a document from an earlier version, which
                lists none, loads unchanged.
        """
        self._files: dict[str, FileData] = files if files is not None else {}
        self._directories: set[str] = {_normal(path) for path in directories or ()}

    @property
    def files(self) -> dict[str, FileData]:
        """Files by absolute path; JSON-serialisable as it stands."""
        return self._files

    @property
    def directories(self) -> set[str]:
        """Directories created; persist it with `sorted()` beside `files`."""
        return self._directories

    def is_file(self, path: str) -> bool:
        """Whether a file is stored at `path`."""
        return _normal(path) in self._files

    def is_dir(self, path: str) -> bool:
        """Whether `path` is the root, a directory created, or holds anything."""
        path = _normal(path)
        if path == "/" or path in self._directories:
            return True
        prefix = path + "/"
        return any(p.startswith(prefix) for p in self._files) or any(
            d.startswith(prefix) for d in self._directories
        )

    def exists(self, path: str) -> bool:
        """Whether `path` is a file or a directory."""
        return self.is_file(path) or self.is_dir(path)

    def read_bytes(self, path: str) -> bytes:
        """A file's exact bytes.

        Raises:
            IsADirectoryError: `path` is a directory.
            NotADirectoryError: A file stands where one of its parents should be.
            FileNotFoundError: Nothing is at `path`.
            ValueError: The stored content is base64 that does not decode — a
                document this class did not write.
        """
        path = _normal(path)
        stored = self._files.get(path)
        if stored is not None:
            return _content_bytes(stored, path)
        if self.is_dir(path):
            raise IsADirectoryError(path)
        self._check_parents(path)
        raise FileNotFoundError(path)

    def write_bytes(self, path: str, data: bytes) -> None:
        """Store a file, creating and recording its parents.

        Raises:
            IsADirectoryError: `path` is a directory.
            NotADirectoryError: A file stands where one of its parents should be.
        """
        path = _normal(path)
        if self.is_dir(path):
            raise IsADirectoryError(path)
        self._check_parents(path)
        lines, encoding = _to_storage(data)
        now = _timestamp()
        existing = self._files.get(path)
        entry = FileData(
            content=lines,
            created_at=existing["created_at"] if existing else now,
            modified_at=now,
        )
        if encoding is not None:
            entry["encoding"] = encoding
        self._files[path] = entry
        self._record_parents(path)

    def size(self, path: str) -> int:
        """The length of the file at `path` in bytes, as `read_bytes` would return it."""
        return len(self.read_bytes(path))

    def list_dir(self, path: str) -> list[tuple[str, bool]]:
        """The names directly in a directory, each with whether it is one, sorted.

        Raises:
            NotADirectoryError: `path` is a file, or a file stands in its way.
            FileNotFoundError: Nothing is at `path`.
        """
        path = _normal(path)
        if path in self._files:
            raise NotADirectoryError(path)
        if not self.is_dir(path):
            self._check_parents(path)
            raise FileNotFoundError(path)
        prefix = "/" if path == "/" else path + "/"
        entries: dict[str, bool] = {}
        for stored in (*self._files, *self._directories):
            if not stored.startswith(prefix) or stored == path:
                continue
            name, _, rest = stored[len(prefix) :].partition("/")
            entries[name] = entries.get(name, False) or bool(rest) or stored in self._directories
        return sorted(entries.items())

    def make_dir(self, path: str) -> None:
        """Create a directory and any missing parents; one that exists is fine.

        Raises:
            FileExistsError: A file is at `path`.
            NotADirectoryError: A file stands where one of its parents should be.
        """
        path = _normal(path)
        if path in self._files:
            raise FileExistsError(path)
        self._check_parents(path)
        if path != "/":
            self._directories.add(path)
            self._record_parents(path)

    def remove(self, path: str) -> None:
        """Remove a file, or a directory and everything under it.

        Raises:
            FileNotFoundError: Nothing is at `path`.
        """
        path = _normal(path)
        if self._files.pop(path, None) is not None:
            return
        if not self.is_dir(path):
            raise FileNotFoundError(path)
        prefix = "/" if path == "/" else path + "/"
        for stored in [p for p in self._files if p.startswith(prefix)]:
            del self._files[stored]
        self._directories = {d for d in self._directories if d != path and not d.startswith(prefix)}

    def _record_parents(self, path: str) -> None:
        """Record the directories above `path`, which a write or `make_dir` created."""
        parent = posixpath.dirname(path)
        while parent != "/":
            self._directories.add(parent)
            parent = posixpath.dirname(parent)

    def _check_parents(self, path: str) -> None:
        """Refuse a path that runs through a file, the way a filesystem does."""
        parent = posixpath.dirname(path)
        while parent != "/":
            if parent in self._files:
                raise NotADirectoryError(parent)
            parent = posixpath.dirname(parent)


def _normal(path: str) -> str:
    """`path` absolute, with `.`, `..` and repeated slashes collapsed as text."""
    return posixpath.normpath(posixpath.join("/", path)).replace("//", "/")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


BYTES_ERRORS = "surrogateescape"
"""How a *legacy* document's text is encoded back to the bytes it was written from.

Nothing written by this version needs it. Before `FileData.encoding` existed,
binary content was decoded with `errors="surrogateescape"` and stored as lines
of text — exact on the way back out, and the reason the resulting document could
not be serialised (see :class:`~pydantic_ai_backends.FileData`). A host that
persisted such a document and loads it here still gets its bytes back, because
encoding with the same handler is the inverse of how they went in.
"""


def _to_storage(data: bytes) -> tuple[list[str], Literal["base64"] | None]:
    """The lines to store for `data`, and the encoding marker they need.

    Bytes that decode as UTF-8 are stored as lines, so the document stays
    readable to whoever inspects it; anything else becomes base64, which is what
    keeps the document valid JSON.
    """
    try:
        return data.decode("utf-8").split("\n"), None
    except UnicodeDecodeError:
        return [base64.b64encode(data).decode("ascii")], "base64"


def _content_bytes(data: FileData, path: str) -> bytes:
    """The file's bytes, whichever way its content is stored."""
    if data.get("encoding") == "base64":
        try:
            return base64.b64decode("".join(data["content"]), validate=True)
        except binascii.Error as error:
            raise ValueError(f"{path} holds base64 that does not decode") from error
    return "\n".join(data["content"]).encode("utf-8", errors=BYTES_ERRORS)
