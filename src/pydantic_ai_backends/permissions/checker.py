"""Evaluating operations against a permission ruleset."""

from __future__ import annotations

import functools
import re
from collections.abc import Awaitable, Callable
from typing import Literal

from typing_extensions import deprecated

from pydantic_ai_backends.permissions.types import (
    PermissionAction,
    PermissionOperation,
    PermissionRule,
    PermissionRuleset,
)

AskCallback = Callable[[PermissionOperation, str, str], Awaitable[bool]]
"""Approval callback: `(operation, target, reason)` -> whether to allow."""

AskFallback = Literal["deny", "error"]
"""What an "ask" does when no callback can answer it."""

PATTERN_CACHE_SIZE = 512
"""Compiled patterns kept around. Every check walks a ruleset's rules, so
recompiling them per call showed up in profiles of read-heavy agent runs."""

MATCHES_NOTHING = re.compile(r"(?!)")
"""What an unrenderable glob compiles to, so a bad rule is inert, not an error."""


class PermissionAskError(Exception):
    """Raised when an operation needs approval and `ask_fallback="error"`.

    Named `PermissionAskError` so it does not shadow the builtin
    `PermissionError` (an `OSError` subclass) for importers of this module.

    Attributes:
        operation: The operation that needed approval.
        target: The path or command it addressed.
        reason: Why approval was being sought.
    """

    def __init__(
        self,
        operation: PermissionOperation,
        target: str,
        reason: str = "",
    ):
        self.operation = operation
        self.target = target
        self.reason = reason
        message = f"Permission required for {operation} on '{target}'"
        if reason:
            message += f": {reason}"
        super().__init__(message)


@deprecated("Use `PermissionAskError` instead; this name shadows the builtin PermissionError.")
class PermissionError(PermissionAskError):
    """Deprecated alias for :class:`PermissionAskError`."""


class PermissionDeniedError(Exception):
    """Raised when an operation is explicitly denied.

    Attributes:
        operation: The operation that was denied.
        target: The path or command it addressed.
        rule: The rule that denied it, when a rule rather than a default did.
    """

    def __init__(
        self,
        operation: PermissionOperation,
        target: str,
        rule: PermissionRule | None = None,
    ):
        self.operation = operation
        self.target = target
        self.rule = rule
        message = f"Permission denied for {operation} on '{target}'"
        if rule and rule.description:
            message += f": {rule.description}"
        super().__init__(message)


