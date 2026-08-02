"""In-memory file storage backend."""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime, timezone
from typing import Literal

from wcmatch import glob as wcglob

from pydantic_ai_backends._editing import Replacement, replace_in_content
from pydantic_ai_backends._paths import normalize_path, unsafe_path_reason
from pydantic_ai_backends.types import EditResult, FileData, FileInfo, GrepMatch, WriteResult


def is_hidden_path(path: str) -> bool:
    """Whether any directory or the filename itself starts with a dot."""
    return path.startswith(".") or "/." in path


class StateBackend:
    """In-memory file storage backend.

    Files live in a dictionary and are ephemeral — lost when the process ends,
    unless a host persists :attr:`files` and hands it back. Useful for testing,
    for scratch space alongside a real backend, and as the whole storage layer
    for an application that keeps the document itself.

    Binary content is stored base64 (see :class:`~pydantic_ai_backends.FileData`)
    so that document is always JSON. `read` and `grep` decline to treat such a
    file as text rather than showing its encoded form; `read_bytes` returns
    exactly what was written.

    Example:
        ```python
        from pydantic_ai_backends import StateBackend

        backend = StateBackend()
        backend.write("/src/app.py", "print('hello')")
        content = backend.read("/src/app.py")
        print(content)  # "     1\\tprint('hello')"
        matches = backend.grep_raw("print")

        restored = StateBackend(files=json.loads(json.dumps(backend.files)))
        ```
    """

    def __init__(self, files: dict[str, FileData] | None = None):
        """Initialize the backend.

        Args:
            files: Optional initial file dictionary. A document a previous
                instance produced, including one that has been through JSON,
                loads unchanged — as does one written before `encoding` existed.
        """
        self._files: dict[str, FileData] = files if files is not None else {}

    @property
    def files(self) -> dict[str, FileData]:
        """The internal files dictionary.

        Always a JSON-serialisable document: `json.loads(json.dumps(files))`
        round-trips, and so does storing it in a PostgreSQL `jsonb` column.
        """
        return self._files

    def exists(self, path: str) -> bool:
        """Whether a file is stored at `path`."""
        if unsafe_path_reason(path) is not None:
            return False
        return normalize_path(path) in self._files

    def ls_info(self, path: str) -> list[FileInfo]:
        """List the files and directories directly under `path`."""
        if unsafe_path_reason(path) is not None:
            return []

        path = normalize_path(path)
        prefix = path if path == "/" else path + "/"
        entries: dict[str, FileInfo] = {}

        for file_path, file_data in self._files.items():
            if file_path == path:
                name = file_path.rsplit("/", 1)[-1]
                entries[name] = _file_entry(name, file_path, file_data)
                continue
            if not file_path.startswith(prefix):
                continue

            name, _, rest = file_path[len(prefix) :].partition("/")
            if name in entries:
                continue
            if rest:
                entries[name] = FileInfo(name=name, path=prefix + name, is_dir=True, size=None)
            else:
                entries[name] = _file_entry(name, file_path, file_data)

        return sorted(entries.values(), key=lambda x: (not x["is_dir"], x["name"]))

    def read_bytes(self, path: str) -> bytes:
        """Read a whole file as bytes, or `b""` when there is none at `path`."""
        if unsafe_path_reason(path) is not None:
            return b""

        stored = self._files.get(normalize_path(path))
        if stored is None:
            return b""
        return _content_bytes(stored)

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a slice of a file with line numbers.

        A binary file is refused rather than rendered: its stored form is
        base64, and handing that to a model as if it were the file's text is
        worse than saying there is nothing here to read.
        """
        reason = unsafe_path_reason(path)
        if reason is not None:
            return f"Error: {reason}"

        path = normalize_path(path)
        stored = self._files.get(path)
        if stored is None:
            return f"Error: File '{path}' not found"
        if _is_binary(stored):
            return f"Error: File '{path}' is binary; read it as bytes instead"

        lines = stored["content"]
        if offset >= len(lines):
            return f"Error: Offset {offset} exceeds file length ({len(lines)} lines)"

        end = min(offset + limit, len(lines))
        numbered = "\n".join(f"{i + 1:>6}\t{lines[i]}" for i in range(offset, end))
        if end < len(lines):
            return f"{numbered}\n\n... ({len(lines) - end} more lines)"
        return numbered

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write a file, replacing any existing content."""
        reason = unsafe_path_reason(path)
        if reason is not None:
            return WriteResult(error=reason)

        path = normalize_path(path)
        lines, encoding = _to_storage(content)

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
        return WriteResult(path=path)

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit a file by replacing a string."""
        reason = unsafe_path_reason(path)
        if reason is not None:
            return EditResult(error=reason)

        path = normalize_path(path)
        stored = self._files.get(path)
        if stored is None:
            return EditResult(error=f"File '{path}' not found")
        if _is_binary(stored):
            return EditResult(error=f"File '{path}' is binary and cannot be edited as text")

        outcome = replace_in_content(
            "\n".join(stored["content"]), old_string, new_string, replace_all
        )
        if not isinstance(outcome, Replacement):
            return EditResult(error=outcome)

        stored["content"] = outcome.content.split("\n")
        stored["modified_at"] = _timestamp()
        return EditResult(path=path, occurrences=outcome.occurrences)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Match stored paths against a glob pattern."""
        if unsafe_path_reason(path) is not None:
            return []

        path = normalize_path(path)
        root = "" if path == "/" else path
        full_pattern = f"{root}/{pattern.lstrip('/')}"

        results = [
            _file_entry(file_path.rsplit("/", 1)[-1], file_path, file_data)
            for file_path, file_data in self._files.items()
            if wcglob.globmatch(file_path, full_pattern, flags=wcglob.GLOBSTAR)
        ]
        return sorted(results, key=lambda x: x["path"])

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search stored file contents for a regex."""
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: Invalid regex pattern: {e}"

        searchable = self._searchable_paths(path, ignore_hidden)
        if isinstance(searchable, str):
            return searchable

        if glob:
            glob_pattern = "/" + glob.lstrip("/")
            searchable = [
                p for p in searchable if wcglob.globmatch(p, glob_pattern, flags=wcglob.GLOBSTAR)
            ]

        # Binary files are skipped rather than searched: their stored form is
        # base64, so a pattern would be matched against an encoding nobody wrote
        # and the hit would name a line number that does not exist in the file.
        return [
            GrepMatch(path=file_path, line_number=i + 1, line=line)
            for file_path in searchable
            if not _is_binary(self._files[file_path])
            for i, line in enumerate(self._files[file_path]["content"])
            if regex.search(line)
        ]

    def _searchable_paths(self, path: str | None, ignore_hidden: bool) -> list[str] | str:
        """Paths grep should walk, or an error message when `path` is invalid.

        A file named outright is searched even when hidden; `ignore_hidden` only
        filters the directory walk.
        """
        visible = [p for p in self._files if not ignore_hidden or not is_hidden_path(p)]

        if path is None:
            return visible

        reason = unsafe_path_reason(path)
        if reason is not None:
            return f"Error: {reason}"

        path = normalize_path(path)
        if path in self._files:
            return [path]

        prefix = path if path == "/" else path + "/"
        return [p for p in visible if p.startswith(prefix)]


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

Kept for that alone. New binary content never reaches this path: `_to_storage`
sends it to base64 instead.
"""


