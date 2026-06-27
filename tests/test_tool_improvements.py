"""Tests for read/glob/grep/edit behavioral improvements."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from pydantic_ai_backends import LocalBackend, create_console_toolset, ensure_async
from pydantic_ai_backends.backends.local import MAX_READ_OUTPUT
from pydantic_ai_backends.toolsets.console import (
    _edit_staleness_error,
    _fingerprint,
    _read_fingerprints,
    _record_read,
)


@dataclass
class _Deps:
    backend: LocalBackend


def _ctx(backend: LocalBackend) -> RunContext[_Deps]:
    return RunContext(deps=_Deps(backend=backend), model=TestModel(), usage=RunUsage())


class TestGlobMtimeSort:
    def test_newest_first(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("old.py", "# old")
        backend.write("mid.py", "# mid")
        backend.write("new.py", "# new")
        # Stamp distinct mtimes (oldest → newest).
        os.utime(tmp_path / "old.py", (1000, 1000))
        os.utime(tmp_path / "mid.py", (2000, 2000))
        os.utime(tmp_path / "new.py", (3000, 3000))

        names = [e["name"] for e in backend.glob_info("*.py")]
        assert names == ["new.py", "mid.py", "old.py"]

    def test_same_mtime_ties_break_by_path(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("b.py", "")
        backend.write("a.py", "")
        for n in ("a.py", "b.py"):
            os.utime(tmp_path / n, (5000, 5000))
        names = [e["name"] for e in backend.glob_info("*.py")]
        assert names == ["a.py", "b.py"]  # stable, path-ordered on tie


class TestReadCeiling:
    def test_normal_read_unaffected(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("small.txt", "line1\nline2\nline3\n")
        out = backend.read("small.txt")
        assert "line1" in out and "Error" not in out

    def test_default_read_truncates_when_huge(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=tmp_path)
        # One enormous line so a default read blows past the ceiling.
        backend.write("huge.txt", "x" * (MAX_READ_OUTPUT + 5000))
        out = backend.read("huge.txt")  # default: no explicit range
        assert not out.startswith("Error")
        assert "truncated at" in out
        assert len(out) <= MAX_READ_OUTPUT + 200  # ceiling + the notice

    def test_explicit_range_too_large_errors(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("huge.txt", "x" * (MAX_READ_OUTPUT + 5000))
        out = backend.read("huge.txt", offset=0, limit=5)  # explicit limit
        assert out.startswith("Error")
        assert "too large" in out

    def test_explicit_small_range_ok(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("multi.txt", "\n".join(f"line{i}" for i in range(100)))
        out = backend.read("multi.txt", offset=10, limit=5)
        assert "line10" in out and "Error" not in out


class TestEditStalenessUnit:
    def test_fingerprint_distinguishes_content(self) -> None:
        assert _fingerprint(b"a") == _fingerprint(b"a")
        assert _fingerprint(b"a") != _fingerprint(b"b")

    async def test_never_read_is_not_stale(self, tmp_path: Path) -> None:
        be = LocalBackend(root_dir=tmp_path)
        be.write("f.txt", "hi")
        # No recorded read → not our concern → no error.
        assert await _edit_staleness_error(ensure_async(be), be, "f.txt") is None

    async def test_unchanged_after_read_is_ok(self, tmp_path: Path) -> None:
        be = LocalBackend(root_dir=tmp_path)
        be.write("f.txt", "hello")
        _record_read(be, "f.txt", b"hello")
        assert await _edit_staleness_error(ensure_async(be), be, "f.txt") is None

    async def test_changed_after_read_errors(self, tmp_path: Path) -> None:
        be = LocalBackend(root_dir=tmp_path)
        be.write("f.txt", "hello")
        _record_read(be, "f.txt", b"hello")
        be.write("f.txt", "hello world")  # external change
        err = await _edit_staleness_error(ensure_async(be), be, "f.txt")
        assert err is not None and "changed since you last read it" in err


class TestEditStalenessIntegration:
    async def test_read_then_edit_succeeds(self, tmp_path: Path) -> None:
        be = LocalBackend(root_dir=tmp_path)
        be.write("f.txt", "alpha beta")
        ts = create_console_toolset()
        ctx = _ctx(be)
        await ts.tools["read_file"].function(ctx, "f.txt")
        out = await ts.tools["edit_file"].function(ctx, "f.txt", "alpha", "ALPHA")
        assert "Edited" in out
        assert be.read("f.txt").endswith("ALPHA beta")

    async def test_edit_after_external_change_blocks(self, tmp_path: Path) -> None:
        be = LocalBackend(root_dir=tmp_path)
        be.write("f.txt", "alpha beta")
        ts = create_console_toolset()
        ctx = _ctx(be)
        await ts.tools["read_file"].function(ctx, "f.txt")
        be.write("f.txt", "alpha beta gamma")  # changed behind the agent's back
        out = await ts.tools["edit_file"].function(ctx, "f.txt", "alpha", "ALPHA")
        assert "changed since you last read it" in out
        # The stale edit did not apply.
        assert "ALPHA" not in be.read("f.txt")

    async def test_edit_without_read_is_allowed(self, tmp_path: Path) -> None:
        be = LocalBackend(root_dir=tmp_path)
        be.write("f.txt", "alpha beta")
        ts = create_console_toolset()
        out = await ts.tools["edit_file"].function(_ctx(be), "f.txt", "alpha", "ALPHA")
        assert "Edited" in out  # never read → not blocked

    async def test_write_then_edit_succeeds(self, tmp_path: Path) -> None:
        be = LocalBackend(root_dir=tmp_path)
        ts = create_console_toolset()
        ctx = _ctx(be)
        await ts.tools["write_file"].function(ctx, "n.txt", "one two three")
        out = await ts.tools["edit_file"].function(ctx, "n.txt", "two", "TWO")
        assert "Edited" in out

    async def test_consecutive_edits_after_read(self, tmp_path: Path) -> None:
        be = LocalBackend(root_dir=tmp_path)
        be.write("f.txt", "a b c")
        ts = create_console_toolset()
        ctx = _ctx(be)
        await ts.tools["read_file"].function(ctx, "f.txt")
        assert "Edited" in await ts.tools["edit_file"].function(ctx, "f.txt", "a", "A")
        # Second edit works without a re-read (fingerprint re-recorded post-edit).
        assert "Edited" in await ts.tools["edit_file"].function(ctx, "f.txt", "b", "B")
        assert be.read("f.txt").endswith("A B c")

    def teardown_method(self) -> None:
        _read_fingerprints.clear()


def _png_bytes(w: int, h: int) -> bytes:
    import io

    from PIL import Image

    img = Image.new("RGB", (w, h), (120, 60, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _dims(data: bytes) -> tuple[int, int]:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return img.size


class TestImageDownscale:
    def test_small_image_unchanged(self) -> None:
        from pydantic_ai_backends.toolsets.console import _downscale_image

        data = _png_bytes(100, 80)
        assert _downscale_image(data, max_dim=1568) == data

    def test_large_image_downscaled(self) -> None:
        from pydantic_ai_backends.toolsets.console import _downscale_image

        data = _png_bytes(3000, 2000)
        out = _downscale_image(data, max_dim=1568)
        assert max(_dims(out)) <= 1568
        assert out != data

    async def test_read_file_tool_downscales(self, tmp_path: Path) -> None:
        from pydantic_ai.messages import BinaryContent

        be = LocalBackend(root_dir=tmp_path)
        (tmp_path / "big.png").write_bytes(_png_bytes(4000, 2500))
        ts = create_console_toolset(image_support=True)
        out = await ts.tools["read_file"].function(_ctx(be), "big.png")
        assert isinstance(out, BinaryContent)
        assert max(_dims(out.data)) <= 1568


class TestGrepIgnoreDirs:
    def test_path_ignored_helper(self) -> None:
        from pydantic_ai_backends.backends.local import _grep_path_ignored

        assert _grep_path_ignored(("src", "node_modules", "x.js"), True) is True
        assert _grep_path_ignored(("src", "app.py"), True) is False
        assert _grep_path_ignored(("src", ".env"), True) is True  # hidden + ignore
        assert _grep_path_ignored(("src", ".env"), False) is False  # ignore off → keep
        assert _grep_path_ignored(("__pycache__", "x"), False) is False  # ignore off → keep
        assert _grep_path_ignored(("__pycache__", "x"), True) is True  # junk skipped

    def test_python_grep_skips_build_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pydantic_ai_backends.backends.local as local_mod

        # Force the Python fallback (pretend ripgrep isn't installed).
        monkeypatch.setattr(local_mod.shutil, "which", lambda _: None)

        be = LocalBackend(root_dir=tmp_path)
        be.write("src/app.py", "needle here")
        be.write("node_modules/pkg/index.js", "needle here too")
        be.write("__pycache__/cached.py", "needle cached")

        matches = be.grep_raw("needle")
        assert isinstance(matches, list)
        paths = [m["path"] for m in matches]
        assert any("app.py" in p for p in paths)
        assert not any("node_modules" in p for p in paths)
        assert not any("__pycache__" in p for p in paths)
