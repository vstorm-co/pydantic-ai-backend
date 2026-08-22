"""What the model reads about each console tool.

A tool definition is a prompt: the model chooses a tool, and fills in its
arguments, from nothing but the text here. So each tool's text is one object
rather than a loose string — :class:`ToolText` holds what the tool does, when to
use it and when not, what every argument means, and **what comes back**, which
is the half a tool description usually leaves out. The model then knows that
`grep` answers three different shapes, that a listing may be a truncated slice,
and that a failed `execute` still returns its output.

What reaches the model is shaped the way pydantic-ai shapes a docstring with a
`Returns:` section - `<summary>` around the prose, `<returns>` around the return
description - so these tools read the same as every tool built from a docstring
rather than introducing a second convention into one tool list.

Two audiences read this text and they need different amounts of it. A coding
agent wants the git and dependency guidance; an agent that keeps a scratch
workspace for a report never sees a repository and pays for those sentences on
every request. `profile` decides: `"coding"` includes the extra block, `"agent"`
leaves it out.

The `*_DESCRIPTION` constants are still exported, rendered from the same objects,
so a caller that imports one keeps working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

Profile = Literal["agent", "coding"]
"""How much guidance a tool's description carries.

`"coding"` is everything, including the block written for an agent working in a
repository. `"agent"` drops that block and keeps what is true of any workspace.
"""

DEFAULT_PROFILE: Profile = "coding"
"""Profile used when a caller names none — what every existing caller had."""


@dataclass(frozen=True)
class ToolText:
    """Everything the model reads about one tool.

    Held as fields rather than one string because the parts have different
    destinations: `summary`, `usage`, `coding` and `returns` are composed into
    the tool's description, while `args` becomes the per-argument text in its
    JSON schema. Splitting them is also what lets a host show `summary` in its
    own catalogue and know it is the first sentence the model reads, rather than
    a paraphrase written in another repository.
    """

    summary: str
    """One sentence: what the tool does. Also what a catalogue should show."""

    usage: str = ""
    """When to use it, when to use another tool, and what it will not do."""

    coding: str = ""
    """Guidance only an agent working in a repository needs.

    Rendered under the `"coding"` profile and omitted under `"agent"`. Anything
    true of any workspace belongs in `usage` instead.
    """

    args: Mapping[str, str] = field(default_factory=dict)
    """One entry per argument, keyed exactly as the parameter is named."""

    returns: str = ""
    """The shape of the result, including its failures and its truncation."""

    def render(self, profile: Profile = DEFAULT_PROFILE) -> str:
        """The description handed to the model.

        Shaped the way pydantic-ai shapes a docstring that has a `Returns:`
        section - the prose inside `<summary>`, the return description inside
        `<returns>` - because that is what every tool built from a docstring
        already sends, and a host registering these beside its own would
        otherwise put two conventions in one tool list. A prose `Returns:`
        paragraph was the first attempt and is what that inconsistency looked
        like. `tests/test_tool_text.py` pins the shape against a tool the
        framework renders itself, so a change there fails here rather than
        drifting quietly.

        Args:
            profile: Which audience to write for.
        """
        parts = [self.summary]
        if self.usage:
            parts.append(self.usage)
        if self.coding and profile == "coding":
            parts.append(self.coding)
        body = "\n\n".join(parts)
        if not self.returns:
            return body
        return (
            f"<summary>{body}</summary>\n"
            f"<returns>\n<description>{self.returns}</description>\n</returns>"
        )

    def docstring(self) -> str:
        """A Google-style docstring carrying the argument text.

        Set on the tool function before it is registered, because per-argument
        descriptions reach the JSON schema through the docstring and through
        nothing else — which is why they cannot simply live in `render`. The
        summary is repeated here for a reader of the generated docstring; the
        model reads `render` instead, since an explicit description wins over
        the docstring's own summary.
        """
        lines = [self.summary]
        if self.args:
            lines.append("")
            lines.append("Args:")
            lines.extend(f"    {name}: {text}" for name, text in self.args.items())
        return "\n".join(lines)


CONSOLE_SYSTEM_PROMPT = """\
## Console Tools

You have access to filesystem tools (ls, read_file, write_file, edit_file, \
glob, grep) and shell execution (execute). Read each tool's description for \
detailed usage guidance.
"""

HASHLINE_CONSOLE_PROMPT = """\
## Console Tools — Hashline Edit Mode

