"""Tests for the shell commands both sandbox base classes derive from.

These were unreachable while `BaseSandbox` carried a blanket `# pragma: no
cover`: the derivation every shell-backed sandbox depends on — Docker, Daytona,
Kubernetes, and any third-party one — contributed nothing to the coverage gate.
"""

from __future__ import annotations

import base64
import re
import shlex

import pytest

from pydantic_ai_backends.backends import _shell
from pydantic_ai_backends.types import ExecuteResponse


def _ok(output: str, truncated: bool = False) -> ExecuteResponse:
    return ExecuteResponse(output=output, exit_code=0, truncated=truncated)


def _failed(output: str = "boom", exit_code: int = 2) -> ExecuteResponse:
    return ExecuteResponse(output=output, exit_code=exit_code)


class TestQuoting:
    """A path from a model is untrusted input on its way to a shell."""

    @pytest.mark.parametrize(
        "build",
        [
            lambda p: _shell.exists_command(p),
            lambda p: _shell.ls_command(p),
            lambda p: _shell.read_bytes_command(p),
            lambda p: _shell.read_command(p, 0, 10),
            lambda p: _shell.write_command(p, "x"),
            lambda p: _shell.glob_command("*.py", p),
            lambda p: _shell.grep_command("x", p),
        ],
    )
    def test_a_hostile_path_is_quoted(self, build):
        command = build("/tmp/x; rm -rf /")

        assert "; rm -rf /" not in command.replace("'/tmp/x; rm -rf /'", "")
        assert "'/tmp/x; rm -rf /'" in command


class TestExists:
    def test_it_tests_for_a_regular_file(self):
        assert _shell.exists_command("/a/b") == "test -f /a/b"


class TestLs:
    LISTING = """total 12
drwxr-xr-x 2 root root 4096 Jan  1 00:00 .
drwxr-xr-x 3 root root 4096 Jan  1 00:00 ..
drwxr-xr-x 2 root root 4096 Jan  1 00:00 src
-rw-r--r-- 1 root root  128 Jan  1 00:00 notes.md
"""

    def test_directories_sort_before_files(self):
        rows = _shell.parse_ls(_ok(self.LISTING), "/work")

        assert [(row["name"], row["is_dir"]) for row in rows] == [
            ("src", True),
            ("notes.md", False),
        ]

    def test_sizes_come_from_the_fifth_field(self):
        rows = _shell.parse_ls(_ok(self.LISTING), "/work")

        assert next(row for row in rows if row["name"] == "notes.md")["size"] == 128
        # A directory's size is its inode's, which says nothing useful.
        assert next(row for row in rows if row["name"] == "src")["size"] == 4096

    def test_dot_entries_are_dropped(self):
        rows = _shell.parse_ls(_ok(self.LISTING), "/work")

        assert {".", ".."}.isdisjoint({row["name"] for row in rows})

    def test_a_name_with_spaces_survives(self):
        listing = "total 4\n-rw-r--r-- 1 root root 1 Jan  1 00:00 my notes.md\n"

        rows = _shell.parse_ls(_ok(listing), "/work")

        assert [row["name"] for row in rows] == ["my notes.md"]

    def test_a_short_line_is_skipped(self):
        listing = "total 4\ngarbage\n-rw-r--r-- 1 root root 1 Jan  1 00:00 real.txt\n"

        assert [row["name"] for row in _shell.parse_ls(_ok(listing), "/w")] == ["real.txt"]

    def test_a_non_numeric_size_becomes_none(self):
        listing = "total 4\n-rw-r--r-- 1 root root    - Jan  1 00:00 odd.txt\n"

        assert _shell.parse_ls(_ok(listing), "/w")[0]["size"] is None

    def test_a_failed_listing_is_empty(self):
        assert _shell.parse_ls(_failed(), "/w") == []

    @pytest.mark.parametrize(
        "root,expected",
        [
            ("/work", "/work/notes.md"),
            ("/work/", "/work/notes.md"),
            ("/", "/notes.md"),
            # Built from the shell-quoted root this was `'/my work'/notes.md`,
            # which the model then sent back to `read` and which does not exist.
            ("/my work", "/my work/notes.md"),
            ("/it's mine", "/it's mine/notes.md"),
        ],
    )
    def test_the_reported_path_is_usable_not_shell_quoted(self, root: str, expected: str):
        listing = "total 4\n-rw-r--r-- 1 root root 1 Jan  1 00:00 notes.md\n"

        rows = _shell.parse_ls(_ok(listing), root)

        assert rows[0]["path"] == expected


