"""Applying a permission ruleset to the console tools' workspace operations.

A ruleset can resolve an operation to "ask", but a file operation in the middle
of a tool call has nobody to ask. This module holds that reconciliation in one
place: which operations refuse outright, which quietly hide results, and how an
`execute` is inspected for the paths it would touch.
"""

from __future__ import annotations

import os
import posixpath
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai_backends.permissions.checker import PermissionAskError, PermissionChecker
from pydantic_ai_backends.types import EditResult, WriteResult

if TYPE_CHECKING:
    from pydantic_ai.workspaces import Workspace

    from pydantic_ai_backends.permissions.checker import AskCallback, AskFallback
    from pydantic_ai_backends.permissions.types import PermissionOperation, PermissionRuleset
    from pydantic_ai_backends.toolsets._workspace import FileOps
    from pydantic_ai_backends.types import ExecuteResponse, FileInfo, GrepMatch

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

    def execute_denial_reason(self, command: str, *, root: Path | None = None) -> str | None:
        """Why `command` is refused, checking its rules and its path arguments.

        Beyond the command-pattern rules, path-looking tokens are resolved and
        refused when one hits a read or write deny rule, so the obvious bypass
        (`cat restricted/secret.txt`) is caught.

        This is defense in depth, not a boundary — a shell can reach a file in
        ways string inspection cannot see. For enforced isolation use a
        sandboxed workspace such as `DockerWorkspace`.

        Args:
            command: The command line.
            root: Directory the command runs in, when it is not the guard's own.
        """
        reason = self.denial_reason("execute", command)
        if reason is not None:
            return reason

        for target in sorted(self.command_path_targets(command, root=root)):
            for operation in GUARDED_COMMAND_OPERATIONS:
                if self.is_denied(operation, target):
                    return (
                        f"{PERMISSION_DENIED_PREFIX}: command references '{target}', "
                        f"which is denied for {operation}"
                    )
        return None

    def command_path_targets(self, command: str, *, root: Path | None = None) -> set[str]:
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

        base = root if root is not None else self._root
        targets: set[str] = set()
        for candidate in candidates:
            if not candidate:
                continue
            expanded = os.path.expanduser(candidate)
            path = Path(expanded)
            absolute = path if path.is_absolute() else base / expanded
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