def _to_storage(content: str | bytes) -> tuple[list[str], Literal["base64"] | None]:
    """The lines to store for `content`, and the encoding marker they need.

    Text — including bytes that decode as UTF-8 — is stored as lines, because a
    file written as `b"print(1)"` should still be readable, greppable and
    editable as the text it is. Only content that cannot be UTF-8 becomes
    base64, which is what keeps the stored document valid JSON.
    """
    if isinstance(content, bytes):
        try:
            return content.decode("utf-8").split("\n"), None
        except UnicodeDecodeError:
            return [base64.b64encode(content).decode("ascii")], "base64"

    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        # A `str` carrying lone surrogates — what a caller gets from decoding
        # bytes with `surrogateescape`, and the one way text could still put an
        # unserialisable value into the document. Store the bytes it stands for.
        raw = content.encode("utf-8", errors=BYTES_ERRORS)
        return [base64.b64encode(raw).decode("ascii")], "base64"
    return content.split("\n"), None


def _is_binary(data: FileData) -> bool:
    """Whether this entry's `content` is base64 rather than lines of text."""
    return data.get("encoding") == "base64"


def _content_bytes(data: FileData) -> bytes:
    """The file's bytes, whichever way its content is stored.

    Returns `b""` for a base64 entry that does not decode, per the protocol's
    contract that `read_bytes` never reports an error it could be confused for
    content. Reachable because `files=` accepts a document this backend did not
    write, and a truncated one is a real way to arrive here.
    """
    if _is_binary(data):
        try:
            return base64.b64decode("".join(data["content"]), validate=True)
        except binascii.Error:
            return b""
    return _encode("\n".join(data["content"]))


def _encode(text: str) -> bytes:
    """Stored text back as the bytes it was written from."""
    return text.encode("utf-8", errors=BYTES_ERRORS)


def _file_entry(name: str, path: str, data: FileData) -> FileInfo:
    return FileInfo(
        name=name,
        path=path,
        is_dir=False,
        # The size a reader gets from `read_bytes`, which for a base64 entry is
        # the decoded length and not the length of the encoding. The separators
        # count too: text is stored split on "\n" and rejoined on the way out,
        # so summing the lines alone reported one byte less per line.
        size=len(_content_bytes(data)),
    )
