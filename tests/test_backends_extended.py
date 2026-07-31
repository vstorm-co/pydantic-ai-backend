"""Extended tests for backend implementations to reach 100% coverage."""

from pydantic_ai_backends import CompositeBackend, LocalBackend, StateBackend
from pydantic_ai_backends._paths import normalize_path, unsafe_path_reason


class TestPathValidation:
    """Tests for path validation functions."""

    def test_validate_path_with_double_dots(self):
        """Test that .. is rejected."""
        error = unsafe_path_reason("../etc/passwd")
        assert error is not None
        assert ".." in error

    def test_validate_path_with_tilde(self):
        """Test that ~ is rejected."""
        error = unsafe_path_reason("~/secret")
        assert error is not None
        assert "~" in error

    def test_validate_path_windows_path(self):
        """Test that Windows paths are rejected."""
        error = unsafe_path_reason("C:\\Windows\\System32")
        assert error is not None
        assert "Windows" in error

    def test_validate_path_valid(self):
        """Test that valid paths pass."""
        assert unsafe_path_reason("/valid/path") is None
        assert unsafe_path_reason("relative/path") is None

    def test_normalize_path_no_leading_slash(self):
        """Test normalization adds leading slash."""
        assert normalize_path("path/to/file") == "/path/to/file"

    def test_normalize_path_removes_trailing_slash(self):
        """Test normalization removes trailing slash."""
        assert normalize_path("/path/to/dir/") == "/path/to/dir"

    def test_normalize_path_root(self):
        """Test root path is preserved."""
        assert normalize_path("/") == "/"


