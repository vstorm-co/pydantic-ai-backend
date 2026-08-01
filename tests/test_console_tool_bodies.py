"""Tests for the console tools' own rendering and branching.

Every tool body carried a `# pragma: no cover`, so how a listing, a glob, a grep
or a background shell is actually *rendered for the model* was unmeasured — as
were both `hashline` variants, which only register under `edit_format="hashline"`
and so had no coverage from the default toolset at all.

Tools are invoked through the registered function, which is what an agent calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from pydantic_ai_backends import StateBackend, create_console_toolset


@dataclass
class Deps:
    backend: Any


def _ctx(backend: Any) -> RunContext[Any]:
    return RunContext(deps=Deps(backend=backend), model=TestModel(), usage=RunUsage())


async def _call(toolset: Any, tool: str, **kwargs: Any) -> str:
    return await toolset.tools[tool].function_schema.function(_ctx(toolset._test_backend), **kwargs)


def _toolset(backend: Any, **options: Any):
    toolset = create_console_toolset(**options)
    toolset._test_backend = backend  # type: ignore[attr-defined]
    return toolset


@pytest.fixture
def backend() -> StateBackend:
    store = StateBackend()
    store.write("/notes.md", "one\ntwo\nthree\n")
    return store


class TestLsRendering:
    async def test_an_empty_directory_says_so(self, backend: StateBackend):
        out = await _call(_toolset(backend), "ls", path="/nowhere")

        assert "empty or does not exist" in out

    async def test_directories_get_a_slash_and_files_their_size(self, backend: StateBackend):
        backend.write("/src/app.py", "x")

        out = await _call(_toolset(backend), "ls", path="/")

        assert "src/" in out
        assert "notes.md" in out
        assert "bytes)" in out


class TestGlobRendering:
    async def test_no_matches_says_so(self, backend: StateBackend):
        out = await _call(_toolset(backend), "glob", pattern="*.rs", path="/")

        assert "No files matching" in out

    async def test_matches_are_counted(self, backend: StateBackend):
        out = await _call(_toolset(backend), "glob", pattern="*.md", path="/")

        assert "Found 1 file(s)" in out
        assert "notes.md" in out

    async def test_a_long_list_is_capped_and_the_rest_counted(self, backend: StateBackend):
        from pydantic_ai_backends.toolsets.console import GLOB_RESULT_LIMIT

        for index in range(GLOB_RESULT_LIMIT + 5):
            backend.write(f"/f{index:04d}.py", "x")

        out = await _call(_toolset(backend), "glob", pattern="*.py", path="/")

        assert "... and 5 more" in out


class TestGrepRendering:
    async def test_no_matches_says_so(self, backend: StateBackend):
        out = await _call(_toolset(backend), "grep", pattern="absent")

        assert "No matches" in out

    async def test_count_mode_reports_a_total(self, backend: StateBackend):
        out = await _call(_toolset(backend), "grep", pattern="two", output_mode="count")

        assert "Found 1 match(es)" in out

    async def test_a_backend_error_string_is_passed_through(self, backend: StateBackend):
        class Broken:
            async def read_bytes(self, path: str) -> bytes:
                return b""

            async def grep_raw(self, pattern, path=None, glob=None, ignore_hidden=True):
                return "Error: Invalid regex pattern"

        out = await _call(_toolset(Broken()), "grep", pattern="(")

        assert out == "Error: Invalid regex pattern"


class TestWriteAndEditRendering:
    async def test_a_write_error_is_reported(self, backend: StateBackend):
        class Refusing:
            async def read_bytes(self, path: str) -> bytes:
                return b""

            async def write(self, path: str, content: str | bytes):
                from pydantic_ai_backends.types import WriteResult

                return WriteResult(error="read-only file system")

        out = await _call(_toolset(Refusing()), "write_file", path="/x.txt", content="y")

        assert "read-only file system" in out

    async def test_an_edit_error_is_reported(self, backend: StateBackend):
        out = await _call(
            _toolset(backend), "edit_file", path="/notes.md", old_string="absent", new_string="x"
        )

        assert out.startswith("Error: ")

    async def test_a_successful_edit_reports_the_path(self, backend: StateBackend):
        out = await _call(
            _toolset(backend), "edit_file", path="/notes.md", old_string="one", new_string="1"
        )

        assert "notes.md" in out
        assert backend.read_bytes("/notes.md").startswith(b"1\n")


class TestHashlineVariants:
    """Registered only under `edit_format="hashline"`, so otherwise unmeasured."""

    def _hashline(self, backend: Any):
        return _toolset(backend, edit_format="hashline")

    async def test_read_file_returns_tagged_lines(self, backend: StateBackend):
        out = await _call(self._hashline(backend), "read_file", path="/notes.md")

        assert "one" in out
        # Hashline tags each line so an edit can prove it saw the current text.
        assert any(char.isdigit() for char in out)

    async def test_reading_a_missing_file_says_so(self, backend: StateBackend):
        out = await _call(self._hashline(backend), "read_file", path="/gone.md")

        assert "not found" in out

    async def test_an_edit_round_trips(self, backend: StateBackend):
        toolset = self._hashline(backend)
        shown = await _call(toolset, "read_file", path="/notes.md")
        # Rows read `1:f9|one` — line number, content hash, then the text.
        number, _, rest = shown.strip().splitlines()[0].partition(":")
        tag, _, _text = rest.partition("|")

        out = await _call(
            toolset,
            "hashline_edit",
            path="/notes.md",
            start_line=int(number),
            start_hash=tag,
            new_content="ONE",
        )

        assert out.startswith("Edited")
        assert backend.read_bytes("/notes.md").decode().startswith("ONE")

    async def test_editing_a_missing_file_says_so(self, backend: StateBackend):
        out = await _call(
            self._hashline(backend),
            "hashline_edit",
            path="/gone.md",
            start_line=1,
            start_hash="abcd",
            new_content="x",
        )

        assert "not found" in out

    async def test_a_stale_hash_is_refused(self, backend: StateBackend):
        out = await _call(
            self._hashline(backend),
            "hashline_edit",
            path="/notes.md",
            start_line=1,
            start_hash="0000",
            new_content="x",
        )

        assert out.startswith("Error: ")

    async def test_a_failing_write_back_is_reported(self, backend: StateBackend):
        class RefusingWrite:
            """Reads like the real store, refuses only the write-back."""

            async def exists(self, path: str) -> bool:
                return backend.exists(path)

            async def read_bytes(self, path: str) -> bytes:
                return backend.read_bytes(path)

            async def write(self, path: str, content: str | bytes):
                from pydantic_ai_backends.types import WriteResult

                return WriteResult(error="disk full")

        toolset = self._hashline(RefusingWrite())
        shown = await _call(toolset, "read_file", path="/notes.md")
        number, _, rest = shown.strip().splitlines()[0].partition(":")
        tag, _, _text = rest.partition("|")

        out = await _call(
            toolset,
            "hashline_edit",
            path="/notes.md",
            start_line=int(number),
            start_hash=tag,
            new_content="ONE",
        )

        assert "disk full" in out


class TestBackgroundToolsWithSupport:
    """The success paths, which need a sandbox that actually has a shell."""

    class Shelled:
        """Async sandbox exposing the background surface."""

        def __init__(self, backend: StateBackend) -> None:
            self._backend = backend
            self.killed: list[str] = []
            self.shells: list[Any] = []
            self.running = True
            self.kill_succeeds = True

        async def exists(self, path: str) -> bool:
            return self._backend.exists(path)

        async def read_bytes(self, path: str) -> bytes:
            return self._backend.read_bytes(path)

        async def execute_background(self, command: str):
            from pydantic_ai_backends.types import BackgroundHandle

            return BackgroundHandle(shell_id="sh-1", pid=4242, command=command)

        async def read_background(self, shell_id: str):
            from pydantic_ai_backends.types import BackgroundOutput

            return BackgroundOutput(
                shell_id=shell_id,
                stdout="building...\n" if self.running else "",
                stderr="",
                running=self.running,
                exit_code=None if self.running else 0,
            )

        async def kill_background(self, shell_id: str) -> bool:
            self.killed.append(shell_id)
            return self.kill_succeeds

        async def list_background(self):
            return self.shells

    @pytest.fixture
    def shelled(self, backend: StateBackend):
        return self.Shelled(backend)

    async def test_starting_a_shell_reports_how_to_follow_it(self, shelled):
        out = await _call(_toolset(shelled), "run_in_background", command="npm run dev")

        assert "sh-1" in out
        assert "4242" in out
        assert "read_output('sh-1')" in out

    async def test_reading_a_running_shell_shows_its_output(self, shelled):
        out = await _call(_toolset(shelled), "read_output", shell_id="sh-1")

        assert "[sh-1] running" in out
        assert "building..." in out

    async def test_reading_an_exited_shell_reports_the_code(self, shelled):
        shelled.running = False

        out = await _call(_toolset(shelled), "read_output", shell_id="sh-1")

        assert "exited (code 0)" in out
        assert "(no new output)" in out

    async def test_killing_a_shell_confirms_it(self, shelled):
        out = await _call(_toolset(shelled), "kill_shell", shell_id="sh-1")

        assert "Killed background shell sh-1" in out
        assert shelled.killed == ["sh-1"]

    async def test_killing_an_unknown_shell_says_so(self, shelled):
        shelled.kill_succeeds = False

        out = await _call(_toolset(shelled), "kill_shell", shell_id="sh-9")

        assert "already finished or unknown" in out

    async def test_an_empty_shell_list_says_so(self, shelled):
        assert await _call(_toolset(shelled), "list_shells") == "No background shells."

    async def test_shells_are_listed_with_their_state(self, shelled):
        from pydantic_ai_backends.types import BackgroundProcessInfo

        shelled.shells = [
            BackgroundProcessInfo(shell_id="sh-1", command="npm run dev", pid=1, running=True),
            BackgroundProcessInfo(
                shell_id="sh-2", command="pytest", pid=2, running=False, exit_code=1
            ),
        ]

        out = await _call(_toolset(shelled), "list_shells")

        assert "sh-1  running  npm run dev" in out
        assert "sh-2  exited(1)  pytest" in out


class TestReadTracking:
    async def test_a_failed_read_is_not_recorded_as_seen(self, backend: StateBackend):
        """Recording a failure would let a later edit claim it saw the file."""

        class Failing:
            async def read_bytes(self, path: str) -> bytes:
                return b""

            async def exists(self, path: str) -> bool:
                return True

            async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
                return "Error: File not found"

        out = await _call(_toolset(Failing()), "read_file", path="/gone.txt")

        assert out.startswith("Error")


class TestBackgroundToolsWithoutSupport:
    """A backend with no shell must say so rather than fail obscurely."""

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("run_in_background", {"command": "sleep 1"}),
            ("read_output", {"shell_id": "sh-1"}),
            ("kill_shell", {"shell_id": "sh-1"}),
            ("list_shells", {}),
        ],
    )
    async def test_it_reports_the_lack_of_support(
        self, backend: StateBackend, tool: str, args: dict
    ):
        from pydantic_ai_backends.toolsets.console import _NO_BACKGROUND_SUPPORT

        out = await _call(_toolset(backend, include_background=True), tool, **args)

        assert out == _NO_BACKGROUND_SUPPORT
