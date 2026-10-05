"""Tests for the console tools' own rendering and branching.

Tools are invoked through the registered function, which is what an agent calls,
against a `StateBackend` document as the workspace.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.workspaces import ReadOnlyWorkspace, Workspace

from pydantic_ai_backends import create_console_toolset
from tests.support import call, ctx, document


async def _call(workspace: Workspace, tool: str, toolset: Any = None, **kwargs: Any) -> str:
    return await call(toolset or create_console_toolset(), tool, ctx(workspace), **kwargs)


@pytest.fixture
def backend() -> Workspace:
    return document({"/notes.md": "one\ntwo\nthree\n"})


class TestLsRendering:
    async def test_an_empty_directory_says_so(self, backend: Workspace):
        out = await _call(backend, "ls", path="/nowhere")

        assert "empty or does not exist" in out

    async def test_directories_get_a_slash_and_files_their_size(self, backend: Workspace):
        await backend.write_bytes("/src/app.py", b"x")

        out = await _call(backend, "ls", path="/")

        assert "src/" in out
        assert "notes.md" in out
        assert "bytes)" in out


class TestGlobRendering:
    async def test_no_matches_says_so(self, backend: Workspace):
        out = await _call(backend, "glob", pattern="*.rs", path="/")

        assert "No files matching" in out

    async def test_matches_are_counted(self, backend: Workspace):
        out = await _call(backend, "glob", pattern="*.md", path="/")

        assert "Found 1 file(s)" in out
        assert "notes.md" in out

    async def test_a_long_list_is_capped_and_the_rest_counted(self, backend: Workspace):
        from pydantic_ai_backends.toolsets.console import GLOB_RESULT_LIMIT

        for index in range(GLOB_RESULT_LIMIT + 5):
            await backend.write_bytes(f"/f{index:04d}.py", b"x")

        out = await _call(backend, "glob", pattern="*.py", path="/")

        assert "... and 5 more" in out


class TestGrepRendering:
    async def test_no_matches_says_so(self, backend: Workspace):
        out = await _call(backend, "grep", pattern="absent")

        assert "No matches" in out

    async def test_count_mode_reports_a_total(self, backend: Workspace):
        out = await _call(backend, "grep", pattern="two", output_mode="count")

        assert "Found 1 match(es)" in out

    async def test_an_error_string_is_passed_through(self, backend: Workspace):
        out = await _call(backend, "grep", pattern="(")

        assert out.startswith("Error: Invalid regex pattern")


class TestWriteAndEditRendering:
    async def test_a_write_error_is_reported(self, backend: Workspace):
        out = await _call(ReadOnlyWorkspace(backend), "write_file", path="/x.txt", content="y")

        assert "read-only" in out

    async def test_an_edit_error_is_reported(self, backend: Workspace):
        out = await _call(
            backend, "edit_file", path="/notes.md", old_string="absent", new_string="x"
        )

        assert out.startswith("Error: ")

    async def test_a_successful_edit_reports_the_path(self, backend: Workspace):
        out = await _call(backend, "edit_file", path="/notes.md", old_string="one", new_string="1")

        assert "notes.md" in out
        assert (await backend.read_bytes("/notes.md")).startswith(b"1\n")


class TestHashlineVariants:
    """Registered only under `edit_format="hashline"`, so otherwise unmeasured."""

    hashline = create_console_toolset(edit_format="hashline")

    async def test_read_file_returns_tagged_lines(self, backend: Workspace):
        out = await _call(backend, toolset=self.hashline, tool="read_file", path="/notes.md")

        assert "one" in out
        # Hashline tags each line so an edit can prove it saw the current text.
        assert any(char.isdigit() for char in out)

    async def test_reading_a_missing_file_says_so(self, backend: Workspace):
        out = await _call(backend, toolset=self.hashline, tool="read_file", path="/gone.md")

        assert "not found" in out

    async def test_an_edit_round_trips(self, backend: Workspace):
        shown = await _call(backend, "read_file", self.hashline, path="/notes.md")
        # Rows read `1:f9|one` — line number, content hash, then the text.
        number, _, rest = shown.strip().splitlines()[0].partition(":")
        tag, _, _text = rest.partition("|")

        out = await _call(
            backend,
            "hashline_edit",
            self.hashline,
            path="/notes.md",
            start_line=int(number),
            start_hash=tag,
            new_content="ONE",
        )

        assert out.startswith("Edited")
        assert (await backend.read_bytes("/notes.md")).decode().startswith("ONE")

    async def test_editing_a_missing_file_says_so(self, backend: Workspace):
        out = await _call(
            backend,
            "hashline_edit",
            self.hashline,
            path="/gone.md",
            start_line=1,
            start_hash="abcd",
            new_content="x",
        )

        assert "not found" in out

    async def test_a_stale_hash_is_refused(self, backend: Workspace):
        out = await _call(
            backend,
            "hashline_edit",
            self.hashline,
            path="/notes.md",
            start_line=1,
            start_hash="0000",
            new_content="x",
        )

        assert out.startswith("Error: ")

    async def test_a_failing_write_back_is_reported(self, backend: Workspace):
        read_only = ReadOnlyWorkspace(backend)
        shown = await _call(read_only, "read_file", self.hashline, path="/notes.md")
        number, _, rest = shown.strip().splitlines()[0].partition(":")
        tag, _, _text = rest.partition("|")

        out = await _call(
            read_only,
            "hashline_edit",
            self.hashline,
            path="/notes.md",
            start_line=int(number),
            start_hash=tag,
            new_content="ONE",
        )

        assert "read-only" in out


class TestReadTracking:
    async def test_a_failed_read_is_not_recorded_as_seen(self, backend: Workspace):
        """Recording a failure would let a later edit claim it saw the file."""

        out = await _call(backend, "read_file", path="/gone.txt")

        assert out.startswith("Error")


class TestAFinishedWorkspaceIsReleased:
    """A long-lived toolset must not keep every workspace it ever worked in."""

    async def test_it_is_collectable_while_the_toolset_lives(self):
        import gc
        import weakref

        toolset = create_console_toolset()
        refs = []
        for _ in range(3):
            workspace = document({"/notes.md": "one\n"})
            await _call(workspace, "read_file", toolset, path="/notes.md")
            refs.append(weakref.ref(workspace))
        del workspace
        gc.collect()

        assert [ref() for ref in refs] == [None, None, None]

    async def test_what_was_read_still_guards_the_next_edit(self):
        toolset = create_console_toolset()
        workspace = document({"/notes.md": "one\n"})
        await _call(workspace, "read_file", toolset, path="/notes.md")
        await workspace.write_bytes("/notes.md", b"changed\n")

        out = await _call(
            workspace, "edit_file", toolset, path="/notes.md", old_string="changed", new_string="x"
        )

        assert "changed since you last read it" in out
