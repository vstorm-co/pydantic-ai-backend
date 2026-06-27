"""Console toolset for AI agents - file operations and shell execution."""

from __future__ import annotations

import asyncio
import hashlib
import weakref
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from pydantic_ai import BinaryContent, RunContext
from pydantic_ai.toolsets import FunctionToolset

from pydantic_ai_backends.adapter import ensure_async
from pydantic_ai_backends.protocol import AsyncBackendProtocol, BackendProtocol
from pydantic_ai_backends.types import GrepMatch

EditFormat = Literal["str_replace", "hashline"]
"""Supported file-editing formats for the console toolset."""

IMAGE_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
"""File extensions recognized as images when image_support is enabled."""

IMAGE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
"""Mapping of file extensions to MIME media types for images."""

DEFAULT_MAX_IMAGE_BYTES: int = 50 * 1024 * 1024
"""Default maximum image file size (50MB)."""

DEFAULT_MAX_IMAGE_DIMENSION: int = 1568
"""Longest image edge (px) sent to the model. Larger images are downscaled to
fit — this matches Anthropic's recommended image size and keeps big screenshots
from wasting tokens. Requires Pillow (the `images` extra); a no-op without it."""

DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({"pdf"})
"""File extensions recognized as binary documents when document_support is enabled.

Kept deliberately separate from IMAGE_EXTENSIONS: images and documents are
distinct content kinds that may eventually be handled differently (e.g. OCR for
images, native document understanding or text extraction for PDF/DOCX)."""

DOCUMENT_MEDIA_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
}
"""Mapping of document file extensions to MIME media types.

Documents are returned as ``BinaryContent`` so that models capable of document
understanding (OpenAI/Anthropic/Gemini) can read them directly instead of
receiving garbled or empty text."""

DEFAULT_MAX_DOCUMENT_BYTES: int = 50 * 1024 * 1024
"""Default maximum document file size (50MB)."""

if TYPE_CHECKING:
    from pydantic_ai.toolsets import FunctionToolset

    from pydantic_ai_backends.permissions.types import PermissionRuleset


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

# ── Tool description constants ────────────────────────────────────────────
# Each constant is the full description passed to @toolset.tool(description=...).
# Guidance that was previously in the system prompt now lives here, closer to
# the tool's point of use.

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


@runtime_checkable
class ConsoleDeps(Protocol):
    """Protocol for dependencies that provide a backend."""

    @property
    def backend(self) -> BackendProtocol | AsyncBackendProtocol:
        """The backend for file operations."""
        ...


class _ConsoleToolsetTestAttrs(Protocol):
    """Protocol for test-only attributes on console toolset."""

    _console_default_ignore_hidden: bool
    _console_grep_impl: Callable[..., Awaitable[str]]


def _requires_approval_from_ruleset(
    ruleset: PermissionRuleset | None,
    operation: str,
    legacy_flag: bool,
) -> bool:
    """Determine if a tool requires approval based on ruleset or legacy flag.

    If a ruleset is provided, checks if the operation's default action is "ask".
    Otherwise, falls back to the legacy boolean flag.
    """
    from pydantic_ai_backends.permissions.types import OperationPermissions

    if ruleset is None:
        return legacy_flag

    op_perms: OperationPermissions | None = getattr(ruleset, operation, None)
    if op_perms is None:
        # Use global default
        return ruleset.default == "ask"
    return op_perms.default == "ask"


def _is_denied_by_ruleset(
    ruleset: PermissionRuleset | None,
    operation: str,
) -> bool:
    """Check if an operation is denied by the ruleset.

    Returns True when the operation's default action is "deny",
    meaning the corresponding tools should not be registered at all.
    """
    from pydantic_ai_backends.permissions.types import OperationPermissions

    if ruleset is None:
        return False

    op_perms: OperationPermissions | None = getattr(ruleset, operation, None)
    if op_perms is None:
        return ruleset.default == "deny"
    return op_perms.default == "deny"


