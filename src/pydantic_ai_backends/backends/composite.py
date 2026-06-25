"""Composite backend that routes operations by path prefix."""

from __future__ import annotations

from pydantic_ai_backends.adapter import ensure_async
from pydantic_ai_backends.protocol import AsyncBackendProtocol, BackendProtocol
from pydantic_ai_backends.types import EditResult, FileInfo, GrepMatch, WriteResult


class CompositeBackend:
    """Backend that routes operations to different backends by path prefix.

    Allows combining multiple backends (e.g., memory for temp files,
    filesystem for persistent storage) under a unified interface.

    Note:
        Routes are matched using exact-or-child semantics: a path matches a prefix
        if it equals the prefix exactly or starts with `prefix + "/"`.
        Paths are passed to the matched backend **as-is** (no prefix stripping),
        so each backend must accept the full virtual path.

        `StateBackend` accepts any virtual path, making it the natural choice for
        routes. `LocalBackend` validates paths against its `root_dir` and will
        reject virtual paths that differ from the real filesystem path — use it as
        the **default** backend, not inside `routes`.

    Example:
        ```python
        from pydantic_ai_backends import CompositeBackend, StateBackend, LocalBackend

        # LocalBackend as default (real filesystem), StateBackend for ephemeral space
        backend = CompositeBackend(
            default=LocalBackend(root_dir="/home/user/project"),
            routes={
                "/scratch/": StateBackend(),  # Ephemeral virtual space
            },
        )

        # Routes to LocalBackend (real filesystem, relative to root_dir)
        backend.write("src/app.py", "...")

        # Routes to StateBackend (ephemeral)
        backend.write("/scratch/temp.txt", "...")
        ```
    """

    def __init__(
        self,
        default: BackendProtocol,
        routes: dict[str, BackendProtocol] | None = None,
    ):
        """Initialize composite backend.

        Args:
            default: Default backend for paths that don't match any route.
            routes: Dictionary mapping path prefixes to backends.
                    e.g., {"/memories/": store_backend, "/temp/": state_backend}
        """
        self._default = default
        self._routes = routes or {}

        # Sort routes by length (longest first) for correct matching
        self._sorted_prefixes = sorted(self._routes.keys(), key=len, reverse=True)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize path to consistent format."""
        return _normalize_path(path)

    def _get_backend(self, path: str) -> BackendProtocol:
        """Get the appropriate backend for a path."""
        normalized = self._normalize_path(path)
        for prefix in self._sorted_prefixes:
            normalized_prefix = self._normalize_path(prefix)
            if normalized == normalized_prefix or normalized.startswith(normalized_prefix + "/"):
                return self._routes[prefix]
        return self._default

    def exists(self, path: str) -> bool:
        """Check existence via the route handling this path."""
        return self._get_backend(path).exists(path)

    def ls_info(self, path: str) -> list[FileInfo]:
        """List files, aggregating from all relevant backends."""
        normalized = self._normalize_path(path)

        # For root path, aggregate from all backends
        if normalized == "/":
            all_entries: dict[str, FileInfo] = {}

            # First, get entries from default backend
            for entry in self._default.ls_info(normalized):
                all_entries[entry["path"]] = entry

            # Then, add virtual directories for route prefixes
            for prefix in self._routes:
                # Extract first directory from prefix
                parts = prefix.strip("/").split("/")
                if parts[0]:
                    dir_name = parts[0]
                    dir_path = "/" + dir_name
                    if dir_path not in all_entries:
                        all_entries[dir_path] = FileInfo(
                            name=dir_name,
                            path=dir_path,
                            is_dir=True,
                            size=None,
                        )

            return sorted(all_entries.values(), key=lambda x: (not x["is_dir"], x["name"]))

        # Route to appropriate backend
        backend = self._get_backend(normalized)
        return backend.ls_info(normalized)

    def read_bytes(self, path: str) -> bytes:
        """Read bytes from the appropriate backend."""
        return self._get_backend(path).read_bytes(path)

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read from the appropriate backend."""
        return self._get_backend(path).read(path, offset, limit)

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write to the appropriate backend."""
        return self._get_backend(path).write(path, content)

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit using the appropriate backend."""
        return self._get_backend(path).edit(path, old_string, new_string, replace_all)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Glob across all backends if searching from root."""
        if path == "/" or path == "":
            all_results: list[FileInfo] = []

            # Search in default backend
            all_results.extend(self._default.glob_info(pattern, path))

            # Search in each routed backend
            for prefix, backend in self._routes.items():
                results = backend.glob_info(pattern, prefix)
                all_results.extend(results)

            return sorted(all_results, key=lambda x: x["path"])

        return self._get_backend(path).glob_info(pattern, path)

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Grep across all backends if no specific path."""
        if path is None or path == "/" or path == "":
            all_results: list[GrepMatch] = []

            # Search in default backend. Propagate an error string (e.g. an
            # invalid regex) instead of silently dropping it, so callers can
            # tell "no matches" apart from "search failed".
            result = self._default.grep_raw(pattern, path, glob, ignore_hidden)
            if isinstance(result, list):
                all_results.extend(result)
            else:
                return result

            # Search in each routed backend
            for prefix, backend in self._routes.items():
                result = backend.grep_raw(pattern, prefix, glob, ignore_hidden)
                if isinstance(result, list):
                    all_results.extend(result)
                else:
                    return result

            return all_results

        return self._get_backend(path).grep_raw(pattern, path, glob, ignore_hidden)


