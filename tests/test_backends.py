"""Tests for backend implementations."""

import pytest

from pydantic_ai_backends import CompositeBackend, LocalBackend, StateBackend


class TestStateBackend:
    """Tests for StateBackend."""

    def test_write_and_read(self):
        """Test writing and reading a file."""
        backend = StateBackend()

        result = backend.write("/test.txt", "Hello, World!")
        assert result.error is None
        assert result.path == "/test.txt"

        content = backend.read("/test.txt")
        assert "Hello, World!" in content
        assert "1\t" in content  # Line number

    def test_write_multiline(self):
        """Test writing multiline content."""
        backend = StateBackend()

        content = "Line 1\nLine 2\nLine 3"
        backend.write("/multi.txt", content)

        result = backend.read("/multi.txt")
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result

    def test_read_nonexistent(self):
        """Test reading a file that doesn't exist."""
        backend = StateBackend()

        result = backend.read("/nonexistent.txt")
        assert "Error:" in result
        assert "not found" in result

    def test_edit_file(self):
        """Test editing a file."""
        backend = StateBackend()

        backend.write("/test.txt", "Hello, World!")
        result = backend.edit("/test.txt", "World", "Universe")

        assert result.error is None
        assert result.occurrences == 1

        content = backend.read("/test.txt")
        assert "Universe" in content
        assert "World" not in content

    def test_edit_multiple_occurrences(self):
        """Test editing with multiple occurrences."""
        backend = StateBackend()

        backend.write("/test.txt", "foo bar foo baz foo")

        # Should fail without replace_all
        result = backend.edit("/test.txt", "foo", "qux")
        assert result.error is not None
        assert "3 times" in result.error

        # Should succeed with replace_all
        result = backend.edit("/test.txt", "foo", "qux", replace_all=True)
        assert result.error is None
        assert result.occurrences == 3

    def test_ls_info(self):
        """Test listing directory contents."""
        backend = StateBackend()

        backend.write("/dir/file1.txt", "content1")
        backend.write("/dir/file2.txt", "content2")
        backend.write("/dir/subdir/file3.txt", "content3")

        entries = backend.ls_info("/dir")
        names = [e["name"] for e in entries]

        assert "file1.txt" in names
        assert "file2.txt" in names
        assert "subdir" in names

    def test_ls_info_reports_when_a_file_was_modified(self):
        """A listing entry carries the timestamp the store records on write."""
        backend = StateBackend()
        backend.write("/dir/file.txt", "content")

        entries = backend.ls_info("/dir")

        assert entries[0]["modified_at"] == backend.files["/dir/file.txt"]["modified_at"]

    def test_a_directory_entry_has_no_modified_time(self):
        """Directories are synthesised from paths; there is nothing to date."""
        backend = StateBackend()
        backend.write("/dir/subdir/file.txt", "content")

        (subdir,) = [e for e in backend.ls_info("/dir") if e["is_dir"]]

        assert subdir.get("modified_at") is None

    def test_a_document_from_before_the_timestamp_lists_none(self):
        """A persisted document written by an older release loads without the key."""
        legacy = {"/old.txt": {"content": ["hi"], "created_at": "2020-01-01T00:00:00+00:00"}}
        backend = StateBackend(files=legacy)  # type: ignore[arg-type]  # the old shape, deliberately

        (entry,) = backend.ls_info("/")

        assert entry.get("modified_at") is None

    def test_glob_info(self):
        """Test glob pattern matching."""
        backend = StateBackend()

        backend.write("/src/main.py", "# main")
        backend.write("/src/utils.py", "# utils")
        backend.write("/src/test.js", "// test")
        backend.write("/lib/helper.py", "# helper")

        # Find all Python files
        results = backend.glob_info("**/*.py")
        paths = [r["path"] for r in results]

        assert "/src/main.py" in paths
        assert "/src/utils.py" in paths
        assert "/lib/helper.py" in paths
        assert "/src/test.js" not in paths

    def test_grep_raw(self):
        """Test grep searching."""
        backend = StateBackend()

        backend.write("/test.py", "def hello():\n    print('Hello')")
        backend.write("/other.py", "def goodbye():\n    print('Goodbye')")

        results = backend.grep_raw("hello")
        assert isinstance(results, list)
        assert len(results) > 0
        assert any(r["path"] == "/test.py" for r in results)

    def test_path_validation(self):
        """Test path validation security."""
        backend = StateBackend()

        # Test .. traversal
        result = backend.write("/../etc/passwd", "hack")
        assert result.error is not None

        # Test ~ expansion
        result = backend.write("~/secret", "hack")
        assert result.error is not None

    def test_exists_returns_true_for_existing_file(self):
        """exists() returns True for a file that was written."""
        backend = StateBackend()
        backend.write("/foo.txt", "content")
        assert backend.exists("/foo.txt") is True

    def test_exists_returns_false_for_missing_file(self):
        """exists() returns False for a path with no stored file."""
        backend = StateBackend()
        assert backend.exists("/does-not-exist.txt") is False

    def test_exists_returns_false_for_invalid_path(self):
        """exists() returns False for paths that fail validation."""
        backend = StateBackend()
        assert backend.exists("../escape.txt") is False
        assert backend.exists("~/secret") is False

    def test_exists_distinguishes_empty_file_from_missing(self):
        """An empty file exists; a missing one does not — even though both
        round-trip through `read_bytes` as `b""`."""
        backend = StateBackend()
        backend.write("/empty.txt", "")
        assert backend.exists("/empty.txt") is True
        assert backend.exists("/never-written.txt") is False
        # read_bytes can't distinguish these — exists() is the point of this PR.
        assert backend.read_bytes("/empty.txt") == b""
        assert backend.read_bytes("/never-written.txt") == b""

    def test_write_bytes(self):
        """Test writing bytes to StateBackend."""
        backend = StateBackend()

        # Write bytes content
        result = backend.write("/binary.txt", b"Hello, bytes!")
        assert result.error is None
        assert result.path == "/binary.txt"

        # Read it back
        content = backend.read("/binary.txt")
        assert "Hello, bytes!" in content

    def test_read_bytes(self):
        """Test reading raw bytes from files.

        Only text and bytes that decode as UTF-8 here — those are stored as
        lines. Content that is not valid UTF-8 takes the base64 path and is
        covered in `tests/test_state_binary.py`.
        """
        backend = StateBackend()

        # Write text content
        backend.write("/text.txt", "Hello, World!")
        data = backend.read_bytes("/text.txt")
        assert isinstance(data, bytes)
        assert data == b"Hello, World!"

        # Write valid UTF-8 bytes content (as text will round-trip correctly)
        backend.write("/valid_utf8.txt", "Hello 世界 🌍")
        data = backend.read_bytes("/valid_utf8.txt")
        assert isinstance(data, bytes)
        assert data == "Hello 世界 🌍".encode()

        # Test multiline content
        backend.write("/multi.txt", "Line 1\nLine 2\nLine 3")
        data = backend.read_bytes("/multi.txt")
        assert isinstance(data, bytes)
        assert data == b"Line 1\nLine 2\nLine 3"

        # Test non-existent file
        data = backend.read_bytes("/nonexistent.txt")
        assert isinstance(data, bytes)
        assert data == b""

        # Test empty file
        backend.write("/empty.txt", "")
        data = backend.read_bytes("/empty.txt")
        assert isinstance(data, bytes)
        assert data == b""

    def test_edge_case_filenames(self):
        """Test handling of edge case filenames with special characters."""
        backend = StateBackend()

        # Filename with spaces
        result = backend.write("/file with spaces.txt", "Content with spaces")
        assert result.error is None
        content = backend.read("/file with spaces.txt")
        assert "Content with spaces" in content

        # Filename with special characters
        result = backend.write("/file-name_test.123.txt", "Special chars")
        assert result.error is None
        content = backend.read("/file-name_test.123.txt")
        assert "Special chars" in content

        # Filename with unicode
        result = backend.write("/файл.txt", "Unicode filename")
        assert result.error is None
        content = backend.read("/файл.txt")
        assert "Unicode filename" in content

        # Filename with emoji
        result = backend.write("/file_🚀.txt", "Emoji filename")
        assert result.error is None
        content = backend.read("/file_🚀.txt")
        assert "Emoji filename" in content

        # Deep nested path
        result = backend.write("/very/deep/nested/path/to/file.txt", "Deep nested")
        assert result.error is None
        content = backend.read("/very/deep/nested/path/to/file.txt")
        assert "Deep nested" in content

        # Path with dots
        result = backend.write("/path/to/../file.txt", "Dots path")
        assert result.error is not None  # Should fail due to .. validation

        # Multiple extensions
        result = backend.write("/archive.tar.gz", "Archive content")
        assert result.error is None
        content = backend.read("/archive.tar.gz")
        assert "Archive content" in content

        # Filename with parentheses and brackets
        result = backend.write("/file (copy) [1].txt", "Brackets and parens")
        assert result.error is None
        content = backend.read("/file (copy) [1].txt")
        assert "Brackets and parens" in content

        # Filename with quotes (single and double)
        result = backend.write("/file's-name.txt", "Single quote")
        assert result.error is None
        content = backend.read("/file's-name.txt")
        assert "Single quote" in content