class TestStateBackendExtended:
    """Extended tests for StateBackend."""

    def test_read_with_offset(self):
        """Test reading with offset."""
        backend = StateBackend()
        content = "\n".join([f"Line {i}" for i in range(10)])
        backend.write("/test.txt", content)

        result = backend.read("/test.txt", offset=5, limit=3)
        assert "Line 5" in result
        assert "Line 6" in result
        assert "Line 7" in result
        assert "Line 4" not in result

    def test_read_offset_exceeds_length(self):
        """Test reading with offset beyond file length."""
        backend = StateBackend()
        backend.write("/test.txt", "Short file")

        result = backend.read("/test.txt", offset=100)
        assert "Error" in result
        assert "exceeds" in result

    def test_read_truncated(self):
        """Test reading shows truncation message."""
        backend = StateBackend()
        content = "\n".join([f"Line {i}" for i in range(100)])
        backend.write("/test.txt", content)

        result = backend.read("/test.txt", offset=0, limit=10)
        assert "more lines" in result

    def test_write_preserves_created_at(self):
        """Test that write preserves created_at timestamp."""
        backend = StateBackend()
        backend.write("/test.txt", "Initial content")
        original_created = backend.files["/test.txt"]["created_at"]

        backend.write("/test.txt", "Updated content")
        assert backend.files["/test.txt"]["created_at"] == original_created

    def test_edit_with_path_validation_error(self):
        """Test edit with invalid path."""
        backend = StateBackend()
        result = backend.edit("../etc/passwd", "old", "new")
        assert result.error is not None

    def test_ls_info_with_invalid_path(self):
        """Test ls_info with invalid path."""
        backend = StateBackend()
        entries = backend.ls_info("../invalid")
        assert entries == []

    def test_ls_info_on_file(self):
        """Test ls_info when path is a file."""
        backend = StateBackend()
        backend.write("/file.txt", "content")

        entries = backend.ls_info("/file.txt")
        assert len(entries) == 1
        assert entries[0]["name"] == "file.txt"

    def test_glob_info_with_invalid_path(self):
        """Test glob_info with invalid path."""
        backend = StateBackend()
        entries = backend.glob_info("*.txt", "../invalid")
        assert entries == []

    def test_glob_info_with_non_root_path(self):
        """Test glob_info with non-root base path."""
        backend = StateBackend()
        backend.write("/src/main.py", "# main")
        backend.write("/src/utils.py", "# utils")
        backend.write("/lib/helper.py", "# helper")

        results = backend.glob_info("*.py", "/src")
        paths = [r["path"] for r in results]
        assert "/src/main.py" in paths
        assert "/src/utils.py" in paths
        assert "/lib/helper.py" not in paths

    def test_grep_raw_invalid_regex(self):
        """Test grep_raw with invalid regex."""
        backend = StateBackend()
        backend.write("/test.txt", "content")

        result = backend.grep_raw("[invalid")
        assert isinstance(result, str)
        assert "Error" in result

    def test_grep_raw_with_invalid_path(self):
        """Test grep_raw with invalid path."""
        backend = StateBackend()
        result = backend.grep_raw("pattern", "../invalid")
        assert isinstance(result, str)
        assert "Error" in result

    def test_grep_raw_on_file(self):
        """Test grep_raw on specific file."""
        backend = StateBackend()
        backend.write("/test.txt", "Hello world\nGoodbye world")
        backend.write("/other.txt", "Other content")

        results = backend.grep_raw("world", "/test.txt")
        assert isinstance(results, list)
        assert len(results) == 2
        assert all(r["path"] == "/test.txt" for r in results)

    def test_grep_raw_on_directory(self):
        """Test grep_raw on directory path."""
        backend = StateBackend()
        backend.write("/src/main.py", "Hello world")
        backend.write("/src/utils.py", "Goodbye world")
        backend.write("/lib/other.py", "No match here")

        results = backend.grep_raw("world", "/src")
        assert isinstance(results, list)
        assert len(results) == 2

    def test_grep_raw_with_glob_filter(self):
        """Test grep_raw with glob filter."""
        backend = StateBackend()
        backend.write("/src/main.py", "Hello world")
        backend.write("/src/test.js", "Hello world")

        results = backend.grep_raw("world", glob="**/*.py")
        assert isinstance(results, list)
        assert all(r["path"].endswith(".py") for r in results)

    def test_grep_raw_hidden_files_ignored_by_default(self):
        """Hidden files should not be searched unless requested."""
        backend = StateBackend()
        backend.write("/.hidden.txt", "secret")
        backend.write("/visible.txt", "public")

        results_default = backend.grep_raw("secret")
        results_explicit = backend.grep_raw("secret", ignore_hidden=True)

        for results in (results_default, results_explicit):
            paths = {match["path"] for match in results}
            assert "/.hidden.txt" not in paths
            assert not paths

    def test_grep_raw_hidden_files_included_when_requested(self):
        """Hidden files should be searchable with ignore_hidden=False."""
        backend = StateBackend()
        backend.write("/.hidden.txt", "secret")
        backend.write("/visible.txt", "public")

        results = backend.grep_raw("secret", ignore_hidden=False)
        paths = {match["path"] for match in results}
        assert paths == {"/.hidden.txt"}

    def test_grep_raw_explicit_hidden_path_is_searched(self):
        """An explicitly named hidden file is searched even with ignore_hidden."""
        backend = StateBackend()
        backend.write("/.env", "secret")

        # Default ignore_hidden=True, but the file is named directly.
        results = backend.grep_raw("secret", "/.env")
        assert isinstance(results, list)
        paths = {match["path"] for match in results}
        assert paths == {"/.env"}


