"""Console toolset for AI agents — file operations and shell execution."""

from __future__ import annotations

import functools
import weakref
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, cast

from pydantic_ai import RunContext
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    SkipModelRequest,
    SkipToolExecution,
    SkipToolValidation,
    UserError,
)
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.workspaces import Workspace

from pydantic_ai_backends.toolsets import _failures, _guard, _ruleset, _tracking
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
from pydantic_ai_backends.toolsets._workspace import FileOps, WorkspaceOps
from pydantic_ai_backends.toolsets.descriptions import (
    CONSOLE_SYSTEM_PROMPT as CONSOLE_SYSTEM_PROMPT,
)
from pydantic_ai_backends.toolsets.descriptions import (
    DEFAULT_PROFILE,
    OVERRIDE_KEYS,
    TOOL_TEXT,
    Profile,
    ToolText,
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
    from pydantic_ai_backends.permissions.checker import AskCallback, AskFallback
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

EXECUTE_TOOLS = frozenset({"execute"})
"""Every tool the "execute" operation governs.

A set, so a ruleset denying shell execution drops every tool that runs a command
the day one more is added beside `execute`."""


class _ConsoleToolsetTestAttrs(Protocol):
    """Attributes attached to the toolset for the test suite to reach."""

    _console_default_ignore_hidden: bool
    _console_grep_impl: Callable[..., Awaitable[str]]
    _console_execute_impl: Callable[..., Awaitable[str]]


_ToolFn = TypeVar("_ToolFn", bound=Callable[..., Awaitable[Any]])

_PASSES_THROUGH = (
    ModelRetry,
    ApprovalRequired,
    CallDeferred,
    SkipToolExecution,
    SkipToolValidation,
    SkipModelRequest,
    UserError,
)
"""Exceptions that must reach pydantic-ai rather than becoming a tool error.

None of the first six is a failure — they steer the run. `ModelRetry` asks the
model to try again with a message, `ApprovalRequired` suspends the call for a
human, `CallDeferred` hands it to an external process, and the `Skip*` family
short-circuits execution or validation. Swallowing `ModelRetry` in particular
turns a retry into a dead end the model cannot recover from.

`UserError` is the seventh for the opposite reason: it means the library was
misused, and reporting a programming error to the model as a failed file
operation hides the bug instead of surfacing it. It subclasses `RuntimeError`, so
the narrower `except RuntimeError` this replaced was already swallowing it.

They share no base class, hence the list. All seven exist as of the
`pydantic-ai-slim` floor this package declares.
"""


def _degrade_on_error(fn: _ToolFn) -> _ToolFn:
    """Turn an exception into a failed tool call, not a failed run.

    The workspace operations return their failures rather than raise, so this
    only catches what slips past them — a provider's transport error, a
    `PermissionError` from the ruleset. Left uncaught, any of those escapes the
    tool and ends the agent's run, which is a far worse outcome than the model
    being told one operation failed and choosing what to do next.

    Applied to every tool rather than the few that looked risky: the file
    operations reach the same workspace over the same transport as `execute`
    does, so there is no reason one would raise and another would not.
    """

    @functools.wraps(fn)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except _PASSES_THROUGH:
            raise
        except Exception as exc:
            return f"Error: {exc}"

    return cast("_ToolFn", guarded)


def create_console_toolset(  # noqa: C901
    id: str | None = None,
    include_execute: bool = True,
    require_write_approval: bool = False,
    require_execute_approval: bool = True,
    default_ignore_hidden: bool = True,
    permissions: PermissionRuleset | None = None,
    ask_callback: AskCallback | None = None,
    ask_fallback: AskFallback = "error",
    max_retries: int = 1,
    image_support: bool = False,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    document_support: bool = False,
    max_document_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
    edit_format: EditFormat = "str_replace",
    descriptions: Mapping[str, str | ToolText] | None = None,
    profile: Profile = DEFAULT_PROFILE,
) -> FunctionToolset[Any]:
    """Create the console tools, operating on the run's Pydantic AI workspace.

    Every tool reaches `ctx.workspace`: whatever a workspace capability on the
    agent supplied — `DockerWorkspace`, `SandboxdWorkspace`, `StateWorkspace`,
    Pydantic AI's `LocalWorkspace`, or the harness's E2B and Modal sandboxes. A
    run without a workspace gets an error from every tool rather than a crash.

    Args:
        id: Optional unique ID for the toolset.
        include_execute: Include the `execute` tool. On a workspace that cannot
            run commands — read-only, or `StateWorkspace` — it answers with the
            workspace's refusal.
        require_write_approval: Whether `write_file` and the edit tool require
            approval. Ignored when `permissions` is given.
        require_execute_approval: Whether `execute` requires approval. Ignored
            when `permissions` is given.
        default_ignore_hidden: Default for `grep`'s hidden-file handling.
        permissions: Ruleset deciding which tools exist and which need approval:
            an operation defaulting to "deny" drops its tools entirely, one
            defaulting to "ask" marks them as requiring approval.
        max_retries: Times a tool may retry within one run, with the message
            fed back to the model — pydantic-ai's own argument validation, and
            the mistakes `toolsets/_failures.py` steers on: a missing file, an
            `old_string` that is absent or matches twice, a stale read. Past the
            budget the message is returned rather than raised, so a run never
            ends on one.
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
        descriptions: Per-tool text overrides, keyed by tool name: `ls`,
            `read_file`, `write_file`, `edit_file`, `hashline_edit`, `glob`,
            `grep`, `execute`. A string replaces the tool's description and leaves
            its argument text alone; a :class:`ToolText` replaces both. An
            unknown key raises `UserError` rather than being ignored, since a
            silent override is one nobody discovers.
        profile: How much guidance the descriptions carry. `"coding"` includes
            the guidance written for an agent working in a repository — git,
            dependencies, debugging a failed command — and `"agent"` leaves it
            out, which is about 250 tokens a request an agent with a scratch
            workspace was paying for advice it could not use.

    Example:
        ```python
        from pydantic_ai import Agent
        from pydantic_ai.capabilities import LocalWorkspace

        from pydantic_ai_backends import create_console_toolset
        from pydantic_ai_backends.permissions import DEFAULT_RULESET

        toolset = create_console_toolset()
        agent = Agent(
            "anthropic:claude-opus-5-5", capabilities=[LocalWorkspace(".")], toolsets=[toolset]
        )

        hashline = create_console_toolset(edit_format="hashline")
        multimodal = create_console_toolset(image_support=True, document_support=True)
        guarded = create_console_toolset(permissions=DEFAULT_RULESET)
        ```
    """
    overrides: Mapping[str, str | ToolText] = descriptions or {}
    unknown = sorted(set(overrides) - OVERRIDE_KEYS)
    if unknown:
        raise UserError(
            f"Unknown tool name(s) in `descriptions`: {', '.join(unknown)}. "
            f"Valid names: {', '.join(sorted(OVERRIDE_KEYS))}."
        )

    operations: weakref.WeakKeyDictionary[Workspace, FileOps] = weakref.WeakKeyDictionary()

    def backend_for(ctx: RunContext[Any]) -> FileOps:
        """The run's workspace operations, with the ruleset applied to them.

        The one place every tool resolves what it operates on, which is why the
        guard goes here: applying per-path rules needs a path, and a path only
        exists per call.

        One instance per workspace, not per call: the tools remember what the
        agent has read on it, so an edit to a file changed since is refused, and
        a fresh object each call would forget every read between two tool calls.
        """
        ops = operations.get(ctx.workspace)
        if ops is None:
            ops = operations[ctx.workspace] = _guard.guarding(
                WorkspaceOps(ctx.workspace),
                permissions,
                workspace=ctx.workspace,
                ask_callback=ask_callback,
                ask_fallback=ask_fallback,
            )
        return ops

    write_approval = _ruleset.requires_approval(permissions, "write", require_write_approval)
    execute_approval = _ruleset.requires_approval(permissions, "execute", require_execute_approval)

    toolset: FunctionToolset[Any] = FunctionToolset(id=id, max_retries=max_retries)

    def described(
        text_id: str,
        tool_name: str | None = None,
        *,
        requires_approval: bool = False,
    ) -> Callable[[_ToolFn], _ToolFn]:
        """Register a tool with the text this configuration gives it.

        One `ToolText` supplies both halves of what the model reads, but they
        travel separately: the description is passed to the decorator, while the
        per-argument text reaches the JSON schema through the function's
        docstring and through nothing else — hence the assignment. It is also
        why the tools below carry a one-line docstring rather than a second copy
        of the argument text, which is a copy that drifts.

        Args:
            text_id: Key in `TOOL_TEXT`. Differs from the tool name only for
                `read_file`, which has one text per edit format.
            tool_name: Name a caller overrides this tool by, when it is not the
                text id.
            requires_approval: Whether the tool call is suspended for a human.
        """
        name = tool_name or text_id
        override = overrides.get(name)
        text = override if isinstance(override, ToolText) else TOOL_TEXT[text_id]
        description = override if isinstance(override, str) else text.render(profile)

        def register(fn: _ToolFn) -> _ToolFn:
            cast("Any", fn).__doc__ = text.docstring()
            registered = toolset.tool(description=description, requires_approval=requires_approval)(
                fn
            )
            return cast("_ToolFn", registered)

        return register

    async def binary_content(
        target: FileOps, path: str
    ) -> Any | None:  # pragma: no cover - exercised through read_file
        """Image or document content for `path`, when either is enabled."""
        if image_support:
            image = await image_content(target, path, max_image_bytes)
            if image is not None:
                return image
        if document_support:
            return await document_content(target, path, max_document_bytes)
        return None

    @described("ls")
    @_degrade_on_error
    async def ls(
        ctx: RunContext[Any],
        path: str = ".",
    ) -> str:
        """List files and directories at the given path."""
        entries = await backend_for(ctx).ls_info(path)
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

        @described("hashline_read_file", "read_file")
        @_degrade_on_error
        async def read_file(
            ctx: RunContext[Any],
            path: str,
            offset: int = 0,
            limit: int = 2000,
        ) -> Any:
            """Read file content with hashline tags."""
            binary = await binary_content(backend_for(ctx), path)
            if binary is not None:
                return binary

            from pydantic_ai_backends.hashline import format_hashline_output

            backend = backend_for(ctx)
            if not await backend.exists(path):
                return _failures.steer(
                    ctx,
                    f"Error: File '{path}' not found. Check the path with `ls` or "
                    "`glob`, then read it again.",
                )

            raw = await backend.read_bytes(path)
            _tracking.record_read(backend_for(ctx), path, raw)
            return format_hashline_output(raw.decode("utf-8", errors="replace"), offset, limit)

    else:

        @described("read_file")
        @_degrade_on_error
        async def read_file(
            ctx: RunContext[Any],
            path: str,
            offset: int = 0,
            limit: int = 2000,
        ) -> Any:
            """Read file content with line numbers."""
            binary = await binary_content(backend_for(ctx), path)
            if binary is not None:
                return binary

            backend = backend_for(ctx)
            result = await backend.read(path, offset, limit)
            if result.startswith("Error"):
                return _failures.steer(ctx, result)
            await _tracking.record_path_read(backend, backend_for(ctx), path)
            return result

    @described("write_file", requires_approval=write_approval)
    @_degrade_on_error
    async def write_file(
        ctx: RunContext[Any],
        path: str,
        content: str,
    ) -> str:
        """Write content to a file."""
        result = await backend_for(ctx).write(path, content)
        if result.error:
            return _failures.steer(ctx, f"Error: {result.error}")

        # The agent knows this file's content now, so an immediate edit must not
        # be refused as stale.
        _tracking.record_read(backend_for(ctx), path, content.encode("utf-8"))
        return f"Wrote {len(content.splitlines())} lines to {result.path}"

    if edit_format == "hashline":

        @described("hashline_edit", requires_approval=write_approval)
        @_degrade_on_error
        async def hashline_edit(
            ctx: RunContext[Any],
            path: str,
            start_line: int,
            start_hash: str,
            new_content: str,
            end_line: int | None = None,
            end_hash: str | None = None,
            insert_after: bool = False,
        ) -> str:
            """Edit a file by referencing lines with their content hashes."""
            from pydantic_ai_backends.hashline import apply_hashline_edit_with_summary

            raw_backend = backend_for(ctx)
            backend = raw_backend

            async with _tracking.edit_lock(raw_backend, path):
                if not await backend.exists(path):
                    return _failures.steer(ctx, f"Error: File '{path}' not found")

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
                    return _failures.steer(ctx, f"Error: {error}")

                written = await backend.write(path, new_text)
                if written.error:
                    return _failures.steer(ctx, f"Error: {written.error}")
                return f"Edited {written.path}: {summary}"

    else:

        @described("edit_file", requires_approval=write_approval)
        @_degrade_on_error
        async def edit_file(
            ctx: RunContext[Any],
            path: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
        ) -> str:
            """Edit a file by performing exact string replacement."""
            raw_backend = backend_for(ctx)
            backend = raw_backend

            # Locked for the same reason `hashline_edit` is: every backend's
            # `edit` is a read, a replace and a write, so two edits to one path
            # in flight together lose one of them. The staleness check belongs
            # inside the lock too — checked outside, it is answered before the
            # other edit's write and passes on content that no longer exists.
            async with _tracking.edit_lock(raw_backend, path):
                stale = await _tracking.staleness_error(backend, raw_backend, path)
                if stale is not None:
                    return _failures.steer(ctx, stale)

                result = await backend.edit(path, old_string, new_string, replace_all)
                if result.error:
                    return _failures.steer(ctx, f"Error: {result.error}")

                # The agent's view is the post-edit content now, so a follow-up
                # edit must not be flagged as stale.
                await _tracking.record_path_read(backend, raw_backend, path)
                return f"Edited {result.path}: replaced {result.occurrences} occurrence(s)"

    @described("glob")
    @_degrade_on_error
    async def glob(
        ctx: RunContext[Any],
        pattern: str,
        path: str = ".",
    ) -> str:
        """Find files matching a glob pattern."""
        entries = await backend_for(ctx).glob_info(pattern, path)
        if not entries:
            return f"No files matching '{pattern}' in {path}"

        lines = [f"Found {len(entries)} file(s) matching '{pattern}':"]
        lines.extend(f"  {entry['path']}" for entry in entries[:GLOB_RESULT_LIMIT])
        if len(entries) > GLOB_RESULT_LIMIT:
            lines.append(f"  ... and {len(entries) - GLOB_RESULT_LIMIT} more")
        return "\n".join(lines)

    @described("grep")
    @_degrade_on_error
    async def grep(
        ctx: RunContext[Any],
        pattern: str,
        path: str | None = None,
        glob_pattern: str | None = None,
        output_mode: Literal["content", "files_with_matches", "count"] = "files_with_matches",
        ignore_hidden: bool = default_ignore_hidden,
    ) -> str:
        """Search for a regex pattern across files."""
        result = await backend_for(ctx).grep_raw(pattern, path, glob_pattern, ignore_hidden)
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

        @described("execute", requires_approval=execute_approval)
        @_degrade_on_error
        async def execute(
            ctx: RunContext[Any],
            command: str,
            timeout: int | None = DEFAULT_EXECUTE_TIMEOUT,
        ) -> str:
            """Execute a shell command in the working directory."""
            result = await backend_for(ctx).execute(command, timeout)

            output = result.output
            if result.truncated:
                output += "\n\n... (output truncated)"
            if result.exit_code is not None and result.exit_code != 0:
                return f"Command failed (exit code {result.exit_code}):\n{output}"
            return str(output)

        # Exposed for the test suite.
        cast(_ConsoleToolsetTestAttrs, toolset)._console_execute_impl = execute

    for tool_name in _denied_tools(permissions):
        toolset.tools.pop(tool_name, None)

    return toolset


def _denied_tools(permissions: PermissionRuleset | None) -> set[str]:
    """Tools to unregister because their operation is denied outright."""
    denied: set[str] = set()
    if _ruleset.is_denied(permissions, "write"):
        denied.add("write_file")
    if _ruleset.is_denied(permissions, "edit"):
        denied.update({"edit_file", "hashline_edit"})
    if _ruleset.is_denied(permissions, "execute"):
        denied |= EXECUTE_TOOLS
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