class TestLocalBackend:
    """Tests for LocalBackend."""

    def test_write_and_read(self, tmp_path):
        """Test writing and reading a file."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.write("test.txt", "Hello, World!")
        assert result.error is None

        content = backend.read("test.txt")
        assert "Hello, World!" in content

    def test_write_bytes(self, tmp_path):
        """Test writing bytes to a file."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.write("binary.dat", b"\x80\x81\x82")
        assert result.error is None

        # Verify file was written as bytes
        full_path = tmp_path / "binary.dat"
        assert full_path.exists()
        assert full_path.read_bytes() == b"\x80\x81\x82"

    def test_creates_directory(self, tmp_path):
        """Test that LocalBackend creates directory automatically."""
        new_dir = tmp_path / "new_dir"
        assert not new_dir.exists()

        _ = LocalBackend(root_dir=new_dir)
        assert new_dir.exists()

    def test_edit_file(self, tmp_path):
        """Test editing a file."""
        backend = LocalBackend(root_dir=tmp_path)

        backend.write("test.txt", "Hello, World!")
        result = backend.edit("test.txt", "World", "Universe")

        assert result.error is None

        content = backend.read("test.txt")
        assert "Universe" in content

    def test_glob_info(self, tmp_path):
        """Test glob pattern matching."""
        backend = LocalBackend(root_dir=tmp_path)

        backend.write("src/main.py", "# main")
        backend.write("src/utils.py", "# utils")

        results = backend.glob_info("**/*.py")
        assert len(results) == 2

    def test_read_bytes(self, tmp_path):
        """Test reading raw bytes from files."""
        backend = LocalBackend(root_dir=tmp_path)

        # Write text content
        backend.write("text.txt", "Hello, World!")
        data = backend.read_bytes("text.txt")
        assert isinstance(data, bytes)
        assert data == b"Hello, World!"

        # Write binary content
        backend.write("binary.dat", b"\x00\x01\x02\xff\xfe")
        data = backend.read_bytes("binary.dat")
        assert isinstance(data, bytes)
        assert data == b"\x00\x01\x02\xff\xfe"

        # Test multiline content
        backend.write("multi.txt", "Line 1\nLine 2\nLine 3")
        data = backend.read_bytes("multi.txt")
        assert isinstance(data, bytes)
        assert data == b"Line 1\nLine 2\nLine 3"

        # Test non-existent file
        data = backend.read_bytes("nonexistent.txt")
        assert isinstance(data, bytes)
        assert data == b""

        # Test UTF-8 content
        backend.write("unicode.txt", "Hello 世界 🌍")
        data = backend.read_bytes("unicode.txt")
        assert isinstance(data, bytes)
        assert data == "Hello 世界 🌍".encode()

        # Test empty file
        backend.write("empty.txt", "")
        data = backend.read_bytes("empty.txt")
        assert isinstance(data, bytes)
        assert data == b""

    def test_edge_case_filenames(self, tmp_path):
        """Test handling of edge case filenames with special characters."""
        backend = LocalBackend(root_dir=tmp_path)

        # Filename with spaces
        result = backend.write("file with spaces.txt", "Content with spaces")
        assert result.error is None
        content = backend.read("file with spaces.txt")
        assert "Content with spaces" in content
        # Verify file actually exists on filesystem
        assert (tmp_path / "file with spaces.txt").exists()

        # Filename with special characters
        result = backend.write("file-name_test.123.txt", "Special chars")
        assert result.error is None
        content = backend.read("file-name_test.123.txt")
        assert "Special chars" in content

        # Filename with unicode
        result = backend.write("файл.txt", "Unicode filename")
        assert result.error is None
        content = backend.read("файл.txt")
        assert "Unicode filename" in content
        assert (tmp_path / "файл.txt").exists()

        # Filename with emoji
        result = backend.write("file_🚀.txt", "Emoji filename")
        assert result.error is None
        content = backend.read("file_🚀.txt")
        assert "Emoji filename" in content

        # Deep nested path
        result = backend.write("very/deep/nested/path/to/file.txt", "Deep nested")
        assert result.error is None
        content = backend.read("very/deep/nested/path/to/file.txt")
        assert "Deep nested" in content
        assert (tmp_path / "very" / "deep" / "nested" / "path" / "to" / "file.txt").exists()

        # Multiple extensions
        result = backend.write("archive.tar.gz", "Archive content")
        assert result.error is None
        content = backend.read("archive.tar.gz")
        assert "Archive content" in content

        # Filename with parentheses and brackets
        result = backend.write("file (copy) [1].txt", "Brackets and parens")
        assert result.error is None
        content = backend.read("file (copy) [1].txt")
        assert "Brackets and parens" in content
        assert (tmp_path / "file (copy) [1].txt").exists()

        # Filename with single quote
        result = backend.write("file's-name.txt", "Single quote")
        assert result.error is None
        content = backend.read("file's-name.txt")
        assert "Single quote" in content

        # Test editing file with spaces
        edit_result = backend.edit("file with spaces.txt", "Content", "Modified")
        assert edit_result.error is None
        content = backend.read("file with spaces.txt")
        assert "Modified with spaces" in content

        # Binary file with special name
        result = backend.write("data (binary) [v2].bin", b"\x00\x01\x02\x03")
        assert result.error is None
        data = backend.read_bytes("data (binary) [v2].bin")
        assert data == b"\x00\x01\x02\x03"

    def test_execute(self, tmp_path):
        """Test executing shell commands."""
        backend = LocalBackend(root_dir=tmp_path)

        result = backend.execute("echo 'Hello, World!'")
        assert result.exit_code == 0
        assert "Hello, World!" in result.output

    def test_execute_disabled(self, tmp_path):
        """Test that execute raises error when disabled."""

        backend = LocalBackend(root_dir=tmp_path, enable_execute=False)

        with pytest.raises(RuntimeError, match="Shell execution is disabled"):
            backend.execute("echo 'test'")

    def test_exists_returns_true_for_existing_file(self, tmp_path):
        """exists() returns True after writing a file."""
        backend = LocalBackend(root_dir=tmp_path)
        backend.write("foo.txt", "content")
        assert backend.exists("foo.txt") is True

    def test_exists_returns_false_for_missing_file(self, tmp_path):
        """exists() returns False for an unwritten path."""
        backend = LocalBackend(root_dir=tmp_path)
        assert backend.exists("does-not-exist.txt") is False

    def test_exists_returns_false_for_invalid_path(self, tmp_path):
        """exists() returns False for paths outside allowed directories."""
        backend = LocalBackend(root_dir=tmp_path)
        # Resolves outside root_dir → _validate_path raises PermissionError.
        assert backend.exists("../escape.txt") is False

    def test_exists_returns_false_for_directory(self, tmp_path):
        """A directory is not a file — exists() returns False per the contract."""
        backend = LocalBackend(root_dir=tmp_path)
        (tmp_path / "subdir").mkdir()
        assert backend.exists("subdir") is False

    def test_exists_returns_false_for_null_byte_path(self, tmp_path):
        """POSIX rejects paths with embedded NUL bytes — Path.is_file() raises
        ValueError before stat'ing. The protocol contract is "invalid paths
        return False", so this must not propagate."""
        backend = LocalBackend(root_dir=tmp_path)
        assert backend.exists("foo\x00bar.txt") is False


