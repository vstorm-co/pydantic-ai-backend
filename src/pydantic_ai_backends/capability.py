"""Console capability for pydantic-ai agents.

Provides `ConsoleCapability`, which bundles the console toolset, its
instructions and permission enforcement, all working in the run's Pydantic AI
workspace.

Example:
    ```python
    from pydantic_ai import Agent

    from pydantic_ai_backends import ConsoleCapability, DockerWorkspace
    from pydantic_ai_backends.permissions import READONLY_RULESET

    agent = Agent(
        "anthropic:claude-opus-5-5",
        capabilities=[DockerWorkspace(), ConsoleCapability(permissions=READONLY_RULESET)],
    )
    ```
"""

from __future__ import annotations

from collections.abc import Mapping
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
from pydantic_ai_backends.toolsets.console import (
    DEFAULT_MAX_DOCUMENT_BYTES,
    DEFAULT_MAX_IMAGE_BYTES,
    EditFormat,
    create_console_toolset,
    get_console_system_prompt,
)
from pydantic_ai_backends.toolsets.descriptions import DEFAULT_PROFILE, Profile, ToolText

TOOL_OPERATIONS: dict[str, PermissionOperation] = {
    "ls": "ls",
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
    "hashline_edit": "edit",
    "glob": "glob",
    "grep": "grep",
    "execute": "execute",
}
"""Console tool name -> the permission operation that governs it."""

PATH_OPERATIONS: set[PermissionOperation] = {"read", "write", "edit"}
"""Operations checked per file path, taken from the call's `path` argument."""

COMMAND_OPERATIONS: set[PermissionOperation] = {"execute"}
"""Operations checked per command, taken from the call's `command` argument."""

MUTATING_TOOLS = frozenset({"write_file", "edit_file", "hashline_edit", "execute"})
"""Tools a read-only workspace refuses, so they are not offered on one."""

# glob, grep and ls appear in neither set on purpose: a denied one is hidden
# outright by prepare_tools(), which saves checking every pattern against the
# ruleset on every call.


@dataclass
class ConsoleCapability(AbstractCapability[Any]):
    """Filesystem and shell tools in the run's workspace, with permission enforcement.

    Bundles the console toolset (ls, read_file, write_file, edit_file, glob,
    grep, execute) with instructions and per-tool permission control. The tools
    work in `ctx.workspace`, so the agent needs a workspace capability beside
    this one — `DockerWorkspace`, `SandboxdWorkspace`, `StateWorkspace`,
    Pydantic AI's `LocalWorkspace`, or a harness sandbox such as `E2BSandbox`.
    On a read-only workspace the write and execute tools are not offered.

    When a permission ruleset is provided:
    - Tools for denied operations are dropped from the toolset entirely, and
      hidden again from each request's tool definitions
    - Per-path/command permissions are checked before each tool execution
    - "ask" permissions go to `ask_callback`, and are refused or raised per
      `ask_fallback` when there is none

    Example:
        ```python
        from pydantic_ai import Agent

        from pydantic_ai_backends import ConsoleCapability, SandboxdWorkspace
        from pydantic_ai_backends.permissions import READONLY_RULESET

        sandbox = SandboxdWorkspace(service_url="http://sandboxd:8080", token="...")
        agent = Agent("anthropic:claude-opus-5-5", capabilities=[sandbox, ConsoleCapability()])

        # Read-only agent — write/edit/execute tools are hidden
        reader = Agent(
            "anthropic:claude-opus-5-5",
            capabilities=[sandbox, ConsoleCapability(permissions=READONLY_RULESET)],
        )
        ```
    """

    include_execute: bool = True
    """Whether to include the execute tool."""

    edit_format: EditFormat = "str_replace"
    """Edit format: 'str_replace' or 'hashline'."""

    image_support: bool = False
    """Return recognized images from `read_file` as `BinaryContent`.

    Without it a multimodal model reading a `.png` gets the bytes as garbled
    text. With it the agent can look at a chart it rendered a moment ago, which
    is the loop that makes producing one useful.
    """

    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES
    """Largest image returned; bigger ones yield an error."""

    document_support: bool = False
    """Return recognized documents (`.pdf`) as `BinaryContent`.

    Separate from `image_support` because the model support is separate: a model
    that sees images does not necessarily read PDFs natively.
    """

    max_document_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES
    """Largest document returned; bigger ones yield an error."""

    descriptions: Mapping[str, str | ToolText] | None = None
    """Per-tool text overrides, keyed by tool name.

    A host that lists these tools in its own catalogue needs the text it shows
    and the text the model reads to be the same string. Without this the two are
    written in different repositories and drift, and the drift is invisible: the
    person choosing what to allow and the model choosing when to act are reading
    different descriptions of the same tool.

    A string replaces the tool's description and leaves its argument text alone;
    a `ToolText` replaces both. An unknown tool name raises rather than being
    ignored.
    """

    profile: Profile = DEFAULT_PROFILE
    """How much guidance the tool descriptions carry.

    `"coding"` includes what an agent working in a repository needs — git,
    dependencies, reading a failed command's output. `"agent"` leaves it out,
    which is what an agent whose workspace is scratch space for one conversation
    should be paying for.
    """

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
            include_execute=self.include_execute,
            edit_format=self.edit_format,
            image_support=self.image_support,
            max_image_bytes=self.max_image_bytes,
            document_support=self.document_support,
            max_document_bytes=self.max_document_bytes,
            descriptions=self.descriptions,
            profile=self.profile,
            # Passed through as well as being enforced in `prepare_tools`: the
            # toolset drops a denied operation's tools outright, so they are gone
            # rather than merely hidden from one request's tool definitions - and
            # it is what applies the per-path rules, which nothing used to.
            permissions=self.permissions,
            # So the guard inside the toolset resolves an "ask" the same way this
            # capability does. Without them a ruleset holding an "ask" would refuse
            # or raise there on a different rule from the one stated here.
            ask_callback=self.ask_callback,
            ask_fallback=self.ask_fallback,
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
        """Hide tools for denied operations, and mutating ones on a read-only workspace."""
        if ctx.workspace.read_only:
            tool_defs = [td for td in tool_defs if td.name not in MUTATING_TOOLS]
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
