"""Which failures steer the model with `ModelRetry`, and which are its answer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from pydantic_ai_backends import LocalBackend, StateBackend, create_console_toolset
from pydantic_ai_backends.backends._guard import PERMISSION_DENIED_PREFIX, PermissionGuard
from pydantic_ai_backends.permissions import create_ruleset
from pydantic_ai_backends.toolsets._failures import is_refusal, steer


@dataclass
class _Deps:
    backend: Any


def _ctx(backend: Any, *, retry: int = 0, max_retries: int = 1) -> RunContext[Any]:
    """A context with retry budget left, unless the test says otherwise."""
    return RunContext(
        deps=_Deps(backend=backend),
        model=TestModel(),
        usage=RunUsage(),
        retry=retry,
        max_retries=max_retries,
    )


async def _call(toolset: Any, tool: str, ctx: RunContext[Any], **args: Any) -> Any:
    return await toolset.tools[tool].function_schema.function(ctx, **args)


class TestAMistakeSteersTheModel:
    """A call the model could have got right comes back as `ModelRetry`."""

    async def test_an_old_string_matching_twice_is_a_retry(self):
        backend = StateBackend()
        backend.write("/f.py", "x = 1\nx = 1\n")
        toolset = create_console_toolset()
        ctx = _ctx(backend)
        await _call(toolset, "read_file", ctx, path="/f.py")

        with pytest.raises(ModelRetry) as exc:
            await _call(
                toolset, "edit_file", ctx, path="/f.py", old_string="x = 1", new_string="x = 2"
            )

        assert "found 2 times" in str(exc.value)

    async def test_an_old_string_that_is_absent_is_a_retry(self):
        backend = StateBackend()
        backend.write("/f.py", "x = 1\n")
        toolset = create_console_toolset()
        ctx = _ctx(backend)
        await _call(toolset, "read_file", ctx, path="/f.py")

        with pytest.raises(ModelRetry):
            await _call(
                toolset, "edit_file", ctx, path="/f.py", old_string="y = 2", new_string="y = 3"
            )

    async def test_editing_a_file_that_changed_since_the_read_is_a_retry(self):
        backend = StateBackend()
        backend.write("/f.py", "x = 1\n")
        toolset = create_console_toolset()
        ctx = _ctx(backend)
        await _call(toolset, "read_file", ctx, path="/f.py")
        backend.write("/f.py", "x = 99\n")

        with pytest.raises(ModelRetry) as exc:
            await _call(
                toolset, "edit_file", ctx, path="/f.py", old_string="x = 99", new_string="x = 2"
            )

        assert "changed since you last read it" in str(exc.value)

    async def test_reading_a_file_that_is_not_there_is_a_retry(self):
        toolset = create_console_toolset()

        with pytest.raises(ModelRetry) as exc:
            await _call(toolset, "read_file", _ctx(StateBackend()), path="/nope.txt")

        assert "not found" in str(exc.value)

    async def test_a_hashline_edit_against_a_stale_hash_is_a_retry(self):
        backend = StateBackend()
        backend.write("/f.py", "x = 1\n")
        toolset = create_console_toolset(edit_format="hashline")

        with pytest.raises(ModelRetry):
            await _call(
                toolset,
                "hashline_edit",
                _ctx(backend),
                path="/f.py",
                start_line=1,
                start_hash="zz",
                new_content="x = 2",
            )

    async def test_hashline_reading_a_missing_file_is_a_retry(self):
        toolset = create_console_toolset(edit_format="hashline")

        with pytest.raises(ModelRetry) as exc:
            await _call(toolset, "read_file", _ctx(StateBackend()), path="/nope.txt")

        assert "glob" in str(exc.value)


class TestTheLastAttemptNeverKillsTheRun:
    """`ModelRetry` past a tool's budget ends the whole run, so the floor is a string."""

    async def test_the_message_is_returned_rather_than_raised(self):
        backend = StateBackend()
        backend.write("/f.py", "x = 1\nx = 1\n")
        toolset = create_console_toolset()
        ctx = _ctx(backend, retry=1, max_retries=1)
        await _call(toolset, "read_file", ctx, path="/f.py")

        out = await _call(
            toolset, "edit_file", ctx, path="/f.py", old_string="x = 1", new_string="x = 2"
        )

        assert isinstance(out, str)
        assert "found 2 times" in out

    async def test_a_toolset_with_no_retries_behaves_as_it_did_before(self):
        """`max_retries=0` is every failure as a returned string, as ever."""
        toolset = create_console_toolset(max_retries=0)

        out = await _call(
            toolset, "read_file", _ctx(StateBackend(), max_retries=0), path="/nope.txt"
        )

        assert isinstance(out, str)
        assert out.startswith("Error:")


class TestARefusalIsNotAMistake:
    """A retry prompt on a denial invites the model to find a way around it."""

    async def test_a_denied_write_is_returned_not_retried(self, tmp_path: Path):
        """`.env` is denied by the default secrets rules."""
        backend = LocalBackend(root_dir=tmp_path)
        toolset = create_console_toolset(permissions=create_ruleset(allow_write=True))

        out = await _call(toolset, "write_file", _ctx(backend), path=".env", content="TOKEN=x")

        assert isinstance(out, str)
        assert PERMISSION_DENIED_PREFIX in out

    def test_the_refusal_prefix_is_the_guard_s_own(self, tmp_path: Path):
        """One string, two modules — a rename that split them would be silent."""
        guard = PermissionGuard(create_ruleset(), root=tmp_path)

        reason = guard.denial_reason("read", ".env")

        assert reason is not None
        assert is_refusal(reason)

    def test_a_mistake_is_not_a_refusal(self):
        assert not is_refusal("Error: File '/x.txt' not found")


class TestAResultIsNotAMistake:
    """What the model must reason about rather than call again differently."""

    async def test_a_command_that_exits_non_zero_is_a_result(self, tmp_path: Path):
        toolset = create_console_toolset()

        out = await _call(
            toolset, "execute", _ctx(LocalBackend(root_dir=tmp_path)), command="exit 3"
        )

        assert "exit code 3" in out

    async def test_finding_nothing_is_an_answer(self, tmp_path: Path):
        toolset = create_console_toolset()

        out = await _call(
            toolset, "grep", _ctx(LocalBackend(root_dir=tmp_path)), pattern="nothing-here"
        )

        assert "No matches" in out

    async def test_a_transport_failure_is_reported_not_retried(self):
        """A dropped socket is not something different arguments would fix."""

        class Hostile(StateBackend):
            def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
                raise OSError("connection reset by peer")

        toolset = create_console_toolset()

        out = await _call(toolset, "read_file", _ctx(Hostile()), path="/f.txt")

        assert "connection reset by peer" in out


class TestSteer:
    def test_it_raises_while_a_retry_remains(self):
        with pytest.raises(ModelRetry):
            steer(_ctx(StateBackend()), "Error: try something else")

    def test_it_returns_on_the_last_attempt(self):
        assert steer(_ctx(StateBackend(), retry=1), "Error: out of budget") == (
            "Error: out of budget"
        )
