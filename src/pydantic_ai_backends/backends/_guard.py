"""Applying a permission ruleset from code that cannot prompt the user.

A ruleset can resolve an operation to "ask", but file operations on a sync
backend have nobody to ask. This module holds that reconciliation in one place:
which operations refuse outright, which quietly hide results, and how an
`execute` is inspected for the paths it would touch.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai_backends.permissions.checker import PermissionAskError, PermissionChecker
from pydantic_ai_backends.types import EditResult, WriteResult

if TYPE_CHECKING:
    from typing import Any

    from pydantic_ai_backends.permissions.checker import AskCallback, AskFallback
    from pydantic_ai_backends.permissions.types import PermissionOperation, PermissionRuleset
    from pydantic_ai_backends.protocol import AsyncBackendProtocol, BackendProtocol
    from pydantic_ai_backends.types import FileInfo, GrepMatch

GUARDED_COMMAND_OPERATIONS: tuple[PermissionOperation, ...] = ("read", "write")
"""Operations whose deny rules also block a command that names such a path."""

PERMISSION_DENIED_PREFIX = "Permission denied"
"""How every refusal this module returns rather than raises begins.

A constant because the console toolset has to tell a refusal from a mistake: a
mistake is handed back as `ModelRetry` so the model tries again, and a refusal
must not be, since a retry prompt invites it to look for a way around the rule.
`toolsets/_failures.py` is the reader.
"""


class PermissionGuard:
    """Decides synchronously whether one operation may proceed.

    Args:
        ruleset: Rules to enforce.
        root: Directory commands run in, used to resolve their path arguments.
        ask_callback: Async approval callback, passed through to the checker.
        ask_fallback: What an unanswerable "ask" does — `"deny"` refuses,
            `"error"` raises :class:`PermissionAskError`.
    """

    def __init__(
        self,
        ruleset: PermissionRuleset,
        root: Path,
        ask_callback: AskCallback | None = None,
        ask_fallback: AskFallback = "error",
    ) -> None:
        self._checker = PermissionChecker(
            ruleset=ruleset,
            ask_callback=ask_callback,
            ask_fallback=ask_fallback,
        )
        self._root = root
        self._ask_fallback = ask_fallback

    @property
    def checker(self) -> PermissionChecker:
        """The underlying checker."""
        return self._checker

    def denial_reason(self, operation: PermissionOperation, target: str) -> str | None:
        """Why `operation` on `target` is refused, or `None` when it may proceed.

        Raises:
            PermissionAskError: If the rules require approval and
                `ask_fallback="error"`.
        """
        action = self._checker.check_sync(operation, target)
        if action == "allow":
            return None

        if action == "deny":
            rule = self._checker.find_matching_rule(operation, target)
            if rule and rule.description:
                return f"{PERMISSION_DENIED_PREFIX}: {rule.description}"
            return f"{PERMISSION_DENIED_PREFIX} for {operation} on '{target}'"

        if self._ask_fallback == "deny":
            return f"{PERMISSION_DENIED_PREFIX} for {operation} on '{target}' (approval required)"
        raise PermissionAskError(operation, target, "Approval required but no callback")

    def is_denied(self, operation: PermissionOperation, target: str) -> bool:
        """Whether the rules explicitly deny this target.

        Used by `ls`, `glob` and `grep`, which hide denied entries and treat
        "ask" as visible — otherwise a ruleset defaulting to "ask" would blank
        out every listing.
        """
        return self._checker.check_sync(operation, target) == "deny"

    def hides_from_grep(self, path: str) -> bool:
        """Whether `path` must not contribute grep matches.

        A read deny counts as well as a grep deny, because a match carries file
        content and would otherwise leak it through search results.
        """
        return self.is_denied("grep", path) or self.is_denied("read", path)

    def execute_denial_reason(self, command: str) -> str | None:
        """Why `command` is refused, checking its rules and its path arguments.

        Beyond the command-pattern rules, path-looking tokens are resolved and
        refused when one hits a read or write deny rule, so the obvious bypass
        (`cat restricted/secret.txt`) is caught.

        This is defense in depth, not a boundary — a shell can reach a file in
        ways string inspection cannot see. For enforced isolation use a
        sandboxed backend such as `DockerSandbox`.
        """
        reason = self.denial_reason("execute", command)
        if reason is not None:
            return reason

        for target in sorted(self.command_path_targets(command)):
            for operation in GUARDED_COMMAND_OPERATIONS:
                if self.is_denied(operation, target):
                    return (
                        f"{PERMISSION_DENIED_PREFIX}: command references '{target}', "
                        f"which is denied for {operation}"
                    )
        return None

    def command_path_targets(self, command: str) -> set[str]:
        """Filesystem paths a command plausibly references.

        Tokens (and the value half of `--flag=value`) are expanded and resolved
        against the root the command runs in. Tokens that are not paths resolve
        to something harmless that matches no rule.
        """
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            # Unbalanced quotes: fall back to whitespace splitting so a
            # malformed command cannot dodge the guard entirely.
            tokens = [token.strip("\"'") for token in command.split()]

        candidates = {token for token in tokens if token}
        candidates |= {token.split("=", 1)[1] for token in tokens if "=" in token}

        targets: set[str] = set()
        for candidate in candidates:
            if not candidate:
                continue
            expanded = os.path.expanduser(candidate)
            path = Path(expanded)
            absolute = path if path.is_absolute() else self._root / expanded
            # Both forms, because a rule is written against the path a person
            # types and `resolve()` answers with the path the filesystem means.
            # On macOS `/etc` is a symlink to `/private/etc`, so a rule reading
            # `/etc/**` matched nothing once resolved - and any symlinked
            # directory does the same on any platform. Matching both can only
            # deny more, never less, which is the right direction for a check
            # that is defence in depth.
            targets.add(str(absolute))
            targets.add(str(absolute.resolve()))
        return targets


class GuardedBackend:
    """Any backend, with a ruleset's per-path rules actually applied.

    `PermissionGuard` has existed since `LocalBackend` needed it, and only
    `LocalBackend` ever used it — so a ruleset handed to `ConsoleCapability`
    reached `requires_approval` (the approval flags) and `_denied_tools` (drop a
    tool whose *operation* defaults to deny) and nothing else. Per-path `rules`
    were never read. With every operation left at `default="allow"` and the
    patterns in `rules`, a caller who had written out `**/.env`, `**/*.pem` and
    `/etc/**` got no enforcement at all, and the result looked like a working
    boundary — which is worse than rejecting the ruleset outright.

    Wrapping the backend rather than checking inside each tool is what makes it
    total: every console tool reaches the filesystem through this protocol, so a
    tool added in a later release is covered on the day it arrives rather than the
    day somebody remembers to list it.

    **Content and mutation are refused; listings are filtered by their own rules.**
    `read`, `read_bytes` and `grep_raw` are the three ways bytes leave a file, and
    `write` and `edit` the two that change one. `grep_raw` is filtered rather than
    refused because it takes a tree and not a path — a pattern over `/`
    legitimately spans the workspace, and `GrepMatch` carries the matching *line*,
    so an unfiltered grep would hand over the contents of exactly the files the
    rules protect. It therefore drops a match on anything denied for `grep` *or*
    `read`, which is what `PermissionGuard.hides_from_grep` is for.

    `ls_info` and `glob_info` drop entries denied for `ls` and `glob`
    respectively, matching `LocalBackend` — and deliberately **not** consulting
    the `read` rules. So a ruleset that denies reading `**/.env` still lists it by
    name, and one that wants the name hidden has to say so on `ls` and `glob`.
    Worth knowing rather than guessing at: a name is a weaker claim than the
    contents, `ls` returning nothing for a file an agent can see with `exists`
    would send it rewriting one it cannot read, and a ruleset defaulting to
    `"ask"` would otherwise blank out every listing.

    `execute` goes through `execute_denial_reason`, so the obvious bypass
    (`cat restricted/secret.txt`) is caught. That is defence in depth and not a
    boundary: a shell reaches files in ways string inspection cannot see, and real
    isolation is a sandboxed backend's job.

    Async throughout, and `ensure_async` is applied to what it wraps. A sync guard
    over an async backend would return un-awaited coroutines, and the alternative —
    two parallel implementations — is two things to keep in agreement. The console
    toolset calls `ensure_async` on every tool call anyway, so this costs nothing
    that was not already paid.

    Args:
        backend: What to wrap. Sync or async; both come out async.
        ruleset: Rules to apply.
        root: Directory commands run in, for resolving their path arguments.
        ask_callback: Async approval callback, passed to the checker.
        ask_fallback: What an unanswerable "ask" does.
    """

    def __init__(
        self,
        backend: BackendProtocol | AsyncBackendProtocol,
        ruleset: PermissionRuleset,
        *,
        root: Path | None = None,
        ask_callback: AskCallback | None = None,
        ask_fallback: AskFallback = "error",
    ) -> None:
        from pydantic_ai_backends.adapter import ensure_async

        self._backend = ensure_async(backend)
        # Kept as well: the async adapter proxies the protocol and `execute`, but
        # not `stop`, `id` or a backend's own extras, and `__getattr__` below has
        # to be able to reach them.
        self._raw = backend
        self._guard = PermissionGuard(
            ruleset,
            root if root is not None else Path("/"),
            ask_callback=ask_callback,
            ask_fallback=ask_fallback,
        )

    @property
    def permissions(self) -> PermissionRuleset:
        """The ruleset in force.

        Present so a caller — and `create_console_toolset` — can tell a backend
        that already enforces its own rules from one that does not, and avoid
        wrapping twice.
        """
        return self._guard.checker.ruleset

    async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        reason = self._guard.denial_reason("read", path)
        if reason is not None:
            raise PermissionError(reason)
        return await self._backend.read(path, offset, limit)

    async def read_bytes(self, path: str) -> bytes:
        reason = self._guard.denial_reason("read", path)
        if reason is not None:
            raise PermissionError(reason)
        return await self._backend.read_bytes(path)

    async def write(self, path: str, content: str | bytes) -> WriteResult:
        reason = self._guard.denial_reason("write", path)
        if reason is not None:
            return WriteResult(error=reason)
        return await self._backend.write(path, content)

    async def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        reason = self._guard.denial_reason("edit", path)
        if reason is not None:
            return EditResult(error=reason)
        return await self._backend.edit(path, old_string, new_string, replace_all)

    async def exists(self, path: str) -> bool:
        return await self._backend.exists(path)

    async def ls_info(self, path: str) -> list[FileInfo]:
        entries = await self._backend.ls_info(path)
        return [entry for entry in entries if not self._guard.is_denied("ls", entry["path"])]

    async def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        entries = await self._backend.glob_info(pattern, path)
        return [entry for entry in entries if not self._guard.is_denied("glob", entry["path"])]

    async def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        found = await self._backend.grep_raw(pattern, path, glob, ignore_hidden)
        # A string is the backend's own error or "no matches", not a result set.
        if isinstance(found, str):
            return found
        return [match for match in found if not self._guard.hides_from_grep(match["path"])]

    def _guarded_execute(self) -> Any:
        """`execute`, refusing a command that names a path the rules deny.

        Deliberately **not** a method on the class, and that is not a style
        choice. The console toolset asks `hasattr(backend, "execute")` to decide
        whether a backend can run commands at all, and answers
        "Backend does not support command execution" when it cannot. A declared
        method would make that true for every backend - so a `StateBackend`, which
        has no shell, would stop giving that answer and start raising instead.

        Reached through `__getattr__`, the `getattr` below raises `AttributeError`
        for a backend with no `execute`, which is exactly what `hasattr` needs to
        see.

        Raises:
            AttributeError: If the wrapped backend cannot execute anything.
        """
        inner = getattr(self._backend, "execute")  # noqa: B009 - AttributeError is the point

        async def execute(command: str, timeout: int | None = None) -> Any:
            reason = self._guard.execute_denial_reason(command)
            if reason is not None:
                raise PermissionError(reason)
            return await inner(command, timeout)

        return execute

    def __getattr__(self, name: str) -> Any:
        """Everything the protocol does not name, straight through to the backend.

        A sandbox is more than the protocol: `stop`, `start`, `id`, `files` on a
        `StateBackend`. Delegating by name keeps this wrapper from quietly removing
        a capability the way an explicit list would.

        `execute` is the exception, because it is the one non-protocol operation
        the rules have something to say about - see `_guarded_execute`.

        Everything else comes back with the shape the *unwrapped* backend has, so
        `stop` stays synchronous. That is the right contract for a transparent
        wrapper: code that worked on the backend works on this.
        """
        if name == "execute":
            return self._guarded_execute()
        return getattr(self._raw, name)

    def __repr__(self) -> str:
        return f"<GuardedBackend({self._raw!r})>"


def guarding(
    backend: BackendProtocol | AsyncBackendProtocol,
    ruleset: PermissionRuleset | None,
    *,
    root: Path | None = None,
    ask_callback: AskCallback | None = None,
    ask_fallback: AskFallback = "error",
) -> BackendProtocol | AsyncBackendProtocol:
    """`backend` with `ruleset` enforced, or `backend` unchanged.

    Unchanged in the two cases where wrapping would be wrong rather than merely
    unnecessary: there is no ruleset to apply, or the backend already applies one
    of its own — `LocalBackend` does, and wrapping it would check every path twice
    and report the second refusal.
    """
    if ruleset is None:
        return backend
    if getattr(backend, "permissions", None) is not None:
        return backend
    return GuardedBackend(
        backend,
        ruleset,
        root=root,
        ask_callback=ask_callback,
        ask_fallback=ask_fallback,
    )
