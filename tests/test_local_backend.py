"""Tests for LocalBackend."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pydantic_ai_backends import LocalBackend


class TestLocalBackendInit:
    """Test LocalBackend initialization."""

    def test_init_with_root_dir(self, tmp_path: Path):
        """Test initialization with root_dir."""
        backend = LocalBackend(root_dir=tmp_path)
        assert backend.root_dir == tmp_path
        assert backend.execute_enabled is True

    def test_init_with_allowed_directories(self, tmp_path: Path):
        """Test initialization with allowed_directories."""
        dir1 = tmp_path / "project"
        dir2 = tmp_path / "libs"

        backend = LocalBackend(allowed_directories=[str(dir1), str(dir2)])

        # Directories should be created
        assert dir1.exists()
        assert dir2.exists()
        # root_dir should default to first allowed directory
        assert backend.root_dir == dir1

    def test_init_with_explicit_root_dir_and_allowed(self, tmp_path: Path):
        """Test that explicit root_dir takes precedence."""
        dir1 = tmp_path / "project"
        work = tmp_path / "work"
        work.mkdir()

        backend = LocalBackend(
            root_dir=work,
            allowed_directories=[str(dir1)],
        )

        assert backend.root_dir == work

    def test_init_with_execute_disabled(self, tmp_path: Path):
        """Test initialization with execute disabled."""
        backend = LocalBackend(root_dir=tmp_path, enable_execute=False)
        assert backend.execute_enabled is False

    def test_init_creates_sandbox_id(self, tmp_path: Path):
        """Test that sandbox_id is generated."""
        backend = LocalBackend(root_dir=tmp_path)
        assert backend.id is not None
        assert len(backend.id) > 0

    def test_init_with_custom_sandbox_id(self, tmp_path: Path):
        """Test initialization with custom sandbox_id."""
        backend = LocalBackend(root_dir=tmp_path, sandbox_id="my-sandbox")
        assert backend.id == "my-sandbox"


class TestLocalBackendFileOps:
    """Test LocalBackend file operations."""

    def test_write_and_read(self, tmp_path: Path):
        """Test writing and reading files."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.write("test.txt", "hello world")
        assert result.error is None

        content = backend.read("test.txt")
        assert "hello world" in content
        assert "1\t" in content  # Line number

    def test_write_creates_directories(self, tmp_path: Path):
        """Test that write creates parent directories."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.write("deep/nested/file.txt", "content")
        assert result.error is None
        assert (tmp_path / "deep" / "nested" / "file.txt").exists()

    def test_read_nonexistent_file(self, tmp_path: Path):
        """Test reading a nonexistent file."""
        backend = LocalBackend(root_dir=tmp_path)

        content = backend.read("nonexistent.txt")
        assert "Error" in content
        assert "not found" in content

    def test_edit_file(self, tmp_path: Path):
        """Test editing a file."""
        backend = LocalBackend(root_dir=tmp_path)

        backend.write("test.txt", "hello world")
        result = backend.edit("test.txt", "hello", "goodbye")

        assert result.error is None
        assert result.occurrences == 1

        content = backend.read("test.txt")
        assert "goodbye world" in content

    def test_edit_file_not_found(self, tmp_path: Path):
        """Test editing a nonexistent file."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.edit("nonexistent.txt", "old", "new")
        assert result.error is not None
        assert "not found" in result.error

    @pytest.mark.parametrize(
        "content",
        [
            "line1\nline2\nline3\n",
            "line1\r\nline2\r\nline3\r\n",
            "line1\r\r\nline2\r\r\nline3\r\r\n",
        ],
    )
    def test_write_does_not_double_carriage_returns(self, tmp_path: Path, content: str):
        """Write must not accumulate \\r from text-mode translation (issue #51)."""
        backend = LocalBackend(root_dir=tmp_path)

        backend.write("file.py", content)
        written = (tmp_path / "file.py").read_bytes()

        assert b"\r\r" not in written
        assert written.replace(b"\r\n", b"\n") == b"line1\nline2\nline3\n"

    def test_edit_does_not_double_carriage_returns(self, tmp_path: Path):
        """Edit must normalize line endings like write (issue #51)."""
        backend = LocalBackend(root_dir=tmp_path)

        backend.write("file.py", "alpha\nbeta\n")
        backend.edit("file.py", "beta", "beta\r\ngamma")
        written = (tmp_path / "file.py").read_bytes()

        assert b"\r\r" not in written

    def test_ls_info(self, tmp_path: Path):
        """Test listing directory contents."""
        backend = LocalBackend(root_dir=tmp_path)

        backend.write("file1.txt", "a")
        backend.write("file2.txt", "b")
        (tmp_path / "subdir").mkdir()

        entries = backend.ls_info(".")
        assert len(entries) == 3
        names = [e["name"] for e in entries]
        assert "file1.txt" in names
        assert "file2.txt" in names
        assert "subdir" in names

    def test_glob_info(self, tmp_path: Path):
        """Test finding files with glob pattern."""
        backend = LocalBackend(root_dir=tmp_path)

        backend.write("test1.py", "# python")
        backend.write("test2.py", "# python")
        backend.write("readme.md", "# markdown")

        entries = backend.glob_info("*.py")
        assert len(entries) == 2

    def test_grep_raw(self, tmp_path: Path):
        """Test searching files with grep."""
        backend = LocalBackend(root_dir=tmp_path)

        backend.write("test.txt", "hello world\nfoo bar\nhello again")

        result = backend.grep_raw("hello")
        assert isinstance(result, list)
        assert len(result) == 2

    def test_grep_raw_hidden_files_ignored_by_default(self, tmp_path: Path):
        """Hidden files should be excluded when ignore_hidden is True."""
        backend = LocalBackend(root_dir=tmp_path)

        backend.write(".secret.txt", "hidden content")
        backend.write("visible.txt", "visible content")
        backend.write(".venv/test.txt", "another hidden content")

        matches_default = backend.grep_raw("content")
        matches_explicit = backend.grep_raw("content", ignore_hidden=True)

        for matches in (matches_default, matches_explicit):
            paths = {match["path"] for match in matches}
            assert paths == {str(tmp_path / "visible.txt")}

    def test_grep_raw_hidden_files_included_when_requested(self, tmp_path: Path):
        """Hidden files should be searched when ignore_hidden=False."""
        backend = LocalBackend(root_dir=tmp_path)

        backend.write(".secret.txt", "hidden content")
        backend.write("visible.txt", "visible content")
        backend.write(".venv/test.txt", "another hidden content")
        matches = backend.grep_raw("content", ignore_hidden=False)
        paths = {match["path"] for match in matches}
        assert paths == {
            str(tmp_path / "visible.txt"),
            str(tmp_path / ".secret.txt"),
            str(tmp_path / ".venv/test.txt"),
        }


