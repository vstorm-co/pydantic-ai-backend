"""Composite backends that route operations by path prefix."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic_ai_backends._paths import normalize_path
from pydantic_ai_backends.adapter import ensure_async
from pydantic_ai_backends.protocol import AsyncBackendProtocol, BackendProtocol
from pydantic_ai_backends.types import EditResult, FileInfo, GrepMatch, WriteResult

BackendT = TypeVar("BackendT")

ROOT_PATHS = ("/", "")
"""Paths that mean "everywhere", and so fan out to every backend."""


class PrefixRouter(Generic[BackendT]):
    """Maps a path to the backend responsible for it.

    A path matches a prefix when it equals the prefix or sits under it. Longer
    prefixes are tried first, so a nested route wins over its parent.
    """

    def __init__(self, default: BackendT, routes: dict[str, BackendT] | None = None) -> None:
        self.default = default
        self.routes = routes or {}
        self._prefixes = sorted(self.routes, key=len, reverse=True)

    def for_path(self, path: str) -> BackendT:
        """The backend handling `path`, or the default when no route matches."""
        normalized = normalize_path(path)
        for prefix in self._prefixes:
            normalized_prefix = normalize_path(prefix)
            if normalized == normalized_prefix or normalized.startswith(normalized_prefix + "/"):
                return self.routes[prefix]
        return self.default

    def route_directories(self, entries: dict[str, FileInfo]) -> dict[str, FileInfo]:
        """Add a virtual directory entry for each route's first path segment.

        Routed backends hold paths that the default backend knows nothing about,
        so a listing of `/` would otherwise not show them at all.
        """
        for prefix in self.routes:
            name = prefix.strip("/").split("/")[0]
            if not name:
                continue
            path = "/" + name
            entries.setdefault(path, FileInfo(name=name, path=path, is_dir=True, size=None))
        return entries


def _sorted_entries(entries: dict[str, FileInfo]) -> list[FileInfo]:
    return sorted(entries.values(), key=lambda x: (not x["is_dir"], x["name"]))


class CompositeBackend:
    """Backend that routes operations to other backends by path prefix.

    Note:
        Paths reach the matched backend **as-is** — no prefix is stripped — so
        every backend must accept the full virtual path. `StateBackend` accepts
        any path, which makes it the natural choice for a route. `LocalBackend`
        validates paths against its `root_dir` and will reject virtual paths, so
        use it as the **default**, not inside `routes`.

    Example:
        ```python
        from pydantic_ai_backends import CompositeBackend, LocalBackend, StateBackend

        backend = CompositeBackend(
            default=LocalBackend(root_dir="/home/user/project"),
            routes={"/scratch/": StateBackend()},
        )

        backend.write("src/app.py", "...")       # real filesystem
        backend.write("/scratch/temp.txt", "...")  # ephemeral
        ```
    """

    def __init__(
        self,
        default: BackendProtocol,
        routes: dict[str, BackendProtocol] | None = None,
    ):
        """Initialize the composite.

        Args:
            default: Backend for paths that match no route.
            routes: Path prefix to backend, e.g. `{"/memories/": store}`.
        """
        self._router: PrefixRouter[BackendProtocol] = PrefixRouter(default, routes)

    def exists(self, path: str) -> bool:
        """Check existence via the backend handling this path."""
        return self._router.for_path(path).exists(path)

    def ls_info(self, path: str) -> list[FileInfo]:
        """List one directory, showing route mount points when listing `/`."""
        normalized = normalize_path(path)
        if normalized != "/":
            return self._router.for_path(normalized).ls_info(normalized)

        entries = {entry["path"]: entry for entry in self._router.default.ls_info(normalized)}
        return _sorted_entries(self._router.route_directories(entries))

    def read_bytes(self, path: str) -> bytes:
        """Read bytes from the backend handling this path."""
        return self._router.for_path(path).read_bytes(path)

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read from the backend handling this path."""
        return self._router.for_path(path).read(path, offset, limit)

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write to the backend handling this path."""
        return self._router.for_path(path).write(path, content)

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit via the backend handling this path."""
        return self._router.for_path(path).edit(path, old_string, new_string, replace_all)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Match files, searching every backend when starting from the root."""
        if path not in ROOT_PATHS:
            return self._router.for_path(path).glob_info(pattern, path)

        results = list(self._router.default.glob_info(pattern, path))
        for prefix, backend in self._router.routes.items():
            results.extend(backend.glob_info(pattern, prefix))
        return sorted(results, key=lambda x: x["path"])

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search, covering every backend when no specific path is given.

        An error from any backend is returned as-is rather than dropped, so a
        failed search is never mistaken for no matches.
        """
        if path is not None and path not in ROOT_PATHS:
            return self._router.for_path(path).grep_raw(pattern, path, glob, ignore_hidden)

        matches: list[GrepMatch] = []
        searches = [(path, self._router.default), *self._router.routes.items()]
        for search_path, backend in searches:
            result = backend.grep_raw(pattern, search_path, glob, ignore_hidden)
            if not isinstance(result, list):
                return result
            matches.extend(result)
        return matches


class AsyncCompositeBackend:
    """Async backend that routes operations to other backends by path prefix.

    Accepts sync and async sub-backends; sync ones are wrapped with
    :func:`~pydantic_ai_backends.ensure_async`.

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
        """Initialize the composite.

        Args:
            default: Backend for paths that match no route.
            routes: Path prefix to backend.
        """
        self._router: PrefixRouter[AsyncBackendProtocol] = PrefixRouter(
            ensure_async(default),
            {prefix: ensure_async(backend) for prefix, backend in (routes or {}).items()},
        )

    async def exists(self, path: str) -> bool:
        """Check existence via the backend handling this path."""
        return await self._router.for_path(path).exists(path)

    async def ls_info(self, path: str) -> list[FileInfo]:
        """List one directory, showing route mount points when listing `/`."""
        normalized = normalize_path(path)
        if normalized != "/":
            return await self._router.for_path(normalized).ls_info(normalized)

        listed = await self._router.default.ls_info(normalized)
        entries = {entry["path"]: entry for entry in listed}
        return _sorted_entries(self._router.route_directories(entries))

    async def read_bytes(self, path: str) -> bytes:
        """Read bytes from the backend handling this path."""
        return await self._router.for_path(path).read_bytes(path)

    async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read from the backend handling this path."""
        return await self._router.for_path(path).read(path, offset, limit)

    async def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write to the backend handling this path."""
        return await self._router.for_path(path).write(path, content)

    async def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit via the backend handling this path."""
        return await self._router.for_path(path).edit(path, old_string, new_string, replace_all)

    async def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Match files, searching every backend when starting from the root."""
        if path not in ROOT_PATHS:
            return await self._router.for_path(path).glob_info(pattern, path)

        results = list(await self._router.default.glob_info(pattern, path))
        for prefix, backend in self._router.routes.items():
            results.extend(await backend.glob_info(pattern, prefix))
        return sorted(results, key=lambda x: x["path"])

    async def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search, covering every backend when no specific path is given."""
        if path is not None and path not in ROOT_PATHS:
            return await self._router.for_path(path).grep_raw(pattern, path, glob, ignore_hidden)

        matches: list[GrepMatch] = []
        searches = [(path, self._router.default), *self._router.routes.items()]
        for search_path, backend in searches:
            result = await backend.grep_raw(pattern, search_path, glob, ignore_hidden)
            if not isinstance(result, list):
                return result
            matches.extend(result)
        return matches