class TestCompositeBackend:
    """Tests for CompositeBackend."""

    def test_routing(self):
        """Test that operations are routed to correct backend."""
        memory_backend = StateBackend()
        temp_backend = StateBackend()

        composite = CompositeBackend(
            default=memory_backend,
            routes={"/temp/": temp_backend},
        )

        # Write to default backend
        composite.write("/data.txt", "default data")
        assert "/data.txt" in memory_backend.files
        assert "/data.txt" not in temp_backend.files

        # Write to temp backend
        composite.write("/temp/cache.txt", "temp data")
        assert "/temp/cache.txt" in temp_backend.files
        assert "/temp/cache.txt" not in memory_backend.files

    def test_aggregated_ls(self):
        """Test that root ls aggregates from all backends."""
        backend1 = StateBackend()
        backend2 = StateBackend()

        backend1.write("/file1.txt", "content1")
        backend2.write("/special/file2.txt", "content2")

        composite = CompositeBackend(
            default=backend1,
            routes={"/special/": backend2},
        )

        entries = composite.ls_info("/")
        names = [e["name"] for e in entries]

        assert "file1.txt" in names
        assert "special" in names  # Virtual directory for route

    def test_read_bytes(self):
        """Test reading raw bytes through composite backend.

        Note: Using StateBackend instances which store text, so binary
        data will be converted. This tests routing, not binary handling.
        """
        backend1 = StateBackend()
        backend2 = StateBackend()

        composite = CompositeBackend(
            default=backend1,
            routes={"/special/": backend2},
        )

        # Write to default backend
        composite.write("/default.txt", "default content")
        data = composite.read_bytes("/default.txt")
        assert isinstance(data, bytes)
        assert data == b"default content"

        # Write to routed backend
        composite.write("/special/routed.txt", "routed content")
        data = composite.read_bytes("/special/routed.txt")
        assert isinstance(data, bytes)
        assert data == b"routed content"

        # Write UTF-8 text to routed backend
        composite.write("/special/unicode.txt", "Hello 世界")
        data = composite.read_bytes("/special/unicode.txt")
        assert isinstance(data, bytes)
        assert data == "Hello 世界".encode()

        # Test non-existent file
        data = composite.read_bytes("/nonexistent.txt")
        assert isinstance(data, bytes)
        assert data == b""

    def test_edge_case_filenames_routing(self):
        """Test routing with edge case filenames across different backends."""
        # Use StateBackend for virtual path handling
        default_backend = StateBackend()
        special_backend = StateBackend()

        composite = CompositeBackend(
            default=default_backend,
            routes={"/special/": special_backend},
        )

        # File with spaces in default backend
        result = composite.write("/file with spaces.txt", "default backend")
        assert result.error is None
        content = composite.read("/file with spaces.txt")
        assert "default backend" in content

        # File with spaces in routed backend
        result = composite.write("/special/file with spaces.txt", "routed backend")
        assert result.error is None
        content = composite.read("/special/file with spaces.txt")
        assert "routed backend" in content

        # Unicode filenames in different backends
        result = composite.write("/файл.txt", "default unicode")
        assert result.error is None
        result = composite.write("/special/文件.txt", "routed unicode")
        assert result.error is None

        # Emoji filenames
        result = composite.write("/emoji_😀.txt", "default emoji")
        assert result.error is None
        result = composite.write("/special/emoji_🚀.txt", "routed emoji")
        assert result.error is None

        # Complex filenames with special chars
        result = composite.write("/report (final) [v2.1].txt", "default complex")
        assert result.error is None
        result = composite.write("/special/data [backup] (2024).txt", "routed complex")
        assert result.error is None

        # Verify routing is correct
        assert "default complex" in composite.read("/report (final) [v2.1].txt")
        assert "routed complex" in composite.read("/special/data [backup] (2024).txt")

        # Deep nested paths with special chars
        result = composite.write("/deep/path with spaces/file.txt", "default nested")
        assert result.error is None
        result = composite.write("/special/deep/path (v2)/file's.txt", "routed nested")
        assert result.error is None

        # Test editing files with special names across backends
        edit_result = composite.edit("/file with spaces.txt", "default", "modified")
        assert edit_result.error is None
        assert "modified backend" in composite.read("/file with spaces.txt")

        edit_result = composite.edit("/special/file with spaces.txt", "routed", "updated")
        assert edit_result.error is None
        assert "updated backend" in composite.read("/special/file with spaces.txt")

    def test_exists_routes_to_correct_backend(self):
        """exists() consults the backend the path routes to, not the default."""
        default_backend = StateBackend()
        special_backend = StateBackend()
        composite = CompositeBackend(
            default=default_backend,
            routes={"/special/": special_backend},
        )

        composite.write("/data.txt", "default")
        composite.write("/special/cache.txt", "routed")

        # True via default route
        assert composite.exists("/data.txt") is True
        # True via /special/ route
        assert composite.exists("/special/cache.txt") is True
        # False — file lives in special_backend, not default_backend
        assert default_backend.exists("/special/cache.txt") is False

    def test_exists_returns_false_for_missing_file(self):
        """Missing paths return False regardless of which backend routes them."""
        composite = CompositeBackend(
            default=StateBackend(),
            routes={"/special/": StateBackend()},
        )
        assert composite.exists("/missing.txt") is False
        assert composite.exists("/special/missing.txt") is False

    def test_exists_returns_false_for_invalid_path(self):
        """Invalid paths return False (routed to a StateBackend which rejects them)."""
        composite = CompositeBackend(default=StateBackend())
        assert composite.exists("../escape.txt") is False
