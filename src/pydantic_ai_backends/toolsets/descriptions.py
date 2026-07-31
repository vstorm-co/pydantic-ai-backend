"""Prompts and tool descriptions for the console toolset.

Each constant is the full description handed to `@toolset.tool(description=...)`.
Usage guidance lives here, next to the tool it governs, rather than in the
system prompt — a model reads a tool's description when it is choosing that
tool, which is exactly when the guidance is needed.
"""

from __future__ import annotations

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

LS_DESCRIPTION = """\
List files and directories at the given path, showing names and sizes.

Use `glob` instead when you need to find files by pattern (e.g., all *.py files).
Use `ls` when you need to see the full contents of a specific directory."""

READ_FILE_DESCRIPTION = """\
Read file content with line numbers. ALWAYS read a file before editing it.

Results are returned with line numbers (like `cat -n`).

Usage:
- For large files (>200 lines), use pagination: first scan with `limit=100` \
to understand structure, then read targeted sections with `offset` and `limit`.
- For small files, read the whole file by not providing offset/limit.
- You can read multiple files in parallel — call read_file multiple times \
in a single response for maximum efficiency.
- If a file doesn't exist, an error is returned.
- Read existing files before modifying them — understand the codebase first.
- Mimic existing code style, naming conventions, and patterns."""

HASHLINE_READ_FILE_DESCRIPTION = """\
Read file content with hashline tags. ALWAYS read a file before editing it.

Each line is tagged with a content hash: `{line_number}:{hash}|{content}`.
Use the `line:hash` pair when calling `hashline_edit`.

Usage:
- For large files (>200 lines), use pagination: first scan with \
`limit=100` to understand structure, then read targeted sections.
- For small files, read the whole file by not providing offset/limit.
- You can read multiple files in parallel for maximum efficiency.
- Read existing files before modifying them — understand the codebase first."""

WRITE_FILE_DESCRIPTION = """\
Write content to a file. Creates the file if it doesn't exist, \
or completely overwrites it if it does. Parent directories are created as needed.

IMPORTANT:
- ALWAYS prefer `edit_file` over `write_file` for existing files — \
`edit_file` makes targeted changes while `write_file` replaces the entire file.
- Only use `write_file` for: (1) creating new files, or (2) complete rewrites.
- NEVER create new files unless explicitly required — prefer editing existing ones.
- Do NOT create README, documentation, or summary files unless asked.
- Never write secrets (.env, credentials.json, API keys) to files."""

EDIT_FILE_DESCRIPTION = """\
Edit a file by performing exact string replacement. This is the preferred \
way to modify existing files.

IMPORTANT:
- You MUST `read_file` first before editing — you need to see the exact content \
to construct a correct `old_string`.
- Preserve exact indentation (tabs vs spaces) as it appears in the file.
- The edit will FAIL if `old_string` is not found, or if it appears more \
than once (unless `replace_all=True`). If it fails, provide more surrounding \
context to make `old_string` unique.
- After editing a file, re-read it before making subsequent edits to the same \
file — auto-formatters or pre-commit hooks may have changed content on disk.
- When making substitutions or replacements, change ONLY the exact tokens \
specified. Do NOT adjust surrounding text (articles, grammar, punctuation).
- Use `replace_all=True` when renaming a variable, function, or string \
across the entire file."""

HASHLINE_EDIT_DESCRIPTION = """\
Edit a file by referencing lines with their content hashes. \
This is the preferred way to modify existing files.

You MUST `read_file` first to see the `line:hash` tags.

Operations:
- **Replace single line**: set `start_line` + `start_hash` + `new_content`
- **Replace range**: also set `end_line` + `end_hash`
- **Insert after**: set `insert_after=True` to add lines after the anchor
- **Delete**: set `new_content=""`

If the hash doesn't match, the file changed since your last read — \
re-read it first. When making multiple edits, work **bottom-to-top** so \
line numbers don't shift.

After editing, re-read before making subsequent edits to the same file — \
auto-formatters or pre-commit hooks may have changed content on disk.
When making substitutions, change ONLY the exact tokens specified. \
Do NOT adjust surrounding text (articles, grammar, punctuation)."""

