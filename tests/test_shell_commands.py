"""Tests for the `find` and `grep` commands the console's search tools run in a workspace."""

from __future__ import annotations

import shlex

import pytest

from pydantic_ai_backends.toolsets import _shell
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
            lambda p: _shell.glob_command("*.py", p),
            lambda p: _shell.grep_command("x", p),
        ],
    )
    def test_a_hostile_path_is_quoted(self, build):
        command = build("/tmp/x; rm -rf /")

        assert "; rm -rf /" not in command.replace("'/tmp/x; rm -rf /'", "")
        assert "'/tmp/x; rm -rf /'" in command


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

        assert "--exclude-dir='.[!./]*'" in command
        assert "--exclude-dir='*/.[!.]*'" in command
        assert "--exclude=" not in command

    def test_hidden_files_can_be_included(self):
        assert "--exclude" not in _shell.grep_command("todo", ignore_hidden=False)

    def test_a_glob_narrows_the_search(self):
        assert "--include='*.py'" in _shell.grep_command("todo", glob="*.py")

    def test_it_searches_the_cwd_when_given_no_path(self):
        assert _shell.grep_command("todo").endswith(" .")

    def test_every_hit_names_its_file(self):
        """GNU grep leaves the name out for a single file, which parses as no match."""
        assert shlex.split(_shell.grep_command("todo", "a.py"))[1] == "-rnH"

    async def test_a_single_file_is_searched(self, tmp_path) -> None:
        from pydantic_ai_backends.toolsets._workspace import WorkspaceOps
        from tests.support import local

        (tmp_path / "a.py").write_text("todo: this\n")
        matches = await WorkspaceOps(local(tmp_path)).grep_raw("todo", "a.py")
        assert matches == [{"path": "a.py", "line_number": 1, "line": "todo: this"}]

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
        """Unquoted, the shell expands them against the working directory."""
        argv = shlex.split(_shell.grep_command("x", "/w"))
        assert {"--exclude-dir=.[!./]*", "--exclude-dir=*/.[!.]*"} <= set(argv)
        assert "'.[!./]*'" in _shell.grep_command("x", "/w")


class TestHiddenExclusionKeepsTheStartingDirectory:
    async def test_a_search_of_the_working_directory_finds_files(self, tmp_path) -> None:
        """BSD grep applies `--exclude-dir` to `.` itself; `.*` matched it."""
        from pydantic_ai_backends.toolsets._workspace import WorkspaceOps
        from tests.support import local

        (tmp_path / "visible.txt").write_text("needle")
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "secret.txt").write_text("needle")
        matches = await WorkspaceOps(local(tmp_path)).grep_raw("needle")
        assert isinstance(matches, list)
        assert [m["path"].removeprefix("./") for m in matches] == ["visible.txt"]

    async def test_subdirectories_are_searched_and_hidden_ones_skipped(self, tmp_path) -> None:
        """BSD grep matched `.[!.]*` against `./src` and skipped every subdirectory."""
        from pydantic_ai_backends.toolsets._workspace import WorkspaceOps
        from tests.support import local

        for path in ("src/app.py", "a.b/c.txt", ".git/HEAD", "src/.cache/x"):
            (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / path).write_text("needle")
        matches = await WorkspaceOps(local(tmp_path)).grep_raw("needle")
        assert isinstance(matches, list)
        assert sorted(m["path"].removeprefix("./") for m in matches) == ["a.b/c.txt", "src/app.py"]


class TestHiddenMatch:
    @pytest.mark.parametrize(
        ("path", "root", "hidden"),
        [
            ("./notes.txt", None, False),
            ("./.env", None, True),
            ("./src/.cache/x", ".", True),
            (".github/workflows/ci.yml", ".github", False),
            ("/w/.github/ci.yml", "/w", True),
            ("/w/src/x.py", "/w/", False),
            ("../up.txt", None, False),
        ],
    )
    def test_measured_from_the_search_root(self, path: str, root: str | None, hidden: bool) -> None:
        assert _shell.hidden_match(path, root) is hidden