class TestLocalBackendAllowedDirectories:
    """Test LocalBackend with allowed_directories restriction."""

    def test_read_within_allowed(self, tmp_path: Path):
        """Test read() works within allowed directory."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        test_file = allowed / "test.txt"
        test_file.write_text("hello world")

        backend = LocalBackend(allowed_directories=[str(allowed)])

        result = backend.read(str(test_file))
        assert "hello world" in result

    def test_read_outside_allowed(self, tmp_path: Path):
        """Test read() fails outside allowed directory."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()

        backend = LocalBackend(allowed_directories=[str(allowed)])

        result = backend.read("/etc/passwd")
        assert "Error" in result
        assert "Access denied" in result

    def test_write_within_allowed(self, tmp_path: Path):
        """Test write() works within allowed directory."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()

        backend = LocalBackend(allowed_directories=[str(allowed)])

        result = backend.write(str(allowed / "new_file.txt"), "content")
        assert result.error is None
        assert (allowed / "new_file.txt").read_text() == "content"

    def test_write_outside_allowed(self, tmp_path: Path):
        """Test write() fails outside allowed directory."""
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()

        backend = LocalBackend(allowed_directories=[str(allowed)])

        result = backend.write(str(outside / "file.txt"), "content")
        assert result.error is not None
        assert "Access denied" in result.error

    def test_path_traversal_blocked(self, tmp_path: Path):
        """Test that path traversal attempts are blocked."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()

        backend = LocalBackend(allowed_directories=[str(allowed)])

        # Try to escape using ..
        result = backend.read(str(allowed / ".." / "etc" / "passwd"))
        assert "Error" in result


