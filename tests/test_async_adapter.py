from __future__ import annotations

from pydantic_ai_backends import (
    AsyncBackendAdapter,
    AsyncCompositeBackend,
    AsyncSandboxAdapter,
    ExecuteResponse,
    StateBackend,
    WriteResult,
    ensure_async,
)
from pydantic_ai_backends.types import EditResult, FileInfo, GrepMatch


class PublicReadBytesBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def exists(self, path: str) -> bool:
        self.calls.append(("exists", (path,)))
        return path == "/file.txt"

    def ls_info(self, path: str) -> list[FileInfo]:
        self.calls.append(("ls_info", (path,)))
        return [FileInfo(name="file.txt", path="/file.txt", is_dir=False, size=4)]

    def read_bytes(self, path: str) -> bytes:
        self.calls.append(("read_bytes", (path,)))
        return b"data"

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        self.calls.append(("read", (path, offset, limit)))
        return "     1\tdata"

    def write(self, path: str, content: str | bytes) -> WriteResult:
        self.calls.append(("write", (path, content)))
        return WriteResult(path=path)

    def edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        self.calls.append(("edit", (path, old_string, new_string, replace_all)))
        return EditResult(path=path, occurrences=1)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        self.calls.append(("glob_info", (pattern, path)))
        return [FileInfo(name="file.txt", path="/file.txt", is_dir=False, size=4)]

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        self.calls.append(("grep_raw", (pattern, path, glob, ignore_hidden)))
        return [GrepMatch(path="/file.txt", line_number=1, line="data", match="data")]


class PrivateReadBytesBackend(PublicReadBytesBackend):
    read_bytes = None  # type: ignore[assignment]

    def _read_bytes(self, path: str) -> bytes:
        self.calls.append(("_read_bytes", (path,)))
        return b"private"


class SandboxBackend(PublicReadBytesBackend):
    id = "sandbox"

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        self.calls.append(("execute", (command, timeout)))
        return ExecuteResponse(output="ok", exit_code=0)


class AsyncExecuteSandboxBackend(SandboxBackend):
    async def async_execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        self.calls.append(("async_execute", (command, timeout)))
        return ExecuteResponse(output="async ok", exit_code=0)


class AsyncNativeBackend:
    async def exists(self, path: str) -> bool:
        return True

    async def read_bytes(self, path: str) -> bytes:
        return b"async"


async def test_adapter_delegates_file_methods_to_sync_backend() -> None:
    backend = PublicReadBytesBackend()
    adapter = AsyncBackendAdapter(backend)

    assert adapter.unwrap() is backend
    assert await adapter.exists("/file.txt") is True
    assert await adapter.ls_info("/") == [
        FileInfo(name="file.txt", path="/file.txt", is_dir=False, size=4)
    ]
    assert await adapter.read_bytes("/file.txt") == b"data"
    assert await adapter.read("/file.txt", 1, 2) == "     1\tdata"
    assert await adapter.write("/new.txt", "new") == WriteResult(path="/new.txt")
    assert await adapter.edit("/file.txt", "a", "b", True) == EditResult(
        path="/file.txt", occurrences=1
    )
    assert await adapter.glob_info("*.txt", "/") == [
        FileInfo(name="file.txt", path="/file.txt", is_dir=False, size=4)
    ]
    assert await adapter.grep_raw("data", "/", "*.txt", False) == [
        GrepMatch(path="/file.txt", line_number=1, line="data", match="data")
    ]

    assert ("read_bytes", ("/file.txt",)) in backend.calls


async def test_adapter_falls_back_to_private_read_bytes() -> None:
    backend = PrivateReadBytesBackend()
    adapter = AsyncBackendAdapter(backend)

    assert await adapter.read_bytes("/file.txt") == b"private"
    assert backend.calls == [("_read_bytes", ("/file.txt",))]