@functools.lru_cache(maxsize=PATTERN_CACHE_SIZE)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob pattern to an anchored regex.

    Supports `*` (anything but `/`), `**` (anything, including `/`), `?` (one
    character but `/`) and `[seq]` character classes with `!` or `^` negation.

    A pattern whose character class is malformed beyond rescue — `[\\]`, whose
    only `]` is escaped — renders to a regex `re` rejects. It compiles to a
    pattern matching nothing, so the rule is inert and the operation's own
    default decides. The alternative was `re.error` escaping the permission
    check, which every caller here is promised will only ever return an action.
    """
    parts: list[str] = []
    index = 0

    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")  # Zero or more directories.
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        elif pattern[index] == "[":
            rendered, index = _character_class(pattern, index)
            parts.append(rendered)
        else:
            parts.append(re.escape(pattern[index]))
            index += 1

    try:
        return re.compile("^" + "".join(parts) + "$")
    except re.error:
        return MATCHES_NOTHING


def _character_class(pattern: str, start: int) -> tuple[str, int]:
    """Render the character class starting at `start`, and where it ends."""
    end = start + 1
    # Glob negates with '!', regex with '^'.
    negated = end < len(pattern) and pattern[end] in "!^"
    if negated:
        end += 1
    if end < len(pattern) and pattern[end] == "]":
        end += 1
    while end < len(pattern) and pattern[end] != "]":
        end += 1

    if end >= len(pattern):
        # No closing bracket, so the '[' is a literal.
        return re.escape("["), start + 1
    if negated:
        return f"[^{pattern[start + 2 : end]}]", end + 1
    return pattern[start : end + 1], end + 1


def matches_pattern(target: str, pattern: str) -> bool:
    """Whether a path or command matches a glob pattern."""
    return bool(glob_to_regex(pattern).match(target))


class PermissionChecker:
    """Checks operations against a permission ruleset.

    Rules are evaluated in order and the first match wins. With no match the
    operation's default applies, falling back to the ruleset's global default.

    Example:
        ```python
        from pydantic_ai_backends.permissions import DEFAULT_RULESET, PermissionChecker

        async def ask_user(op: str, target: str, reason: str) -> bool:
            return input(f"Allow {op} on {target}? ").lower() == "y"

        checker = PermissionChecker(ruleset=DEFAULT_RULESET, ask_callback=ask_user)

        action = checker.check_sync("read", "/path/to/file")
        allowed = await checker.check("write", "/path/to/file", "Save changes")
        ```
    """

    def __init__(
        self,
        ruleset: PermissionRuleset,
        ask_callback: AskCallback | None = None,
        ask_fallback: AskFallback = "error",
    ):
        """Initialize the checker.

        Args:
            ruleset: The ruleset to check against.
            ask_callback: Async callback for "ask" actions.
            ask_fallback: What an unanswerable "ask" does — `"deny"` returns
                False, `"error"` raises.
        """
        self._ruleset = ruleset
        self._ask_callback = ask_callback
        self._ask_fallback = ask_fallback

    @property
    def ruleset(self) -> PermissionRuleset:
        """The ruleset being checked against."""
        return self._ruleset

    def check_sync(self, operation: PermissionOperation, target: str) -> PermissionAction:
        """Resolve the action for an operation without invoking any callback.

        Args:
            operation: The operation type.
            target: The path or command being accessed.
        """
        rule = self.find_matching_rule(operation, target)
        if rule is not None:
            return rule.action
        return self._ruleset.get_operation_permissions(operation).default

    def find_matching_rule(
        self, operation: PermissionOperation, target: str
    ) -> PermissionRule | None:
        """The first rule matching this operation and target, if any."""
        permissions = self._ruleset.get_operation_permissions(operation)
        return next(
            (rule for rule in permissions.rules if matches_pattern(target, rule.pattern)),
            None,
        )

    async def check(
        self,
        operation: PermissionOperation,
        target: str,
        reason: str = "",
    ) -> bool:
        """Resolve an operation, asking for approval when the rules say so.

        Args:
            operation: The operation type.
            target: The path or command being accessed.
            reason: Human-readable reason, passed to the callback.

        Returns:
            True when the operation is allowed.

        Raises:
            PermissionDeniedError: If it is denied, or approval was refused.
            PermissionAskError: If approval is needed, no callback can give it
                and `ask_fallback="error"`.
        """
        action = self.check_sync(operation, target)

        if action == "allow":
            return True

        if action == "deny":
            raise PermissionDeniedError(
                operation, target, self.find_matching_rule(operation, target)
            )

        if self._ask_callback is not None:
            if await self._ask_callback(operation, target, reason):
                return True
            raise PermissionDeniedError(operation, target)

        if self._ask_fallback == "error":
            raise PermissionAskError(operation, target, reason)
        raise PermissionDeniedError(operation, target)

    def is_allowed(self, operation: PermissionOperation, target: str) -> bool:
        """Whether the operation would proceed without asking."""
        return self.check_sync(operation, target) == "allow"

    def is_denied(self, operation: PermissionOperation, target: str) -> bool:
        """Whether the operation would be refused outright."""
        return self.check_sync(operation, target) == "deny"

    def requires_approval(self, operation: PermissionOperation, target: str) -> bool:
        """Whether the operation would need user approval."""
        return self.check_sync(operation, target) == "ask"
