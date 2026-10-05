"""`ConfinedWorkspace`: file operations, and the console's searches, stay in the root."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace

from pydantic_ai_backends import create_console_toolset
from pydantic_ai_backends.toolsets._workspace import WorkspaceOps
from pydantic_ai_backends.workspaces import ConfinedWorkspace, WorkspacePathError
from tests.support import call, ctx, document


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """A project directory, and a directory beside it the workspace must not reach."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    (project / "src").mkdir(parents=True)
    outside.mkdir()
    (project / "src" / "app.py").write_text("token = 'inside'\n")
    (outside / "secret.txt").write_text("token = 'outside'\n")
    return project, outside


def confined(project: Path) -> ConfinedWorkspace:
    return ConfinedWorkspace(Workspace(LocalWorkspaceBackend(project)))


class TestFileOperations:
    async def test_paths_inside_work(self, tree: tuple[Path, Path]) -> None:
        project, _ = tree
        workspace = confined(project)
        assert await workspace.read_text("src/app.py") == "token = 'inside'\n"
        await workspace.write_text(f"{project}/src/new.py", "x = 1\n")
        await workspace.make_dir("build")
        assert await workspace.exists("build")
        assert [entry.name for entry in await workspace.list_dir("src")] == ["app.py", "new.py"]
        assert (await workspace.stat("src/new.py")).size == 6
        await workspace.remove("build")
        assert not (project / "build").exists()
        assert [entry.name for entry in await workspace.list_dir(".")] == ["src"]

    @pytest.mark.parametrize("spelling", ["../outside/secret.txt", "{outside}/secret.txt"])
    async def test_a_path_outside_is_refused(self, tree: tuple[Path, Path], spelling: str) -> None:
        project, outside = tree
        workspace = confined(project)
        path = spelling.format(outside=outside)
        with pytest.raises(WorkspacePathError, match="outside the workspace"):
            await workspace.read_bytes(path)
        with pytest.raises(PermissionError):
            await workspace.write_text(path, "overwritten")
        assert (outside / "secret.txt").read_text() == "token = 'outside'\n"

    @pytest.mark.parametrize(
        "operation",
        ["stat", "list_dir", "make_dir", "remove", "exists"],
    )
    async def test_every_file_operation_is_checked(
        self, tree: tuple[Path, Path], operation: str
    ) -> None:
        project, outside = tree
        with pytest.raises(WorkspacePathError):
            await getattr(confined(project), operation)(str(outside))
        assert (outside / "secret.txt").exists()

    async def test_a_symlink_leading_out_is_refused(self, tree: tuple[Path, Path]) -> None:
        project, outside = tree
        (project / "link").symlink_to(outside)
        with pytest.raises(WorkspacePathError):
            await confined(project).read_text("link/secret.txt")

    async def test_commands_are_not_confined(self, tree: tuple[Path, Path]) -> None:
        """Only a sandbox isolates a shell; the docstring says so, and this pins it."""
        project, outside = tree
        result = await confined(project).run(["cat", str(outside / "secret.txt")])
        assert result.stdout == "token = 'outside'\n"


class TestSearches:
    async def test_a_search_inside_finds_files(self, tree: tuple[Path, Path]) -> None:
        project, _ = tree
        ops = WorkspaceOps(Workspace(confined(project)))
        assert [entry["name"] for entry in await ops.glob_info("*.py")] == ["app.py"]
        matches = await ops.grep_raw("token")
        assert isinstance(matches, list) and len(matches) == 1

    async def test_a_glob_rooted_outside_is_refused(self, tree: tuple[Path, Path]) -> None:
        project, outside = tree
        ops = WorkspaceOps(confined(project))
        with pytest.raises(WorkspacePathError):
            await ops.glob_info("*.txt", str(outside))

    async def test_a_grep_rooted_outside_is_an_error(self, tree: tuple[Path, Path]) -> None:
        project, outside = tree
        found = await WorkspaceOps(confined(project)).grep_raw("token", "../outside")
        assert isinstance(found, str) and "outside the workspace" in found
        assert str(outside) not in found.replace(f"'{outside}", "")

    async def test_an_unconfined_workspace_searches_anywhere(self, tree: tuple[Path, Path]) -> None:
        project, outside = tree
        ops = WorkspaceOps(Workspace(LocalWorkspaceBackend(project)))
        assert [entry["name"] for entry in await ops.glob_info("*.txt", str(outside))] == [
            "secret.txt"
        ]

    async def test_a_confined_document_is_searched_by_walking(self) -> None:
        workspace = ConfinedWorkspace(document({"/notes/a.md": "token"}))
        assert [entry["name"] for entry in await WorkspaceOps(workspace).glob_info("*.md")] == [
            "a.md"
        ]


class TestTheConsole:
    async def test_the_model_reads_a_refusal_rather_than_losing_the_run(
        self, tree: tuple[Path, Path]
    ) -> None:
        project, outside = tree
        toolset = create_console_toolset()
        context = ctx(Workspace(confined(project)))
        answer = await call(toolset, "read_file", context, path=str(outside / "secret.txt"))
        assert "outside the workspace" in answer
        answer = await call(toolset, "glob", context, pattern="*.txt", path=str(outside))
        assert answer.startswith("Error:") and "outside the workspace" in answer
