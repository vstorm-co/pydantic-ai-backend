"""The permission guard and the walking search, at the branches tools rarely reach."""

from __future__ import annotations

from pathlib import Path

import pytest

from pydantic_ai_backends.permissions import (
    OperationPermissions,
    PermissionAskError,
    PermissionRule,
    PermissionRuleset,
)
from pydantic_ai_backends.toolsets._guard import PermissionGuard
from pydantic_ai_backends.toolsets._workspace import WorkspaceOps
from tests.support import document, local


def _guard(ruleset: PermissionRuleset, ask_fallback: str = "error") -> PermissionGuard:
    return PermissionGuard(ruleset, Path("/work"), ask_fallback=ask_fallback)  # type: ignore[arg-type]


class TestPermissionGuard:
    def test_the_checker_is_exposed(self) -> None:
        ruleset = PermissionRuleset(default="allow")
        assert _guard(ruleset).checker.ruleset is ruleset

    def test_a_deny_without_a_description_names_the_operation(self) -> None:
        ruleset = PermissionRuleset(
            default="allow",
            read=OperationPermissions(rules=[PermissionRule(pattern="**/.env", action="deny")]),
        )
        assert _guard(ruleset).denial_reason("read", "/w/.env") == (
            "Permission denied for read on '/w/.env'"
        )

    def test_an_ask_without_a_callback(self) -> None:
        ruleset = PermissionRuleset(default="ask")
        assert "approval required" in (_guard(ruleset, "deny").denial_reason("write", "x") or "")
        with pytest.raises(PermissionAskError):
            _guard(ruleset).denial_reason("write", "x")

    def test_a_command_its_own_rules_deny(self) -> None:
        ruleset = PermissionRuleset(
            default="allow",
            execute=OperationPermissions(
                rules=[PermissionRule(pattern="rm *", action="deny", description="No deleting")]
            ),
        )
        assert _guard(ruleset).execute_denial_reason("rm -rf x") == "Permission denied: No deleting"

    def test_path_targets_survive_unbalanced_quotes_and_flags(self) -> None:
        guard = _guard(PermissionRuleset(default="allow"))
        targets = guard.command_path_targets("cat 'secret.txt --out= --file=/etc/passwd")
        assert "/work/secret.txt" in targets
        assert "/etc/passwd" in targets


class TestWalkingSearch:
    """A workspace without commands is searched by listing and reading its files."""

    @pytest.fixture
    def ops(self) -> WorkspaceOps:
        return WorkspaceOps(
            document(
                {
                    "/src/app.py": "needle in code",
                    "/src/notes.md": "needle in notes",
                    "/.hidden/x.py": "needle hidden",
                    "/blob.bin": b"\xff\xfe needle",
                }
            )
        )

    async def test_hidden_files_and_binaries_are_skipped(self, ops: WorkspaceOps) -> None:
        found = await ops.grep_raw("needle")
        assert isinstance(found, list)
        assert sorted(m["path"] for m in found) == ["/src/app.py", "/src/notes.md"]

    async def test_hidden_files_can_be_included(self, ops: WorkspaceOps) -> None:
        found = await ops.grep_raw("needle", ignore_hidden=False)
        assert isinstance(found, list) and "/.hidden/x.py" in {m["path"] for m in found}

    async def test_a_glob_narrows_by_name(self, ops: WorkspaceOps) -> None:
        found = await ops.grep_raw("needle", glob="*.py")
        assert isinstance(found, list) and [m["path"] for m in found] == ["/src/app.py"]

    async def test_one_file_can_be_searched(self, ops: WorkspaceOps) -> None:
        found = await ops.grep_raw("code", path="/src/app.py")
        assert isinstance(found, list) and [m["line_number"] for m in found] == [1]

    async def test_a_missing_path_is_an_error(self, ops: WorkspaceOps) -> None:
        assert str(await ops.grep_raw("x", path="/nowhere")).startswith("Error:")

    async def test_glob_walks_from_a_subdirectory(self, ops: WorkspaceOps) -> None:
        found = await ops.glob_info("*.md", "/src")
        assert [entry["path"] for entry in found] == ["/src/notes.md"]

    async def test_an_unreadable_directory_is_skipped(self, tmp_path: Path) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "f.txt").write_text("needle")
        (tmp_path / "open.txt").write_text("needle")
        locked.chmod(0)
        try:
            from pydantic_ai.workspaces import ReadOnlyWorkspace

            ops = WorkspaceOps(ReadOnlyWorkspace(local(tmp_path)))
            found = await ops.grep_raw("needle")
        finally:
            locked.chmod(0o755)
        assert isinstance(found, list)
        assert [Path(m["path"]).name for m in found] == ["open.txt"]


class TestShellSearchKeepsHiddenFilesWhenAsked:
    async def test_ignore_hidden_off_returns_them(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("needle")
        found = await WorkspaceOps(local(tmp_path)).grep_raw("needle", ignore_hidden=False)
        assert isinstance(found, list) and len(found) == 1