You have access to filesystem tools (ls, read_file, write_file, hashline_edit, \
glob, grep) and shell execution (execute). File contents use **hashline format** \
— each line is tagged with a content hash. Read each tool's description for \
detailed usage guidance.
"""


LS_TEXT = ToolText(
    summary="List the files and directories at a path, with their sizes.",
    usage=(
        "Use it to see what one directory holds. To find files by name or extension "
        "anywhere below it, use `glob`: that searches recursively and this does not."
    ),
    args={"path": "Directory to list. Defaults to the working directory."},
    returns=(
        "One line per entry, directories marked with a trailing `/` and files with "
        "their size in bytes. An empty directory and a missing one answer with the "
        "same sentence, so an empty listing is not proof the directory exists."
    ),
)

READ_FILE_TEXT = ToolText(
    summary="Read a file's contents, with line numbers.",
    usage=(
        "Read a file before editing it: `edit_file` matches on exact text and refuses "
        "an edit to a file that changed since your last read, so this is both how you "
        "learn what to replace and how you clear that check.\n\n"
        "`offset` counts from 0 while the line numbers shown count from 1, so "
        "`offset=100` starts at the line labelled 101. For a long file, read a first "
        "slice with `limit=100` to see its shape, then read the part you need. Call "
        "this tool more than once in a response when you need several files.\n\n"
        "An image or PDF may come back as content you can look at directly rather "
        "than as text; work from what you see instead of parsing it."
    ),
    args={
        "path": "File to read, absolute or relative to the working directory.",
        "offset": "First line to return, counting from 0. Defaults to 0.",
        "limit": "How many lines to return. Defaults to 2000.",
    },
    returns=(
        "The requested lines, each prefixed with its 1-based line number and a tab, "
        "with a `... (N more lines)` trailer when the slice stops short of the end. A "
        "missing file, a binary one or an offset past the end come back as a line "
        "starting `Error:` — the run continues, so correct it and call again."
    ),
)

HASHLINE_READ_FILE_TEXT = ToolText(
    summary="Read a file's contents, each line tagged with a content hash.",
    usage=(
        "Read a file before editing it: `hashline_edit` addresses lines by the "
        "`line:hash` pair this returns, and refuses an edit whose hash no longer "
        "matches — which is how it notices the file changed under you.\n\n"
        "`offset` counts from 0 while the tags count from 1. For a long file, read a "
        "first slice with `limit=100`, then read the part you need."
    ),
    args={
        "path": "File to read, absolute or relative to the working directory.",
        "offset": "First line to return, counting from 0. Defaults to 0.",
        "limit": "How many lines to return. Defaults to 2000.",
    },
    returns=(
        "One line per line of the file, formatted `{line_number}:{hash}|{content}`, "
        "the hash being two characters belonging to that line's exact content. A "
        "slice that stops short of the end carries a `... (N more lines)` trailer, "
        "and a missing file or an out-of-range offset a line starting `Error:`."
    ),
)

WRITE_FILE_TEXT = ToolText(
    summary="Write a file, creating it or replacing everything in it.",
    usage=(
        "For a file that already exists, prefer `edit_file`: it changes the part you "
        "name, where this replaces the whole file and loses anything you left out. "
        "Use `write_file` to create a file, or when a full rewrite is what you mean. "
        "Missing parent directories are created for you.\n\n"
        "Keep credentials out of it — a workspace is not a secret store, and its "
        "files are readable by anyone who can see this conversation."
    ),
    coding=(
        "Create a new file only when the work needs one; extending a file that exists "
        "is usually the smaller change. Leave README, documentation and summary files "
        "alone unless they were asked for."
    ),
    args={
        "path": "File to write, absolute or relative to the working directory.",
        "content": "The file's complete new content.",
    },
    returns=(
        "`Wrote N lines to {path}`, or a line starting `Error:`. A successful write "
        "counts as a read, so the file can be edited straight afterwards."
    ),
)

EDIT_FILE_TEXT = ToolText(
    summary="Replace an exact string in a file.",
    usage=(
        "`old_string` has to match the file character for character, indentation "
        "included, which is why you read the file first. It also has to identify one "
        "place: if it appears more than once the edit is refused, so include the "
        "surrounding lines until it is unique — or pass `replace_all=True` when every "
        "occurrence is what you mean, as in a rename.\n\n"
        "Change only the tokens you meant to change, leaving the wording and "
        "formatting around them alone. A formatter or a commit hook can rewrite the "
        "file after an edit, so read it again before the next edit to it."
    ),
    args={
        "path": "File to edit, absolute or relative to the working directory.",
        "old_string": "Text to replace, matched exactly, whitespace included.",
        "new_string": "Text to put in its place. Must differ from `old_string`.",
        "replace_all": (
            "Replace every occurrence. Defaults to false, which requires "
            "`old_string` to appear exactly once."
        ),
    },
    returns=(
        "`Edited {path}: replaced N occurrence(s)`, or a line starting `Error:` "
        "saying the string was not found, was found N times, or that the file changed "
        "since your last read. All three are recoverable: read the file again and "
        "send an `old_string` that matches one place."
    ),
)

HASHLINE_EDIT_TEXT = ToolText(
    summary="Replace, insert or delete lines addressed by their content hashes.",
    usage=(
        "Every edit anchors on a `line:hash` pair from `read_file`. Replace one line "
        "with `start_line`, `start_hash` and `new_content`; replace a range by adding "
        "`end_line` and `end_hash`; insert with `insert_after=True`; delete by sending "
        "an empty `new_content`.\n\n"
        "A hash that no longer matches means the file changed since you read it — "
        "read it again rather than guessing at the new numbering. Making several "
        "edits to one file, work from the bottom up so the lines above stay valid."
    ),
    args={
        "path": "File to edit, absolute or relative to the working directory.",
        "start_line": "1-based line the edit starts at, as shown in its tag.",
        "start_hash": "The two-character hash shown for that line.",
        "new_content": "Replacement text. An empty string deletes the line or range.",
        "end_line": "1-based last line of an inclusive range. Omit for a single line.",
        "end_hash": "The two-character hash shown for `end_line`.",
        "insert_after": (
            "Insert `new_content` after `start_line` instead of replacing it. Defaults to false."
        ),
    },
    returns=(
        "`Edited {path}:` and what changed — lines replaced, inserted or deleted, and "
        "where — or a line starting `Error:`. A hash mismatch is recoverable by "
        "reading the file again."
    ),
)

GLOB_TEXT = ToolText(
    summary="Find files whose path matches a glob pattern.",
    usage=(
        "This is how you discover files before reading or editing them. `*` matches "
        "within one path segment and `**` across directories, so `*.py` finds Python "
        "files beside you and `**/*.py` finds them at any depth; `**/*.{js,ts}` "
        "matches either extension. To search what is inside files, use `grep`."
    ),
    args={
        "pattern": 'Glob pattern, e.g. `"*.py"`, `"**/*.ts"`, `"**/test_*.py"`.',
        "path": "Directory to search from. Defaults to the working directory.",
    },
    returns=(
        "A count, then up to 100 matching paths; the rest are summarised as `... and "
        "N more`, which means you are seeing a slice — narrow the pattern when the "
        "whole set matters. No match answers `No files matching ...`."
    ),
)

GREP_TEXT = ToolText(
    summary="Search the contents of files for a regular expression.",
    usage=(
        "Use this rather than a shell `grep` or `rg` through `execute`: it works on "
        "every backend, including those with no shell. Full regex syntax is "
        'supported, e.g. `"log.*Error"`. Narrow the search with `path` for a subtree '
        "and `glob_pattern` for a file type, and choose how much comes back with "
        "`output_mode`."
    ),
    args={
        "pattern": "Regular expression to search for.",
        "path": "File or directory to search. Defaults to the working directory.",
        "glob_pattern": 'Only search files matching this, e.g. `"*.py"`, `"*.{js,ts}"`.',
        "output_mode": (
            '`"files_with_matches"` for paths (the default), `"content"` for the '
            'matching lines, `"count"` for how many there are.'
        ),
        "ignore_hidden": "Skip hidden files and directories.",
    },
    returns=(
        "`files_with_matches` lists paths, `content` lists `path:line: text` with each "
        "line cut to 100 characters, `count` returns a number. The two lists stop at "
        "50 entries and say `... and N more`, so read a full list as a slice. No match "
        "answers `No matches for ...`."
    ),
)

EXECUTE_TEXT = ToolText(
    summary="Run a shell command in the working directory.",
    usage=(
        "Use it for work that needs a shell — running a program, a build, a test "
        "suite, a package manager. For anything the other tools already do, use them: "
        "`read_file` rather than `cat`, `write_file` rather than a redirect, "
        "`edit_file` rather than `sed`, `glob` rather than `find`, `grep` rather than "
        "shell `grep`. They work on every backend and report what happened in a form "
        "you can act on.\n\n"
        "Quote paths containing spaces. Chain steps that depend on each other into "
        "one command with `&&`, and run independent ones as separate calls in the "
        "same response. Raise `timeout` for a command that will take longer than two "
        "minutes; a command that never exits on its own, such as a server, blocks "
        "until that timeout and is then killed. When output is long, redirect it to a "
        "file and read that."
    ),
    coding=(
        "When a command fails, read the whole output before changing anything — the "
        "cause is usually above the last line. Reproduce a failure before fixing it, "
        "change one thing at a time, and after three failed attempts at one approach "
        "try a different one. If something is missing, check what is installed "
        "(`which <tool>`, `pip list`) and install it with the package manager the "
        "project implies.\n\n"
        "With git: make new commits rather than amending, stage the paths you changed "
        "rather than `git add -A`, which is how a `.env` gets committed, and commit "
        "only when asked. `push --force`, `reset --hard`, `clean -f`, `branch -D` and "
        "`--no-verify` discard someone's work or their checks, so run them only when "
        "the request names them, and check what a destructive command points at first."
    ),
    args={
        "command": "Shell command to run.",
        "timeout": "Seconds to allow before the command is killed. Defaults to 120.",
    },
    returns=(
        "The command's stdout and stderr together. A non-zero exit arrives as "
        "`Command failed (exit code N):` and that output, a timeout as `Error: "
        "Command timed out` with code 124, and long output is cut with a `... (output "
        "truncated)` trailer. A failed command is a result, not the end of the run."
    ),
)

RUN_IN_BACKGROUND_TEXT = ToolText(
    summary="Start a long-running command and return immediately.",
    usage=(
        "For a process that does not exit on its own — a dev server, a watcher, a log "
        "tail. `execute` blocks until its command finishes and kills it at the "
        "timeout, so a server started that way is reaped before it is useful. Follow "
        "this one with `read_output`, probe it from a separate `execute` call, and "
        "stop it with `kill_shell` when you are done."
    ),
    args={"command": "Shell command to start detached, e.g. a dev server."},
    returns="The shell's id and process id, and the calls that follow it.",
)

READ_OUTPUT_TEXT = ToolText(
    summary="Read what a background shell has printed since your last read.",
    usage=("Call it again to follow a slow startup: each call returns only what is new."),
    args={"shell_id": "The id `run_in_background` returned."},
    returns=(
        "The shell id, whether it is still running or the code it exited with, and "
        "its new stdout and stderr — `(no new output)` when there was none."
    ),
)

KILL_SHELL_TEXT = ToolText(
    summary="Stop a background shell.",
    usage=(
        "Stop one as soon as you no longer need it: a shell left running holds its "
        "port and its process for whatever comes next here."
    ),
    args={"shell_id": "The id `run_in_background` returned."},
    returns="Confirmation, or a note that the shell had already finished.",
)

LIST_SHELLS_TEXT = ToolText(
    summary="List the background shells started in this session.",
    usage="Use it to find a shell whose id you no longer have.",
    returns=(
        "One line per shell: its id, whether it is running or the code it exited "
        "with, and the command it was started from."
    ),
)


TOOL_TEXT: Mapping[str, ToolText] = {
    "ls": LS_TEXT,
    "read_file": READ_FILE_TEXT,
    "hashline_read_file": HASHLINE_READ_FILE_TEXT,
    "write_file": WRITE_FILE_TEXT,
    "edit_file": EDIT_FILE_TEXT,
    "hashline_edit": HASHLINE_EDIT_TEXT,
    "glob": GLOB_TEXT,
    "grep": GREP_TEXT,
    "execute": EXECUTE_TEXT,
    "run_in_background": RUN_IN_BACKGROUND_TEXT,
    "read_output": READ_OUTPUT_TEXT,
    "kill_shell": KILL_SHELL_TEXT,
    "list_shells": LIST_SHELLS_TEXT,
}
"""Every text the console toolset can register, keyed by its id.

