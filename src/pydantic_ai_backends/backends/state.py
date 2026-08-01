"""In-memory file storage backend."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from wcmatch import glob as wcglob

from pydantic_ai_backends._editing import Replacement, replace_in_content
from pydantic_ai_backends._paths import normalize_path, unsafe_path_reason
from pydantic_ai_backends.types import EditResult, FileData, FileInfo, GrepMatch, WriteResult


def is_hidden_path(path: str) -> bool:
    """Whether any directory or the filename itself starts with a dot."""
    return path.startswith(".") or "/." in path


class StateBackend:
    """In-memory file storage backend.

    Files live in a dictionary and are ephemeral — lost when the process ends.
    Useful for testing and for scratch space alongside a real backend.

    Example:
        ```python
        from pydantic_ai_backends import StateBackend

        backend = StateBackend()
        backend.write("/src/app.py", "print('hello')")
        content = backend.read("/src/app.py")
        print(content)  # "     1\\tprint('hello')"
        matches = backend.grep_raw("print")
        ```
    """

    def __init__(self, files: dict[str, FileData] | None = None):
        """Initialize the backend.

        Args:
            files: Optional initial file dictionary.
        """
        self._files: dict[str, FileData] = files if files is not None else {}

    @property
    def files(self) -> dict[str, FileData]:
        """The internal files dictionary."""
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
        return _encode("\n".join(stored["content"]))

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a slice of a file with line numbers."""
        reason = unsafe_path_reason(path)
        if reason is not None:
            return f"Error: {reason}"

        path = normalize_path(path)
        stored = self._files.get(path)
        if stored is None:
            return f"Error: File '{path}' not found"

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
        if isinstance(content, bytes):
            content = _decode(content)

        now = _timestamp()
        existing = self._files.get(path)
        self._files[path] = FileData(
            content=content.split("\n"),
            created_at=existing["created_at"] if existing else now,
            modified_at=now,
        )
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

        return [
            GrepMatch(path=file_path, line_number=i + 1, line=line)
            for file_path in searchable
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
"""How bytes cross into the `str` lines this backend stores, and back.

Files here are lines of text, so binary content has to survive a `str` round
trip. `errors="replace"` did not: every byte that is not UTF-8 became U+FFFD, so
a PNG written through `write` came back from `read_bytes` as different bytes,
with a `WriteResult` reporting success. `surrogateescape` maps those bytes to
lone surrogates and maps them back unchanged, which makes the round trip exact.

The cost is that `read` and `grep` show a surrogate where the undecodable byte
was — no worse than the replacement character they showed before, and only for
content that was never text.
"""


def _decode(data: bytes) -> str:
    """Bytes as the `str` this backend stores, reversibly."""
    return data.decode("utf-8", errors=BYTES_ERRORS)


def _encode(text: str) -> bytes:
    """Stored text back as the bytes it was written from."""
    return text.encode("utf-8", errors=BYTES_ERRORS)


def _file_entry(name: str, path: str, data: FileData) -> FileInfo:
    return FileInfo(
        name=name,
        path=path,
        is_dir=False,
        # The separators count: content is stored split on "\n" and rejoined on
        # the way out, so summing the lines alone reported one byte less per line
        # than `read_bytes` returns.
        size=len(_encode("\n".join(data["content"]))),
    )