class TestReadBytes:
    def test_it_reads_base64_not_raw_text(self):
        """Command output crosses back as text, so raw bytes could not survive."""
        assert _shell.read_bytes_command("/f") == "base64 /f"

    def test_content_is_returned(self):
        assert _shell.parse_read_bytes(_ok("aGVsbG8=")) == b"hello"

    def test_arbitrary_bytes_survive(self):
        """A PNG through `cat` came back as U+FFFD for every non-UTF-8 byte."""
        raw = bytes(range(256))

        assert _shell.parse_read_bytes(_ok(base64.b64encode(raw).decode())) == raw

    def test_the_wrapping_newlines_base64_adds_are_ignored(self):
        payload = base64.encodebytes(b"x" * 200).decode()

        assert _shell.parse_read_bytes(_ok(payload)) == b"x" * 200

    def test_failure_is_empty_never_a_message(self):
        """A caller cannot tell an error string from real file content."""
        assert _shell.parse_read_bytes(_failed("No such file")) == b""

    def test_a_truncated_payload_is_empty_not_wrong_bytes(self):
        """Base64 cut short decodes to bytes that are not the file's."""
        payload = base64.b64encode(b"x" * 300).decode()[:100]

        assert _shell.parse_read_bytes(_ok(payload, truncated=True)) == b""

    def test_output_that_is_not_base64_is_empty(self):
        assert _shell.parse_read_bytes(_ok("not base64 at all !!!")) == b""


class TestRead:
    def test_the_slice_keeps_real_line_numbers(self):
        """`sed | cat -n` would renumber from 1 and be off by the offset."""
        command = _shell.read_command("/f.txt", offset=10, limit=5)

        assert "NR>=11" in command
        assert "NR<=15" in command

    def test_output_passes_through(self):
        assert _shell.parse_read(_ok("     1\tline")) == "     1\tline"

    def test_a_truncated_read_says_so(self):
        assert _shell.parse_read(_ok("body", truncated=True)).endswith("(output truncated)")

    def test_a_failure_becomes_an_error_string(self):
        assert _shell.parse_read(_failed("No such file")).startswith("Error:")


class TestWrite:
    @staticmethod
    def _payload(command: str) -> bytes:
        """What the command actually writes, decoded back out of it."""
        encoded = command.split("printf %s ", 1)[1].split(" ", 1)[0]
        return base64.b64decode(encoded)

    def test_the_content_is_carried_encoded_so_the_shell_expands_nothing(self):
        command = _shell.write_command("/f.sh", "echo $HOME `id` \\n")

        assert "echo $HOME" not in command
        assert self._payload(command) == b"echo $HOME `id` \\n"

    def test_bytes_are_written_as_bytes(self):
        """`b'\\x89PNG'` used to be interpolated as its Python repr."""
        command = _shell.write_command("/img.png", b"\x89PNG\r\n\x1a\n")

        assert self._payload(command) == b"\x89PNG\r\n\x1a\n"

    def test_content_is_written_with_no_newline_added(self):
        """A heredoc's terminator starts a line, so it appended one."""
        assert self._payload(_shell.write_command("/f", "x")) == b"x"
        assert self._payload(_shell.write_command("/f", "x\n")) == b"x\n"

    def test_the_payload_needs_no_quoting(self):
        """base64 is [A-Za-z0-9+/=], so nothing in it can reach the shell."""
        command = _shell.write_command("/f", "'; id; echo '")

        assert re.fullmatch(r"[A-Za-z0-9+/=]*", command.split("printf %s ", 1)[1].split(" ")[0])

    def test_the_parent_directory_is_created(self):
        assert "mkdir -p $(dirname" in _shell.write_command("/deep/f", "x")

    def test_success_reports_the_path(self):
        written = _shell.parse_write(_ok(""), "/f")

        assert written.path == "/f"
        assert written.error is None

    def test_failure_carries_the_output(self):
        assert _shell.parse_write(_failed("Permission denied"), "/f").error == "Permission denied"


