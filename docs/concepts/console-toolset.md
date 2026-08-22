# Console Toolset

The console toolset provides ready-to-use pydantic-ai tools for file operations and shell execution.

!!! info "Requires pydantic-ai"
    ```bash
    pip install pydantic-ai-backend[console]
    ```

## Quick Start

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import LocalBackend, create_console_toolset


@dataclass
class Deps:
    backend: LocalBackend


# Create toolset
toolset = create_console_toolset()

# Create agent with tools
agent = Agent("openai:gpt-4o", deps_type=Deps)
agent = agent.with_toolset(toolset)

# Run
backend = LocalBackend(root_dir="/workspace")
result = agent.run_sync("List all Python files", deps=Deps(backend=backend))
```

## Available Tools

| Tool | Description |
|------|-------------|
| `ls` | List files in a directory |
| `read_file` | Read file content with line numbers |
| `write_file` | Create or overwrite a file |
| `edit_file` | Replace strings in a file |
| `glob` | Find files matching a pattern |
| `grep` | Search for patterns in files |
| `execute` | Run shell commands (optional) |

## Configuration

```python
# Default: execute enabled, requires approval
toolset = create_console_toolset()

# Without shell execution
toolset = create_console_toolset(include_execute=False)

# Auto-approve writes
toolset = create_console_toolset(
    require_write_approval=False,
    require_execute_approval=False,
)

# Custom toolset ID
toolset = create_console_toolset(id="my-console")

# Include hidden files by default for grep
toolset = create_console_toolset(default_ignore_hidden=False)

# Use the hashline edit format instead of exact string replacement
toolset = create_console_toolset(edit_format="hashline")
```

The `edit_format` parameter selects how the model edits files. The default
`"str_replace"` exposes an `edit_file` tool that does exact string matching.
`"hashline"` swaps it for a `hashline_edit` tool — see [Hashline Edit
Format](#hashline-edit-format) below.

## What the Model Reads

Each tool's text is one `ToolText` object: what the tool does, when to use it and
when to reach for another one, what every argument means, and what comes back —
including how a result is truncated and what a failure looks like. The
description handed to the model is composed from it, and the per-argument text
becomes the descriptions in the tool's JSON schema.

```python
from pydantic_ai_backends import TOOL_TEXT

TOOL_TEXT["grep"].summary  # one sentence — what a catalogue should show
TOOL_TEXT["grep"].returns  # the three shapes `output_mode` can answer with
TOOL_TEXT["grep"].args  # keyed by parameter name
TOOL_TEXT["grep"].render()  # the description the model is given
```

`render` produces what pydantic-ai produces for a docstring with a `Returns:`
section — the prose inside `<summary>`, the return description inside
`<returns>`:

```
<summary>Search the contents of files for a regular expression.

Use this rather than a shell `grep` …</summary>
<returns>
<description>`files_with_matches` lists paths, `content` lists `path:line: text` …</description>
</returns>
```

That is deliberate rather than decorative: a host registers these beside tools of
its own that were built from docstrings, and two conventions in one tool list is
one more thing for the model to reconcile. `tests/test_tool_text.py` pins the
shape against a tool the framework renders itself.

### Profiles

Two kinds of agent read these tools and they need different amounts of text. A
coding agent wants the git, dependency and debugging guidance; an agent whose
workspace is scratch space for one conversation never sees a repository and pays
for those sentences on every request.

```python
coding = create_console_toolset()  # the default
lean = create_console_toolset(profile="agent")  # ~240 tokens lighter
```

`"agent"` drops the repository guidance from `execute` and `write_file` and
keeps everything true of any workspace, the return shapes included.

### Custom Tool Descriptions

Override any tool's text with the `descriptions` parameter — useful when a host
lists these tools in its own catalogue and needs the text it shows and the text
the model reads to be one string:

```python
from pydantic_ai_backends import ToolText