The ids are tool names except `hashline_read_file`, which is `read_file` under
the hashline edit format — one tool with two texts, so the registry needs two
keys where a caller overriding it still names the tool, `read_file`.
"""

OVERRIDE_KEYS: frozenset[str] = frozenset(key for key in TOOL_TEXT if key != "hashline_read_file")
"""Tool names a caller may override, which is `TOOL_TEXT` keyed by tool.

Kept as its own set so an unknown key can be refused rather than ignored: a
misspelled or renamed key used to mean the override silently did nothing, and
nothing said so — including to a host whose catalogue then showed one text while
the model read another.
"""


LS_DESCRIPTION = LS_TEXT.render()
READ_FILE_DESCRIPTION = READ_FILE_TEXT.render()
HASHLINE_READ_FILE_DESCRIPTION = HASHLINE_READ_FILE_TEXT.render()
WRITE_FILE_DESCRIPTION = WRITE_FILE_TEXT.render()
EDIT_FILE_DESCRIPTION = EDIT_FILE_TEXT.render()
HASHLINE_EDIT_DESCRIPTION = HASHLINE_EDIT_TEXT.render()
GLOB_DESCRIPTION = GLOB_TEXT.render()
GREP_DESCRIPTION = GREP_TEXT.render()
EXECUTE_DESCRIPTION = EXECUTE_TEXT.render()
RUN_IN_BACKGROUND_DESCRIPTION = RUN_IN_BACKGROUND_TEXT.render()
READ_OUTPUT_DESCRIPTION = READ_OUTPUT_TEXT.render()
KILL_SHELL_DESCRIPTION = KILL_SHELL_TEXT.render()
LIST_SHELLS_DESCRIPTION = LIST_SHELLS_TEXT.render()
