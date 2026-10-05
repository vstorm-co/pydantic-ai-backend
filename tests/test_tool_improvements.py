"""Tests for read/glob/grep/edit behavioral improvements."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai_backends import create_console_toolset
from pydantic_ai_backends.toolsets._tracking import (
    fingerprint,
    read_fingerprints,
    record_read,
    staleness_error,
)
from pydantic_ai_backends.toolsets._workspace import WorkspaceOps
from tests.support import ctx, local


class TestEditStalenessUnit:
    def test_fingerprint_distinguishes_content(self) -> None:
        assert fingerprint(b"a") == fingerprint(b"a")
        assert fingerprint(b"a") != fingerprint(b"b")

    async def test_never_read_is_not_stale(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("hi")
        ops = WorkspaceOps(local(tmp_path))
        # No recorded read → not our concern → no error.
        assert await staleness_error(ops, ops, "f.txt") is None

    async def test_unchanged_after_read_is_ok(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("hello")
        ops = WorkspaceOps(local(tmp_path))
        record_read(ops, "f.txt", b"hello")
        assert await staleness_error(ops, ops, "f.txt") is None

    async def test_changed_after_read_errors(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("hello")
        ops = WorkspaceOps(local(tmp_path))
        record_read(ops, "f.txt", b"hello")
        (tmp_path / "f.txt").write_text("hello world")  # external change
        err = await staleness_error(ops, ops, "f.txt")
        assert err is not None and "changed since you last read it" in err


class TestEditStalenessIntegration:
    async def test_read_then_edit_succeeds(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("alpha beta")
        ts = create_console_toolset()
        context = ctx(local(tmp_path))
        await ts.tools["read_file"].function(context, "f.txt")
        out = await ts.tools["edit_file"].function(context, "f.txt", "alpha", "ALPHA")
        assert "Edited" in out
        assert (tmp_path / "f.txt").read_text().endswith("ALPHA beta")

    async def test_edit_after_external_change_blocks(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("alpha beta")
        ts = create_console_toolset()
        context = ctx(local(tmp_path))
        await ts.tools["read_file"].function(context, "f.txt")
        (tmp_path / "f.txt").write_text("alpha beta gamma")  # changed behind the agent's back
        out = await ts.tools["edit_file"].function(context, "f.txt", "alpha", "ALPHA")
        assert "changed since you last read it" in out
        # The stale edit did not apply.
        assert "ALPHA" not in (tmp_path / "f.txt").read_text()

    async def test_edit_without_read_is_allowed(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("alpha beta")
        ts = create_console_toolset()
        out = await ts.tools["edit_file"].function(ctx(local(tmp_path)), "f.txt", "alpha", "ALPHA")
        assert "Edited" in out  # never read → not blocked

    async def test_write_then_edit_succeeds(self, tmp_path: Path) -> None:
        ts = create_console_toolset()
        context = ctx(local(tmp_path))
        await ts.tools["write_file"].function(context, "n.txt", "one two three")
        out = await ts.tools["edit_file"].function(context, "n.txt", "two", "TWO")
        assert "Edited" in out

    async def test_consecutive_edits_after_read(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("a b c")
        ts = create_console_toolset()
        context = ctx(local(tmp_path))
        await ts.tools["read_file"].function(context, "f.txt")
        assert "Edited" in await ts.tools["edit_file"].function(context, "f.txt", "a", "A")
        # Second edit works without a re-read (fingerprint re-recorded post-edit).
        assert "Edited" in await ts.tools["edit_file"].function(context, "f.txt", "b", "B")
        assert (tmp_path / "f.txt").read_text().endswith("A B c")

    def teardown_method(self) -> None:
        read_fingerprints.clear()


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
        from pydantic_ai_backends.toolsets._content import downscale_image

        data = _png_bytes(100, 80)
        assert downscale_image(data, max_dim=1568) == data

    def test_large_image_downscaled(self) -> None:
        from pydantic_ai_backends.toolsets._content import downscale_image

        data = _png_bytes(3000, 2000)
        out = downscale_image(data, max_dim=1568)
        assert max(_dims(out)) <= 1568
        assert out != data

    async def test_read_file_tool_downscales(self, tmp_path: Path) -> None:
        from pydantic_ai.messages import BinaryContent

        (tmp_path / "big.png").write_bytes(_png_bytes(4000, 2500))
        ts = create_console_toolset(image_support=True)
        out = await ts.tools["read_file"].function(ctx(local(tmp_path)), "big.png")
        assert isinstance(out, BinaryContent)
        assert max(_dims(out.data)) <= 1568


class TestEditLock:
    def test_same_path_shares_a_lock_and_paths_do_not(self) -> None:
        from pydantic_ai_backends.toolsets._tracking import edit_lock

        backend = WorkspaceOps(local(Path("/tmp")))
        other = WorkspaceOps(local(Path("/tmp")))

        assert edit_lock(backend, "a.txt") is edit_lock(backend, "a.txt")
        assert edit_lock(backend, "a.txt") is not edit_lock(backend, "b.txt")
        assert edit_lock(backend, "a.txt") is not edit_lock(other, "a.txt")