class TestGlob:
    def test_a_basename_pattern_matches_at_any_depth(self):
        """`find -path` tests the whole pathname, so the pattern needs `*/`."""
        command = _shell.glob_command("*.py", "/work")

        assert "-path '*/*.py'" in command
        assert "-type f" in command

    @pytest.mark.parametrize("root", ["", "/", "."])
    def test_the_backend_s_root_is_the_working_directory(self, root: str):
        """Not the machine's root, which is what `/` means to `find`.

        A glob of `*` with `/` passed through answered 2540 paths from `/proc`
        and `/usr` on a container and none from the workspace - so the agent's
        own search tool read the image it runs on, and a channel's snapshot of
        "what did the agent write" diffed two photographs of `/proc`.
        """
        assert _shell.glob_command("*.py", root).startswith("find . ")

    def test_an_absolute_root_is_still_absolute(self):
        # `/etc` is a root a caller may mean, and this is not the place to argue.
        assert _shell.glob_command("*.conf", "/etc").startswith("find /etc ")

    @pytest.mark.parametrize(
        ("pattern", "expected"),
        [("**/*", "*/*"), ("**/*.py", "*/*.py"), ("*.py", "*/*.py"), ("src/*.py", "*/src/*.py")],
    )
    def test_a_leading_globstar_is_dropped_rather_than_stacked(self, pattern: str, expected: str):
        """`find -path` matches with fnmatch: `**` is `*`, and every `/` in the
        pattern must be in the path. So `**/*` needed two slashes and missed
        every file at the top level - and `**/*` is what anything walking a tree
        reaches for."""
        assert f"-path '{expected}'" in _shell.glob_command(pattern, "/")

    def test_matches_are_sorted_by_path(self):
        rows = _shell.parse_glob(_ok("/w/b.py\n/w/a.py\n"))

        assert [row["path"] for row in rows] == ["/w/a.py", "/w/b.py"]
        assert [row["name"] for row in rows] == ["a.py", "b.py"]
        assert all(row["is_dir"] is False for row in rows)

    def test_a_failed_search_is_empty(self):
        assert _shell.parse_glob(_failed()) == []


class TestGrep:
    def test_hidden_files_are_excluded_by_default(self):
        command = _shell.grep_command("todo")

        assert "--exclude='.*'" in command
        assert "--exclude-dir='.*'" in command

    def test_hidden_files_can_be_included(self):
        assert "--exclude" not in _shell.grep_command("todo", ignore_hidden=False)

    def test_a_glob_narrows_the_search(self):
        assert "--include='*.py'" in _shell.grep_command("todo", glob="*.py")

    def test_it_searches_the_cwd_when_given_no_path(self):
        assert _shell.grep_command("todo").endswith(" .")

    def test_matches_are_parsed(self):
        found = _shell.parse_grep(_ok("a.py:12:  todo this\nb.py:3:todo that\n"))

        assert found == [
            {"path": "a.py", "line_number": 12, "line": "  todo this"},
            {"path": "b.py", "line_number": 3, "line": "todo that"},
        ]

    def test_a_colon_in_the_line_is_kept_whole(self):
        found = _shell.parse_grep(_ok("a.py:1:key: value: more"))

        assert found[0]["line"] == "key: value: more"

    def test_exit_one_means_no_match_not_failure(self):
        assert _shell.parse_grep(ExecuteResponse(output="", exit_code=1)) == []

    def test_any_other_failure_is_an_error_string(self):
        assert _shell.parse_grep(_failed("bad regex")) == "Error: bad regex"

    def test_unparseable_lines_are_skipped(self):
        found = _shell.parse_grep(_ok("no colons here\na.py:notanumber:x\nb.py:2:kept"))

        assert found == [{"path": "b.py", "line_number": 2, "line": "kept"}]


class TestGrepQuoting:
    """The pattern and glob were wrapped in literal quotes, not `shlex.quote`d.

    One of their own then closed the quoting: a search for `don't` produced an
    unterminated command, and a crafted pattern ran whatever followed it.
    """

    def test_a_pattern_containing_a_quote_stays_one_argument(self):
        command = _shell.grep_command("don't", "/w")

        assert shlex.split(command)[-2:] == ["don't", "/w"]

    def test_a_pattern_cannot_break_out_into_another_command(self):
        command = _shell.grep_command("x'; id; echo '", "/w")

        assert ";" not in shlex.split(command)
        assert "id" not in shlex.split(command)
        assert shlex.split(command)[-2] == "x'; id; echo '"

    def test_a_glob_containing_a_quote_stays_one_argument(self):
        command = _shell.grep_command("x", "/w", glob="a'b*.py")

        assert "--include=a'b*.py" in shlex.split(command)

    def test_a_pattern_starting_with_a_dash_is_a_pattern_not_an_option(self):
        command = _shell.grep_command("-v", "/w")

        assert shlex.split(command)[-3:] == ["-e", "-v", "/w"]

    def test_the_hidden_excludes_stay_quoted(self):
        """Unquoted, the shell expands `.*` against the working directory."""
        assert "--exclude=.*" in shlex.split(_shell.grep_command("x", "/w"))
        assert "'.*'" in _shell.grep_command("x", "/w")
