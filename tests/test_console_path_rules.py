"""A ruleset's per-path rules, applied to the console tools' workspace operations.

`PermissionGuard` once served only the local backend. So a ruleset handed to
`ConsoleCapability` or
`create_console_toolset` reached exactly two things — `requires_approval` for the
write and execute approval flags, and `_denied_tools`, which drops a tool whose
*operation* defaults to `"deny"`. Nothing read `OperationPermissions.rules`.

With every operation left at `default="allow"` and the patterns in `rules` — the
shape a caller writes when they want "allow the workspace, deny credentials and
the system tree" — that was no enforcement at all. Which is worse than rejecting
the ruleset, because the result looks like a working boundary.

`tests/test_console_permissions.py` covers what a ruleset does at construction
time: which tools exist, which need approval. This file covers what it does per
call, with a path in hand.
"""

from __future__ import annotations

from typing import Any

import pytest

from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import (
    SECRETS_PATTERNS,
    SYSTEM_PATTERNS,
    OperationPermissions,
    PermissionRule,
    PermissionRuleset,
)
from pydantic_ai_backends.toolsets._guard import GuardedOps, guarding
from pydantic_ai_backends.toolsets._workspace import WorkspaceOps
from pydantic_ai_backends.types import ExecuteResponse
from tests.support import call, ctx, document

OFF_LIMITS = (*SECRETS_PATTERNS, *SYSTEM_PATTERNS)


def ruleset() -> PermissionRuleset:
    """Allow everything, deny the credentials and the system tree.

    The shape this is all about: an operation default of `"allow"` with the
    interesting part in `rules`.
    """
    deny = [
        PermissionRule(pattern=pattern, action="deny", description="Off limits")
        for pattern in OFF_LIMITS
    ]
    return PermissionRuleset(
        default="allow",
        read=OperationPermissions(default="allow", rules=deny),
        write=OperationPermissions(default="allow", rules=deny),
        edit=OperationPermissions(default="allow", rules=deny),
    )


FILES = {
    "/notes.txt": "ordinary work",
    "/.env": "OPENAI_API_KEY=sk-live-secret",
    "/sub/.env": "NESTED=sk-live-secret",
    "/credentials.txt": "PASSWORD=hunter2",
    "/etc/passwd": "root:x:0:0",
}


def populated() -> WorkspaceOps:
    return WorkspaceOps(document(FILES))


def guarded() -> GuardedOps:
    wrapped = guarding(populated(), ruleset())
    assert isinstance(wrapped, GuardedOps)
    return wrapped


class _Shell(WorkspaceOps):
    """Operations whose commands only report that they ran."""

    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        return ExecuteResponse(output="ran", exit_code=0)


class TestContentAndMutation:
    """The five ways bytes leave a file or change one."""

    @pytest.mark.parametrize("path", ["/.env", "/sub/.env", "/credentials.txt", "/etc/passwd"])
    @pytest.mark.anyio
    async def test_reading_an_off_limits_path_is_refused(self, path: str) -> None:
        with pytest.raises(PermissionError, match="Off limits"):
            await guarded().read(path)

    @pytest.mark.parametrize("path", ["/.env", "/etc/passwd"])
    @pytest.mark.anyio
    async def test_reading_one_as_bytes_is_refused_too(self, path: str) -> None:
        """`read_file` on an image goes through `read_bytes`, so a guard on `read`
        alone leaves the same file readable by asking differently."""
        with pytest.raises(PermissionError, match="Off limits"):
            await guarded().read_bytes(path)

    @pytest.mark.anyio
    async def test_the_workspaces_own_files_still_read(self) -> None:
        assert "ordinary work" in await guarded().read("/notes.txt")
        assert await guarded().read_bytes("/notes.txt") == b"ordinary work"

    @pytest.mark.anyio
    async def test_writing_over_a_credential_is_refused_as_a_value(self) -> None:
        """An error result rather than a raise: it is what the model reads and can
        act on, and the protocol has a place for it."""
        result = await guarded().write("/sub/.env", "x")

        assert result.error is not None
        assert "Off limits" in result.error

    @pytest.mark.anyio
    async def test_an_ordinary_write_still_lands(self) -> None:
        assert (await guarded().write("/report.csv", "a,b")).error is None

    @pytest.mark.anyio
    async def test_editing_a_credential_is_refused_as_a_value(self) -> None:
        result = await guarded().edit("/.env", "sk-live-secret", "x")

        assert result.error is not None

    @pytest.mark.anyio
    async def test_an_ordinary_edit_still_applies(self) -> None:
        assert (await guarded().edit("/notes.txt", "ordinary", "usual")).error is None


