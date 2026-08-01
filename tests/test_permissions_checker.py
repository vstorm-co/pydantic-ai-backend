"""Tests for PermissionChecker."""

import pytest

from pydantic_ai_backends.permissions.checker import (
    PermissionAskError,
    PermissionChecker,
    PermissionDeniedError,
    PermissionError,
    glob_to_regex,
    matches_pattern,
)
from pydantic_ai_backends.permissions.types import (
    OperationPermissions,
    PermissionRule,
    PermissionRuleset,
)


class TestGlobToRegex:
    """Tests for glob pattern to regex conversion."""

    def test_simple_star(self):
        """Test single * matching."""
        regex = glob_to_regex("*.txt")
        assert regex.match("file.txt")
        assert regex.match("test.txt")
        assert not regex.match("dir/file.txt")

    def test_double_star(self):
        """Test ** matching (recursive)."""
        regex = glob_to_regex("**/*.txt")
        assert regex.match("file.txt")
        assert regex.match("dir/file.txt")
        assert regex.match("a/b/c/file.txt")

    def test_question_mark(self):
        """Test ? matching single character."""
        regex = glob_to_regex("file?.txt")
        assert regex.match("file1.txt")
        assert regex.match("fileA.txt")
        assert not regex.match("file12.txt")

    def test_exact_match(self):
        """Test exact filename matching."""
        regex = glob_to_regex(".env")
        assert regex.match(".env")
        assert not regex.match(".env.local")

    def test_character_class(self):
        """Test [abc] character class matching."""
        regex = glob_to_regex("file[123].txt")
        assert regex.match("file1.txt")
        assert regex.match("file2.txt")
        assert regex.match("file3.txt")
        assert not regex.match("file4.txt")

    def test_character_class_with_exclaim_negates(self):
        """Glob `[!abc]` negation maps to regex `[^abc]` (any char except)."""
        regex = glob_to_regex("file[!123].txt")
        # Negation: anything except 1, 2, 3 matches (including '!' itself,
        # which is not one of the negated characters).
        assert regex.match("file4.txt")
        assert regex.match("fileA.txt")
        assert regex.match("file!.txt")
        # The negated characters themselves do not match.
        assert not regex.match("file1.txt")
        assert not regex.match("file2.txt")
        assert not regex.match("file3.txt")

    def test_negated_character_class_caret(self):
        """Test [^abc] negated character class."""
        regex = glob_to_regex("file[^123].txt")
        assert regex.match("file4.txt")
        assert regex.match("fileA.txt")
        assert not regex.match("file1.txt")

    def test_character_class_with_bracket_first(self):
        """Test character class with ] as first character."""
        regex = glob_to_regex("file[]abc].txt")
        assert regex.match("file].txt")
        assert regex.match("filea.txt")
        assert not regex.match("filed.txt")

    def test_unclosed_bracket_literal(self):
        """Test that unclosed [ is treated as literal."""
        regex = glob_to_regex("file[.txt")
        assert regex.match("file[.txt")
        assert not regex.match("file1.txt")


class TestMatchesPattern:
    """Tests for pattern matching function."""

    def test_simple_match(self):
        """Test simple pattern matching."""
        assert matches_pattern("test.txt", "*.txt")
        assert not matches_pattern("test.py", "*.txt")

    def test_recursive_match(self):
        """Test recursive pattern matching with **."""
        assert matches_pattern("/home/user/.env", "**/.env")
        assert matches_pattern(".env", "**/.env")
        assert matches_pattern("project/config/.env", "**/.env")

    def test_env_patterns(self):
        """Test common .env patterns."""
        pattern = "**/.env*"
        assert matches_pattern("/home/user/.env", pattern)
        assert matches_pattern("/home/user/.env.local", pattern)
        assert matches_pattern(".env.production", pattern)

    def test_secrets_pattern(self):
        """Test secrets pattern matching."""
        pattern = "**/secrets/**"
        assert matches_pattern("/home/user/secrets/api.key", pattern)
        assert matches_pattern("project/secrets/config.json", pattern)
        assert not matches_pattern("/home/user/config.json", pattern)