GLOB_DESCRIPTION = """\
Find files matching a glob pattern. Use this to discover files in the \
codebase before reading or editing them.

Common patterns:
- `"*.py"` — Python files in current directory only
- `"**/*.py"` — Python files recursively in all subdirectories
- `"src/**/*.ts"` — TypeScript files under src/
- `"**/test_*.py"` — All test files recursively
- `"**/*.{js,ts,tsx}"` — Multiple extensions

You can call glob multiple times in a single response to search for \
different patterns in parallel."""

GREP_DESCRIPTION = """\
Search for a regex pattern across files. ALWAYS use this instead of \
shell `grep` or `rg` commands.

Supports full regex syntax (e.g., `"log.*Error"`, `"function\\s+\\w+"`).

Output modes:
- `"files_with_matches"` (default) — returns only file paths that match
- `"content"` — returns matching lines with file path and line numbers
- `"count"` — returns the number of matches"""

EXECUTE_DESCRIPTION = """\
Execute a shell command in the working directory.

IMPORTANT: This tool is for operations that REQUIRE a real shell — \
running tests, builds, git commands, package installs, running scripts.

## You MUST use specialized tools instead of shell equivalents:
- `read_file` instead of `cat`, `head`, `tail`
- `edit_file`/`hashline_edit` instead of `sed`, `awk`
- `write_file` instead of `echo >` or `cat <<EOF`
- `glob` instead of `find` or `ls`
- `grep` instead of shell `grep` or `rg`

## Usage
- Always quote file paths containing spaces with double quotes.
- Prefer absolute paths over relative paths.
- When running multiple independent commands, make separate `execute` calls \
in a single response (parallel execution).
- When commands depend on each other, chain with `&&` in a single call \
(e.g., `cd /project && make test`).
- For long-running commands (builds, large test suites), increase the timeout.
- For verbose output, redirect to a temp file and inspect with `read_file`.

## Debugging
- Read the FULL error output when a command fails — the root cause is often \
in the middle of a traceback, not the last line.
- Reproduce the error before attempting a fix.
- Change one thing at a time — don't make multiple speculative fixes.
- If something fails 3 times with the same approach, STOP and try a \
completely different strategy.

## Dependencies
- If a command fails because a package or tool is missing, install it \
immediately (`pip install X`, `npm install X`) and retry.
- Check what's already installed before installing new packages \
(`which <tool>`, `pip list`).
- Use the project's package manager (check for pyproject.toml, package.json, \
Cargo.toml).

## Git Safety
- NEVER run destructive git commands unless explicitly asked: \
`push --force`, `reset --hard`, `clean -f`, `branch -D`, `checkout .`
- NEVER skip hooks (`--no-verify`) unless explicitly asked.
- NEVER force push to main/master — warn the user first.
- ALWAYS create NEW commits rather than amending existing ones \
(unless the user explicitly asks for amend).
- When staging files, prefer `git add <specific files>` over `git add -A` \
or `git add .` to avoid accidentally including .env, credentials, or binaries.
- NEVER commit changes unless the user explicitly asks.

## Safety
- Be careful not to introduce command injection vulnerabilities.
- Never commit secrets (.env, credentials.json, API keys).
- Be careful with destructive commands (`rm -rf`, `drop table`, etc.) — \
verify the target path/object before executing."""

RUN_IN_BACKGROUND_DESCRIPTION = """\
Start a long-lived command in the background and return immediately with a \
`shell_id`. Use this for processes that don't exit on their own — dev servers, \
watchers, `uvicorn`/`npm run dev`, tailing logs.

Do NOT use plain `execute` for servers: it blocks until the command finishes \
and kills the process tree on timeout, so a server gets reaped.

After starting: poll its output with `read_output(shell_id)`, probe it from a \
separate `execute` call (e.g. `curl`), and stop it with `kill_shell(shell_id)` \
when done. Don't write your own launcher scripts — use this tool."""

READ_OUTPUT_DESCRIPTION = """\
Read output produced by a background shell since your last read (stdout+stderr), \
along with whether it's still running and its exit code if finished. Call \
repeatedly to follow a server's startup logs."""

KILL_SHELL_DESCRIPTION = """\
Stop a background shell started with `run_in_background`. Always kill background \
shells you no longer need (e.g. a dev server after you've verified it)."""

LIST_SHELLS_DESCRIPTION = """\
List the background shells started this session with their status \
(running / exited) and command."""
