"""Console toolset for AI agents — file operations and shell execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from pydantic_ai_backends.adapter import ensure_async
from pydantic_ai_backends.protocol import AsyncBackendProtocol, BackendProtocol
from pydantic_ai_backends.toolsets import _ruleset, _tracking
from pydantic_ai_backends.toolsets._content import (
    DEFAULT_MAX_DOCUMENT_BYTES as DEFAULT_MAX_DOCUMENT_BYTES,
)
from pydantic_ai_backends.toolsets._content import (
    DEFAULT_MAX_IMAGE_BYTES as DEFAULT_MAX_IMAGE_BYTES,
)
from pydantic_ai_backends.toolsets._content import (
    DEFAULT_MAX_IMAGE_DIMENSION as DEFAULT_MAX_IMAGE_DIMENSION,
)
from pydantic_ai_backends.toolsets._content import (
    DOCUMENT_EXTENSIONS as DOCUMENT_EXTENSIONS,
)
from pydantic_ai_backends.toolsets._content import (
    DOCUMENT_MEDIA_TYPES as DOCUMENT_MEDIA_TYPES,
)
from pydantic_ai_backends.toolsets._content import (
    IMAGE_EXTENSIONS as IMAGE_EXTENSIONS,
)
from pydantic_ai_backends.toolsets._content import (
    IMAGE_MEDIA_TYPES as IMAGE_MEDIA_TYPES,
)
from pydantic_ai_backends.toolsets._content import document_content, image_content
from pydantic_ai_backends.toolsets.descriptions import (
    CONSOLE_SYSTEM_PROMPT as CONSOLE_SYSTEM_PROMPT,
)
from pydantic_ai_backends.toolsets.descriptions import (
    EDIT_FILE_DESCRIPTION as EDIT_FILE_DESCRIPTION,
)
from pydantic_ai_backends.toolsets.descriptions import (
    EXECUTE_DESCRIPTION as EXECUTE_DESCRIPTION,
)
from pydantic_ai_backends.toolsets.descriptions import (
    GLOB_DESCRIPTION as GLOB_DESCRIPTION,
)
from pydantic_ai_backends.toolsets.descriptions import (
    GREP_DESCRIPTION as GREP_DESCRIPTION,
)
from pydantic_ai_backends.toolsets.descriptions import (
    HASHLINE_CONSOLE_PROMPT as HASHLINE_CONSOLE_PROMPT,
)
from pydantic_ai_backends.toolsets.descriptions import (
    HASHLINE_EDIT_DESCRIPTION as HASHLINE_EDIT_DESCRIPTION,
)
from pydantic_ai_backends.toolsets.descriptions import (
    HASHLINE_READ_FILE_DESCRIPTION as HASHLINE_READ_FILE_DESCRIPTION,
)
from pydantic_ai_backends.toolsets.descriptions import (
    KILL_SHELL_DESCRIPTION,
    LIST_SHELLS_DESCRIPTION,
    READ_OUTPUT_DESCRIPTION,
    RUN_IN_BACKGROUND_DESCRIPTION,
)
from pydantic_ai_backends.toolsets.descriptions import (
    LS_DESCRIPTION as LS_DESCRIPTION,
)
from pydantic_ai_backends.toolsets.descriptions import (
    READ_FILE_DESCRIPTION as READ_FILE_DESCRIPTION,
)
from pydantic_ai_backends.toolsets.descriptions import (
    WRITE_FILE_DESCRIPTION as WRITE_FILE_DESCRIPTION,
)
from pydantic_ai_backends.types import GrepMatch

if TYPE_CHECKING:
    from pydantic_ai_backends.permissions.types import PermissionRuleset

EditFormat = Literal["str_replace", "hashline"]
"""Supported file-editing formats for the console toolset."""

GLOB_RESULT_LIMIT = 100
"""Matches listed by `glob` before the rest are summarised as a count."""

GREP_RESULT_LIMIT = 50
"""Files or lines listed by `grep` before the rest are summarised as a count."""

GREP_LINE_WIDTH = 100
"""Characters of a matching line shown in `grep`'s content mode."""

DEFAULT_EXECUTE_TIMEOUT = 120


@runtime_checkable
class ConsoleDeps(Protocol):
    """Dependencies that provide a backend for the console tools."""

    @property
    def backend(self) -> BackendProtocol | AsyncBackendProtocol:
        """The backend for file operations."""
        ...


class _ConsoleToolsetTestAttrs(Protocol):
    """Attributes attached to the toolset for the test suite to reach."""

    _console_default_ignore_hidden: bool
    _console_grep_impl: Callable[..., Awaitable[str]]