async def test_ensure_async_is_idempotent_and_preserves_async_native_backend() -> None:
    sync_backend = PublicReadBytesBackend()
    adapter = ensure_async(sync_backend)

    assert isinstance(adapter, AsyncBackendAdapter)
    assert ensure_async(adapter) is adapter

    async_backend = AsyncNativeBackend()
    assert ensure_async(async_backend) is async_backend
    assert await ensure_async(async_backend).read_bytes("/anything") == b"async"


async def test_ensure_async_wraps_sandbox_backends_with_execute() -> None:
    backend = SandboxBackend()
    adapter = ensure_async(backend)

    assert isinstance(adapter, AsyncSandboxAdapter)
    assert await adapter.execute("echo ok", 5) == ExecuteResponse(output="ok", exit_code=0)
    assert backend.calls[-1] == ("execute", ("echo ok", 5))


async def test_sandbox_adapter_prefers_async_execute_when_available() -> None:
    backend = AsyncExecuteSandboxBackend()
    adapter = ensure_async(backend)

    assert isinstance(adapter, AsyncSandboxAdapter)
    assert await adapter.execute("echo ok", 5) == ExecuteResponse(output="async ok", exit_code=0)
    assert backend.calls[-1] == ("async_execute", ("echo ok", 5))
    assert not any(call[0] == "execute" for call in backend.calls)


# --- AsyncCompositeBackend tests ---


async def test_async_composite_mixed_sync_async_routing() -> None:
    sync_backend = StateBackend()
    async_backend = ensure_async(StateBackend())

    composite = AsyncCompositeBackend(
        default=sync_backend,
        routes={"/scratch/": async_backend},
    )

    await composite.write("/default.txt", "default data")
    await composite.write("/scratch/temp.txt", "scratch data")

    assert await composite.read("/default.txt") == "     1\tdefault data"
    assert await composite.read("/scratch/temp.txt") == "     1\tscratch data"


async def test_async_composite_read_write_edit_routing() -> None:
    default = StateBackend()
    routed = StateBackend()
    composite = AsyncCompositeBackend(default=default, routes={"/data/": routed})

    await composite.write("/data/file.txt", "hello world")
    assert await composite.exists("/data/file.txt") is True
    assert await composite.exists("/data/missing.txt") is False

    result = await composite.edit("/data/file.txt", "hello", "goodbye")
    assert result.path == "/data/file.txt"

    content = await composite.read("/data/file.txt")
    assert "goodbye world" in content

    raw = await composite.read_bytes("/data/file.txt")
    assert raw == b"goodbye world"


async def test_async_composite_ls_info_root_aggregation() -> None:
    default = StateBackend()
    scratch = StateBackend()

    default.write("/readme.txt", "hi")
    scratch.write("/scratch/tmp.txt", "tmp")

    composite = AsyncCompositeBackend(default=default, routes={"/scratch/": scratch})
    entries = await composite.ls_info("/")

    paths = [e["path"] for e in entries]
    assert "/scratch" in paths
    assert "/readme.txt" in paths


async def test_async_composite_glob_info_root_aggregation() -> None:
    default = StateBackend()
    routed = StateBackend()

    default.write("/a.txt", "a")
    routed.write("/data/b.txt", "b")

    composite = AsyncCompositeBackend(default=default, routes={"/data/": routed})
    results = await composite.glob_info("**/*.txt")

    paths = [r["path"] for r in results]
    assert "/a.txt" in paths
    assert "/data/b.txt" in paths


async def test_async_composite_grep_raw_aggregation() -> None:
    default = StateBackend()
    routed = StateBackend()

    default.write("/a.py", "foo = 1")
    routed.write("/lib/b.py", "foo = 2")

    composite = AsyncCompositeBackend(default=default, routes={"/lib/": routed})
    results = await composite.grep_raw("foo")

    assert isinstance(results, list)
    paths = [r["path"] for r in results]
    assert "/a.py" in paths
    assert "/lib/b.py" in paths


