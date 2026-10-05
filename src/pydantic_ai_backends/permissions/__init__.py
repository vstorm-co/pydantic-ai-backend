"""Fine-grained permission system for file operations and command execution.

This module provides a pattern-based permission system that controls access
to file operations and shell commands.

Basic usage:
    ```python
    from pydantic_ai_backends import ConsoleCapability
    from pydantic_ai_backends.permissions import (
        DEFAULT_RULESET,
        PermissionChecker,
    )

    # Create a checker with the default ruleset
    checker = PermissionChecker(ruleset=DEFAULT_RULESET)

    # Check if an operation is allowed
    if checker.is_allowed("read", "/path/to/file.txt"):
        print("Read is allowed")

    # Enforce it on the console tools
    console = ConsoleCapability(permissions=DEFAULT_RULESET)
    ```

Available presets:
    - DEFAULT_RULESET: Safe defaults (allow reads except secrets, ask for writes)
    - PERMISSIVE_RULESET: Allow most operations, deny only dangerous ones
    - READONLY_RULESET: Allow read operations only
    - STRICT_RULESET: Everything requires approval
"""

from __future__ import annotations

from pydantic_ai_backends.permissions.checker import (
    AskCallback,
    AskFallback,
    PermissionAskError,
    PermissionChecker,
    PermissionDeniedError,
    PermissionError,
)
from pydantic_ai_backends.permissions.presets import (
    DEFAULT_RULESET,
    PERMISSIVE_RULESET,
    READONLY_RULESET,
    SECRETS_PATTERNS,
    STRICT_RULESET,
    SYSTEM_PATTERNS,
    create_ruleset,
)
from pydantic_ai_backends.permissions.types import (
    OperationPermissions,
    PermissionAction,
    PermissionOperation,
    PermissionRule,
    PermissionRuleset,
)

__all__ = [
    # Types
    "PermissionAction",
    "PermissionOperation",
    "PermissionRule",
    "OperationPermissions",
    "PermissionRuleset",
    # Checker
    "PermissionChecker",
    "PermissionAskError",
    "PermissionError",
    "PermissionDeniedError",
    "AskCallback",
    "AskFallback",
    # Presets
    "DEFAULT_RULESET",
    "PERMISSIVE_RULESET",
    "READONLY_RULESET",
    "STRICT_RULESET",
    "SECRETS_PATTERNS",
    "SYSTEM_PATTERNS",
    "create_ruleset",
]
