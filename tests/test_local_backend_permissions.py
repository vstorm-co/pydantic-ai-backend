"""Tests for LocalBackend permission integration."""

from pathlib import Path

import pytest

from pydantic_ai_backends import LocalBackend
from pydantic_ai_backends.permissions import (
    OperationPermissions,
    PermissionAskError,
    PermissionChecker,
    PermissionRule,
    PermissionRuleset,
)


class TestLocalBackendPermissionsInit:
    """Tests for LocalBackend initialization with permissions."""

    def test_init_without_permissions(self, tmp_path: Path):
        """Test that permissions are optional."""
        backend = LocalBackend(root_dir=tmp_path)

        assert backend.permissions is None
        assert backend.permission_checker is None

    def test_init_with_permissions(self, tmp_path: Path):
        """Test initialization with permissions."""
        ruleset = PermissionRuleset(default="deny")
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        assert backend.permissions is ruleset
        assert backend.permission_checker is not None
        assert isinstance(backend.permission_checker, PermissionChecker)

    def test_init_with_ask_callback(self, tmp_path: Path):
        """Test initialization with ask callback."""

        async def my_callback(op: str, target: str, reason: str) -> bool:
            return True

        ruleset = PermissionRuleset(default="ask")
        backend = LocalBackend(
            root_dir=tmp_path,
            permissions=ruleset,
            ask_callback=my_callback,
        )

        assert backend.permission_checker is not None

    def test_init_with_ask_fallback(self, tmp_path: Path):
        """Test initialization with ask fallback."""
        ruleset = PermissionRuleset(default="ask")
        backend = LocalBackend(
            root_dir=tmp_path,
            permissions=ruleset,
            ask_fallback="deny",
        )

        assert backend.permission_checker is not None


class TestLocalBackendReadPermissions:
    """Tests for read operation permission checks."""

    def test_read_allowed(self, tmp_path: Path):
        """Test read when permission allows."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(default="allow"),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        # Create and read a file
        (tmp_path / "test.txt").write_text("content")
        result = backend.read("test.txt")

        assert "content" in result
        assert "Error" not in result

    def test_read_denied(self, tmp_path: Path):
        """Test read when permission denies."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(default="deny"),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        (tmp_path / "test.txt").write_text("content")
        result = backend.read("test.txt")

        assert "Error" in result
        assert "Permission denied" in result

    def test_read_denied_by_rule(self, tmp_path: Path):
        """Test read denied by specific rule."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(
                default="allow",
                rules=[
                    PermissionRule(
                        pattern="**/.env",
                        action="deny",
                        description="Protect env files",
                    )
                ],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        env_file = tmp_path / ".env"
        env_file.write_text("SECRET=123")

        result = backend.read(".env")

        assert "Error" in result
        assert "Protect env files" in result

    def test_read_ask_with_deny_fallback(self, tmp_path: Path):
        """Test read with ask action and deny fallback."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(default="ask"),
        )
        backend = LocalBackend(
            root_dir=tmp_path,
            permissions=ruleset,
            ask_fallback="deny",
        )

        (tmp_path / "test.txt").write_text("content")
        result = backend.read("test.txt")

        assert "Error" in result
        assert "approval required" in result

    def test_read_ask_with_error_fallback(self, tmp_path: Path):
        """Test read with ask action and error fallback."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(default="ask"),
        )
        backend = LocalBackend(
            root_dir=tmp_path,
            permissions=ruleset,
            ask_fallback="error",
        )

        (tmp_path / "test.txt").write_text("content")

        with pytest.raises(PermissionAskError):
            backend.read("test.txt")


class TestLocalBackendWritePermissions:
    """Tests for write operation permission checks."""

    def test_write_allowed(self, tmp_path: Path):
        """Test write when permission allows."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="allow"),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.write("test.txt", "content")

        assert result.error is None
        assert (tmp_path / "test.txt").read_text() == "content"

    def test_write_denied(self, tmp_path: Path):
        """Test write when permission denies."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="deny"),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.write("test.txt", "content")

        assert result.error is not None
        assert "Permission denied" in result.error
        assert not (tmp_path / "test.txt").exists()

    def test_write_denied_by_rule(self, tmp_path: Path):
        """Test write denied by specific rule."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(
                default="allow",
                rules=[
                    PermissionRule(
                        pattern="**/sensitive.txt",
                        action="deny",
                        description="Protect sensitive file",
                    )
                ],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.write("sensitive.txt", "content")

        assert result.error is not None
        assert "Protect sensitive file" in result.error