async def test_async_composite_grep_raw_error_propagation() -> None:
    backend = PublicReadBytesBackend()

    class ErrorGrepBackend(PublicReadBytesBackend):
        def grep_raw(
            self,
            pattern: str,
            path: str | None = None,
            glob: str | None = None,
            ignore_hidden: bool = True,
        ) -> list[GrepMatch] | str:
            return "regex error: invalid pattern"

    composite = AsyncCompositeBackend(default=ErrorGrepBackend(), routes={"/ok/": backend})
    result = await composite.grep_raw("(invalid")
    assert isinstance(result, str)
    assert "error" in result


async def test_async_composite_grep_raw_routed_error_propagation() -> None:
    class ErrorGrepBackend(PublicReadBytesBackend):
        def grep_raw(
            self,
            pattern: str,
            path: str | None = None,
            glob: str | None = None,
            ignore_hidden: bool = True,
        ) -> list[GrepMatch] | str:
            return "regex error: invalid pattern"

    composite = AsyncCompositeBackend(
        default=PublicReadBytesBackend(), routes={"/err/": ErrorGrepBackend()}
    )
    result = await composite.grep_raw("(invalid")
    assert isinstance(result, str)
    assert "error" in result


async def test_async_composite_exists_routing() -> None:
    default = StateBackend()
    routed = StateBackend()

    default.write("/a.txt", "a")
    routed.write("/mount/b.txt", "b")

    composite = AsyncCompositeBackend(default=default, routes={"/mount/": routed})
    assert await composite.exists("/a.txt") is True
    assert await composite.exists("/mount/b.txt") is True
    assert await composite.exists("/mount/missing.txt") is False
    assert await composite.exists("/missing.txt") is False


async def test_async_composite_path_normalization() -> None:
    default = StateBackend()
    routed = StateBackend()

    composite = AsyncCompositeBackend(default=default, routes={"/data/": routed})

    await composite.write("/data/file.txt", "content")
    # Path without leading slash should still route correctly
    assert await composite.exists("data/file.txt") is True


async def test_async_composite_glob_info_routed_path() -> None:
    routed = StateBackend()
    routed.write("/data/file.txt", "content")

    composite = AsyncCompositeBackend(default=StateBackend(), routes={"/data/": routed})
    results = await composite.glob_info("*.txt", "/data/")
    paths = [r["path"] for r in results]
    assert "/data/file.txt" in paths


async def test_async_composite_grep_raw_specific_path() -> None:
    routed = StateBackend()
    routed.write("/lib/code.py", "target = 42")

    composite = AsyncCompositeBackend(default=StateBackend(), routes={"/lib/": routed})
    results = await composite.grep_raw("target", "/lib/")
    assert isinstance(results, list)
    assert any(r["path"] == "/lib/code.py" for r in results)


async def test_async_composite_ls_info_routed_path() -> None:
    routed = StateBackend()
    routed.write("/data/file.txt", "content")

    composite = AsyncCompositeBackend(default=StateBackend(), routes={"/data/": routed})
    entries = await composite.ls_info("/data/")
    paths = [e["path"] for e in entries]
    assert "/data/file.txt" in paths


async def test_async_composite_ls_info_root_empty_prefix() -> None:
    composite = AsyncCompositeBackend(default=StateBackend(), routes={"/": StateBackend()})
    entries = await composite.ls_info("/")
    assert isinstance(entries, list)


async def test_async_composite_ls_info_root_duplicate_dir() -> None:
    default = StateBackend()
    default.write("/data/existing.txt", "x")

    composite = AsyncCompositeBackend(default=default, routes={"/data/": StateBackend()})
    entries = await composite.ls_info("/")
    dir_entries = [e for e in entries if e["path"] == "/data"]
    assert len(dir_entries) == 1


async def test_ensure_async_on_composite_backend() -> None:
    from pydantic_ai_backends import CompositeBackend

    sync_composite = CompositeBackend(
        default=StateBackend(),
        routes={"/tmp/": StateBackend()},
    )
    sync_composite.write("/tmp/file.txt", "data")

    async_adapter = ensure_async(sync_composite)
    content = await async_adapter.read("/tmp/file.txt")
    assert "data" in content