class TestPermissionChecker:
    """Tests for PermissionChecker class."""

    def test_init(self):
        """Test checker initialization."""
        ruleset = PermissionRuleset()
        checker = PermissionChecker(ruleset)
        assert checker.ruleset is ruleset

    def test_check_sync_allow(self):
        """Test synchronous check returning allow."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(default="allow"),
        )
        checker = PermissionChecker(ruleset)

        action = checker.check_sync("read", "/path/to/file.txt")
        assert action == "allow"

    def test_check_sync_deny(self):
        """Test synchronous check returning deny."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/.env", action="deny")],
            ),
        )
        checker = PermissionChecker(ruleset)

        action = checker.check_sync("read", "/home/user/.env")
        assert action == "deny"

    def test_check_sync_ask(self):
        """Test synchronous check returning ask."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="ask"),
        )
        checker = PermissionChecker(ruleset)

        action = checker.check_sync("write", "/path/to/file.txt")
        assert action == "ask"

    def test_check_sync_rule_order(self):
        """Test that first matching rule wins."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(
                default="deny",
                rules=[
                    PermissionRule(pattern="**/safe/*.txt", action="allow"),
                    PermissionRule(pattern="**/*.txt", action="deny"),
                ],
            ),
        )
        checker = PermissionChecker(ruleset)

        # First rule should match
        assert checker.check_sync("read", "/project/safe/test.txt") == "allow"
        # Second rule matches
        assert checker.check_sync("read", "/project/other/test.txt") == "deny"

    def test_is_allowed(self):
        """Test is_allowed helper."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(default="allow"),
            write=OperationPermissions(default="ask"),
        )
        checker = PermissionChecker(ruleset)

        assert checker.is_allowed("read", "/file.txt")
        assert not checker.is_allowed("write", "/file.txt")

    def test_is_denied(self):
        """Test is_denied helper."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(
                default="allow",
                rules=[PermissionRule(pattern="**/.env", action="deny")],
            ),
        )
        checker = PermissionChecker(ruleset)

        assert checker.is_denied("read", "/home/.env")
        assert not checker.is_denied("read", "/home/file.txt")

    def test_requires_approval(self):
        """Test requires_approval helper."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="ask"),
            read=OperationPermissions(default="allow"),
        )
        checker = PermissionChecker(ruleset)

        assert checker.requires_approval("write", "/file.txt")
        assert not checker.requires_approval("read", "/file.txt")

    def test_find_matching_rule(self):
        """Test finding matching rules."""
        rule = PermissionRule(
            pattern="**/.env",
            action="deny",
            description="Protect env files",
        )
        ruleset = PermissionRuleset(
            read=OperationPermissions(default="allow", rules=[rule]),
        )
        checker = PermissionChecker(ruleset)

        found = checker.find_matching_rule("read", "/home/.env")
        assert found is rule

        not_found = checker.find_matching_rule("read", "/home/file.txt")
        assert not_found is None


class TestPermissionCheckerAsync:
    """Tests for async check methods."""

    @pytest.mark.asyncio
    async def test_check_allow(self):
        """Test async check for allowed operations."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(default="allow"),
        )
        checker = PermissionChecker(ruleset)

        result = await checker.check("read", "/file.txt")
        assert result is True

    @pytest.mark.asyncio
    async def test_check_deny_raises(self):
        """Test async check raises for denied operations."""
        ruleset = PermissionRuleset(
            read=OperationPermissions(default="deny"),
        )
        checker = PermissionChecker(ruleset)

        with pytest.raises(PermissionDeniedError) as exc_info:
            await checker.check("read", "/file.txt")

        assert exc_info.value.operation == "read"
        assert exc_info.value.target == "/file.txt"

    @pytest.mark.asyncio
    async def test_check_deny_with_rule_description(self):
        """Test denied error includes rule description."""
        rule = PermissionRule(
            pattern="**/.env",
            action="deny",
            description="Environment files protected",
        )
        ruleset = PermissionRuleset(
            read=OperationPermissions(default="allow", rules=[rule]),
        )
        checker = PermissionChecker(ruleset)

        with pytest.raises(PermissionDeniedError) as exc_info:
            await checker.check("read", "/home/.env")

        assert exc_info.value.rule is rule
        assert "Environment files protected" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_check_ask_with_callback_approved(self):
        """Test async check with callback that approves."""

        async def approve(op: str, target: str, reason: str) -> bool:
            return True

        ruleset = PermissionRuleset(
            write=OperationPermissions(default="ask"),
        )
        checker = PermissionChecker(ruleset, ask_callback=approve)

        result = await checker.check("write", "/file.txt", "Save changes")
        assert result is True

    @pytest.mark.asyncio
    async def test_check_ask_with_callback_denied(self):
        """Test async check with callback that denies."""

        async def deny(op: str, target: str, reason: str) -> bool:
            return False

        ruleset = PermissionRuleset(
            write=OperationPermissions(default="ask"),
        )
        checker = PermissionChecker(ruleset, ask_callback=deny)

        with pytest.raises(PermissionDeniedError):
            await checker.check("write", "/file.txt")

    @pytest.mark.asyncio
    async def test_check_ask_no_callback_error_fallback(self):
        """Test ask without callback raises error with error fallback."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="ask"),
        )
        checker = PermissionChecker(ruleset, ask_fallback="error")

        with pytest.raises(PermissionAskError) as exc_info:
            await checker.check("write", "/file.txt", "Save changes")

        assert exc_info.value.operation == "write"
        assert exc_info.value.target == "/file.txt"
        assert exc_info.value.reason == "Save changes"

    @pytest.mark.asyncio
    async def test_check_ask_no_callback_deny_fallback(self):
        """Test ask without callback denies with deny fallback."""
        ruleset = PermissionRuleset(
            write=OperationPermissions(default="ask"),
        )
        checker = PermissionChecker(ruleset, ask_fallback="deny")

        with pytest.raises(PermissionDeniedError):
            await checker.check("write", "/file.txt")


class TestPermissionErrors:
    """Tests for permission error classes."""

    def test_permission_error_message(self):
        """Test PermissionAskError message formatting."""
        error = PermissionAskError("write", "/file.txt", "Save changes")
        assert "write" in str(error)
        assert "/file.txt" in str(error)
        assert "Save changes" in str(error)

    def test_permission_error_no_reason(self):
        """Test PermissionAskError without reason."""
        error = PermissionAskError("read", "/file.txt")
        assert "read" in str(error)
        assert "/file.txt" in str(error)

    def test_permission_error_deprecated_alias(self):
        """The legacy `PermissionError` name is a deprecated subclass alias."""
        with pytest.warns(DeprecationWarning):
            error = PermissionError("read", "/file.txt")
        # Instances of the deprecated alias are still PermissionAskError.
        assert isinstance(error, PermissionAskError)
        assert "read" in str(error)

    def test_permission_denied_error_message(self):
        """Test PermissionDeniedError message formatting."""
        error = PermissionDeniedError("execute", "rm -rf /")
        assert "execute" in str(error)
        assert "rm -rf /" in str(error)

    def test_permission_denied_error_with_rule(self):
        """Test PermissionDeniedError with rule description."""
        rule = PermissionRule(
            pattern="rm -rf *",
            action="deny",
            description="Dangerous command blocked",
        )
        error = PermissionDeniedError("execute", "rm -rf /", rule)
        assert "Dangerous command blocked" in str(error)


class TestAnUnrenderablePatternIsInert:
    """`[\\]` renders to `^[\\]$`, which `re` rejects as an unterminated set.

    A rule carrying one used to raise `re.error` out of the permission check,
    which every caller is promised only ever returns an action.
    """

    def test_it_compiles_to_something_that_matches_nothing(self):
        compiled = glob_to_regex("[\\]")

        assert compiled.match("") is None
        assert compiled.match("[\\]") is None
        assert compiled.match("anything at all") is None

    def test_a_rule_carrying_one_does_not_raise(self):
        checker = PermissionChecker(
            ruleset=PermissionRuleset(
                read=OperationPermissions(
                    default="allow",
                    rules=[PermissionRule(pattern="[\\]", action="deny")],
                )
            )
        )

        assert checker.check_sync("read", "/notes.md") == "allow"

    def test_a_well_formed_class_still_works(self):
        assert matches_pattern("a.py", "[ab].py")
        assert not matches_pattern("c.py", "[ab].py")
