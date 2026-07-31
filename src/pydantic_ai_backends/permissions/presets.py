"""Ready-made permission rulesets for common postures."""

from __future__ import annotations

from pydantic_ai_backends.permissions.types import (
    OperationPermissions,
    PermissionAction,
    PermissionRule,
    PermissionRuleset,
)

SECRETS_PATTERNS = [
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.crt",
    "**/credentials*",
    "**/secrets*",
    "**/*secret*",
    "**/*password*",
    "**/.aws/**",
    "**/.ssh/**",
    "**/.gnupg/**",
]
"""Paths that typically hold credentials."""

SYSTEM_PATTERNS = [
    "/etc/**",
    "/var/**",
    "/usr/**",
    "/bin/**",
    "/sbin/**",
    "/boot/**",
    "/sys/**",
    "/proc/**",
]
"""Paths owned by the operating system rather than the workspace."""

DANGEROUS_COMMANDS = [
    "rm -rf /*",
    "rm -rf /",
    ":(){:|:&};:",  # Fork bomb.
    "dd if=*of=/dev/*",
    "mkfs*",
    "> /dev/sda",
    "chmod -R 777 /",
]
"""Commands with no legitimate use inside a workspace."""

SECRETS_DESCRIPTION = "Protect sensitive files"
SYSTEM_DESCRIPTION = "Protect sensitive and system files"
DANGEROUS_DESCRIPTION = "Block dangerous commands"


def deny_rules(patterns: list[str], description: str) -> list[PermissionRule]:
    """Turn a list of patterns into deny rules sharing one description."""
    return [
        PermissionRule(pattern=pattern, action="deny", description=description)
        for pattern in patterns
    ]


DEFAULT_RULESET = PermissionRuleset(
    default="ask",
    read=OperationPermissions(
        default="allow",
        rules=deny_rules(SECRETS_PATTERNS, SECRETS_DESCRIPTION),
    ),
    write=OperationPermissions(
        default="ask",
        rules=deny_rules(SECRETS_PATTERNS, SECRETS_DESCRIPTION),
    ),
    edit=OperationPermissions(
        default="ask",
        rules=deny_rules(SECRETS_PATTERNS, SECRETS_DESCRIPTION),
    ),
    execute=OperationPermissions(
        default="ask",
        rules=deny_rules(DANGEROUS_COMMANDS, DANGEROUS_DESCRIPTION),
    ),
    glob=OperationPermissions(default="allow"),
    grep=OperationPermissions(default="allow"),
    ls=OperationPermissions(default="allow"),
)
"""Safe default: reads allowed except secrets, writes and commands ask first."""

PERMISSIVE_RULESET = PermissionRuleset(
    default="allow",
    read=OperationPermissions(
        default="allow",
        rules=deny_rules(SECRETS_PATTERNS, SECRETS_DESCRIPTION),
    ),
    write=OperationPermissions(
        default="allow",
        rules=deny_rules(SECRETS_PATTERNS + SYSTEM_PATTERNS, SYSTEM_DESCRIPTION),
    ),
    edit=OperationPermissions(
        default="allow",
        rules=deny_rules(SECRETS_PATTERNS + SYSTEM_PATTERNS, SYSTEM_DESCRIPTION),
    ),
    execute=OperationPermissions(
        default="allow",
        rules=deny_rules(DANGEROUS_COMMANDS, DANGEROUS_DESCRIPTION),
    ),
    glob=OperationPermissions(default="allow"),
    grep=OperationPermissions(default="allow"),
    ls=OperationPermissions(default="allow"),
)
"""Everything allowed except secrets, system paths and dangerous commands."""

READONLY_RULESET = PermissionRuleset(
    default="deny",
    read=OperationPermissions(
        default="allow",
        rules=deny_rules(SECRETS_PATTERNS, SECRETS_DESCRIPTION),
    ),
    write=OperationPermissions(default="deny"),
    edit=OperationPermissions(default="deny"),
    execute=OperationPermissions(default="deny"),
    glob=OperationPermissions(default="allow"),
    grep=OperationPermissions(default="allow"),
    ls=OperationPermissions(default="allow"),
)
"""Reads, listings and searches only — nothing may change or run."""

STRICT_RULESET = PermissionRuleset(
    default="ask",
    read=OperationPermissions(
        default="ask",
        rules=deny_rules(SECRETS_PATTERNS, SECRETS_DESCRIPTION),
    ),
    write=OperationPermissions(
        default="ask",
        rules=deny_rules(SECRETS_PATTERNS, SECRETS_DESCRIPTION),
    ),
    edit=OperationPermissions(
        default="ask",
        rules=deny_rules(SECRETS_PATTERNS, SECRETS_DESCRIPTION),
    ),
    execute=OperationPermissions(
        default="ask",
        rules=deny_rules(DANGEROUS_COMMANDS, DANGEROUS_DESCRIPTION),
    ),
    glob=OperationPermissions(default="ask"),
    grep=OperationPermissions(default="ask"),
    ls=OperationPermissions(default="ask"),
)
"""Every operation requires explicit approval."""


def create_ruleset(
    *,
    default: PermissionAction = "ask",
    allow_read: bool = True,
    allow_write: bool = False,
    allow_edit: bool = False,
    allow_execute: bool = False,
    allow_glob: bool = True,
    allow_grep: bool = True,
    allow_ls: bool = True,
    deny_secrets: bool = True,
) -> PermissionRuleset:
    """Build a ruleset from per-operation allow/ask switches.

    Each `allow_*` flag chooses between `"allow"` and `"ask"` for that
    operation's default.

    Args:
        default: Global default for operations with no configuration.
        allow_read: Allow reads outright rather than asking.
        allow_write: Allow writes outright rather than asking.
        allow_edit: Allow edits outright rather than asking.
        allow_execute: Allow commands outright rather than asking.
        allow_glob: Allow globbing outright rather than asking.
        allow_grep: Allow searching outright rather than asking.
        allow_ls: Allow listings outright rather than asking.
        deny_secrets: Deny the paths in `SECRETS_PATTERNS` for read/write/edit.

    Example:
        ```python
        ruleset = create_ruleset(allow_read=True, allow_write=True, allow_execute=False)
        ```
    """
    secret_rules = deny_rules(SECRETS_PATTERNS, SECRETS_DESCRIPTION) if deny_secrets else []

    return PermissionRuleset(
        default=default,
        read=OperationPermissions(default=_allow_or_ask(allow_read), rules=secret_rules),
        write=OperationPermissions(default=_allow_or_ask(allow_write), rules=secret_rules),
        edit=OperationPermissions(default=_allow_or_ask(allow_edit), rules=secret_rules),
        execute=OperationPermissions(default=_allow_or_ask(allow_execute)),
        glob=OperationPermissions(default=_allow_or_ask(allow_glob)),
        grep=OperationPermissions(default=_allow_or_ask(allow_grep)),
        ls=OperationPermissions(default=_allow_or_ask(allow_ls)),
    )


def _allow_or_ask(allowed: bool) -> PermissionAction:
    return "allow" if allowed else "ask"