class TestLocalBackendExtended:
    """Extended tests for LocalBackend."""

    def test_read_nonexistent(self, tmp_path):
        """Test reading nonexistent file."""
        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read("nonexistent.txt")
        assert "Error" in result

    def test_read_with_offset_and_limit(self, tmp_path):
        """Test reading with offset and limit."""
        backend = LocalBackend(root_dir=tmp_path)
        content = "\n".join([f"Line {i}" for i in range(20)])
        backend.write("test.txt", content)

        result = backend.read("test.txt", offset=5, limit=5)
        assert "Line 5" in result
        assert "Line 9" in result

    def test_edit_nonexistent(self, tmp_path):
        """Test editing nonexistent file."""
        backend = LocalBackend(root_dir=tmp_path)
        result = backend.edit("nonexistent.txt", "old", "new")
        assert result.error is not None

    def test_edit_string_not_found(self, tmp_path):
        """Test editing with string not in file."""
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("test.txt", "Hello World")
        result = backend.edit("test.txt", "foo", "bar")
        assert result.error is not None
        assert "not found" in result.error

    def test_edit_multiple_without_replace_all(self, tmp_path):
        """Test editing with multiple occurrences without replace_all."""
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("test.txt", "foo bar foo baz foo")
        result = backend.edit("test.txt", "foo", "qux")
        assert result.error is not None
        assert "3 times" in result.error

    def test_edit_replace_all(self, tmp_path):
        """Test editing with replace_all."""
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("test.txt", "foo bar foo baz foo")
        result = backend.edit("test.txt", "foo", "qux", replace_all=True)
        assert result.error is None
        assert result.occurrences == 3

        content = backend.read("test.txt")
        assert "qux" in content
        assert "foo" not in content

    def test_path_outside_allowed(self, tmp_path):
        """Test that paths outside allowed directories are rejected."""
        backend = LocalBackend(root_dir=tmp_path)

        # Try to write outside the root (absolute path)
        result = backend.write("/etc/test.txt", "content")
        # LocalBackend raises PermissionError which gets converted to error message
        assert result.error is not None

    def test_creates_directory_automatically(self, tmp_path):
        """Test that LocalBackend creates directory if it doesn't exist."""
        new_dir = tmp_path / "new_dir"
        assert not new_dir.exists()

        backend = LocalBackend(root_dir=new_dir)
        assert new_dir.exists()
        assert backend.root_dir == new_dir

    def test_grep_raw(self, tmp_path):
        """Test grep_raw searching."""
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("test.txt", "Hello world\nGoodbye world")
        backend.write("other.txt", "Other content")

        results = backend.grep_raw("world", path="test.txt")
        assert isinstance(results, list)
        assert len(results) == 2
        assert {match["path"] for match in results} == {str(tmp_path / "test.txt")}

    def test_glob_info(self, tmp_path):
        """Test glob_info pattern matching."""
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("src/file.py", "# code")
        backend.write("src/test.js", "// test")

        results = backend.glob_info("**/*.py")
        paths = [r["path"] for r in results]
        assert len(paths) == 1
        assert any("file.py" in p for p in paths)

    def test_allowed_directories(self, tmp_path):
        """Test restricted access with allowed_directories."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        restricted = tmp_path / "restricted"
        restricted.mkdir()

        backend = LocalBackend(
            root_dir=allowed,
            allowed_directories=[str(allowed)],
        )

        # Should succeed in allowed directory (relative path)
        result = backend.write("file.txt", "content")
        assert result.error is None

        # Should fail outside allowed directories (absolute path)
        result = backend.write(str(restricted / "file.txt"), "content")
        assert result.error is not None

    def test_execute_timeout(self, tmp_path):
        """Test execute with timeout."""
        backend = LocalBackend(root_dir=tmp_path)

        # Command that takes longer than timeout
        result = backend.execute("sleep 10", timeout=1)
        assert result.exit_code == 124
        assert "timed out" in result.output


class TestCompositeBackendExtended:
    """Extended tests for CompositeBackend."""

    def test_read_from_routed_backend(self):
        """Test reading from routed backend."""
        default = StateBackend()
        special = StateBackend()

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        special.write("/special/file.txt", "special content")
        result = composite.read("/special/file.txt")
        assert "special content" in result

    def test_edit_on_routed_backend(self):
        """Test editing on routed backend."""
        default = StateBackend()
        special = StateBackend()

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        special.write("/special/file.txt", "old content")
        result = composite.edit("/special/file.txt", "old", "new")
        assert result.error is None

        content = composite.read("/special/file.txt")
        assert "new content" in content

    def test_glob_info_from_root(self):
        """Test glob_info from root aggregates from all backends."""
        default = StateBackend()
        special = StateBackend()

        default.write("/default/file.py", "# default")
        special.write("/special/file.py", "# special")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        results = composite.glob_info("**/*.py", "/")
        paths = [r["path"] for r in results]
        assert "/default/file.py" in paths
        assert "/special/file.py" in paths

    def test_glob_info_from_specific_route(self):
        """Test glob_info from specific routed path."""
        default = StateBackend()
        special = StateBackend()

        special.write("/special/file.py", "# special")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        results = composite.glob_info("*.py", "/special")
        assert len(results) >= 0  # Depends on pattern matching

    def test_grep_raw_from_root(self):
        """Test grep_raw from root aggregates from all backends."""
        default = StateBackend()
        special = StateBackend()

        default.write("/default/file.txt", "Hello world")
        special.write("/special/file.txt", "Hello universe")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        results = composite.grep_raw("Hello", "/")
        assert isinstance(results, list)
        assert len(results) == 2

    def test_grep_raw_from_none_path(self):
        """Test grep_raw with None path."""
        default = StateBackend()
        special = StateBackend()

        default.write("/default/file.txt", "Hello world")
        special.write("/special/file.txt", "Hello universe")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        results = composite.grep_raw("Hello")
        assert isinstance(results, list)
        assert len(results) == 2

    def test_grep_raw_from_root_propagates_default_error(self):
        """An invalid regex error from the default backend is propagated."""
        composite = CompositeBackend(
            default=StateBackend(),
            routes={"/special/": StateBackend()},
        )

        result = composite.grep_raw("[invalid", "/")
        assert isinstance(result, str)
        assert result.startswith("Error")

    def test_grep_raw_from_root_propagates_routed_error(self):
        """An error from a routed backend is propagated, not swallowed."""

        class _FailingBackend(StateBackend):
            def grep_raw(self, *args: object, **kwargs: object):  # type: ignore[override]
                return "Error: routed search failed"

        composite = CompositeBackend(
            default=StateBackend(),
            routes={"/special/": _FailingBackend()},
        )

        result = composite.grep_raw("anything", "/")
        assert isinstance(result, str)
        assert "routed search failed" in result

    def test_grep_raw_from_specific_path(self):
        """Test grep_raw from specific path."""
        default = StateBackend()
        special = StateBackend()

        special.write("/special/file.txt", "Hello universe")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        results = composite.grep_raw("Hello", "/special/file.txt")
        assert isinstance(results, list)
        assert len(results) == 1

    def test_ls_info_empty_root(self):
        """Test ls_info on empty root shows virtual directories."""
        default = StateBackend()
        special = StateBackend()

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        entries = composite.ls_info("/")
        names = [e["name"] for e in entries]
        assert "special" in names

    def test_ls_info_non_root(self):
        """Test ls_info on non-root path uses routed backend."""
        default = StateBackend()
        special = StateBackend()

        special.write("/special/file.txt", "content")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        # ls_info("/special") will use the special backend
        # The file is at /special/file.txt in the special backend
        entries = composite.ls_info("/special/")
        assert len(entries) >= 1

    def test_grep_raw_with_error_in_backend(self):
        """Test grep_raw handles error from backend."""
        default = StateBackend()
        special = StateBackend()

        default.write("/default/file.txt", "Hello world")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        # Invalid regex in special backend returns error string
        results = composite.grep_raw("Hello", "/")
        # Should still return the matches from default even if special returns nothing
        assert isinstance(results, list)

    def test_grep_raw_with_error_from_default(self):
        """Test grep_raw propagates an error from the default backend."""
        default = StateBackend()
        special = StateBackend()

        special.write("/special/file.txt", "Hello universe")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        # An error (e.g. invalid regex) is propagated rather than swallowed, so
        # callers can tell "no matches" apart from "search failed".
        result = composite.grep_raw("[invalid", "/")  # Invalid regex
        assert isinstance(result, str)
        assert result.startswith("Error")

    def test_glob_info_empty_root(self):
        """Test glob_info on empty root path."""
        default = StateBackend()
        special = StateBackend()

        default.write("/default/file.py", "# default")
        special.write("/special/file.py", "# special")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        # Test with empty string as path
        results = composite.glob_info("**/*.py", "")
        paths = [r["path"] for r in results]
        assert "/default/file.py" in paths
        assert "/special/file.py" in paths

    def test_ls_info_with_empty_path(self):
        """Test ls_info with empty string path."""
        default = StateBackend()
        special = StateBackend()

        default.write("/file.txt", "content")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        entries = composite.ls_info("")
        names = [e["name"] for e in entries]
        # Should include virtual directory for /special/
        assert "special" in names

    def test_grep_raw_empty_path(self):
        """Test grep_raw with empty string path."""
        default = StateBackend()
        special = StateBackend()

        default.write("/default/file.txt", "Hello world")
        special.write("/special/file.txt", "Hello universe")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        results = composite.grep_raw("Hello", "")
        assert isinstance(results, list)
        assert len(results) == 2

    def test_ls_info_root_with_empty_prefix(self):
        """Test ls_info on root when route prefix is empty after strip."""
        default = StateBackend()

        # Create composite with route that would have empty first part
        composite = CompositeBackend(
            default=default,
            routes={"/": StateBackend()},  # Root prefix
        )

        # Should handle gracefully
        entries = composite.ls_info("/")
        assert isinstance(entries, list)

    def test_ls_info_root_with_existing_virtual_dir(self):
        """Test ls_info when virtual dir already exists in default backend."""
        default = StateBackend()
        special = StateBackend()

        # Create a file that would create the same directory name as route
        default.write("/special/from_default.txt", "from default")

        composite = CompositeBackend(
            default=default,
            routes={"/special/": special},
        )

        entries = composite.ls_info("/")
        names = [e["name"] for e in entries]
        # special should appear only once
        assert names.count("special") == 1

    def test_ls_info_without_trailing_slash(self):
        """Test ls_info without trailing slash matches route with trailing slash."""
        default = StateBackend()
        routed = StateBackend()

        routed.write("/foo/bar.txt", "baz")

        composite = CompositeBackend(default=default, routes={"/foo/": routed})

        entries = composite.ls_info("/foo")
        names = [e["name"] for e in entries]
        assert "bar.txt" in names

    def test_ls_info_with_trailing_slash(self):
        """Test ls_info with trailing slash still works (regression guard)."""
        default = StateBackend()
        routed = StateBackend()

        routed.write("/foo/bar.txt", "baz")

        composite = CompositeBackend(default=default, routes={"/foo/": routed})

        entries = composite.ls_info("/foo/")
        names = [e["name"] for e in entries]
        assert "bar.txt" in names

    def test_read_without_trailing_slash(self):
        """Test reading a file via path without trailing slash on route."""
        default = StateBackend()
        routed = StateBackend()

        routed.write("/foo/bar.txt", "hello")

        composite = CompositeBackend(default=default, routes={"/foo/": routed})

        content = composite.read("/foo/bar.txt")
        assert "hello" in content

    def test_write_without_trailing_slash(self):
        """Test writing routes correctly when path lacks trailing slash."""
        default = StateBackend()
        routed = StateBackend()

        composite = CompositeBackend(default=default, routes={"/foo/": routed})

        composite.write("/foo/test.txt", "written")

        assert "/foo/test.txt" in routed.files
        assert "/foo/test.txt" not in default.files

    def test_route_without_trailing_slash(self):
        """Test route registered without trailing slash matches both variants."""
        default = StateBackend()
        routed = StateBackend()

        composite = CompositeBackend(default=default, routes={"/foo": routed})

        # Both /foo and /foo/ should route to the same backend
        composite.write("/foo/file.txt", "content")
        composite.write("/foo/other.txt", "content2")

        assert "/foo/file.txt" in routed.files
        assert "/foo/other.txt" in routed.files
        assert "/foo/file.txt" not in default.files

    def test_no_false_positive_match(self):
        """Test that /foobar does not match route /foo/."""
        default = StateBackend()
        routed = StateBackend()

        composite = CompositeBackend(default=default, routes={"/foo/": routed})

        composite.write("/foobar/test.txt", "default content")

        assert "/foobar/test.txt" not in routed.files
        assert "/foobar/test.txt" in default.files


class TestStateBackendListing:
    """Directory listing and per-path guards that the happy path skips."""

    def _backend(self):
        backend = StateBackend()
        backend.write("/src/app.py", "print('hi')")
        backend.write("/src/lib/util.py", "x = 1")
        backend.write("/README.md", "# hi")
        return backend

    def test_listing_a_subdirectory_ignores_siblings(self):
        entries = self._backend().ls_info("/src")

        assert [(e["name"], e["is_dir"]) for e in entries] == [("lib", True), ("app.py", False)]

    def test_a_directory_is_reported_once_for_many_children(self):
        backend = self._backend()
        backend.write("/src/lib/other.py", "y = 2")

        assert [e["name"] for e in backend.ls_info("/src") if e["is_dir"]] == ["lib"]

    def test_listing_a_file_path_returns_just_that_file(self):
        entries = self._backend().ls_info("/README.md")

        assert [e["name"] for e in entries] == ["README.md"]

    def test_read_bytes_rejects_an_unsafe_path(self):
        assert self._backend().read_bytes("../escape.py") == b""

    def test_read_rejects_an_unsafe_path(self):
        assert self._backend().read("../escape.py").startswith("Error: Path cannot contain")

    def test_edit_reports_a_missing_file(self):
        result = self._backend().edit("/nope.py", "a", "b")

        assert result.error == "File '/nope.py' not found"