class TestLocalBackendEditPermissions:
    """Tests for edit operation permission checks."""

    def test_edit_allowed(self, tmp_path: Path):
        """Test edit when permission allows."""
        ruleset = PermissionRuleset(
            edit=OperationPermissions(default="allow"),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        (tmp_path / "test.txt").write_text("hello world")
        result = backend.edit("test.txt", "hello", "goodbye")

        assert result.error is None
        assert (tmp_path / "test.txt").read_text() == "goodbye world"

    def test_edit_denied(self, tmp_path: Path):
        """Test edit when permission denies."""
        ruleset = PermissionRuleset(
            edit=OperationPermissions(default="deny"),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        (tmp_path / "test.txt").write_text("hello world")
        result = backend.edit("test.txt", "hello", "goodbye")

        assert result.error is not None
        assert "Permission denied" in result.error
        # Content should be unchanged
        assert (tmp_path / "test.txt").read_text() == "hello world"


class TestLocalBackendExecutePermissions:
    """Tests for execute operation permission checks."""

    def test_execute_allowed(self, tmp_path: Path):
        """Test execute when permission allows."""
        ruleset = PermissionRuleset(
            execute=OperationPermissions(default="allow"),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.execute("echo hello")

        assert result.exit_code == 0
        assert "hello" in result.output

    def test_execute_denied(self, tmp_path: Path):
        """Test execute when permission denies."""
        ruleset = PermissionRuleset(
            execute=OperationPermissions(default="deny"),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.execute("echo hello")

        assert result.exit_code == 1
        assert "Error" in result.output
        assert "Permission denied" in result.output

    def test_execute_denied_by_rule(self, tmp_path: Path):
        """Test execute denied by specific rule."""
        ruleset = PermissionRuleset(
            execute=OperationPermissions(
                default="allow",
                rules=[
                    PermissionRule(
                        pattern="rm *",
                        action="deny",
                        description="Block rm commands",
                    )
                ],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.execute("rm file.txt")

        assert result.exit_code == 1
        assert "Block rm commands" in result.output


class TestLocalBackendReadBytesPermissions:
    """`read_bytes` must honor the same "read" rules as `read` (issue #62)."""

    def test_read_bytes_without_permissions(self, tmp_path: Path):
        """No ruleset — read_bytes returns content."""
        backend = LocalBackend(root_dir=tmp_path)

        (tmp_path / "data.bin").write_bytes(b"\x00\x01payload")

        assert backend.read_bytes("data.bin") == b"\x00\x01payload"

    def test_read_bytes_allowed(self, tmp_path: Path):
        ruleset = PermissionRuleset(read=OperationPermissions(default="allow"))
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        (tmp_path / "data.bin").write_bytes(b"payload")

        assert backend.read_bytes("data.bin") == b"payload"

    def test_read_bytes_denied_by_rule(self, tmp_path: Path):
        """A read deny rule must block the raw-bytes path too."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/restricted/**", action="deny")],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        secret = tmp_path / "restricted" / "secret.txt"
        secret.parent.mkdir()
        secret.write_text("SECRET=123")

        assert backend.read_bytes("restricted/secret.txt") == b""

    def test_read_bytes_default_deny(self, tmp_path: Path):
        ruleset = PermissionRuleset(read=OperationPermissions(default="deny"))
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        (tmp_path / "data.bin").write_bytes(b"payload")

        assert backend.read_bytes("data.bin") == b""

    def test_read_bytes_ask_with_deny_fallback(self, tmp_path: Path):
        ruleset = PermissionRuleset(read=OperationPermissions(default="ask"))
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset, ask_fallback="deny")

        (tmp_path / "data.bin").write_bytes(b"payload")

        assert backend.read_bytes("data.bin") == b""

    def test_read_bytes_ask_with_error_fallback(self, tmp_path: Path):
        ruleset = PermissionRuleset(read=OperationPermissions(default="ask"))
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset, ask_fallback="error")

        (tmp_path / "data.bin").write_bytes(b"payload")

        with pytest.raises(PermissionAskError):
            backend.read_bytes("data.bin")

    def test_read_bytes_outside_allowed_directories(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        backend = LocalBackend(allowed_directories=[str(project_dir)])

        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"payload")

        assert backend.read_bytes(str(outside)) == b""

    def test_read_bytes_missing_file(self, tmp_path: Path):
        backend = LocalBackend(root_dir=tmp_path)

        assert backend.read_bytes("missing.bin") == b""

    def test_read_bytes_directory(self, tmp_path: Path):
        backend = LocalBackend(root_dir=tmp_path)

        (tmp_path / "subdir").mkdir()

        assert backend.read_bytes("subdir") == b""


class TestLocalBackendListingPermissions:
    """`ls`, `glob`, and `grep` honor explicit deny rules (issue #62)."""

    def _restricted_tree(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("print('SECRET_MARKER ok')\n")
        restricted = tmp_path / "restricted"
        restricted.mkdir()
        (restricted / "secret.txt").write_text("SECRET_MARKER hidden\n")

    def test_ls_denied_path_returns_empty(self, tmp_path: Path):
        self._restricted_tree(tmp_path)
        ruleset = PermissionRuleset(
            ls=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/restricted*", action="deny")],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        assert backend.ls_info("restricted") == []

    def test_ls_hides_denied_entries(self, tmp_path: Path):
        self._restricted_tree(tmp_path)
        ruleset = PermissionRuleset(
            ls=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/restricted*", action="deny")],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        names = [info["name"] for info in backend.ls_info(".")]

        assert "app.py" in names
        assert "restricted" not in names

    def test_ls_global_ask_default_still_lists(self, tmp_path: Path):
        """A global default of "ask" must not blank out listings (compat)."""
        self._restricted_tree(tmp_path)
        ruleset = PermissionRuleset(read=OperationPermissions(default="allow"))
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        names = [info["name"] for info in backend.ls_info(".")]

        assert "app.py" in names

    def test_glob_denied_base_returns_empty(self, tmp_path: Path):
        self._restricted_tree(tmp_path)
        ruleset = PermissionRuleset(
            glob=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/restricted*", action="deny")],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        assert backend.glob_info("**/*.txt", "restricted") == []

    def test_glob_hides_denied_matches(self, tmp_path: Path):
        self._restricted_tree(tmp_path)
        ruleset = PermissionRuleset(
            glob=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/restricted/**", action="deny")],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        paths = [info["path"] for info in backend.glob_info("**/*")]

        assert any(p.endswith("app.py") for p in paths)
        assert not any("secret.txt" in p for p in paths)

    def test_grep_denied_search_path_errors(self, tmp_path: Path):
        self._restricted_tree(tmp_path)
        ruleset = PermissionRuleset(
            grep=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/restricted*", action="deny")],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.grep_raw("SECRET_MARKER", "restricted")

        assert isinstance(result, str)
        assert "Permission denied" in result

    def test_grep_read_deny_hides_content(self, tmp_path: Path):
        """Grep must not leak lines from files the agent may not read."""
        self._restricted_tree(tmp_path)
        ruleset = PermissionRuleset(
            read=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/restricted/**", action="deny")],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.grep_raw("SECRET_MARKER")

        assert isinstance(result, list)
        assert any(m["path"].endswith("app.py") for m in result)
        assert not any("secret.txt" in m["path"] for m in result)

    def test_grep_grep_deny_hides_file(self, tmp_path: Path):
        self._restricted_tree(tmp_path)
        ruleset = PermissionRuleset(
            grep=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/restricted/**", action="deny")],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.grep_raw("SECRET_MARKER")

        assert isinstance(result, list)
        assert not any("secret.txt" in m["path"] for m in result)


class TestLocalBackendExecutePathGuard:
    """Best-effort guard: commands referencing read/write-denied paths (issue #62)."""

    def _backend(self, tmp_path: Path) -> LocalBackend:
        secret = tmp_path / "restricted" / "secret.txt"
        secret.parent.mkdir(exist_ok=True)
        secret.write_text("SECRET=123")
        ruleset = PermissionRuleset(
            read=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/restricted/**", action="deny")],
            ),
            execute=OperationPermissions(default="allow"),
        )
        return LocalBackend(root_dir=tmp_path, permissions=ruleset)

    def test_execute_denies_relative_path_to_denied_file(self, tmp_path: Path):
        backend = self._backend(tmp_path)

        result = backend.execute("cat restricted/secret.txt")

        assert result.exit_code == 1
        assert "Permission denied" in result.output
        assert "SECRET" not in result.output

    def test_execute_denies_absolute_path_to_denied_file(self, tmp_path: Path):
        backend = self._backend(tmp_path)

        result = backend.execute(f"cat {tmp_path / 'restricted' / 'secret.txt'}")

        assert result.exit_code == 1
        assert "Permission denied" in result.output
        assert "SECRET" not in result.output

    def test_execute_denies_flag_value_path(self, tmp_path: Path):
        backend = self._backend(tmp_path)

        result = backend.execute("some-tool --empty= --input=restricted/secret.txt")

        assert result.exit_code == 1
        assert "Permission denied" in result.output

    def test_execute_guard_survives_unbalanced_quotes(self, tmp_path: Path):
        backend = self._backend(tmp_path)

        result = backend.execute("cat 'restricted/secret.txt")

        assert result.exit_code == 1
        assert "Permission denied" in result.output

    def test_execute_denies_write_denied_path(self, tmp_path: Path):
        ruleset = PermissionRuleset(
            write=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/protected/**", action="deny")],
            ),
            execute=OperationPermissions(default="allow"),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.execute("touch protected/new.txt")

        assert result.exit_code == 1
        assert "Permission denied" in result.output

    def test_execute_allows_unrelated_command(self, tmp_path: Path):
        backend = self._backend(tmp_path)

        result = backend.execute("echo hello ./allowed/path.txt")

        assert result.exit_code == 0
        assert "hello" in result.output

    def test_execute_rule_check_still_wins(self, tmp_path: Path):
        """The command-pattern check runs before the path guard."""
        ruleset = PermissionRuleset(
            execute=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="rm *", action="deny")],
            ),
        )
        backend = LocalBackend(root_dir=tmp_path, permissions=ruleset)

        result = backend.execute("rm file.txt")

        assert result.exit_code == 1
        assert "Permission denied" in result.output

    async def test_async_execute_uses_path_guard(self, tmp_path: Path):
        backend = self._backend(tmp_path)

        result = await backend.async_execute("cat restricted/secret.txt")

        assert result.exit_code == 1
        assert "Permission denied" in result.output

    def test_execute_background_uses_path_guard(self, tmp_path: Path):
        backend = self._backend(tmp_path)

        with pytest.raises(PermissionError, match="Permission denied"):
            backend.execute_background("cat restricted/secret.txt")


class TestLocalBackendPermissionsWithAllowedDirectories:
    """Tests for permissions combined with allowed_directories."""

    def test_allowed_directories_checked_first(self, tmp_path: Path):
        """Test that allowed_directories are checked before permissions."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ruleset = PermissionRuleset(
            read=OperationPermissions(default="allow"),
        )
        backend = LocalBackend(
            allowed_directories=[str(project_dir)],
            permissions=ruleset,
        )

        # Create file outside allowed directories
        outside = tmp_path / "outside.txt"
        outside.write_text("content")

        # Should fail due to allowed_directories, not permissions
        result = backend.read(str(outside))

        assert "Error" in result
        assert "outside allowed directories" in result

    def test_both_checks_pass(self, tmp_path: Path):
        """Test when both checks pass."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ruleset = PermissionRuleset(
            read=OperationPermissions(default="allow"),
        )
        backend = LocalBackend(
            allowed_directories=[str(project_dir)],
            permissions=ruleset,
        )

        (project_dir / "test.txt").write_text("content")
        result = backend.read("test.txt")

        assert "content" in result
        assert "Error" not in result
