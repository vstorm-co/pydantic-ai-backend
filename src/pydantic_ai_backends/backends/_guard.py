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

if TYPE_CHECKING:
    from pydantic_ai_backends.permissions.checker import AskCallback, AskFallback
    from pydantic_ai_backends.permissions.types import PermissionOperation, PermissionRuleset

GUARDED_COMMAND_OPERATIONS: tuple[PermissionOperation, ...] = ("read", "write")
"""Operations whose deny rules also block a command that names such a path."""


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
                return f"Permission denied: {rule.description}"
            return f"Permission denied for {operation} on '{target}'"

        if self._ask_fallback == "deny":
            return f"Permission denied for {operation} on '{target}' (approval required)"
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
                        f"Permission denied: command references '{target}', "
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
            resolved = path.resolve() if path.is_absolute() else (self._root / expanded).resolve()
            targets.add(str(resolved))
        return targets
