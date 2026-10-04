"""The console toolset, driven against real workspaces."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic_ai import BinaryContent
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    SkipModelRequest,
    SkipToolExecution,
    SkipToolValidation,
    UserError,
)
from pydantic_ai.messages import ModelResponse
from pydantic_ai.workspaces import ReadOnlyWorkspace, Workspace

from pydantic_ai_backends import (
    ConsoleToolset,
    create_console_toolset,
    get_console_system_prompt,
)
from pydantic_ai_backends.toolsets._content import (
    DEFAULT_MAX_DOCUMENT_BYTES,
    DEFAULT_MAX_IMAGE_BYTES,
    DOCUMENT_EXTENSIONS,
    DOCUMENT_MEDIA_TYPES,
    IMAGE_EXTENSIONS,
    IMAGE_MEDIA_TYPES,
    document_content,
    image_content,
)
from pydantic_ai_backends.toolsets._workspace import WorkspaceOps
from tests.support import Raising, call, ctx, document, local

PDF_DATA = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG_DATA = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class TestCreateConsoleToolset:
    def test_the_default_tools(self) -> None:
        names = set(create_console_toolset().tools)
        assert names == {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"}

    def test_without_execute(self) -> None:
        assert "execute" not in create_console_toolset(include_execute=False).tools

    def test_a_custom_id(self) -> None:
        assert create_console_toolset(id="my-console").id == "my-console"

    def test_execute_needs_approval_by_default(self) -> None:
        toolset = create_console_toolset()
        assert toolset.tools["execute"].requires_approval is True
        assert toolset.tools["write_file"].requires_approval is False

    def test_write_approval(self) -> None:
        toolset = create_console_toolset(require_write_approval=True)
        assert toolset.tools["write_file"].requires_approval is True
        assert toolset.tools["edit_file"].requires_approval is True

    def test_the_hidden_file_default_is_configurable(self) -> None:
        toolset = create_console_toolset(default_ignore_hidden=False)
        assert toolset._console_default_ignore_hidden is False  # type: ignore[attr-defined]
        grep_impl = toolset._console_grep_impl  # type: ignore[attr-defined]
        assert inspect.signature(grep_impl).parameters["ignore_hidden"].default is False

    def test_the_alias(self) -> None:
        assert ConsoleToolset is create_console_toolset


class TestSystemPrompt:
    def test_it_names_the_operations(self) -> None:
        prompt = get_console_system_prompt().lower()
        for word in ("read", "write", "edit", "execute"):
            assert word in prompt


class TestContentConstants:
    def test_images(self) -> None:
        assert {"png", "jpg", "jpeg", "gif", "webp"} <= IMAGE_EXTENSIONS
        assert "svg" not in IMAGE_EXTENSIONS
        assert all(ext in IMAGE_MEDIA_TYPES for ext in IMAGE_EXTENSIONS)
        assert IMAGE_MEDIA_TYPES["jpg"] == "image/jpeg"
        assert DEFAULT_MAX_IMAGE_BYTES == 50 * 1024 * 1024

    def test_documents(self) -> None:
        assert "pdf" in DOCUMENT_EXTENSIONS and "pdf" not in IMAGE_EXTENSIONS
        assert all(ext in DOCUMENT_MEDIA_TYPES for ext in DOCUMENT_EXTENSIONS)
        assert DOCUMENT_MEDIA_TYPES["pdf"] == "application/pdf"

    def test_exported_from_the_package(self) -> None:
        import pydantic_ai_backends as package

        assert package.IMAGE_EXTENSIONS == IMAGE_EXTENSIONS
        assert package.DOCUMENT_MEDIA_TYPES == DOCUMENT_MEDIA_TYPES
        assert package.DEFAULT_MAX_DOCUMENT_BYTES == DEFAULT_MAX_DOCUMENT_BYTES


class TestContentHelpers:
    async def test_a_pdf_becomes_a_document(self, tmp_path: Path) -> None:
        (tmp_path / "report.pdf").write_bytes(PDF_DATA)
        result = await document_content(
            WorkspaceOps(local(tmp_path)), "report.pdf", DEFAULT_MAX_DOCUMENT_BYTES
        )
        assert isinstance(result, BinaryContent)
        assert (result.media_type, result.data, result.is_document) == (
            "application/pdf",
            PDF_DATA,
            True,
        )

    async def test_size_and_absence(self, tmp_path: Path) -> None:
        (tmp_path / "huge.pdf").write_bytes(PDF_DATA)
        (tmp_path / "test.png").write_bytes(PNG_DATA)
        ops = WorkspaceOps(local(tmp_path))
        size = len(PDF_DATA) / (1024 * 1024)
        assert await document_content(ops, "huge.pdf", 1) == (
            f"Error: Document 'huge.pdf' too large ({size:.1f}MB, max 0.0MB)"
        )
        assert await document_content(ops, "missing.pdf", DEFAULT_MAX_DOCUMENT_BYTES) == (
            "Error: Document file 'missing.pdf' not found or empty"
        )
        assert await image_content(ops, "missing.png", DEFAULT_MAX_IMAGE_BYTES) == (
            "Error: Image file 'missing.png' not found or empty"
        )
        assert await image_content(ops, "test.png", 1) == (
            f"Error: Image 'test.png' too large ({len(PNG_DATA) / (1024 * 1024):.1f}MB, max 0.0MB)"
        )

    async def test_each_helper_ignores_the_other_kind(self, tmp_path: Path) -> None:
        (tmp_path / "test.png").write_bytes(PNG_DATA)
        (tmp_path / "report.pdf").write_bytes(PDF_DATA)
        (tmp_path / "Makefile").write_text("all:\n")
        ops = WorkspaceOps(local(tmp_path))
        assert await document_content(ops, "test.png", DEFAULT_MAX_DOCUMENT_BYTES) is None
        assert await document_content(ops, "Makefile", DEFAULT_MAX_DOCUMENT_BYTES) is None
        assert await image_content(ops, "report.pdf", DEFAULT_MAX_IMAGE_BYTES) is None
        image = await image_content(ops, "test.png", DEFAULT_MAX_IMAGE_BYTES)
        assert isinstance(image, BinaryContent) and image.is_image


class TestReadFile:
    @pytest.mark.parametrize("edit_format", ["str_replace", "hashline"])
    async def test_a_pdf_comes_back_as_a_document(self, tmp_path: Path, edit_format: str) -> None:
        (tmp_path / "report.pdf").write_bytes(PDF_DATA)
        toolset = create_console_toolset(document_support=True, edit_format=edit_format)  # type: ignore[arg-type]
        result = await call(toolset, "read_file", ctx(local(tmp_path)), path="report.pdf")
        assert isinstance(result, BinaryContent) and result.data == PDF_DATA

    async def test_a_pdf_without_document_support_is_not_binary(self, tmp_path: Path) -> None:
        (tmp_path / "report.pdf").write_bytes(PDF_DATA)
        toolset = create_console_toolset(image_support=True)
        result = await call(toolset, "read_file", ctx(local(tmp_path)), path="report.pdf")
        assert not isinstance(result, BinaryContent)

    @pytest.mark.parametrize("edit_format", ["str_replace", "hashline"])
    async def test_text_stays_text_with_both_flags(self, tmp_path: Path, edit_format: str) -> None:
        (tmp_path / "readme.txt").write_text("Hello, world!")
        (tmp_path / "test.png").write_bytes(PNG_DATA)
        toolset = create_console_toolset(
            image_support=True,
            document_support=True,
            edit_format=edit_format,  # type: ignore[arg-type]
        )
        context = ctx(local(tmp_path))
        text = await call(toolset, "read_file", context, path="readme.txt")
        assert isinstance(text, str) and "Hello, world!" in text
        image = await call(toolset, "read_file", context, path="test.png")
        assert isinstance(image, BinaryContent) and image.media_type == "image/png"

    async def test_works_in_a_document_workspace(self) -> None:
        toolset = create_console_toolset()
        result = await call(
            toolset, "read_file", ctx(document({"/a.txt": "one\ntwo"})), path="a.txt"
        )
        assert result == "     1\tone\n     2\ttwo"


class TestSearch:
    @pytest.fixture(params=["shell", "walk"])
    def workspace(self, request: pytest.FixtureRequest, tmp_path: Path) -> Workspace:
        files = {f"/f{i:03d}.txt": "needle here" for i in range(60)}
        if request.param == "walk":
            return document(files)
        for name, content in files.items():
            (tmp_path / name.lstrip("/")).write_text(content)
        return local(tmp_path)

    async def test_a_long_file_list_is_capped_and_counted(self, workspace: Workspace) -> None:
        out = await call(create_console_toolset(), "grep", ctx(workspace), pattern="needle")
        assert out.startswith("Files containing 'needle':")
        assert "... and 10 more files" in out

    async def test_content_mode_is_capped_and_counted(self, workspace: Workspace) -> None:
        out = await call(
            create_console_toolset(),
            "grep",
            ctx(workspace),
            pattern="needle",
            output_mode="content",
        )
        assert out.startswith("Matches for 'needle':")
        assert "... and 10 more matches" in out

    async def test_glob_finds_files(self, workspace: Workspace) -> None:
        out = await call(create_console_toolset(), "glob", ctx(workspace), pattern="f00*.txt")
        assert out.startswith("Found 10 file(s) matching 'f00*.txt':")


class TestExecute:
    async def test_output_and_exit_codes(self, tmp_path: Path) -> None:
        toolset = create_console_toolset()
        context = ctx(local(tmp_path))
        assert await call(toolset, "execute", context, command="printf hi") == "hi"
        failed = await call(toolset, "execute", context, command="printf 'not found'; exit 127")
        assert "exit code 127" in failed and "not found" in failed

    async def test_long_output_says_it_was_cut(self, tmp_path: Path) -> None:
        out = await call(
            create_console_toolset(),
            "execute",
            ctx(local(tmp_path)),
            command="head -c 200000 /dev/zero",
        )
        assert out.endswith("(output truncated)")

    async def test_a_timeout(self, tmp_path: Path) -> None:
        out = await call(
            create_console_toolset(), "execute", ctx(local(tmp_path)), command="sleep 30", timeout=1
        )
        assert "exit code 124" in out and "Command timed out" in out

    async def test_a_workspace_without_commands_refuses(self) -> None:
        out = await call(create_console_toolset(), "execute", ctx(document()), command="ls")
        assert out.startswith("Command failed (exit code 1):") and "Error:" in out

    async def test_a_read_only_workspace_refuses(self, tmp_path: Path) -> None:
        out = await call(
            create_console_toolset(),
            "execute",
            ctx(ReadOnlyWorkspace(local(tmp_path))),
            command="ls",
        )
        assert "Error:" in out


class TestEveryToolDegradesOnAProviderError:
    """A provider's transport error must fail one tool call, not the agent's run."""

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("ls", {"path": "/"}),
            ("read_file", {"path": "/f.txt"}),
            ("write_file", {"path": "/f.txt", "content": "x"}),
            ("edit_file", {"path": "/f.txt", "old_string": "a", "new_string": "b"}),
            ("glob", {"pattern": "*.py"}),
            ("grep", {"pattern": "todo"}),
            ("execute", {"command": "echo hi"}),
        ],
    )
    async def test_it_is_reported_not_raised(self, tool: str, args: dict[str, object]) -> None:
        context = ctx(Workspace(Raising(RuntimeError("connection reset by peer"))))
        out = await call(create_console_toolset(), tool, context, **args)
        assert isinstance(out, str) and "Error" in out


class TestControlFlowExceptionsPassThrough:
    @pytest.mark.parametrize(
        "error",
        [
            ModelRetry("try a different path"),
            ApprovalRequired(),
            CallDeferred(),
            SkipToolExecution("a canned result"),
            SkipToolValidation({"path": "/"}),
            SkipModelRequest(ModelResponse(parts=[])),
            UserError("the library was misused"),
        ],
    )
    async def test_it_reaches_the_framework(self, error: Exception) -> None:
        context = ctx(Workspace(Raising(error)))
        with pytest.raises(type(error)):
            await call(create_console_toolset(), "ls", context, path="/")


class TestReadsAreRememberedAcrossCalls:
    async def test_an_edit_after_an_outside_change_is_refused(self, tmp_path: Path) -> None:
        """The operations are one instance per workspace, or every read is forgotten."""
        (tmp_path / "a.txt").write_text("old")
        toolset = create_console_toolset()
        context = ctx(local(tmp_path))
        await call(toolset, "read_file", context, path="a.txt")
        (tmp_path / "a.txt").write_text("changed by a formatter")
        out = await call(
            toolset, "edit_file", context, path="a.txt", old_string="old", new_string="new"
        )
        assert "changed since you last read it" in out


class TestWithoutAWorkspace:
    async def test_the_tools_answer_with_the_reason(self) -> None:
        from pydantic_ai import RunContext
        from pydantic_ai.models.test import TestModel
        from pydantic_ai.usage import RunUsage

        context = RunContext(deps=None, model=TestModel(), usage=RunUsage())
        out = await call(create_console_toolset(), "read_file", context, path="a.txt")
        assert "Error" in str(out)
