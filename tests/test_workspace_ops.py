"""`WorkspaceOps`, the console tools' operations on a Pydantic AI workspace."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.workspaces import (
    CommandResult,
    LocalWorkspaceBackend,
    ReadOnlyWorkspace,
    Workspace,
    WorkspaceCommand,
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
)

from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends._limits import MAX_EXECUTE_OUTPUT_BYTES
from pydantic_ai_backends.permissions import PERMISSIVE_RULESET
from pydantic_ai_backends.toolsets._workspace import WorkspaceOps


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(LocalWorkspaceBackend(tmp_path))


@pytest.fixture
def sandbox(workspace: Workspace) -> WorkspaceOps:
    return WorkspaceOps(workspace)


class _Scripted(LocalWorkspaceBackend):
    """A local backend whose `run` raises what a test tells it to."""

    def __init__(self, path: Path, error: Exception) -> None:
        super().__init__(path)
        self.error = error

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        raise self.error


class TestIdentity:
    def test_exposes_its_workspace(self, workspace: Workspace) -> None:
        assert WorkspaceOps(workspace).workspace is workspace


class TestExecute:
    async def test_stdout_then_stderr_with_the_exit_code(self, sandbox: WorkspaceOps) -> None:
        result = await sandbox.execute("printf out; printf err >&2; exit 3")
        assert (result.output, result.exit_code, result.truncated) == ("outerr", 3, False)

    async def test_output_past_the_ceiling_is_cut(self, sandbox: WorkspaceOps) -> None:
        result = await sandbox.execute(f"head -c {MAX_EXECUTE_OUTPUT_BYTES + 10} /dev/zero")
        assert result.truncated and len(result.output) == MAX_EXECUTE_OUTPUT_BYTES

    async def test_a_timeout_keeps_what_was_printed(self, sandbox: WorkspaceOps) -> None:
        result = await sandbox.execute("printf partial; sleep 30", timeout=1)
        assert result.exit_code == 124
        assert result.output == "Error: Command timed out\npartial"

    async def test_output_over_the_workspace_limit(self, tmp_path: Path) -> None:
        error = WorkspaceOutputLimitError("too much", limit=1, stdout="a", stderr="b")
        result = await WorkspaceOps(Workspace(_Scripted(tmp_path, error))).execute("yes")
        assert (result.output, result.exit_code, result.truncated) == ("ab", 1, True)

    async def test_a_timeout_with_no_output(self, tmp_path: Path) -> None:
        error = WorkspaceTimeoutError("slow")
        result = await WorkspaceOps(Workspace(_Scripted(tmp_path, error))).execute("sleep 9")
        assert result.output == "Error: Command timed out"

    async def test_a_refusal_is_returned(self, workspace: Workspace) -> None:
        result = await WorkspaceOps(ReadOnlyWorkspace(workspace)).execute("touch x")
        assert result.exit_code == 1 and result.output.startswith("Error: ")


class TestFiles:
    async def test_write_read_and_list(self, sandbox: WorkspaceOps, tmp_path: Path) -> None:
        assert (await sandbox.write("dir/notes.txt", "one\ntwo\nthree")).path == "dir/notes.txt"
        assert (tmp_path / "dir" / "notes.txt").read_text() == "one\ntwo\nthree"
        assert await sandbox.read("dir/notes.txt") == "     1\tone\n     2\ttwo\n     3\tthree"
        assert await sandbox.read("dir/notes.txt", 1, 1) == "     2\ttwo\n\n... (1 more lines)"
        listing = await sandbox.ls_info("dir")
        assert [(e["name"], e["is_dir"], e["size"]) for e in listing] == [("notes.txt", False, 13)]

    async def test_bytes_round_trip(self, sandbox: WorkspaceOps) -> None:
        await sandbox.write("blob.bin", b"\x00\xff")
        assert await sandbox.read_bytes("blob.bin") == b"\x00\xff"

    async def test_exists_means_a_regular_file(self, sandbox: WorkspaceOps) -> None:
        await sandbox.write("dir/file", "x")
        assert await sandbox.exists("dir/file")
        assert not await sandbox.exists("dir")
        assert not await sandbox.exists("missing")

    async def test_failures_are_returned(self, sandbox: WorkspaceOps) -> None:
        await sandbox.write("file", "x")
        assert await sandbox.ls_info("missing") == []
        assert await sandbox.read_bytes("missing") == b""
        assert await sandbox.read("missing") == "Error: File 'missing' not found"
        assert (await sandbox.read(".")).startswith("Error: ")
        assert (await sandbox.write("file/child", "x")).error is not None

    async def test_an_offset_past_the_end(self, sandbox: WorkspaceOps) -> None:
        await sandbox.write("short.txt", "only")
        assert await sandbox.read("short.txt", 5) == "Error: Offset 5 exceeds file length (1 lines)"
        await sandbox.write("empty.txt", "")
        assert await sandbox.read("empty.txt") == ""

    async def test_an_undecodable_document(self, sandbox: WorkspaceOps) -> None:
        await sandbox.write("broken.pdf", b"not a pdf")
        assert (await sandbox.read("broken.pdf")).startswith("[Error: ")
        assert (await sandbox.edit("broken.pdf", "a", "b")).error is not None

    async def test_reads_work_on_a_read_only_workspace(self, workspace: Workspace) -> None:
        await workspace.write_text("kept.txt", "kept")
        read_only = WorkspaceOps(ReadOnlyWorkspace(workspace))
        assert await read_only.read("kept.txt") == "     1\tkept"
        assert (await read_only.write("new.txt", "x")).error is not None
        assert (await read_only.edit("kept.txt", "kept", "gone")).error is not None


class TestEdit:
    async def test_replaces_and_counts(self, sandbox: WorkspaceOps, tmp_path: Path) -> None:
        await sandbox.write("code.py", "a = 1\na = 1\n")
        edited = await sandbox.edit("code.py", "a = 1", "a = 2", replace_all=True)
        assert (edited.path, edited.occurrences) == ("code.py", 2)
        assert (tmp_path / "code.py").read_text() == "a = 2\na = 2\n"

    async def test_refusals(self, sandbox: WorkspaceOps) -> None:
        await sandbox.write("code.py", "a\na\n")
        assert (await sandbox.edit("missing.py", "a", "b")).error == "File 'missing.py' not found"
        assert "found 2 times" in ((await sandbox.edit("code.py", "a", "b")).error or "")
        assert (await sandbox.edit(".", "a", "b")).error is not None


def _script(*steps: ToolCallPart | str) -> FunctionModel:
    """A model that makes one call per turn, then answers with the last step."""
    remaining = list(steps)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        step = remaining.pop(0)
        return ModelResponse(parts=[TextPart(step) if isinstance(step, str) else step])

    return FunctionModel(respond)


class TestAnAgentRun:
    async def test_tools_reach_the_workspace(self, tmp_path: Path) -> None:
        agent = Agent(
            _script(
                ToolCallPart("write_file", {"path": "hello.txt", "content": "hi"}),
                ToolCallPart("execute", {"command": "cat hello.txt"}),
                "done",
            ),
            capabilities=[
                LocalWorkspace(tmp_path),
                ConsoleCapability(permissions=PERMISSIVE_RULESET),
            ],
        )
        result = await agent.run("write and read")
        assert (tmp_path / "hello.txt").read_text() == "hi"
        returns = [
            part.content
            for message in result.all_messages()
            for part in message.parts
            if part.part_kind == "tool-return"
        ]
        assert returns[1] == "hi"