toolset = create_console_toolset(
    descriptions={
        # A string replaces the description; the argument text is kept.
        "execute": "Run a shell command in the customer's workspace.",
        # A ToolText replaces both halves.
        "read_file": ToolText(
            summary="Read a file from the shared drive.",
            args={"path": "Path under /drive.", "offset": "...", "limit": "..."},
            returns="The file's lines, numbered from 1.",
        ),
    }
)
```

Valid keys are the tool names: `ls`, `read_file`, `write_file`, `edit_file`,
`hashline_edit`, `glob`, `grep`, `execute`, `run_in_background`, `read_output`,
`kill_shell`, `list_shells`. An unknown key raises `UserError` rather than being
ignored — a misspelled override that silently reaches nothing is one nobody
discovers.

## Failures

A tool reports trouble in one of two shapes, and they say different things to the
model.

**A mistake it can fix raises `ModelRetry`** — the message goes back as a retry
prompt and the model gets another attempt at the call:

- a file that is not there, or an offset past its end
- an `old_string` that matches nothing, or matches more than once
- a file edited between the read and the edit, or a hashline hash that no longer
  matches

**Everything else is returned as text**, because it is the answer rather than a
malformed call: a non-zero exit from `execute`, a `grep` that found nothing, a
backend with no shell, a dropped connection — and a **permission refusal**, which
is deliberate. A retry prompt on a refusal invites the model to look for a way
around the rule.

`max_retries` is the budget, and it is a floor as much as a ceiling: on a tool's
final attempt the message is returned instead of raised. `ModelRetry` past the
budget ends the whole run with `UnexpectedModelBehavior`, so a model that mistypes
an `old_string` twice would kill a run that would otherwise have carried on. The
worst case here is the behaviour this library had before — an error string the
model reads and adapts to.

## Permission-based Configuration

For fine-grained control, use the permission system:

```python
from pydantic_ai_backends import create_console_toolset
from pydantic_ai_backends.permissions import (
    DEFAULT_RULESET,
    READONLY_RULESET,
    PermissionRuleset,
    OperationPermissions,
)

# Use pre-configured presets
toolset = create_console_toolset(permissions=DEFAULT_RULESET)

# Read-only toolset
toolset = create_console_toolset(permissions=READONLY_RULESET)

# Custom permissions
custom = PermissionRuleset(
    write=OperationPermissions(default="allow"),  # No approval needed
    execute=OperationPermissions(default="ask"),  # Requires approval
)
toolset = create_console_toolset(permissions=custom)
```

When `permissions` is provided, it overrides the legacy `require_write_approval` and `require_execute_approval` flags.

An operation whose default is `"deny"` has its tools removed outright. For
`execute` that means **all five** shell tools — `execute`, `run_in_background`,
`read_output`, `kill_shell` and `list_shells` — since they are the same operation
reached different ways.

Note what this does and does not do. The ruleset decides which tools *exist* and
which need approval; it does not filter individual paths. Per-path enforcement is
the backend's job, so pass the ruleset to the backend as well when you want it:

```python
ruleset = READONLY_RULESET
toolset = create_console_toolset(permissions=ruleset)
backend = LocalBackend(root_dir="/workspace", permissions=ruleset)
```

See [Permissions](permissions.md) for full documentation.

## Image Support

When working with multimodal models (e.g., GPT-4o, Claude), you can enable image support so that `read_file` returns image data that the model can see, instead of garbled binary text.

```python
# Enable image support
toolset = create_console_toolset(image_support=True)
```

When `image_support=True`, reading a file with a recognized image extension returns a `BinaryContent` object that pydantic-ai sends to the model as an inline image. For all other file types, `read_file` behaves normally and returns text.

### Recognized Image Types

| Extension | Media Type |
|-----------|------------|
| `.png` | `image/png` |
| `.jpg` | `image/jpeg` |
| `.jpeg` | `image/jpeg` |
| `.gif` | `image/gif` |
| `.webp` | `image/webp` |

### Size Limits

Large images are rejected to avoid excessive token usage. The default limit is 50 MB:

```python
# Default: 50 MB max
toolset = create_console_toolset(image_support=True)

# Custom limit: 5 MB max
toolset = create_console_toolset(
    image_support=True,
    max_image_bytes=5 * 1024 * 1024,
)
```

Images exceeding the limit return an error message like: `Error: Image 'photo.png' too large (12.3MB, max 5.0MB)`.

### Example: Visual Analysis Agent

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import LocalBackend, create_console_toolset


@dataclass
class Deps:
    backend: LocalBackend


# Enable image support for multimodal model
toolset = create_console_toolset(image_support=True)

agent = Agent(
    "openai:gpt-4o",  # Multimodal model
    system_prompt="You can read and analyze images using read_file.",
    deps_type=Deps,
)
agent = agent.with_toolset(toolset)

result = agent.run_sync(
    "Read screenshot.png and describe what you see",
    deps=Deps(backend=LocalBackend(root_dir="/workspace")),
)
```