class TestLocalBackendExecute:
    """Test LocalBackend shell execution."""

    def test_execute_basic(self, tmp_path: Path):
        """Test basic command execution."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.execute("echo 'hello'")
        assert result.exit_code == 0
        assert "hello" in result.output

    def test_execute_with_timeout(self, tmp_path: Path):
        """Test command execution with timeout."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.execute("sleep 0.1 && echo done", timeout=10)
        assert result.exit_code == 0
        assert "done" in result.output

    def test_execute_timeout_exceeded(self, tmp_path: Path):
        """Test command execution timeout."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.execute("sleep 10", timeout=1)
        assert result.exit_code == 124
        assert "timed out" in result.output

    def test_execute_disabled_raises(self, tmp_path: Path):
        """Test that execute raises when disabled."""
        backend = LocalBackend(root_dir=tmp_path, enable_execute=False)

        with pytest.raises(RuntimeError) as exc_info:
            backend.execute("echo 'hello'")

        assert "disabled" in str(exc_info.value)

    def test_execute_working_dir(self, tmp_path: Path):
        """Test that execute uses root_dir as working directory."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.execute("pwd")
        assert result.exit_code == 0
        assert str(tmp_path) in result.output


class TestLocalBackendAsyncExecute:
    """Test LocalBackend async shell execution."""

    async def test_async_execute_basic(self, tmp_path: Path):
        """Test basic async command execution."""
        backend = LocalBackend(root_dir=tmp_path)

        result = await backend.async_execute("echo 'hello'")
        assert result.exit_code == 0
        assert "hello" in result.output

    async def test_async_execute_disabled_raises(self, tmp_path: Path):
        """Test that async_execute raises RuntimeError when disabled."""
        backend = LocalBackend(root_dir=tmp_path, enable_execute=False)

        with pytest.raises(RuntimeError, match="disabled"):
            await backend.async_execute("echo 'hello'")

    async def test_async_execute_permission_denied(self, tmp_path: Path):
        """Test async execute returns error when permission denies."""
        from pydantic_ai_backends.permissions import OperationPermissions, PermissionRuleset

        ruleset = PermissionRuleset(execute=OperationPermissions(default="deny"))
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = await backend.async_execute("echo hello")

        assert result.exit_code == 1
        assert "Error" in result.output
        assert "Permission denied" in result.output

    async def test_async_execute_timeout_exceeded(self, tmp_path: Path):
        """Test async command execution timeout returns timed-out response."""
        backend = LocalBackend(root_dir=tmp_path)

        result = await backend.async_execute("sleep 10", timeout=1)
        assert result.exit_code == 124
        assert "timed out" in result.output

    async def test_async_execute_cancelled(self, tmp_path: Path):
        """Test that CancelledError propagates from async_execute."""
        backend = LocalBackend(root_dir=tmp_path)

        task = asyncio.create_task(backend.async_execute("sleep 60"))
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_async_execute_cancel_kills_grandchildren(self, tmp_path: Path):
        """On Unix, cancelling must reap the entire process group, not just the shell."""

        if sys.platform == "win32":
            pytest.skip("Process-group semantics are Unix-specific")

        backend = LocalBackend(root_dir=tmp_path)

        # Write the grandchild's PID to a file before sleeping so we can verify
        # the kill reaches it. The shell ($$) is the parent; we want the actual
        # sleep grandchild.
        pid_file = tmp_path / "pid"
        cmd = f'(sleep 60 & echo $! > "{pid_file}"; wait)'

        task = asyncio.create_task(backend.async_execute(cmd))

        # Wait for the pid file to appear (grandchild has been spawned).
        for _ in range(50):
            await asyncio.sleep(0.05)
            if pid_file.exists() and pid_file.read_text().strip():
                break
        else:
            task.cancel()
            pytest.fail("grandchild never wrote pid file")

        grandchild_pid = int(pid_file.read_text().strip())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Give the OS a moment to deliver SIGKILL.
        await asyncio.sleep(0.1)
        # os.kill(pid, 0) raises ProcessLookupError if the process is gone.
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild_pid, 0)