def create_console_toolset(  # noqa: C901
    id: str | None = None,
    backend: BackendProtocol | AsyncBackendProtocol | None = None,
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

    Works with any backend implementing `BackendProtocol` — `LocalBackend`,
    `DockerSandbox`, `StateBackend` and so on.

    Args:
        id: Optional unique ID for the toolset.
        backend: Backend every tool operates on. When omitted, each call reads
            `ctx.deps.backend` instead, which requires the agent's deps to
            satisfy :class:`ConsoleDeps`. Pass it explicitly when the host owns
            its own deps type and cannot add a `backend` field to it — a
            capability holding a sandbox, for instance.
        include_execute: Include the `execute` tool. Requires a backend with an
            `execute` method.
        include_background: Include the background-shell tools. Requires a
            backend implementing `BackgroundSandboxProtocol`.
        require_write_approval: Whether `write_file` and the edit tool require
            approval. Ignored when `permissions` is given.
        require_execute_approval: Whether `execute` requires approval. Ignored
            when `permissions` is given.
        default_ignore_hidden: Default for `grep`'s hidden-file handling.
        permissions: Ruleset deciding which tools exist and which need approval:
            an operation defaulting to "deny" drops its tools entirely, one
            defaulting to "ask" marks them as requiring approval.
        max_retries: Times a tool may retry within one run after invalid
            arguments, with the validation error fed back to the model.
        image_support: Return recognized image files (`.png`, `.jpg`, `.jpeg`,
            `.gif`, `.webp`) as `BinaryContent` a multimodal model can see,
            instead of garbled text.
        max_image_bytes: Largest image returned; bigger ones yield an error.
        document_support: Return recognized documents (`.pdf`) as
            `BinaryContent` for models that understand documents natively. Kept
            separate from `image_support` so the two can evolve apart.
        max_document_bytes: Largest document returned; bigger ones yield an error.
        edit_format: `"str_replace"` matches exact strings; `"hashline"` tags
            each line with a content hash so the model references lines by
            `number:hash` instead of reproducing text.
        descriptions: Per-tool description overrides, keyed by tool name: `ls`,
            `read_file`, `write_file`, `edit_file`, `hashline_edit`, `glob`,
            `grep`, `execute`, `run_in_background`, `read_output`, `kill_shell`,
            `list_shells`.

    Example:
        ```python
        from dataclasses import dataclass

        from pydantic_ai_backends import LocalBackend, create_console_toolset
        from pydantic_ai_backends.permissions import DEFAULT_RULESET

        @dataclass
        class MyDeps:
            backend: LocalBackend

        toolset = create_console_toolset()
        deps = MyDeps(backend=LocalBackend("/workspace"))

        hashline = create_console_toolset(edit_format="hashline")
        multimodal = create_console_toolset(image_support=True, document_support=True)
        guarded = create_console_toolset(permissions=DEFAULT_RULESET)
        ```
    """
    described = descriptions or {}

    def backend_for(ctx: RunContext[ConsoleDeps]) -> BackendProtocol | AsyncBackendProtocol:
        return backend if backend is not None else ctx.deps.backend

    write_approval = _ruleset.requires_approval(permissions, "write", require_write_approval)
    execute_approval = _ruleset.requires_approval(permissions, "execute", require_execute_approval)

    toolset: FunctionToolset[ConsoleDeps] = FunctionToolset(id=id, max_retries=max_retries)

    async def binary_content(
        target: BackendProtocol | AsyncBackendProtocol, path: str
    ) -> Any | None:  # pragma: no cover - exercised through read_file
        """Image or document content for `path`, when either is enabled."""
        if image_support:
            image = await image_content(target, path, max_image_bytes)
            if image is not None:
                return image
        if document_support:
            return await document_content(target, path, max_document_bytes)
        return None

    @toolset.tool(description=described.get("ls", LS_DESCRIPTION))
    async def ls(  # pragma: no cover
        ctx: RunContext[ConsoleDeps],
        path: str = ".",
    ) -> str:
        """List files and directories at the given path.

        Args:
            path: Directory path to list. Defaults to current directory.
        """
        entries = await ensure_async(backend_for(ctx)).ls_info(path)
        if not entries:
            return f"Directory '{path}' is empty or does not exist"

        lines = [f"Contents of {path}:"]
        for entry in entries:
            if entry["is_dir"]:
                lines.append(f"  {entry['name']}/")
            else:
                size = entry.get("size")
                lines.append(f"  {entry['name']}{f' ({size} bytes)' if size is not None else ''}")
        return "\n".join(lines)

    if edit_format == "hashline":

        @toolset.tool(description=described.get("read_file", HASHLINE_READ_FILE_DESCRIPTION))
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
                limit: Maximum number of lines to read.
            """
            binary = await binary_content(backend_for(ctx), path)
            if binary is not None:
                return binary

            from pydantic_ai_backends.hashline import format_hashline_output

            backend = ensure_async(backend_for(ctx))
            if not await backend.exists(path):
                return f"Error: File '{path}' not found"

            raw = await backend.read_bytes(path)
            _tracking.record_read(backend_for(ctx), path, raw)
            return format_hashline_output(raw.decode("utf-8", errors="replace"), offset, limit)

    else:

        @toolset.tool(description=described.get("read_file", READ_FILE_DESCRIPTION))
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
                limit: Maximum number of lines to read.
            """
            binary = await binary_content(backend_for(ctx), path)
            if binary is not None:
                return binary

            backend = ensure_async(backend_for(ctx))
            result = await backend.read(path, offset, limit)
            if not result.startswith("Error"):
                await _tracking.record_path_read(backend, backend_for(ctx), path)
            return result

    @toolset.tool(
        description=described.get("write_file", WRITE_FILE_DESCRIPTION),
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
        result = await ensure_async(backend_for(ctx)).write(path, content)
        if result.error:
            return f"Error: {result.error}"

        # The agent knows this file's content now, so an immediate edit must not
        # be refused as stale.
        _tracking.record_read(backend_for(ctx), path, content.encode("utf-8"))
        return f"Wrote {len(content.splitlines())} lines to {result.path}"

    if edit_format == "hashline":

        @toolset.tool(
            description=described.get("hashline_edit", HASHLINE_EDIT_DESCRIPTION),
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

            raw_backend = backend_for(ctx)
            backend = ensure_async(raw_backend)

            async with _tracking.edit_lock(raw_backend, path):
                if not await backend.exists(path):
                    return f"Error: File '{path}' not found"

                current = (await backend.read_bytes(path)).decode("utf-8", errors="replace")
                new_text, error, summary = apply_hashline_edit_with_summary(
                    current,
                    start_line,
                    start_hash,
                    new_content,
                    end_line,
                    end_hash,
                    insert_after,
                )
                if error:
                    return f"Error: {error}"

                written = await backend.write(path, new_text)
                if written.error:
                    return f"Error: {written.error}"
                return f"Edited {written.path}: {summary}"

    else:

        @toolset.tool(
            description=described.get("edit_file", EDIT_FILE_DESCRIPTION),
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
            raw_backend = backend_for(ctx)
            backend = ensure_async(raw_backend)

            stale = await _tracking.staleness_error(backend, raw_backend, path)
            if stale is not None:
                return stale

            result = await backend.edit(path, old_string, new_string, replace_all)
            if result.error:
                return f"Error: {result.error}"

            # The agent's view is the post-edit content now, so a follow-up edit
            # must not be flagged as stale.
            await _tracking.record_path_read(backend, raw_backend, path)
            return f"Edited {result.path}: replaced {result.occurrences} occurrence(s)"

    @toolset.tool(description=described.get("glob", GLOB_DESCRIPTION))
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
        entries = await ensure_async(backend_for(ctx)).glob_info(pattern, path)
        if not entries:
            return f"No files matching '{pattern}' in {path}"

        lines = [f"Found {len(entries)} file(s) matching '{pattern}':"]
        lines.extend(f"  {entry['path']}" for entry in entries[:GLOB_RESULT_LIMIT])
        if len(entries) > GLOB_RESULT_LIMIT:
            lines.append(f"  ... and {len(entries) - GLOB_RESULT_LIMIT} more")
        return "\n".join(lines)

    @toolset.tool(description=described.get("grep", GREP_DESCRIPTION))
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
        result = await ensure_async(backend_for(ctx)).grep_raw(
            pattern, path, glob_pattern, ignore_hidden
        )
        if isinstance(result, str):
            return result
        if not result:
            return f"No matches for '{pattern}'"

        matches: list[GrepMatch] = result
        if output_mode == "count":
            return f"Found {len(matches)} match(es) for '{pattern}'"

        if output_mode == "files_with_matches":
            files = sorted({match["path"] for match in matches})
            return _truncated_list(f"Files containing '{pattern}':", files, "more files")

        rendered = [
            f"{m['path']}:{m['line_number']}: {m['line'][:GREP_LINE_WIDTH]}" for m in matches
        ]
        return _truncated_list(f"Matches for '{pattern}':", rendered, "more matches")

    # Exposed for the test suite.
    cast(_ConsoleToolsetTestAttrs, toolset)._console_default_ignore_hidden = default_ignore_hidden
    cast(_ConsoleToolsetTestAttrs, toolset)._console_grep_impl = grep

    if include_execute:

        @toolset.tool(
            description=described.get("execute", EXECUTE_DESCRIPTION),
            requires_approval=execute_approval,
        )
        async def execute(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
            command: str,
            timeout: int | None = DEFAULT_EXECUTE_TIMEOUT,
        ) -> str:
            """Execute a shell command in the working directory.

            Args:
                command: Shell command to execute.
                timeout: Maximum execution time in seconds. Increase \
for long-running builds or test suites.
            """
            target = backend_for(ctx)
            async_backend = ensure_async(target)

            if not hasattr(async_backend, "execute"):
                return "Error: Backend does not support command execution"
            if hasattr(target, "execute_enabled") and not target.execute_enabled:  # pyright: ignore[reportAttributeAccessIssue]
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

        def background(ctx: RunContext[ConsoleDeps]) -> Any | None:  # pragma: no cover
            """The async background sandbox, or `None` when unsupported."""
            backend = ensure_async(backend_for(ctx))
            return backend if hasattr(backend, "execute_background") else None

        @toolset.tool(
            description=described.get("run_in_background", RUN_IN_BACKGROUND_DESCRIPTION),
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
            sandbox = background(ctx)
            if sandbox is None:
                return _NO_BACKGROUND_SUPPORT
            try:
                handle = await sandbox.execute_background(command)
            except (RuntimeError, PermissionError) as e:
                return f"Error: {e}"
            return (
                f"Started background shell {handle.shell_id} (pid {handle.pid}).\n"
                f"Use read_output('{handle.shell_id}') to follow its output and "
                f"kill_shell('{handle.shell_id}') to stop it."
            )

        @toolset.tool(description=described.get("read_output", READ_OUTPUT_DESCRIPTION))
        async def read_output(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
            shell_id: str,
        ) -> str:
            """Read new output from a background shell.

            Args:
                shell_id: The id returned by run_in_background.
            """
            sandbox = background(ctx)
            if sandbox is None:
                return _NO_BACKGROUND_SUPPORT

            result = await sandbox.read_background(shell_id)
            status = "running" if result.running else f"exited (code {result.exit_code})"
            body = (result.stdout + result.stderr).strip() or "(no new output)"
            return f"[{result.shell_id}] {status}\n{body}"

        @toolset.tool(
            description=described.get("kill_shell", KILL_SHELL_DESCRIPTION),
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
            sandbox = background(ctx)
            if sandbox is None:
                return _NO_BACKGROUND_SUPPORT
            if await sandbox.kill_background(shell_id):
                return f"Killed background shell {shell_id}."
            return f"Background shell {shell_id} was already finished or unknown."

        @toolset.tool(description=described.get("list_shells", LIST_SHELLS_DESCRIPTION))
        async def list_shells(  # pragma: no cover
            ctx: RunContext[ConsoleDeps],
        ) -> str:
            """List the background shells started this session."""
            sandbox = background(ctx)
            if sandbox is None:
                return _NO_BACKGROUND_SUPPORT

            infos = await sandbox.list_background()
            if not infos:
                return "No background shells."
            return "\n".join(
                f"{i.shell_id}  {'running' if i.running else f'exited({i.exit_code})'}  {i.command}"
                for i in infos
            )

    for tool_name in _denied_tools(permissions):
        toolset.tools.pop(tool_name, None)

    return toolset


_NO_BACKGROUND_SUPPORT = "Error: Backend does not support background processes"


def _denied_tools(permissions: PermissionRuleset | None) -> set[str]:
    """Tools to unregister because their operation is denied outright."""
    denied: set[str] = set()
    if _ruleset.is_denied(permissions, "write"):
        denied.add("write_file")
    if _ruleset.is_denied(permissions, "edit"):
        denied.update({"edit_file", "hashline_edit"})
    if _ruleset.is_denied(permissions, "execute"):
        denied.add("execute")
    return denied


def _truncated_list(header: str, items: list[str], noun: str) -> str:
    """Render `items` under `header`, summarising anything past the limit."""
    lines = [header, *(f"  {item}" for item in items[:GREP_RESULT_LIMIT])]
    if len(items) > GREP_RESULT_LIMIT:
        lines.append(f"  ... and {len(items) - GREP_RESULT_LIMIT} {noun}")
    return "\n".join(lines)


def get_console_system_prompt(edit_format: EditFormat = "str_replace") -> str:
    """The system prompt describing the console tools.

    Args:
        edit_format: Which edit format to describe.
    """
    if edit_format == "hashline":
        return HASHLINE_CONSOLE_PROMPT
    return CONSOLE_SYSTEM_PROMPT


ConsoleToolset = create_console_toolset
"""Alias for :func:`create_console_toolset`."""
