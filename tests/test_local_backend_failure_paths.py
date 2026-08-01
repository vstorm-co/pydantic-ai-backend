"""Failure and denial paths of `LocalBackend`.

Every one of these was behind a `# pragma: no cover`, so the handlers that turn a
denied path or a filesystem error into a reportable result were never exercised —
in the backend most users touch, and mostly on its permission boundary.

`_resolve` raises `PermissionError` for anything outside the allowed directories,
so an absolute path outside `root_dir` is what reaches each operation's handler.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pydantic_ai_backends import LocalBackend


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "kept.txt").write_text("one\ntwo\nthree\n")
    return root


@pytest.fixture
def backend(workspace: Path) -> LocalBackend:
    return LocalBackend(root_dir=str(workspace))


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A real file the backend is not allowed to reach."""
    target = tmp_path / "outside.txt"
    target.write_text("secret\n")
    return target


class TestDeniedPathsAreReportedNotRaised:
    """A path outside the root must degrade the way the protocol documents."""

    def test_exists_is_false(self, backend: LocalBackend, outside: Path):
        assert backend.exists(str(outside)) is False

    def test_ls_info_is_empty(self, backend: LocalBackend, tmp_path: Path):
        assert backend.ls_info(str(tmp_path)) == []

    def test_read_is_an_error_string(self, backend: LocalBackend, outside: Path):
        assert "outside allowed directories" in backend.read(str(outside))

    def test_read_bytes_is_empty_never_a_message(self, backend: LocalBackend, outside: Path):
        assert backend.read_bytes(str(outside)) == b""

    def test_write_reports_an_error(self, backend: LocalBackend, tmp_path: Path):
        result = backend.write(str(tmp_path / "nope.txt"), "x")

        assert result.error is not None
        assert result.path is None

    def test_edit_reports_an_error(self, backend: LocalBackend, outside: Path):
        result = backend.edit(str(outside), "secret", "public")

        assert result.error is not None
        assert outside.read_text() == "secret\n"

    def test_glob_info_is_empty(self, backend: LocalBackend, tmp_path: Path):
        assert backend.glob_info("*.txt", str(tmp_path)) == []

    def test_grep_raw_reports_or_finds_nothing(self, backend: LocalBackend, tmp_path: Path):
        found = backend.grep_raw("secret", path=str(tmp_path))

        assert found == [] or isinstance(found, str)


class TestListingEdgeCases:
    def test_listing_a_file_returns_just_that_file(self, backend: LocalBackend, workspace: Path):
        rows = backend.ls_info(str(workspace / "kept.txt"))

        assert [row["name"] for row in rows] == ["kept.txt"]
        assert rows[0]["is_dir"] is False

    def test_a_missing_directory_is_empty(self, backend: LocalBackend, workspace: Path):
        assert backend.ls_info(str(workspace / "gone")) == []

    def test_an_entry_that_cannot_be_resolved_is_skipped(
        self, backend: LocalBackend, workspace: Path
    ):
        """A symlink out of the workspace must not be enumerated."""
        (workspace / "escape").symlink_to(workspace.parent)

        names = [row["name"] for row in backend.ls_info(str(workspace))]

        assert "escape" not in names
        assert "kept.txt" in names

    def test_an_unreadable_directory_is_empty(self, backend: LocalBackend, workspace: Path):
        locked = workspace / "locked"
        locked.mkdir()
        os.chmod(locked, 0o000)
        try:
            assert backend.ls_info(str(locked)) == []
        finally:
            os.chmod(locked, 0o755)


class TestReadEdgeCases:
    def test_reading_a_directory_says_so(self, backend: LocalBackend, workspace: Path):
        assert "is a directory" in backend.read(str(workspace))

    def test_an_offset_past_the_end_says_how_long_the_file_is(
        self, backend: LocalBackend, workspace: Path
    ):
        out = backend.read("kept.txt", offset=99)

        assert "exceeds file length" in out
        assert "3 lines" in out

    def test_an_unreadable_file_is_reported(self, backend: LocalBackend, workspace: Path):
        locked = workspace / "locked.txt"
        locked.write_text("x")
        os.chmod(locked, 0o000)
        try:
            assert "Permission denied" in backend.read("locked.txt")
        finally:
            os.chmod(locked, 0o644)

    def test_an_os_error_is_reported(self, backend: LocalBackend, monkeypatch):
        def explode(*args, **kwargs):
            raise OSError("input/output error")

        monkeypatch.setattr("builtins.open", explode)

        assert "input/output error" in backend.read("kept.txt")