class TestKillProcTree:
    """Test _kill_proc_tree platform branches."""

    def test_kill_proc_tree_windows_calls_proc_kill(self, monkeypatch: pytest.MonkeyPatch):
        """On Windows, _kill_proc_tree delegates to proc.kill() directly."""

        import pydantic_ai_backends.backends.local as local_mod

        monkeypatch.setattr(local_mod.sys, "platform", "win32")
        proc = MagicMock()
        LocalBackend._kill_proc_tree(proc)
        proc.kill.assert_called_once_with()

    def test_kill_proc_tree_windows_swallows_process_lookup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """ProcessLookupError from a race with a process that already exited is suppressed."""

        import pydantic_ai_backends.backends.local as local_mod

        monkeypatch.setattr(local_mod.sys, "platform", "win32")
        proc = MagicMock()
        proc.kill.side_effect = ProcessLookupError
        # Must not raise.
        LocalBackend._kill_proc_tree(proc)


class TestShellCmd:
    """Test _shell_cmd platform selection."""

    def test_shell_cmd_unix(self, monkeypatch: pytest.MonkeyPatch):
        """On non-Windows platforms, returns sh -c."""
        import pydantic_ai_backends.backends.local as local_mod

        monkeypatch.setattr(local_mod.sys, "platform", "linux")
        assert LocalBackend._shell_cmd("ls -la") == ["sh", "-c", "ls -la"]

    def test_shell_cmd_windows(self, monkeypatch: pytest.MonkeyPatch):
        """On Windows, returns cmd /c."""
        import pydantic_ai_backends.backends.local as local_mod

        monkeypatch.setattr(local_mod.sys, "platform", "win32")
        assert LocalBackend._shell_cmd("dir") == ["cmd", "/c", "dir"]

    def test_shell_cmd_darwin(self, monkeypatch: pytest.MonkeyPatch):
        """On macOS, returns sh -c (non-Windows branch)."""
        import pydantic_ai_backends.backends.local as local_mod

        monkeypatch.setattr(local_mod.sys, "platform", "darwin")
        assert LocalBackend._shell_cmd("echo hi") == ["sh", "-c", "echo hi"]


class TestLocalBackendPathResolution:
    """Test LocalBackend path resolution and non-existent file handling."""

    def test_read_file_absolute_path(self, tmp_path: Path):
        """Read using a full absolute path."""
        backend = LocalBackend(root_dir=tmp_path)
        (tmp_path / "abs_test.txt").write_text("absolute content")

        content = backend.read(str(tmp_path / "abs_test.txt"))
        assert "absolute content" in content

    def test_read_file_relative_path(self, tmp_path: Path):
        """Read using a relative path, resolved from root_dir."""
        backend = LocalBackend(root_dir=tmp_path)
        (tmp_path / "rel_test.txt").write_text("relative content")

        content = backend.read("rel_test.txt")
        assert "relative content" in content

    def test_read_file_nonexistent_no_crash(self, tmp_path: Path):
        """Reading a non-existent file returns a graceful error string."""
        backend = LocalBackend(root_dir=tmp_path)

        content = backend.read("does_not_exist.txt")
        assert "Error" in content
        assert "not found" in content

    def test_read_bytes_nonexistent(self, tmp_path: Path):
        """read_bytes returns empty bytes for a non-existent file."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.read_bytes("no_such_file.bin")
        assert result == b""

    def test_read_bytes_nonexistent_absolute(self, tmp_path: Path):
        """read_bytes returns empty bytes for a non-existent absolute path."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.read_bytes(str(tmp_path / "no_such_file.bin"))
        assert result == b""

    def test_write_and_read_relative_path(self, tmp_path: Path):
        """Write and read using relative paths resolved from root_dir."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.write("new_file.txt", "hello world")
        assert result.error is None

        content = backend.read("new_file.txt")
        assert "hello world" in content

    def test_custom_root_dir(self, tmp_path: Path):
        """Paths resolve against a custom root_dir."""
        custom_root = tmp_path / "custom" / "root"
        custom_root.mkdir(parents=True)

        backend = LocalBackend(root_dir=custom_root)
        backend.write("test.txt", "custom root content")

        assert (custom_root / "test.txt").read_text() == "custom root content"
        content = backend.read("test.txt")
        assert "custom root content" in content