_edit_locks: weakref.WeakKeyDictionary[Any, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()

# Per-backend record of the content fingerprint the agent last saw for each path
# (set on read_file / write_file). Powers the edit staleness guard: editing a
# file that has changed on disk since it was read is refused so the agent
# re-reads first. Files never read through the tools are left alone.
_read_fingerprints: weakref.WeakKeyDictionary[Any, dict[str, str]] = weakref.WeakKeyDictionary()


def _fingerprint(data: bytes) -> str:
    """Stable content fingerprint used to detect changes between read and edit."""
    return hashlib.sha256(data).hexdigest()


def _record_read(backend: Any, path: str, data: bytes) -> None:
    """Remember the content the agent just saw at `path` (read or write)."""
    _read_fingerprints.setdefault(backend, {})[path] = _fingerprint(data)


async def _edit_staleness_error(
    backend_async: AsyncBackendProtocol, raw_backend: Any, path: str
) -> str | None:
    """Return an error if `path` changed since it was last read, else None.

    Only files previously seen through the tools are guarded — an edit to a file
    that was never read proceeds (the edit's own match check still applies). When
    a guarded file's on-disk content no longer matches what the agent read, the
    edit is refused so the agent re-reads the current contents first.
    """
    recorded = _read_fingerprints.get(raw_backend, {}).get(path)
    if recorded is None:
        return None
    try:
        current = await backend_async.read_bytes(path)
    except Exception:  # pragma: no cover - defensive; the edit itself will report
        return None
    if _fingerprint(current) != recorded:
        return (
            f"Error: '{path}' changed since you last read it. Read it again "
            "before editing so your change applies to the current contents."
        )
    return None


async def _record_path_read(  # pragma: no cover - glue, exercised via the file tools
    backend_async: AsyncBackendProtocol, raw_backend: Any, path: str
) -> None:
    """Read `path`'s bytes and record their fingerprint (best-effort)."""
    try:
        data = await backend_async.read_bytes(path)
    except Exception:
        return
    _record_read(raw_backend, path, data)


def _file_extension(path: str) -> str:
    """Return the lowercase file extension (without the dot), or "" if none."""
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


async def _read_binary_within_limit(
    backend: BackendProtocol | AsyncBackendProtocol,
    path: str,
    max_bytes: int,
    kind: str,
) -> bytes | str:
    """Read raw bytes, enforcing the not-found/empty and size-limit guards.

    Args:
        backend: Backend to read from.
        path: File path to read.
        max_bytes: Maximum allowed file size in bytes.
        kind: Human-readable content kind ("Image" / "Document") for error text.

    Returns:
        The file bytes, or an error string when the file is missing/empty or
        exceeds ``max_bytes``.
    """
    raw = await ensure_async(backend).read_bytes(path)
    if not raw:
        return f"Error: {kind} file '{path}' not found or empty"
    if len(raw) > max_bytes:
        size_mb = len(raw) / (1024 * 1024)
        limit_mb = max_bytes / (1024 * 1024)
        return f"Error: {kind} '{path}' too large ({size_mb:.1f}MB, max {limit_mb:.1f}MB)"
    return raw


def _downscale_image(data: bytes, max_dim: int = DEFAULT_MAX_IMAGE_DIMENSION) -> bytes:
    """Shrink an oversized image so it fits the model's image budget.

    If Pillow is available and the image's longest edge exceeds ``max_dim``, the
    image is resized (aspect preserved) and re-encoded. Returns the original
    bytes unchanged when Pillow is missing, the image is already small enough, or
    decoding fails — so it's always safe to call.
    """
    try:
        import io

        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is an optional extra
        return data
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or "PNG"
            if max(img.size) <= max_dim:
                return data
            img.thumbnail((max_dim, max_dim))
            buf = io.BytesIO()
            img.save(buf, format=fmt)
            return buf.getvalue()
    except Exception:  # pragma: no cover - corrupt/unsupported image data
        return data


async def _maybe_image_content(
    backend: BackendProtocol | AsyncBackendProtocol,
    path: str,
    max_image_bytes: int,
) -> BinaryContent | str | None:
    """Return image content for recognized image files.

    Returns:
        - ``BinaryContent`` when ``path`` is a recognized image read within limit.
        - An error string when the file is missing/empty or exceeds the limit.
        - ``None`` when ``path`` is not an image (caller should keep looking).
    """
    ext = _file_extension(path)
    if ext not in IMAGE_EXTENSIONS:
        return None

    result = await _read_binary_within_limit(backend, path, max_image_bytes, "Image")
    if isinstance(result, str):
        return result
    result = _downscale_image(result)
    media_type = IMAGE_MEDIA_TYPES.get(ext, "application/octet-stream")
    return BinaryContent(data=result, media_type=media_type)  # pyright: ignore[reportCallIssue]


async def _maybe_document_content(
    backend: BackendProtocol | AsyncBackendProtocol,
    path: str,
    max_document_bytes: int,
) -> BinaryContent | str | None:
    """Return document content for recognized binary document files.

    Kept separate from image handling so document-specific behavior (e.g. OCR or
    text extraction fallbacks via libraries) can evolve independently.

    Returns:
        - ``BinaryContent`` when ``path`` is a recognized document read within limit.
        - An error string when the file is missing/empty or exceeds the limit.
        - ``None`` when ``path`` is not a document (caller should keep looking).
    """
    ext = _file_extension(path)
    if ext not in DOCUMENT_EXTENSIONS:
        return None

    result = await _read_binary_within_limit(backend, path, max_document_bytes, "Document")
    if isinstance(result, str):
        return result
    media_type = DOCUMENT_MEDIA_TYPES[ext]
    return BinaryContent(data=result, media_type=media_type)  # pyright: ignore[reportCallIssue]


def create_console_toolset(  # noqa: C901
    id: str | None = None,
    include_execute: bool = True,
    include_background: bool = True,
    require_write_approval: bool = False,
    require_execute_approval: bool = True,
    default_ignore_hidden: bool = True,
    permissions: PermissionRuleset | None = None,
    max_retries: int = 1,
    image_support: bool = False,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    document_support: bool = False,
    max_document_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
    edit_format: EditFormat = "str_replace",
    descriptions: dict[str, str] | None = None,
) -> FunctionToolset[ConsoleDeps]:
    """Create a console toolset for file operations and shell execution.

    This toolset provides tools for interacting with the filesystem and
    executing shell commands. It works with any backend that implements
    BackendProtocol (LocalBackend, DockerSandbox, StateBackend, etc.)

    Args:
        id: Optional unique ID for the toolset.
        include_execute: Whether to include the execute tool.
            Requires backend to have execute() method.
        require_write_approval: Whether write_file and edit_file require approval.
            Ignored if permissions is provided.
        require_execute_approval: Whether execute requires approval.
            Ignored if permissions is provided.
        default_ignore_hidden: Default behavior for grep regarding hidden files.
        permissions: Optional permission ruleset to determine tool approval requirements.
            If provided, overrides require_write_approval and require_execute_approval
            based on whether the operation's default action is "ask".
        max_retries: Maximum number of retries for each tool during a run.
            When the model sends invalid arguments (e.g. missing required fields),
            the validation error is fed back and the model can retry up to this
            many times. Defaults to 1.
        image_support: Whether to enable image file handling in read_file.
            When True, reading image files (.png, .jpg, .jpeg, .gif, .webp) returns
            a BinaryContent object that multimodal models can see, instead of garbled
            text. Defaults to False.
        max_image_bytes: Maximum image file size in bytes (default: 50MB).
            Images larger than this will return an error message instead.
            Only used when image_support is True.
        document_support: Whether to enable binary document handling in read_file.
            When True, reading document files (.pdf) returns a BinaryContent object
            that models capable of document understanding (OpenAI/Anthropic/Gemini)
            can read, instead of the empty/garbled text produced by reading a binary
            file as text. Kept separate from image_support so document handling can
            evolve independently (e.g. OCR or text-extraction fallbacks).
            Defaults to False.
        max_document_bytes: Maximum document file size in bytes (default: 50MB).
            Documents larger than this will return an error message instead.
            Only used when document_support is True.
        edit_format: File editing format to use.  `"str_replace"` (default) uses
            exact string matching.  `"hashline"` tags each line with a content hash
            so models can reference lines by number:hash instead of reproducing text.
        descriptions: Optional mapping of tool name to custom description override.
            When provided, the description for a tool is looked up as
            `descriptions.get("tool_name", DEFAULT_DESCRIPTION)`.  Valid keys are:
            `ls`, `read_file`, `write_file`, `edit_file`, `hashline_edit`,
            `glob`, `grep`, `execute`.

    Returns:
        FunctionToolset with console tools.

    Example:
        ```python
        from dataclasses import dataclass
        from pydantic_ai_backends import LocalBackend, create_console_toolset

        @dataclass
        class MyDeps:
            backend: LocalBackend

        toolset = create_console_toolset()
        deps = MyDeps(backend=LocalBackend("/workspace"))

        # With hashline edit format (better accuracy, fewer tokens)
        toolset = create_console_toolset(edit_format="hashline")

        # With image support for multimodal models
        toolset = create_console_toolset(image_support=True)

        # With document (PDF) support for document-understanding models
        toolset = create_console_toolset(document_support=True)

        # Or with permissions
        from pydantic_ai_backends.permissions import DEFAULT_RULESET

        toolset = create_console_toolset(permissions=DEFAULT_RULESET)
        ```
    """

    _descs = descriptions or {}

    # Determine approval requirements and denied operations
    write_approval = _requires_approval_from_ruleset(permissions, "write", require_write_approval)
    execute_approval = _requires_approval_from_ruleset(
        permissions, "execute", require_execute_approval
    )
    # Track denied operations to remove tools after registration
    _denied_tools: set[str] = set()
    if _is_denied_by_ruleset(permissions, "write"):
        _denied_tools.add("write_file")
    if _is_denied_by_ruleset(permissions, "edit"):
        _denied_tools.update({"edit_file", "hashline_edit"})
    if _is_denied_by_ruleset(permissions, "execute"):
        _denied_tools.add("execute")

    toolset: FunctionToolset[ConsoleDeps] = FunctionToolset(id=id, max_retries=max_retries)

    @toolset.tool(description=_descs.get("ls", LS_DESCRIPTION))
    async def ls(  # pragma: no cover
        ctx: RunContext[ConsoleDeps],
        path: str = ".",
    ) -> str:
        """List files and directories at the given path.

        Args:
            path: Directory path to list. Defaults to current directory.
        """
        backend = ensure_async(ctx.deps.backend)
        entries = await backend.ls_info(path)

        if not entries:
            return f"Directory '{path}' is empty or does not exist"

        lines = [f"Contents of {path}:"]
        for entry in entries:
            if entry["is_dir"]:
                lines.append(f"  {entry['name']}/")
            else:
                size = entry.get("size")
                size_str = f" ({size} bytes)" if size is not None else ""
                lines.append(f"  {entry['name']}{size_str}")

        return "\n".join(lines)

    # --- read_file tool ---
    if edit_format == "hashline":

        @toolset.tool(description=_descs.get("read_file", HASHLINE_READ_FILE_DESCRIPTION))
        async def read_file(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
            path: str,
            offset: int = 0,
            limit: int = 2000,
        ) -> Any:
            """Read file content with hashline tags.

            Args:
                path: Absolute or relative path to the file to read.
                offset: Line number to start reading from (0-indexed).
                limit: Maximum number of lines to read. Defaults to 2000.
            """
            if image_support:
                image = await _maybe_image_content(ctx.deps.backend, path, max_image_bytes)
                if image is not None:
                    return image
            if document_support:
                document = await _maybe_document_content(ctx.deps.backend, path, max_document_bytes)
                if document is not None:
                    return document

            from pydantic_ai_backends.hashline import format_hashline_output

            backend = ensure_async(ctx.deps.backend)
            if not await backend.exists(path):
                return f"Error: File '{path}' not found"
            raw_bytes = await backend.read_bytes(path)
            text = raw_bytes.decode("utf-8", errors="replace")
            _record_read(ctx.deps.backend, path, raw_bytes)
            return format_hashline_output(text, offset, limit)

    else:

        @toolset.tool(description=_descs.get("read_file", READ_FILE_DESCRIPTION))
        async def read_file(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
            path: str,
            offset: int = 0,
            limit: int = 2000,
        ) -> Any:
            """Read file content with line numbers.

            Args:
                path: Absolute or relative path to the file to read.
                offset: Line number to start reading from (0-indexed).
                limit: Maximum number of lines to read. Defaults to 2000.
            """
            if image_support:
                image = await _maybe_image_content(ctx.deps.backend, path, max_image_bytes)
                if image is not None:
                    return image
            if document_support:
                document = await _maybe_document_content(ctx.deps.backend, path, max_document_bytes)
                if document is not None:
                    return document
            backend = ensure_async(ctx.deps.backend)
            result = await backend.read(path, offset, limit)
            if not result.startswith("Error"):
                await _record_path_read(backend, ctx.deps.backend, path)
            return result

    # --- write_file tool ---
    @toolset.tool(
        description=_descs.get("write_file", WRITE_FILE_DESCRIPTION),
        requires_approval=write_approval,
    )
    async def write_file(  # pragma: no cover
        ctx: RunContext[ConsoleDeps],
        path: str,
        content: str,
    ) -> str:
        """Write content to a file.

        Args:
            path: Path to the file to write.
            content: Complete content to write to the file.
        """
        backend = ensure_async(ctx.deps.backend)
        result = await backend.write(path, content)

        if result.error:
            return f"Error: {result.error}"

        # The agent now knows this file's content — record it so an immediate
        # edit isn't blocked as stale.
        _record_read(ctx.deps.backend, path, content.encode("utf-8"))
        lines = len(content.splitlines())
        return f"Wrote {lines} lines to {result.path}"

    # --- edit tool (str_replace or hashline) ---
    if edit_format == "hashline":

        @toolset.tool(
            description=_descs.get("hashline_edit", HASHLINE_EDIT_DESCRIPTION),
            requires_approval=write_approval,
        )
        async def hashline_edit(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
            path: str,
            start_line: int,
            start_hash: str,
            new_content: str,
            end_line: int | None = None,
            end_hash: str | None = None,
            insert_after: bool = False,
        ) -> str:
            """Edit a file by referencing lines with their content hashes.

            Args:
                path: Path to the file to edit.
                start_line: 1-indexed line number to start the edit.
                start_hash: 2-char content hash of the start line (from read_file).
                new_content: Replacement text. Empty string deletes line(s).
                end_line: 1-indexed end of range (inclusive). Omit for single-line edit.
                end_hash: 2-char content hash of the end line. Optional validation.
                insert_after: If True, insert new_content after start_line instead \
of replacing it.
            """
            from pydantic_ai_backends.hashline import apply_hashline_edit_with_summary

            raw_backend = ctx.deps.backend
            backend = ensure_async(raw_backend)
            per_path = _edit_locks.setdefault(raw_backend, {})
            if path not in per_path:
                per_path[path] = asyncio.Lock()
            async with per_path[path]:
                # Read current file content
                if not await backend.exists(path):
                    return f"Error: File '{path}' not found"
                raw_bytes = await backend.read_bytes(path)

                text = raw_bytes.decode("utf-8", errors="replace")

                # Apply edit
                new_text, error, summary = apply_hashline_edit_with_summary(
                    text,
                    start_line,
                    start_hash,
                    new_content,
                    end_line,
                    end_hash,
                    insert_after,
                )

                if error:
                    return f"Error: {error}"

                # Write back
                write_result = await backend.write(path, new_text)
                if write_result.error:
                    return f"Error: {write_result.error}"

                return f"Edited {write_result.path}: {summary}"

    else:

        @toolset.tool(
            description=_descs.get("edit_file", EDIT_FILE_DESCRIPTION),
            requires_approval=write_approval,
        )
        async def edit_file(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
            path: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
        ) -> str:
            """Edit a file by performing exact string replacement.

            Args:
                path: Path to the file to edit.
                old_string: Exact string to find and replace. Must match file content exactly \
including whitespace and indentation.
                new_string: Replacement string. Must be different from old_string.
                replace_all: If True, replace all occurrences. If False (default), \
the old_string must appear exactly once in the file.
            """
            raw_backend = ctx.deps.backend
            backend = ensure_async(raw_backend)

            stale = await _edit_staleness_error(backend, raw_backend, path)
            if stale is not None:
                return stale

            result = await backend.edit(path, old_string, new_string, replace_all)

            if result.error:
                return f"Error: {result.error}"

            # The agent's view is now the post-edit content — record it so a
            # follow-up edit isn't wrongly flagged as stale.
            await _record_path_read(backend, raw_backend, path)
            return f"Edited {result.path}: replaced {result.occurrences} occurrence(s)"

    @toolset.tool(description=_descs.get("glob", GLOB_DESCRIPTION))
    async def glob(  # pragma: no cover
        ctx: RunContext[ConsoleDeps],
        pattern: str,
        path: str = ".",
    ) -> str:
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern to match.
            path: Base directory to search from. Defaults to current directory.
        """
        backend = ensure_async(ctx.deps.backend)
        entries = await backend.glob_info(pattern, path)

        if not entries:
            return f"No files matching '{pattern}' in {path}"

        lines = [f"Found {len(entries)} file(s) matching '{pattern}':"]
        for entry in entries[:100]:
            lines.append(f"  {entry['path']}")

        if len(entries) > 100:
            lines.append(f"  ... and {len(entries) - 100} more")

        return "\n".join(lines)

    @toolset.tool(description=_descs.get("grep", GREP_DESCRIPTION))
    async def grep(  # pragma: no cover
        ctx: RunContext[ConsoleDeps],
        pattern: str,
        path: str | None = None,
        glob_pattern: str | None = None,
        output_mode: Literal["content", "files_with_matches", "count"] = "files_with_matches",
        ignore_hidden: bool = default_ignore_hidden,
    ) -> str:
        """Search for a regex pattern across files.

        Args:
            pattern: Regex pattern to search for.
            path: File or directory to search in. If None, searches current directory.
            glob_pattern: Filter files by pattern (e.g., `"*.py"`, `"*.{js,ts}"`).
            output_mode: Output format — `"content"`, `"files_with_matches"`, or `"count"`.
            ignore_hidden: Whether to skip hidden files/directories.
        """
        backend = ensure_async(ctx.deps.backend)
        result = await backend.grep_raw(pattern, path, glob_pattern, ignore_hidden)

        if isinstance(result, str):
            return result  # Error message

        if not result:
            return f"No matches for '{pattern}'"

        matches: list[GrepMatch] = result

        if output_mode == "count":
            return f"Found {len(matches)} match(es) for '{pattern}'"

        if output_mode == "files_with_matches":
            files = sorted(set(m["path"] for m in matches))
            lines = [f"Files containing '{pattern}':"]
            for f in files[:50]:
                lines.append(f"  {f}")
            if len(files) > 50:
                lines.append(f"  ... and {len(files) - 50} more files")
            return "\n".join(lines)

        # content mode
        lines = [f"Matches for '{pattern}':"]
        for m in matches[:50]:
            lines.append(f"  {m['path']}:{m['line_number']}: {m['line'][:100]}")
        if len(matches) > 50:
            lines.append(f"  ... and {len(matches) - 50} more matches")
        return "\n".join(lines)

    # Expose references for testing
    cast(_ConsoleToolsetTestAttrs, toolset)._console_default_ignore_hidden = default_ignore_hidden
    cast(_ConsoleToolsetTestAttrs, toolset)._console_grep_impl = grep

    if include_execute:

        @toolset.tool(
            description=_descs.get("execute", EXECUTE_DESCRIPTION),
            requires_approval=execute_approval,
        )
        async def execute(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
            command: str,
            timeout: int | None = 120,
        ) -> str:
            """Execute a shell command in the working directory.

            Args:
                command: Shell command to execute.
                timeout: Maximum execution time in seconds. Default 120. Increase \
for long-running builds or test suites.
            """
            backend = ctx.deps.backend
            async_backend = ensure_async(backend)

            # Check if backend supports execute
            if not hasattr(async_backend, "execute"):
                return "Error: Backend does not support command execution"

            # Check if execute is enabled (for LocalBackend)
            if hasattr(backend, "execute_enabled") and not backend.execute_enabled:  # pyright: ignore[reportAttributeAccessIssue]
                return "Error: Shell execution is disabled for this backend"

            try:
                result = await async_backend.execute(command, timeout)  # pyright: ignore[reportAttributeAccessIssue]
            except RuntimeError as e:
                return f"Error: {e}"

            output = result.output
            if result.truncated:
                output += "\n\n... (output truncated)"

            if result.exit_code is not None and result.exit_code != 0:
                return f"Command failed (exit code {result.exit_code}):\n{output}"

            return str(output)

    if include_execute and include_background:

        def _bg_backend(ctx: RunContext[ConsoleDeps]) -> Any | None:  # pragma: no cover
            """Return the async background sandbox, or None if unsupported."""
            async_backend = ensure_async(ctx.deps.backend)
            if not hasattr(async_backend, "execute_background"):
                return None
            return async_backend

        @toolset.tool(
            description=_descs.get("run_in_background", RUN_IN_BACKGROUND_DESCRIPTION),
            requires_approval=execute_approval,
        )
        async def run_in_background(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
            command: str,
        ) -> str:
            """Start a long-lived command in the background.

            Args:
                command: Shell command to run detached (e.g. a dev server).
            """
            bg = _bg_backend(ctx)
            if bg is None:
                return "Error: Backend does not support background processes"
            try:
                handle = await bg.execute_background(command)
            except (RuntimeError, PermissionError) as e:
                return f"Error: {e}"
            return (
                f"Started background shell {handle.shell_id} (pid {handle.pid}).\n"
                f"Use read_output('{handle.shell_id}') to follow its output and "
                f"kill_shell('{handle.shell_id}') to stop it."
            )

        @toolset.tool(description=_descs.get("read_output", READ_OUTPUT_DESCRIPTION))
        async def read_output(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
            shell_id: str,
        ) -> str:
            """Read new output from a background shell.

            Args:
                shell_id: The id returned by run_in_background.
            """
            bg = _bg_backend(ctx)
            if bg is None:
                return "Error: Backend does not support background processes"
            result = await bg.read_background(shell_id)
            status = "running" if result.running else f"exited (code {result.exit_code})"
            body = (result.stdout + result.stderr).strip()
            if not body:
                body = "(no new output)"
            return f"[{result.shell_id}] {status}\n{body}"

        @toolset.tool(
            description=_descs.get("kill_shell", KILL_SHELL_DESCRIPTION),
            requires_approval=execute_approval,
        )
        async def kill_shell(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
            shell_id: str,
        ) -> str:
            """Stop a background shell.

            Args:
                shell_id: The id returned by run_in_background.
            """
            bg = _bg_backend(ctx)
            if bg is None:
                return "Error: Backend does not support background processes"
            killed = await bg.kill_background(shell_id)
            if killed:
                return f"Killed background shell {shell_id}."
            return f"Background shell {shell_id} was already finished or unknown."

        @toolset.tool(description=_descs.get("list_shells", LIST_SHELLS_DESCRIPTION))
        async def list_shells(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
        ) -> str:
            """List the background shells started this session."""
            bg = _bg_backend(ctx)
            if bg is None:
                return "Error: Backend does not support background processes"
            infos = await bg.list_background()
            if not infos:
                return "No background shells."
            lines = [
                f"{i.shell_id}  {'running' if i.running else f'exited({i.exit_code})'}  {i.command}"
                for i in infos
            ]
            return "\n".join(lines)

    # Remove tools for denied operations (fixes issue #23)
    for tool_name in _denied_tools:
        toolset.tools.pop(tool_name, None)

    return toolset


def get_console_system_prompt(edit_format: EditFormat = "str_replace") -> str:
    """Get the system prompt for console tools.

    Args:
        edit_format: Which edit format to describe in the prompt.

    Returns:
        System prompt describing available console tools.
    """
    if edit_format == "hashline":
        return HASHLINE_CONSOLE_PROMPT
    return CONSOLE_SYSTEM_PROMPT


# Convenience alias
ConsoleToolset = create_console_toolset
