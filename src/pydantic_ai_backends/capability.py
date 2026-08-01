"""Console capability for pydantic-ai agents.

Provides `ConsoleCapability` that bundles console toolset + instructions +
permission enforcement via the pydantic-ai capabilities API.

Example:
    ```python
    from pydantic_ai import Agent
    from pydantic_ai_backends import ConsoleCapability
    from pydantic_ai_backends.permissions import READONLY_RULESET

    agent = Agent(
        "openai:gpt-4.1",
        capabilities=[ConsoleCapability(permissions=READONLY_RULESET)],
    )
    ```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset

from pydantic_ai_backends.permissions.checker import (
    AskCallback,
    AskFallback,
    PermissionChecker,
)
from pydantic_ai_backends.permissions.types import (
    PermissionOperation,
    PermissionRuleset,
)
from pydantic_ai_backends.protocol import AsyncBackendProtocol, BackendProtocol
from pydantic_ai_backends.toolsets.console import (
    EditFormat,
    create_console_toolset,
    get_console_system_prompt,
)

TOOL_OPERATIONS: dict[str, PermissionOperation] = {
    "ls": "ls",
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
    "hashline_edit": "edit",
    "glob": "glob",
    "grep": "grep",
    "execute": "execute",
    # The background shells are the "execute" operation reached a different way.
    # Left out of this map they were governed by nothing at all: an unmapped name
    # is both kept by `prepare_tools` and waved through `before_tool_execute`, so
    # a ruleset denying execution still handed the model `run_in_background`.
    # `tests/test_capability.py` asserts this map covers every registered tool.
    "run_in_background": "execute",
    "read_output": "execute",
    "kill_shell": "execute",
    "list_shells": "execute",
}
"""Console tool name -> the permission operation that governs it."""

PATH_OPERATIONS: set[PermissionOperation] = {"read", "write", "edit"}
"""Operations checked per file path, taken from the call's `path` argument."""

COMMAND_OPERATIONS: set[PermissionOperation] = {"execute"}
"""Operations checked per command, taken from the call's `command` argument."""

# glob, grep and ls appear in neither set on purpose: a denied one is hidden
# outright by prepare_tools(), which saves checking every pattern against the
# ruleset on every call.


@dataclass
class ConsoleCapability(AbstractCapability[Any]):
    """Capability providing filesystem tools with permission enforcement.

    Bundles the console toolset (ls, read_file, write_file, edit_file, glob,
    grep, execute) with dynamic instructions and per-tool permission control.

    When a permission ruleset is provided:
    - Tools for denied operations are dropped from the toolset entirely, and
      hidden again from each request's tool definitions
    - Per-path/command permissions are checked before each tool execution
    - "ask" permissions go to `ask_callback`, and are refused or raised per
      `ask_fallback` when there is none

    Example:
        ```python
        from pydantic_ai import Agent
        from pydantic_ai_backends import ConsoleCapability, DockerSandbox
        from pydantic_ai_backends.permissions import READONLY_RULESET

        # Read-only agent — write/edit/execute tools are hidden
        agent = Agent(
            "openai:gpt-4.1",
            capabilities=[ConsoleCapability(permissions=READONLY_RULESET)],
        )

        # A sandbox the capability owns, for an agent whose deps type is not ours
        agent = Agent(
            "openai:gpt-4.1",
            capabilities=[ConsoleCapability(backend=DockerSandbox(runtime="python-web"))],
        )
        ```
    """

    backend: BackendProtocol | AsyncBackendProtocol | None = None
    """Backend the tools operate on.

    When omitted, each call reads `ctx.deps.backend`, which requires the agent's
    deps to satisfy `ConsoleDeps`. Set it when the host owns its deps type and
    cannot add a `backend` field — the capability then carries the backend
    itself, which is also what lets one agent hold a sandbox of its own.
    """

    include_execute: bool = True
    """Whether to include the execute tool."""

    include_background: bool = True
    """Whether to include the background-shell tools.

    Separate from `include_execute` because a backend may support commands
    without supporting long-lived ones — and because an agent that should not
    start a process it cannot see finish wants these off while keeping `execute`.
    """

    edit_format: EditFormat = "str_replace"
    """Edit format: 'str_replace' or 'hashline'."""

    permissions: PermissionRuleset | None = None
    """Permission ruleset for controlling tool access."""

    ask_callback: AskCallback | None = None
    """Async approval callback for operations the ruleset resolves to "ask".

    Without one an "ask" cannot be answered, and `ask_fallback` decides what
    happens instead. Every shipped preset except `PERMISSIVE_RULESET` has at
    least one operation defaulting to "ask", so a ruleset supplied without this
    or `ask_fallback="deny"` refuses those operations by raising.
    """

    ask_fallback: AskFallback = "error"
    """What an unanswerable "ask" does — `"deny"` refuses it, `"error"` raises."""

    _toolset: AbstractToolset[Any] | None = field(default=None, init=False, repr=False)
    _checker: PermissionChecker | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Create the underlying console toolset and permission checker."""
        self._toolset = create_console_toolset(
            backend=self.backend,
            include_execute=self.include_execute,
            include_background=self.include_background,
            edit_format=self.edit_format,
            # Passed through as well as being enforced in `prepare_tools`: the
            # toolset drops a denied operation's tools outright, so they are gone
            # rather than merely hidden from one request's tool definitions.
            permissions=self.permissions,
        )
        if self.permissions is not None:
            self._checker = PermissionChecker(
                ruleset=self.permissions,
                ask_callback=self.ask_callback,
                ask_fallback=self.ask_fallback,
            )

    @classmethod
    def get_serialization_name(cls) -> str:
        """Return name for AgentSpec YAML/JSON serialization."""
        return "ConsoleCapability"

    def get_toolset(self) -> AbstractToolset[Any] | None:
        """Return the console toolset."""
        return self._toolset

    def get_instructions(self) -> str:
        """Return console tool usage instructions."""
        return get_console_system_prompt(edit_format=self.edit_format)

    async def prepare_tools(
        self,
        ctx: RunContext[Any],
        tool_defs: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        """Hide tools for denied operations."""
        if self._checker is None:
            return tool_defs

        result = []
        for td in tool_defs:
            operation = TOOL_OPERATIONS.get(td.name)
            if operation is None:
                result.append(td)
                continue

            action = self._checker.check_sync(operation, "*")
            if action != "deny":
                result.append(td)

        return result

    async def before_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Check this call's path or command against the ruleset.

        Raises:
            PermissionDeniedError: If the ruleset denies the operation.
        """
        if self._checker is None:
            return args

        operation = TOOL_OPERATIONS.get(call.tool_name)
        if operation is None:
            return args

        if operation in PATH_OPERATIONS:
            target = args.get("path", args.get("file_path", "*"))
        elif operation in COMMAND_OPERATIONS:
            target = args.get("command", "*")
        else:
            return args

        await self._checker.check(operation, str(target))
        return args


__all__ = [
    "ConsoleCapability",
]