class GuardedOps:
    """The console tools' operations, with a ruleset's per-path rules applied.

    Wrapping the operations rather than checking inside each tool is what makes
    it total: every console tool reaches the workspace through them, so a tool
    added in a later release is covered on the day it arrives rather than the day
    somebody remembers to list it.

    **Content and mutation are refused; listings are filtered by their own rules.**
    `read`, `read_bytes` and `grep_raw` are the three ways bytes leave a file, and
    `write` and `edit` the two that change one. `grep_raw` is filtered rather than
    refused because it takes a tree and not a path — a pattern over `/`
    legitimately spans the workspace, and `GrepMatch` carries the matching *line*,
    so an unfiltered grep would hand over the contents of exactly the files the
    rules protect. It therefore drops a match on anything denied for `grep` *or*
    `read`, which is what `PermissionGuard.hides_from_grep` is for.

    `ls_info` and `glob_info` drop entries denied for `ls` and `glob`
    respectively — and deliberately **not** consulting the `read` rules. So a
    ruleset that denies reading `**/.env` still lists it by name, and one that
    wants the name hidden has to say so on `ls` and `glob`. A name is a weaker
    claim than the contents, and a ruleset defaulting to `"ask"` would otherwise
    blank out every listing.

    `execute` goes through `execute_denial_reason`, so the obvious bypass
    (`cat restricted/secret.txt`) is caught. That is defence in depth and not a
    boundary: a shell reaches files in ways string inspection cannot see, and real
    isolation is the workspace's job.

    Paths are checked as the model wrote them and as the workspace resolves
    them against its working directory, and a deny on either refuses. A rule
    names a file one way — `/workspace/private/**` — and a model reaches it
    another — `private/notes.txt` — and checking only the spelling the model
    chose let it read exactly what the rule protects.

    Args:
        ops: What to wrap.
        ruleset: Rules to apply.
        workspace: The workspace `ops` reaches, whose working directory relative
            paths and command arguments resolve against. Without one they
            resolve against `/`.
        ask_callback: Async approval callback, passed to the checker.
        ask_fallback: What an unanswerable "ask" does.
    """

    def __init__(
        self,
        ops: FileOps,
        ruleset: PermissionRuleset,
        *,
        workspace: Workspace | None = None,
        ask_callback: AskCallback | None = None,
        ask_fallback: AskFallback = "error",
    ) -> None:
        self._backend = ops
        self._workspace = workspace
        self._guard = PermissionGuard(
            ruleset, Path("/"), ask_callback=ask_callback, ask_fallback=ask_fallback
        )

    async def _base(self) -> str | None:
        """The directory relative paths mean, or `None` without a workspace."""
        return None if self._workspace is None else await self._workspace.working_dir()

    @staticmethod
    def _spellings(path: str, base: str | None) -> list[str]:
        """`path` as written, then as the workspace resolves it when that differs."""
        if base is None:
            return [path]
        resolved = posixpath.normpath(posixpath.join(base, path))
        return [path] if resolved == path else [path, resolved]

    async def _refusal(self, operation: PermissionOperation, path: str) -> str | None:
        for spelling in self._spellings(path, await self._base()):
            reason = self._guard.denial_reason(operation, spelling)
            if reason is not None:
                return reason
        return None

    def _denied(self, operation: PermissionOperation, path: str, base: str | None) -> bool:
        return any(
            self._guard.is_denied(operation, spelling) for spelling in self._spellings(path, base)
        )

    async def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        reason = await self._refusal("read", path)
        if reason is not None:
            raise PermissionError(reason)
        return await self._backend.read(path, offset, limit)

    async def read_bytes(self, path: str) -> bytes:
        reason = await self._refusal("read", path)
        if reason is not None:
            raise PermissionError(reason)
        return await self._backend.read_bytes(path)

    async def write(self, path: str, content: str | bytes) -> WriteResult:
        reason = await self._refusal("write", path)
        if reason is not None:
            return WriteResult(error=reason)
        return await self._backend.write(path, content)

    async def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        reason = await self._refusal("edit", path)
        if reason is not None:
            return EditResult(error=reason)
        return await self._backend.edit(path, old_string, new_string, replace_all)

    async def exists(self, path: str) -> bool:
        return await self._backend.exists(path)

    async def ls_info(self, path: str) -> list[FileInfo]:
        entries = await self._backend.ls_info(path)
        base = await self._base()
        return [entry for entry in entries if not self._denied("ls", entry["path"], base)]

    async def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        entries = await self._backend.glob_info(pattern, path)
        base = await self._base()
        return [entry for entry in entries if not self._denied("glob", entry["path"], base)]

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
        base = await self._base()
        return [
            match
            for match in found
            if not any(
                self._guard.hides_from_grep(spelling)
                for spelling in self._spellings(match["path"], base)
            )
        ]

    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        base = await self._base()
        reason = self._guard.execute_denial_reason(
            command, root=None if base is None else Path(base)
        )
        if reason is not None:
            raise PermissionError(reason)
        return await self._backend.execute(command, timeout)


def guarding(
    ops: FileOps,
    ruleset: PermissionRuleset | None,
    *,
    workspace: Workspace | None = None,
    ask_callback: AskCallback | None = None,
    ask_fallback: AskFallback = "error",
) -> FileOps:
    """`ops` with `ruleset` enforced, or `ops` unchanged when there is none."""
    if ruleset is None:
        return ops
    return GuardedOps(
        ops,
        ruleset,
        workspace=workspace,
        ask_callback=ask_callback,
        ask_fallback=ask_fallback,
    )
