"""Every backend has to agree on how the root of its workspace is spelled.

The console toolset's `ls` and `glob` default to `path="."`. `normalize_path`
prefixed a slash without resolving the segment, so `"."` became `"/."` — a
directory no file is ever stored under. `StateBackend` and `CompositeBackend`
therefore answered every default listing, glob and grep with nothing at all, and
an agent was told its workspace was empty. There was no error anywhere.

Parametrised over the spellings and the backends because the bug was drift
between them, not a mistake in any one: the root was `"/"` in the protocol,
`"."` in `LocalBackend.glob_info`, and `("/", "")` in the composite's fan-out.
"""

from __future__ import annotations

import pytest

from pydantic_ai_backends import CompositeBackend, LocalBackend, StateBackend
from pydantic_ai_backends._paths import normalize_path

ROOT_SPELLINGS = ["/", "", ".", "./", "/."]


def _state() -> StateBackend:
    backend = StateBackend()
    backend.write("/a.py", "x = 1")
    backend.write("/pkg/b.py", "y = 2")
    return backend


def _composite() -> CompositeBackend:
    backend = CompositeBackend(default=StateBackend(), routes={"/scratch/": StateBackend()})
    backend.write("/a.py", "x = 1")
    backend.write("/scratch/b.py", "y = 2")
    return backend


class TestNormalizePath:
    @pytest.mark.parametrize("spelling", ROOT_SPELLINGS)
    def test_every_spelling_of_the_root_normalizes_to_it(self, spelling: str):
        assert normalize_path(spelling) == "/"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("./notes.md", "/notes.md"),
            ("notes.md", "/notes.md"),
            ("/a/./b", "/a/b"),
            ("/a//b", "/a/b"),
            ("/a/b/", "/a/b"),
        ],
    )
    def test_no_op_segments_are_dropped(self, raw: str, expected: str):
        assert normalize_path(raw) == expected

    def test_dotdot_is_left_alone_for_unsafe_path_reason_to_reject(self):
        """Resolving it here would turn a path the caller must be told about
        into a valid one."""
        assert ".." in normalize_path("/a/../b")


class TestStateBackendRootSpellings:
    @pytest.mark.parametrize("spelling", ROOT_SPELLINGS)
    def test_ls(self, spelling: str):
        assert [row["name"] for row in _state().ls_info(spelling)] == ["pkg", "a.py"]

    @pytest.mark.parametrize("spelling", ROOT_SPELLINGS)
    def test_glob(self, spelling: str):
        found = _state().glob_info("**/*.py", spelling)

        assert [row["path"] for row in found] == ["/a.py", "/pkg/b.py"]

    @pytest.mark.parametrize("spelling", ROOT_SPELLINGS)
    def test_grep(self, spelling: str):
        found = _state().grep_raw("=", spelling)

        assert [match["path"] for match in found] == ["/a.py", "/pkg/b.py"]


class TestCompositeBackendRootSpellings:
    @pytest.mark.parametrize("spelling", ROOT_SPELLINGS)
    def test_glob_fans_out_to_every_route(self, spelling: str):
        found = _composite().glob_info("**/*.py", spelling)

        assert [row["path"] for row in found] == ["/a.py", "/scratch/b.py"]

    @pytest.mark.parametrize("spelling", ROOT_SPELLINGS)
    def test_ls_shows_the_route_mount_points(self, spelling: str):
        assert [row["name"] for row in _composite().ls_info(spelling)] == ["scratch", "a.py"]

    @pytest.mark.parametrize("spelling", ROOT_SPELLINGS)
    def test_grep_covers_every_route(self, spelling: str):
        found = _composite().grep_raw("=", spelling)

        assert [match["path"] for match in found] == ["/a.py", "/scratch/b.py"]


class TestLocalBackendRootSpellings:
    """`LocalBackend` addresses the real filesystem, so `"/"` is the host root
    rather than the workspace and is correctly refused. The relative spellings
    have to work, since they are what the toolset sends."""

    @pytest.mark.parametrize("spelling", [".", "./", ""])
    def test_glob(self, spelling: str, tmp_path):
        (tmp_path / "a.py").write_text("x = 1")
        backend = LocalBackend(root_dir=tmp_path)

        assert [row["name"] for row in backend.glob_info("*.py", spelling)] == ["a.py"]

    @pytest.mark.parametrize("spelling", [".", "./", ""])
    def test_ls(self, spelling: str, tmp_path):
        (tmp_path / "a.py").write_text("x = 1")
        backend = LocalBackend(root_dir=tmp_path)

        assert [row["name"] for row in backend.ls_info(spelling)] == ["a.py"]