def _normalize_path(path: str) -> str:
    """Normalize path to consistent format."""
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


class AsyncCompositeBackend:
    """Async backend that routes operations to different backends by path prefix.

    Accepts both sync and async sub-backends; sync backends are wrapped
    via :func:`~pydantic_ai_backends.ensure_async` internally.

    Example:
        ```python
        from pydantic_ai_backends import AsyncCompositeBackend, StateBackend

        backend = AsyncCompositeBackend(
            default=StateBackend(),
            routes={"/scratch/": StateBackend()},
        )
        await backend.write("/scratch/tmp.txt", "data")
        content = await backend.read("/scratch/tmp.txt")
        ```
    """

    def __init__(
        self,
        default: BackendProtocol | AsyncBackendProtocol,
        routes: dict[str, BackendProtocol | AsyncBackendProtocol] | None = None,
    ):
        self._default = ensure_async(default)
        raw_routes = routes or {}
        self._routes = {k: ensure_async(v) for k, v in raw_routes.items()}
        self._sorted_prefixes = sorted(self._routes.keys(), key=len, reverse=True)

    def _get_backend(self, path: str) -> AsyncBackendProtocol:
        normalized = _normalize_path(path)
        for prefix in self._sorted_prefixes:
            normalized_prefix = _normalize_path(prefix)
            if normalized == normalized_prefix or normalized.startswith(normalized_prefix + "/"):
                return self._routes[prefix]
        return self._default

    async def exists(self, path: str) -> bool:
        return await self._get_backend(path).exists(path)

    async def ls_info(self, path: str) -> list[FileInfo]:
        normalized = _normalize_path(path)

        if normalized == "/":
            all_entries: dict[str, FileInfo] = {}

            for entry in await self._default.ls_info(normalized):
                all_entries[entry["path"]] = entry

            for prefix in self._routes:
                parts = prefix.strip("/").split("/")
                if parts[0]:
                    dir_name = parts[0]
                    dir_path = "/" + dir_name
                    if dir_path not in all_entries:
                        all_entries[dir_path] = FileInfo(
                            name=dir_name,
                            path=dir_path,
                            is_dir=True,
                            size=None,
                        )

            return sorted(all_entries.values(), key=lambda x: (not x["is_dir"], x["name"]))

        backend = self._get_backend(normalized)
        return await backend.ls_info(normalized)

    async def read_bytes(self, path: str) -> bytes:
        return await self._get_backend(path).read_bytes(path)

    async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        return await self._get_backend(path).read(path, offset, limit)

    async def write(self, path: str, content: str | bytes) -> WriteResult:
        return await self._get_backend(path).write(path, content)

    async def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return await self._get_backend(path).edit(path, old_string, new_string, replace_all)

    async def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        if path == "/" or path == "":
            all_results: list[FileInfo] = []

            all_results.extend(await self._default.glob_info(pattern, path))

            for prefix, backend in self._routes.items():
                results = await backend.glob_info(pattern, prefix)
                all_results.extend(results)

            return sorted(all_results, key=lambda x: x["path"])

        return await self._get_backend(path).glob_info(pattern, path)

    async def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        if path is None or path == "/" or path == "":
            all_results: list[GrepMatch] = []

            result = await self._default.grep_raw(pattern, path, glob, ignore_hidden)
            if isinstance(result, list):
                all_results.extend(result)
            else:
                return result

            for prefix, backend in self._routes.items():
                result = await backend.grep_raw(pattern, prefix, glob, ignore_hidden)
                if isinstance(result, list):
                    all_results.extend(result)
                else:
                    return result

            return all_results

        return await self._get_backend(path).grep_raw(pattern, path, glob, ignore_hidden)