class TestWriteAndEditFailures:
    def test_writing_over_a_directory_is_reported(self, backend: LocalBackend, workspace: Path):
        (workspace / "adir").mkdir()

        result = backend.write("adir", "x")

        assert result.error is not None

    def test_an_os_error_on_write_is_reported(
        self, backend: LocalBackend, workspace: Path, monkeypatch
    ):
        def explode(self, *args, **kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(Path, "write_text", explode)

        assert "no space left" in str(backend.write("new.txt", "x").error)

    def test_editing_a_missing_file_is_reported(self, backend: LocalBackend):
        assert "not found" in str(backend.edit("gone.txt", "a", "b").error)

    def test_an_unreadable_file_cannot_be_edited(self, backend: LocalBackend, workspace: Path):
        locked = workspace / "locked.txt"
        locked.write_text("a")
        os.chmod(locked, 0o000)
        try:
            assert "Permission denied" in str(backend.edit("locked.txt", "a", "b").error)
        finally:
            os.chmod(locked, 0o644)

    def test_an_os_error_on_edit_is_reported(
        self, backend: LocalBackend, workspace: Path, monkeypatch
    ):
        real_read_text = Path.read_text

        def explode(self, *args, **kwargs):
            if self.name == "kept.txt":
                raise OSError("stale file handle")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", explode)

        assert "stale file handle" in str(backend.edit("kept.txt", "one", "1").error)


class TestConstruction:
    def test_without_a_root_it_uses_the_working_directory(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        backend = LocalBackend()

        assert backend.root_dir == tmp_path.resolve()


class TestReadBytesFailures:
    def test_an_os_error_is_empty_never_a_message(
        self, backend: LocalBackend, workspace: Path, monkeypatch
    ):
        """A caller cannot tell an error message from real file content."""

        def explode(self, *args, **kwargs):
            raise OSError("stale file handle")

        monkeypatch.setattr(Path, "read_bytes", explode)

        assert backend.read_bytes("kept.txt") == b""


class TestGlobFailures:
    def test_one_bad_entry_does_not_truncate_the_answer(
        self, backend: LocalBackend, workspace: Path, monkeypatch
    ):
        """It used to abort the whole walk, returning a silently short listing."""
        for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
            (workspace / name).write_text("x")
        real_stat = Path.stat

        def flaky(self, *args, **kwargs):
            if self.name == "b.txt":
                raise OSError("stale file handle")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky)

        found = {row["name"] for row in backend.glob_info("*.txt")}

        assert found == {"a.txt", "c.txt", "d.txt", "kept.txt"}

    def test_an_entry_denied_by_permissions_is_skipped(
        self, backend: LocalBackend, workspace: Path, monkeypatch
    ):
        (workspace / "a.txt").write_text("x")
        real_stat = Path.stat

        def refuse(self, *args, **kwargs):
            if self.name == "a.txt":
                raise PermissionError("denied")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", refuse)

        assert "a.txt" not in {row["name"] for row in backend.glob_info("*.txt")}

    def test_a_failing_walk_returns_what_it_had(
        self, backend: LocalBackend, workspace: Path, monkeypatch
    ):
        def explode(self, pattern):
            raise OSError("directory vanished")
            yield  # pragma: no cover - makes this a generator

        monkeypatch.setattr(Path, "glob", explode)

        assert backend.glob_info("*.txt") == []


class TestGrepBothImplementations:
    """`shutil.which("rg")` picks the path, so both are forced explicitly."""

    @pytest.fixture
    def with_ripgrep(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/rg")

    @pytest.fixture
    def without_ripgrep(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)

    def _canned_rg(self, monkeypatch, stdout: str):
        class Result:
            def __init__(self) -> None:
                self.stdout = stdout

        recorded: dict[str, list[str]] = {}

        def run(argv, **kwargs):
            recorded["argv"] = argv
            return Result()

        monkeypatch.setattr("subprocess.run", run)
        return recorded

    def test_ripgrep_receives_the_glob_and_hidden_flags(
        self, backend: LocalBackend, with_ripgrep, monkeypatch
    ):
        recorded = self._canned_rg(monkeypatch, "")

        backend.grep_raw("todo", glob="*.py", ignore_hidden=False)

        assert "--glob" in recorded["argv"]
        assert "*.py" in recorded["argv"]
        assert "--hidden" in recorded["argv"]

    def test_a_ripgrep_timeout_is_reported(self, backend: LocalBackend, with_ripgrep, monkeypatch):
        import subprocess

        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)

        monkeypatch.setattr("subprocess.run", timeout)

        assert backend.grep_raw("todo") == "Error: Search timed out"

    def test_ripgrep_missing_at_exec_time_is_reported(
        self, backend: LocalBackend, with_ripgrep, monkeypatch
    ):
        def explode(argv, **kwargs):
            raise OSError("rg vanished")

        monkeypatch.setattr("subprocess.run", explode)

        assert "rg vanished" in str(backend.grep_raw("todo"))

    def test_ripgrep_hits_are_parsed(
        self, backend: LocalBackend, workspace: Path, with_ripgrep, monkeypatch
    ):
        (workspace / "a.py").write_text("todo one\n")
        self._canned_rg(monkeypatch, "a.py:1:todo one\n")

        found = backend.grep_raw("todo")

        assert isinstance(found, list)
        assert found[0]["line_number"] == 1
        assert found[0]["line"] == "todo one"

    def test_a_ripgrep_hit_outside_the_workspace_is_skipped(
        self, backend: LocalBackend, tmp_path: Path, with_ripgrep, monkeypatch
    ):
        """rg is run with cwd inside, but a symlinked hit could still escape."""
        self._canned_rg(monkeypatch, "../outside.txt:1:secret\n")

        assert backend.grep_raw("secret") == []

    def test_ripgrep_respects_a_file_hidden_from_grep(
        self, workspace: Path, with_ripgrep, monkeypatch
    ):
        """Covered only by accident wherever `rg` happens to be installed."""
        (workspace / "visible.txt").write_text("todo yes\n")
        (workspace / "secret.txt").write_text("todo no\n")
        self._canned_rg(monkeypatch, "visible.txt:1:todo yes\nsecret.txt:1:todo no\n")

        backend = LocalBackend(root_dir=str(workspace))
        monkeypatch.setattr(
            backend, "_hidden_from_grep", lambda path: Path(path).name == "secret.txt"
        )

        found = backend.grep_raw("todo")

        assert isinstance(found, list)
        assert [Path(m["path"]).name for m in found] == ["visible.txt"]

    def test_an_invalid_regex_is_reported(
        self, backend: LocalBackend, without_ripgrep, monkeypatch
    ):
        assert "Invalid regex" in str(backend.grep_raw("(unclosed"))

    def test_a_missing_search_path_is_reported(self, backend: LocalBackend, without_ripgrep):
        assert "not found" in str(backend.grep_raw("todo", path="gone"))

    def test_a_single_file_is_searched(
        self, backend: LocalBackend, workspace: Path, without_ripgrep
    ):
        (workspace / "one.txt").write_text("todo here\n")

        found = backend.grep_raw("todo", path="one.txt")

        assert isinstance(found, list)
        assert found[0]["line_number"] == 1

    def test_an_unreadable_file_is_skipped(
        self, backend: LocalBackend, workspace: Path, without_ripgrep
    ):
        (workspace / "readable.txt").write_text("todo yes\n")
        locked = workspace / "locked.txt"
        locked.write_text("todo no\n")
        os.chmod(locked, 0o000)
        try:
            found = backend.grep_raw("todo")
            assert isinstance(found, list)
            assert [Path(m["path"]).name for m in found] == ["readable.txt"]
        finally:
            os.chmod(locked, 0o644)

    def test_a_directory_entry_is_not_opened(
        self, backend: LocalBackend, workspace: Path, without_ripgrep
    ):
        (workspace / "sub").mkdir()
        (workspace / "sub" / "deep.txt").write_text("todo deep\n")

        found = backend.grep_raw("todo")

        assert isinstance(found, list)
        assert [Path(m["path"]).name for m in found] == ["deep.txt"]


class TestExecuteFailures:
    def test_an_unexpected_failure_is_reported(self, backend: LocalBackend, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("fork failed")

        monkeypatch.setattr("subprocess.run", explode)

        result = backend.execute("echo hi")

        assert result.exit_code == 1
        assert "fork failed" in result.output


class TestPermissionDeniedByTheFilesystem:
    """`PermissionError` from the OS, distinct from a denied *path*."""

    def test_write_reports_it(self, backend: LocalBackend, monkeypatch):
        def refuse(self, *args, **kwargs):
            raise PermissionError("read-only file system")

        monkeypatch.setattr(Path, "write_text", refuse)

        assert "Permission denied" in str(backend.write("new.txt", "x").error)

    def test_edit_reports_it_on_the_write_back(
        self, backend: LocalBackend, workspace: Path, monkeypatch
    ):
        def refuse(self, *args, **kwargs):
            raise PermissionError("read-only file system")

        monkeypatch.setattr(Path, "write_text", refuse)

        assert "Permission denied" in str(backend.edit("kept.txt", "one", "1").error)

    def test_edit_reports_an_os_error_on_the_write_back(
        self, backend: LocalBackend, workspace: Path, monkeypatch
    ):
        def explode(self, *args, **kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(Path, "write_text", explode)

        assert "no space left" in str(backend.edit("kept.txt", "one", "1").error)


class TestGrepHidesWhatThePermissionsHide:
    def test_a_file_hidden_from_grep_is_not_matched(self, workspace: Path, monkeypatch):
        """The ruleset can hide a path from search without denying reads."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        (workspace / "visible.txt").write_text("todo yes\n")
        (workspace / "secret.txt").write_text("todo no\n")

        backend = LocalBackend(root_dir=str(workspace))
        monkeypatch.setattr(
            backend, "_hidden_from_grep", lambda path: Path(path).name == "secret.txt"
        )

        found = backend.grep_raw("todo")

        assert isinstance(found, list)
        assert [Path(m["path"]).name for m in found] == ["visible.txt"]

    def test_a_file_outside_the_workspace_is_not_matched(self, workspace: Path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        (workspace / "inside.txt").write_text("todo yes\n")
        backend = LocalBackend(root_dir=str(workspace))
        real_resolve = backend._resolve

        def refuse(path: str):
            if Path(path).name == "inside.txt":
                raise PermissionError("outside allowed directories")
            return real_resolve(path)

        monkeypatch.setattr(backend, "_resolve", refuse)

        assert backend.grep_raw("todo") == []


class TestOutputTruncation:
    def test_a_huge_command_output_is_capped_and_flagged(self, backend: LocalBackend, monkeypatch):
        from pydantic_ai_backends._limits import MAX_EXECUTE_OUTPUT_BYTES

        class Result:
            stdout = "x" * (MAX_EXECUTE_OUTPUT_BYTES + 100)
            stderr = ""
            returncode = 0

        monkeypatch.setattr("subprocess.run", lambda *a, **k: Result())

        result = backend.execute("cat big")

        assert result.truncated is True
        assert len(result.output) == MAX_EXECUTE_OUTPUT_BYTES


class TestRipgrepOutputParsing:
    def test_a_malformed_row_is_skipped(self):
        from pydantic_ai_backends.backends.local import _parse_grep_lines

        rows = _parse_grep_lines("a.py:notanumber:x\nno colons\nb.py:2:kept\n")

        assert rows == [("b.py", 2, "kept")]


class TestBackgroundExecuteFailures:
    async def test_an_unexpected_failure_is_reported(self, backend: LocalBackend, monkeypatch):
        async def explode(*args, **kwargs):
            raise RuntimeError("cannot spawn")

        monkeypatch.setattr("asyncio.create_subprocess_exec", explode)

        result = await backend.async_execute("echo hi")

        assert result.exit_code == 1
        assert "cannot spawn" in result.output