!!! tip "When to enable image support"
    Only enable `image_support` when using a multimodal model that can process images.
    With text-only models, image data will be wasted tokens. When disabled (the default),
    reading an image file returns the raw text representation, which is not useful
    but avoids unexpected behavior.

## Hashline Edit Format

`edit_format="hashline"` is an alternative to the default `"str_replace"` edit
format. Instead of reproducing the exact text it wants to change, the model
references lines by a short content hash, which avoids whitespace-matching
errors and reduces output tokens.

```python
toolset = create_console_toolset(edit_format="hashline")
```

With this format, `read_file` returns each line tagged as
`{line_number}:{hash}|{content}`, where the hash is a 2-character hex digest of
the line:

```
1:a3|def hello():
2:f1|    return "world"
3:0e|
```

The `edit_file` tool is replaced by **`hashline_edit`**, which the model calls
with the `line:hash` pair of the anchor line plus the new content. It supports
single-line replacement, range replacement (`end_line` + `end_hash`), inserting
after a line (`insert_after=True`), and deletion (`new_content=""`). If a hash
no longer matches, the file changed since the last read and the model must
re-read before editing.

!!! tip "Token / accuracy tradeoff"
    Hashline trades a small amount of extra read-time output (the per-line hash
    tags) for cheaper, more reliable edits: the model no longer has to echo back
    long `old_string` blocks, so edit calls use fewer tokens and are less likely
    to fail on whitespace mismatches. `str_replace` (the default) is simpler and
    a better fit for models that have not been prompted for the hashline format.

The matching system prompt for this mode is returned by
[`get_console_system_prompt(edit_format="hashline")`][pydantic_ai_backends.get_console_system_prompt].

## ConsoleDeps Protocol

Your dependencies class must have a `backend` property:

```python
from pydantic_ai_backends import BackendProtocol


class ConsoleDeps(Protocol):
    @property
    def backend(self) -> BackendProtocol: ...
```

Any class with a `backend` attribute works:

```python
from dataclasses import dataclass


@dataclass
class MyDeps:
    backend: LocalBackend
    user_id: str  # Additional fields are fine
```

## System Prompt

Include the console system prompt for better tool usage:

```python
from pydantic_ai_backends import get_console_system_prompt

system_prompt = f"""You are a helpful coding assistant.

{get_console_system_prompt()}
"""

agent = Agent(
    "openai:gpt-4o",
    system_prompt=system_prompt,
    deps_type=Deps,
)
```

## Tool Details

### ls

```python
async def ls(ctx, path: str = ".") -> str:
    """List files and directories at the given path."""
```

### read_file

```python
async def read_file(ctx, path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read file content with line numbers."""
```

### write_file

```python
async def write_file(ctx, path: str, content: str) -> str:
    """Write content to a file (creates or overwrites)."""
```

### edit_file

```python
async def edit_file(
    ctx, path: str, old_string: str, new_string: str, replace_all: bool = False
) -> str:
    """Edit a file by replacing strings."""
```

### glob

```python
async def glob(ctx, pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern."""
```

### grep

```python
async def grep(
    ctx,
    pattern: str,
    path: str | None = None,
    glob_pattern: str | None = None,
    output_mode: str = "files_with_matches",
    ignore_hidden: bool = True,
) -> str:
    """Search for a regex pattern in files.

    Args:
        pattern: Regex pattern to search for.
        path: Optional file/directory scope.
        glob_pattern: Glob filter applied before searching.
        output_mode: "content", "files_with_matches", or "count".
        ignore_hidden: Whether to skip hidden files (defaults to the toolset setting).
    """
```

### execute

```python
async def execute(ctx, command: str, timeout: int | None = 120) -> str:
    """Execute a shell command."""
```

## With Different Backends

The toolset works with any backend:

=== "LocalBackend"

    ```python
    backend = LocalBackend(root_dir="/workspace")
    ```

=== "StateBackend"

    ```python
    backend = StateBackend()
    ```

=== "DockerSandbox"

    ```python
    backend = DockerSandbox(image="python:3.12-slim")
    ```

## Next Steps

- [Permissions](permissions.md) - Fine-grained access control
- [CLI Agent Example](../examples/cli-agent.md) - Build a CLI coding assistant
- [API Reference](../api/toolsets.md) - Complete API
