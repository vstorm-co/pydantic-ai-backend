"""Reading a permission ruleset at toolset construction time.

The toolset is built once, before any path is known, so it can only act on an
operation's *default* action: "deny" means the tool is not registered at all,
"ask" means it is registered as requiring approval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic_ai_backends.permissions.types import OperationPermissions, PermissionRuleset


def requires_approval(
    ruleset: PermissionRuleset | None,
    operation: str,
    fallback: bool,
) -> bool:
    """Whether a tool for `operation` should be marked as needing approval.

    Args:
        ruleset: Ruleset to read, or `None` to use `fallback`.
        operation: Operation name, matching a `PermissionRuleset` field.
        fallback: Used when there is no ruleset.
    """
    if ruleset is None:
        return fallback
    return _default_action(ruleset, operation) == "ask"


def is_denied(ruleset: PermissionRuleset | None, operation: str) -> bool:
    """Whether `operation` is denied outright, so its tools must not exist."""
    if ruleset is None:
        return False
    return _default_action(ruleset, operation) == "deny"


def _default_action(ruleset: PermissionRuleset, operation: str) -> str:
    permissions: OperationPermissions | None = getattr(ruleset, operation, None)
    if permissions is None:
        return ruleset.default
    return permissions.default
