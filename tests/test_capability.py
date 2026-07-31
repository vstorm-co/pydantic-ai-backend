"""Tests for ConsoleCapability."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage

from pydantic_ai_backends import LocalBackend
from pydantic_ai_backends.capability import ConsoleCapability
from pydantic_ai_backends.permissions.presets import PERMISSIVE_RULESET, READONLY_RULESET


def _make_ctx():
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def _make_call(tool_name):
    return ToolCallPart(tool_name=tool_name, args={}, tool_call_id="test")


class TestConsoleCapability:
    def test_default_construction(self):
        cap = ConsoleCapability()
        assert cap.permissions is None
        assert cap.include_execute is True
        assert cap.get_toolset() is not None

    def test_with_permissions(self):
        cap = ConsoleCapability(permissions=READONLY_RULESET)
        assert cap.permissions is READONLY_RULESET
        assert cap._checker is not None

    def test_no_permissions_no_checker(self):
        cap = ConsoleCapability()
        assert cap._checker is None

    def test_serialization_name(self):
        assert ConsoleCapability.get_serialization_name() == "ConsoleCapability"

    def test_get_instructions(self):
        cap = ConsoleCapability()
        instructions = cap.get_instructions()
        assert isinstance(instructions, str)
        assert "read_file" in instructions

    @pytest.mark.anyio
    async def test_agent_runs(self):
        cap = ConsoleCapability()
        agent = Agent(TestModel(call_tools=[]), capabilities=[cap])
        result = await agent.run("Hello")
        assert result.output is not None


class TestPermissionEnforcement:
    @pytest.mark.anyio
    async def test_readonly_hides_write_tools(self):
        """READONLY_RULESET hides write/edit/execute tools."""
        cap = ConsoleCapability(permissions=READONLY_RULESET)
        ctx = _make_ctx()

        tool_defs = [
            ToolDefinition(name="read_file", description="read"),
            ToolDefinition(name="write_file", description="write"),
            ToolDefinition(name="edit_file", description="edit"),
            ToolDefinition(name="execute", description="exec"),
            ToolDefinition(name="glob", description="glob"),
            ToolDefinition(name="grep", description="grep"),
            ToolDefinition(name="ls", description="ls"),
        ]

        result = await cap.prepare_tools(ctx, tool_defs)
        names = {td.name for td in result}

        # Read/glob/grep/ls allowed
        assert "read_file" in names
        assert "glob" in names
        assert "grep" in names
        assert "ls" in names

        # Write/edit/execute denied (hidden)
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "execute" not in names

    @pytest.mark.anyio
    async def test_permissive_keeps_all_tools(self):
        """PERMISSIVE_RULESET keeps all tools visible."""
        cap = ConsoleCapability(permissions=PERMISSIVE_RULESET)
        ctx = _make_ctx()

        tool_defs = [
            ToolDefinition(name="read_file", description="read"),
            ToolDefinition(name="write_file", description="write"),
            ToolDefinition(name="execute", description="exec"),
        ]

        result = await cap.prepare_tools(ctx, tool_defs)
        assert len(result) == 3

    @pytest.mark.anyio
    async def test_no_permissions_passes_all(self):
        """No ruleset → all tools pass through."""
        cap = ConsoleCapability()
        ctx = _make_ctx()

        tool_defs = [
            ToolDefinition(name="write_file", description="write"),
            ToolDefinition(name="execute", description="exec"),
        ]

        result = await cap.prepare_tools(ctx, tool_defs)
        assert len(result) == 2

    @pytest.mark.anyio
    async def test_non_console_tools_pass_through(self):
        """Tools not in console toolset pass through regardless of permissions."""
        cap = ConsoleCapability(permissions=READONLY_RULESET)
        ctx = _make_ctx()

        tool_defs = [
            ToolDefinition(name="custom_tool", description="custom"),
        ]

        result = await cap.prepare_tools(ctx, tool_defs)
        assert len(result) == 1

    @pytest.mark.anyio
    async def test_before_tool_execute_allows_read(self):
        """Read operation passes through with READONLY_RULESET."""
        cap = ConsoleCapability(permissions=READONLY_RULESET)
        ctx = _make_ctx()
        call = _make_call("read_file")
        tool_def = ToolDefinition(name="read_file", description="read")

        result = await cap.before_tool_execute(
            ctx, call=call, tool_def=tool_def, args={"path": "/test.py"}
        )
        assert result == {"path": "/test.py"}

    @pytest.mark.anyio
    async def test_before_tool_execute_no_permissions(self):
        """No permissions → passes through."""
        cap = ConsoleCapability()
        ctx = _make_ctx()
        call = _make_call("write_file")
        tool_def = ToolDefinition(name="write_file", description="write")

        result = await cap.before_tool_execute(
            ctx, call=call, tool_def=tool_def, args={"path": "/test.py"}
        )
        assert result == {"path": "/test.py"}

    @pytest.mark.anyio
    async def test_before_tool_execute_unknown_tool(self):
        """Unknown tool name → passes through."""
        cap = ConsoleCapability(permissions=READONLY_RULESET)
        ctx = _make_ctx()
        call = _make_call("custom_tool")
        tool_def = ToolDefinition(name="custom_tool", description="custom")

        result = await cap.before_tool_execute(
            ctx, call=call, tool_def=tool_def, args={"foo": "bar"}
        )
        assert result == {"foo": "bar"}

    @pytest.mark.anyio
    async def test_before_tool_execute_checks_command(self):
        """Execute tool checks command argument."""
        cap = ConsoleCapability(permissions=PERMISSIVE_RULESET)
        ctx = _make_ctx()
        call = _make_call("execute")
        tool_def = ToolDefinition(name="execute", description="exec")

        result = await cap.before_tool_execute(
            ctx, call=call, tool_def=tool_def, args={"command": "ls -la"}
        )
        assert result == {"command": "ls -la"}

    @pytest.mark.anyio
    async def test_before_tool_execute_glob_passes(self):
        """Glob operation passes without path check."""
        cap = ConsoleCapability(permissions=READONLY_RULESET)
        ctx = _make_ctx()
        call = _make_call("glob")
        tool_def = ToolDefinition(name="glob", description="glob")

        result = await cap.before_tool_execute(
            ctx, call=call, tool_def=tool_def, args={"pattern": "*.py"}
        )
        assert result == {"pattern": "*.py"}


class TestCreateConsoleToolsetDenyFix:
    """Test that create_console_toolset removes denied tools (issue #23 fix)."""

    def test_readonly_removes_write_tools(self):
        """READONLY_RULESET removes write_file, edit_file, execute from toolset."""
        from pydantic_ai_backends.permissions.presets import READONLY_RULESET
        from pydantic_ai_backends.toolsets.console import create_console_toolset

        toolset = create_console_toolset(permissions=READONLY_RULESET)
        tool_names = set(toolset.tools.keys())

        assert "read_file" in tool_names
        assert "ls" in tool_names
        assert "glob" in tool_names
        assert "grep" in tool_names
        assert "write_file" not in tool_names
        assert "edit_file" not in tool_names
        assert "execute" not in tool_names

    def test_permissive_keeps_all_tools(self):
        """PERMISSIVE_RULESET keeps all tools."""
        from pydantic_ai_backends.permissions.presets import PERMISSIVE_RULESET
        from pydantic_ai_backends.toolsets.console import create_console_toolset

        toolset = create_console_toolset(permissions=PERMISSIVE_RULESET)
        tool_names = set(toolset.tools.keys())

        assert "write_file" in tool_names
        assert "execute" in tool_names

    def test_no_permissions_keeps_all(self):
        """No permissions keeps all tools."""
        from pydantic_ai_backends.toolsets.console import create_console_toolset

        toolset = create_console_toolset()
        tool_names = set(toolset.tools.keys())

        assert "write_file" in tool_names
        assert "execute" in tool_names


class TestIsDeniedByRuleset:
    """Direct tests for _is_denied_by_ruleset helper."""

    def test_none_ruleset_not_denied(self):
        from pydantic_ai_backends.toolsets._ruleset import is_denied

        assert is_denied(None, "write") is False

    def test_deny_default(self):
        from pydantic_ai_backends.permissions.types import PermissionRuleset
        from pydantic_ai_backends.toolsets._ruleset import is_denied

        ruleset = PermissionRuleset(default="deny")
        assert is_denied(ruleset, "write") is True

    def test_allow_default(self):
        from pydantic_ai_backends.permissions.types import PermissionRuleset
        from pydantic_ai_backends.toolsets._ruleset import is_denied

        ruleset = PermissionRuleset(default="allow")
        assert is_denied(ruleset, "write") is False

    def test_ask_default(self):
        from pydantic_ai_backends.permissions.types import PermissionRuleset
        from pydantic_ai_backends.toolsets._ruleset import is_denied

        ruleset = PermissionRuleset(default="ask")
        assert is_denied(ruleset, "write") is False

    def test_operation_override(self):
        from pydantic_ai_backends.permissions.types import (
            OperationPermissions,
            PermissionRuleset,
        )
        from pydantic_ai_backends.toolsets._ruleset import is_denied

        ruleset = PermissionRuleset(
            default="allow",
            write=OperationPermissions(default="deny"),
        )
        assert is_denied(ruleset, "write") is True
        assert is_denied(ruleset, "read") is False


class TestHashlineEditDenial:
    """Test that hashline_edit is also removed when edit=deny."""

    def test_readonly_removes_hashline_edit(self):
        from pydantic_ai_backends.permissions.presets import READONLY_RULESET
        from pydantic_ai_backends.toolsets.console import create_console_toolset

        toolset = create_console_toolset(permissions=READONLY_RULESET, edit_format="hashline")
        tool_names = set(toolset.tools.keys())
        assert "hashline_edit" not in tool_names
        assert "read_file" in tool_names


class TestCapabilityOwnedBackend:
    """A capability can carry its own backend, for hosts owning their deps type."""

    def _ctx(self, deps: object) -> RunContext[object]:
        return RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    async def test_tools_use_the_capability_backend_not_the_deps(self, tmp_path: Path):
        backend = LocalBackend(root_dir=str(tmp_path))
        backend.write("owned.txt", "held by the capability")
        capability = ConsoleCapability(backend=backend)
        toolset = capability.get_toolset()
        assert toolset is not None

        # Deps with no `backend` attribute at all: the host owns this type.
        result = await toolset.tools["read_file"].function(self._ctx(object()), "owned.txt")

        assert "held by the capability" in result

    def test_hashline_format_reaches_the_toolset(self):
        """The instructions promised hashline_edit; the tools must offer it."""
        toolset = ConsoleCapability(edit_format="hashline").get_toolset()
        assert toolset is not None

        assert "hashline_edit" in toolset.tools
        assert "edit_file" not in toolset.tools

    def test_str_replace_remains_the_default(self):
        toolset = ConsoleCapability().get_toolset()
        assert toolset is not None

        assert "edit_file" in toolset.tools
        assert "hashline_edit" not in toolset.tools