class TestSearchAndListing:
    @pytest.mark.anyio
    async def test_grep_does_not_return_a_line_from_a_file_it_may_not_read(self) -> None:
        """The one a guard on `read` alone misses entirely.

        `GrepMatch` carries the matching *line*, so an unfiltered grep hands over
        the contents of exactly the files the rules protect — by a different tool,
        with no refusal anywhere.
        """
        ops = WorkspaceOps(
            document(
                {
                    "/credentials.txt": "PASSWORD=hunter2",
                    "/notes.txt": "PASSWORD is stored elsewhere",
                }
            )
        )
        found = await guarding(ops, ruleset()).grep_raw("PASSWORD")

        assert [match["path"] for match in found] == ["/notes.txt"]

    @pytest.mark.anyio
    async def test_a_grep_that_answers_with_a_string_is_passed_through(self) -> None:
        """ "No matches" and a backend error are strings, not result sets."""

        wrapped = GuardedOps(populated(), ruleset())

        assert await wrapped.grep_raw("(unclosed") == (
            "Error: Invalid regex pattern: missing ), unterminated subpattern at position 0"
        )

    @pytest.mark.anyio
    async def test_a_listing_is_filtered_by_the_ls_and_glob_rules(self) -> None:
        """Their own rules, not the `read` ones.

        Hiding entries rather than refusing, because a ruleset defaulting to "ask"
        would otherwise blank out every listing.
        """
        deny_listing = [
            PermissionRule(pattern=pattern, action="deny", description="Off limits")
            for pattern in OFF_LIMITS
        ]
        rules = PermissionRuleset(
            default="allow",
            ls=OperationPermissions(default="allow", rules=deny_listing),
            glob=OperationPermissions(default="allow", rules=deny_listing),
        )
        wrapped = guarding(populated(), rules)

        assert [entry["path"] for entry in await wrapped.glob_info("**/.env")] == []
        listed = [entry["path"] for entry in await wrapped.ls_info("/")]
        assert "/notes.txt" in listed
        assert "/.env" not in listed

    @pytest.mark.anyio
    async def test_a_read_deny_alone_does_not_hide_the_name(self) -> None:
        """Deliberate, and worth pinning so nobody "fixes" it: a name is a weaker
        claim than the contents, and a listing that hid a file `exists` reports
        would send an agent rewriting one it cannot read."""
        wrapped = guarding(populated(), ruleset())

        assert [entry["path"] for entry in await wrapped.glob_info("**/.env")] != []
        with pytest.raises(PermissionError):
            await wrapped.read("/.env")

    @pytest.mark.anyio
    async def test_exists_is_not_filtered(self) -> None:
        """Whether a path is there is a weaker claim than what is in it, and a
        listing that lied about it would make an agent rewrite a file it cannot
        see."""
        assert await guarded().exists("/etc/passwd") is True


class TestExecute:
    @pytest.mark.anyio
    async def test_a_command_naming_a_denied_path_is_refused(self) -> None:
        """The obvious bypass. Defence in depth rather than a boundary — a shell
        reaches files in ways string inspection cannot see."""
        wrapped = guarding(_Shell(document()), ruleset())

        with pytest.raises(PermissionError, match="denied"):
            await wrapped.execute("cat /etc/passwd")

    @pytest.mark.anyio
    async def test_an_ordinary_command_runs(self) -> None:
        wrapped = guarding(_Shell(document()), ruleset())

        assert (await wrapped.execute("ls -la")).output == "ran"


class TestWhatIsNotWrapped:
    def test_no_ruleset_leaves_the_operations_alone(self) -> None:
        ops = populated()

        assert guarding(ops, None) is ops


class TestThroughTheCapability:
    """The assertion that would have caught the original defect.

    Not that the ruleset exists, but that the toolset the model is handed refuses.
    """

    @staticmethod
    async def _read(capability: ConsoleCapability, path: str) -> Any:
        return await call(capability._toolset, "read_file", ctx(document(FILES)), path=path)

    @pytest.mark.anyio
    async def test_a_credential_is_refused_through_the_registered_tool(self) -> None:
        capability = ConsoleCapability(permissions=ruleset(), include_execute=False)

        assert "Permission denied" in await self._read(capability, "/etc/passwd")

    @pytest.mark.anyio
    async def test_an_ordinary_file_is_still_served(self) -> None:
        capability = ConsoleCapability(permissions=ruleset(), include_execute=False)

        assert "ordinary work" in await self._read(capability, "/notes.txt")

    @pytest.mark.anyio
    async def test_a_capability_with_no_ruleset_refuses_nothing(self) -> None:
        capability = ConsoleCapability(include_execute=False)

        assert "root:x:0:0" in await self._read(capability, "/etc/passwd")
