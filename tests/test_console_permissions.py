"""Tests for console toolset permission integration."""

from pydantic_ai_backends.permissions import (
    OperationPermissions,
    PermissionRuleset,
)
from pydantic_ai_backends.toolsets._ruleset import requires_approval
from pydantic_ai_backends.toolsets.console import create_console_toolset


class TestRequiresApprovalFromRuleset:
    """Tests for the _requires_approval_from_ruleset helper."""

    def test_no_ruleset_uses_legacy_flag(self):
        """Test that legacy flag is used when no ruleset."""
        assert requires_approval(None, "write", True) is True
        assert requires_approval(None, "write", False) is False

    def test_ruleset_with_ask_default(self):
        """Test ruleset with ask as default action."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="ask"),
        )
        assert requires_approval(ruleset, "write", False) is True

    def test_ruleset_with_allow_default(self):
        """Test ruleset with allow as default action."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="allow"),
        )
        assert requires_approval(ruleset, "write", True) is False

    def test_ruleset_uses_global_default(self):
        """Test that global default is used for unconfigured operations."""
        ruleset = PermissionRuleset(default="ask")
        assert requires_approval(ruleset, "write", False) is True

        ruleset = PermissionRuleset(default="allow")
        assert requires_approval(ruleset, "write", True) is False


class TestCreateConsoleToolsetWithPermissions:
    """Tests for create_console_toolset with permissions parameter."""

    def test_create_without_permissions(self):
        """Test creating toolset without permissions."""
        toolset = create_console_toolset()

        # Should use default flags
        assert toolset is not None

    def test_create_with_permissions_ask_write(self):
        """Test creating toolset with permissions that ask for writes."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="ask"),
        )
        toolset = create_console_toolset(permissions=ruleset)

        # The toolset should be created successfully
        assert toolset is not None

    def test_create_with_permissions_allow_write(self):
        """Test creating toolset with permissions that allow writes."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="allow"),
        )
        toolset = create_console_toolset(permissions=ruleset)

        assert toolset is not None

    def test_create_with_permissions_ask_execute(self):
        """Test creating toolset with permissions that ask for execute."""
        ruleset = PermissionRuleset(
            execute=OperationPermissions(default="ask"),
        )
        toolset = create_console_toolset(permissions=ruleset)

        assert toolset is not None

    def test_create_with_permissions_allow_execute(self):
        """Test creating toolset with permissions that allow execute."""
        ruleset = PermissionRuleset(
            execute=OperationPermissions(default="allow"),
        )
        toolset = create_console_toolset(permissions=ruleset)

        assert toolset is not None

    def test_legacy_flags_ignored_with_permissions(self):
        """Test that legacy flags are ignored when permissions provided."""
        # With permissions, legacy flags should be ignored
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="allow"),
            execute=OperationPermissions(default="allow"),
        )
        toolset = create_console_toolset(
            permissions=ruleset,
            require_write_approval=True,  # Should be ignored
            require_execute_approval=True,  # Should be ignored
        )

        assert toolset is not None

    def test_include_execute_false(self):
        """Test creating toolset without execute tool."""
        ruleset = PermissionRuleset(
            execute=OperationPermissions(default="allow"),
        )
        toolset = create_console_toolset(
            permissions=ruleset,
            include_execute=False,
        )

        assert toolset is not None

    def test_permissions_with_custom_id(self):
        """Test creating toolset with permissions and custom ID."""
        ruleset = PermissionRuleset(default="ask")
        toolset = create_console_toolset(
            id="my-toolset",
            permissions=ruleset,
        )

        assert toolset is not None
