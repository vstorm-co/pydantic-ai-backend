"""A ruleset's per-path rules, applied to a backend that does not apply its own.

`PermissionGuard` has existed since `LocalBackend` needed it, and only
`LocalBackend` ever used it. So a ruleset handed to `ConsoleCapability` or
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

import asyncio
from typing import Any

import pytest

from pydantic_ai_backends import ConsoleCapability, StateBackend
from pydantic_ai_backends.backends._guard import GuardedBackend, guarding
from pydantic_ai_backends.permissions import (
    SECRETS_PATTERNS,
    SYSTEM_PATTERNS,
    OperationPermissions,
    PermissionRule,
    PermissionRuleset,
)

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


def populated() -> StateBackend:
    backend = StateBackend()
    backend.write("/notes.txt", "ordinary work")
    backend.write("/.env", "OPENAI_API_KEY=sk-live-secret")
    backend.write("/sub/.env", "NESTED=sk-live-secret")
    backend.write("/credentials.txt", "PASSWORD=hunter2")
    backend.write("/etc/passwd", "root:x:0:0")
    return backend


def guarded() -> GuardedBackend:
    wrapped = guarding(populated(), ruleset())
    assert isinstance(wrapped, GuardedBackend)
    return wrapped


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
        backend = StateBackend()
        backend.write("/credentials.txt", "PASSWORD=hunter2")
        backend.write("/notes.txt", "PASSWORD is stored elsewhere")
        wrapped = guarding(backend, ruleset())

        found = await wrapped.grep_raw("PASSWORD")  # type: ignore[union-attr]

        assert [match["path"] for match in found] == ["/notes.txt"]

    @pytest.mark.anyio
    async def test_a_grep_that_answers_with_a_string_is_passed_through(self) -> None:
        """ "No matches" and a backend error are strings, not result sets."""

        class Backend:
            async def read_bytes(self, path: str) -> bytes:
                return b""

            async def grep_raw(self, *args: object, **kwargs: object) -> str:
                return "No matches for 'x'"

        wrapped = GuardedBackend(Backend(), ruleset())  # type: ignore[arg-type]

        assert await wrapped.grep_raw("x") == "No matches for 'x'"

    @pytest.mark.anyio
    async def test_a_listing_is_filtered_by_the_ls_and_glob_rules(self) -> None:
        """Their own rules, not the `read` ones — which matches `LocalBackend`.

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

        assert [entry["path"] for entry in await wrapped.glob_info("**/.env")] == []  # type: ignore[union-attr]
        listed = [entry["path"] for entry in await wrapped.ls_info("/")]  # type: ignore[union-attr]
        assert "/notes.txt" in listed
        assert "/.env" not in listed

    @pytest.mark.anyio
    async def test_a_read_deny_alone_does_not_hide_the_name(self) -> None:
        """Deliberate, and worth pinning so nobody "fixes" it: a name is a weaker
        claim than the contents, and a listing that hid a file `exists` reports
        would send an agent rewriting one it cannot read."""
        wrapped = guarding(populated(), ruleset())

        assert [entry["path"] for entry in await wrapped.glob_info("**/.env")] != []  # type: ignore[union-attr]
        with pytest.raises(PermissionError):
            await wrapped.read("/.env")  # type: ignore[union-attr]

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
        reaches files in ways string inspection cannot see — which is what
        `PermissionGuard.execute_denial_reason` already knew how to do and nothing
        outside `LocalBackend` ever asked it."""

        class Sandbox(StateBackend):
            def execute(self, command: str, timeout: int | None = None) -> str:
                return "ran"

        wrapped = guarding(Sandbox(), ruleset())

        with pytest.raises(PermissionError, match="denied"):
            await wrapped.execute("cat /etc/passwd")  # type: ignore[union-attr]

    @pytest.mark.anyio
    async def test_a_backend_with_no_shell_still_says_so(self) -> None:
        """The toolset asks `hasattr(backend, "execute")` to decide whether to
        answer "Backend does not support command execution". A guard that declared
        `execute` as a method would make that true for a `StateBackend` too, so the
        friendly answer would become a raise - which is why `execute` is reached
        through `__getattr__` rather than defined."""
        wrapped = guarding(populated(), ruleset())

        assert not hasattr(wrapped, "execute")

    @pytest.mark.anyio
    async def test_an_ordinary_command_runs(self) -> None:
        class Sandbox(StateBackend):
            def execute(self, command: str, timeout: int | None = None) -> str:
                return "ran"

        wrapped = guarding(Sandbox(), ruleset())

        assert await wrapped.execute("ls -la") == "ran"  # type: ignore[union-attr]


class TestWhatIsNotWrapped:
    def test_no_ruleset_leaves_the_backend_alone(self) -> None:
        backend = populated()

        assert guarding(backend, None) is backend

    def test_a_backend_that_enforces_its_own_rules_is_left_alone(self) -> None:
        """`LocalBackend` applies a ruleset itself. Wrapping it would check every
        path twice and report the second refusal, which is the same answer with a
        worse message."""

        class SelfEnforcing(StateBackend):
            @property
            def permissions(self) -> PermissionRuleset:
                return ruleset()

        backend = SelfEnforcing()

        assert guarding(backend, ruleset()) is backend

    def test_the_ruleset_is_readable_off_the_wrapper(self) -> None:
        """Which is what stops it being wrapped twice."""
        assert guarded().permissions is not None

    def test_anything_the_protocol_does_not_name_passes_through(self) -> None:
        """A sandbox is more than the protocol: `stop`, `id`, `files`. An explicit
        method list would have dropped them."""

        class Sandbox(StateBackend):
            def stop(self, purge: bool = False) -> str:
                return f"stopped purge={purge}"

        wrapped = guarding(Sandbox(), ruleset())

        assert wrapped.stop(purge=True) == "stopped purge=True"  # type: ignore[union-attr]
        assert wrapped.files == {}  # type: ignore[union-attr]

    def test_something_only_the_raw_backend_has_is_still_reachable(self) -> None:
        """Two places to look, in order. `ensure_async` returns an adapter that
        proxies the protocol and `execute` and nothing else - so `stop` and `id`
        and a backend's own extras are only on the object underneath, and asking
        the adapter first is what keeps `execute` awaitable."""

        class Sandbox(StateBackend):
            @property
            def id(self) -> str:
                return "sbx-1"

        wrapped = guarding(Sandbox(), ruleset())

        assert wrapped.id == "sbx-1"  # type: ignore[union-attr]

    def test_it_says_what_it_wraps(self) -> None:
        assert "GuardedBackend" in repr(guarded())


class TestThroughTheCapability:
    """The assertion that would have caught the original defect.

    Not that the ruleset exists, but that the toolset the model is handed refuses.
    """

    @staticmethod
    async def _call(capability: ConsoleCapability, name: str, **kwargs: Any) -> Any:
        class Ctx:
            pass

        result = capability._toolset.tools[name].function(Ctx(), **kwargs)
        return await result if asyncio.iscoroutine(result) else result

    @pytest.mark.anyio
    async def test_a_credential_is_refused_through_the_registered_tool(self) -> None:
        capability = ConsoleCapability(
            backend=populated(), permissions=ruleset(), include_execute=False
        )

        answer = await self._call(capability, "read_file", path="/etc/passwd")

        assert "Permission denied" in answer

    @pytest.mark.anyio
    async def test_an_ordinary_file_is_still_served(self) -> None:
        capability = ConsoleCapability(
            backend=populated(), permissions=ruleset(), include_execute=False
        )

        answer = await self._call(capability, "read_file", path="/notes.txt")

        assert "ordinary work" in answer

    @pytest.mark.anyio
    async def test_a_capability_with_no_ruleset_refuses_nothing(self) -> None:
        """Every existing caller behaves exactly as before, which is the whole
        reason `guarding` answers with the backend untouched."""
        capability = ConsoleCapability(backend=populated(), include_execute=False)

        answer = await self._call(capability, "read_file", path="/etc/passwd")

        assert "root:x:0:0" in answer

    @pytest.mark.anyio
    async def test_the_backend_from_deps_is_guarded_too(self) -> None:
        """A toolset built with no backend reads `ctx.deps.backend` per call, so
        the guard has to be applied there rather than only to the closure."""
        from pydantic_ai_backends.toolsets.console import create_console_toolset

        toolset = create_console_toolset(permissions=ruleset(), include_execute=False)

        class Deps:
            backend = populated()

        class Ctx:
            deps = Deps()

        answer = await toolset.tools["read_file"].function(Ctx(), path="/etc/passwd")

        assert "Permission denied" in answer
