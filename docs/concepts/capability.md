# ConsoleCapability

`ConsoleCapability` is the recommended way to give a Pydantic AI agent file and shell
tools. It is a [pydantic-ai capability](https://ai.pydantic.dev/capabilities/) that bundles
the console tools, their instructions and permission enforcement, and the tools work in
the run's [workspace](workspaces.md) — `ctx.workspace`, supplied by a workspace capability
on the same agent.

## Why Capability over Toolset?

| Feature | ConsoleCapability | create_console_toolset |
|---------|:-:|:-:|
| Tools registered automatically | Yes | Yes |
| System prompt injected | Yes | Manual |
| Permission enforcement (deny) | Yes (`prepare_tools`) | Only `requires_approval` |
| Per-path permission checks | Yes (`before_tool_execute`) | No |
| Tools for denied operations hidden from the model | Yes | No |

A plain toolset created with a read-only ruleset can still surface
`write_file`/`edit_file`/`execute` to the model and only block them on call.
`ConsoleCapability` removes denied tools entirely via `prepare_tools`, so the
model never sees operations it is not allowed to perform.

## Basic Usage

```python
from pydantic_ai import Agent
from pydantic_ai_backends import ConsoleCapability, DockerWorkspace

agent = Agent(
    "anthropic:claude-opus-5-5",
    capabilities=[DockerWorkspace(image="python:3.12-slim"), ConsoleCapability()],
)
```

Any workspace works: Pydantic AI's own `LocalWorkspace(".")` for trusted local work, the
ones in this library, or a harness sandbox such as `E2BSandbox`. A run without one gets
an error from every tool rather than a crash.

## With Permissions

```python
from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import READONLY_RULESET, PERMISSIVE_RULESET

# Read-only — write/edit/execute tools hidden from model entirely
agent = Agent(
    "openai:gpt-4.1",
    capabilities=[
        ConsoleCapability(permissions=READONLY_RULESET),
    ],
)

# Permissive — everything allowed except secrets
agent = Agent(
    "openai:gpt-4.1",
    capabilities=[
        ConsoleCapability(permissions=PERMISSIVE_RULESET),
    ],
)
```

## How Permissions Work

1. **The toolset drops denied tools** — the ruleset is passed straight through to
   `create_console_toolset`, so a denied operation's tools are never registered.

2. **`prepare_tools`** — hides them again from each request's tool definitions.
   With `READONLY_RULESET`, the model never sees `write_file`, `edit_file` or
   `execute`.

3. **`before_tool_execute`** — checks per-path permissions before each tool call.
   If a specific path is denied (e.g., `.env` files), the call is blocked even if
   the operation is generally allowed.

A workspace that is read-only — `ReadOnlyWorkspace`, or `LocalWorkspace(read_only=True)` —
hides `write_file`, `edit_file` and `execute` the same way, whatever the ruleset says: the
workspace would refuse them anyway, and a model offered a tool that can only fail wastes
turns finding that out.

### Answering an "ask"

An operation resolving to `"ask"` needs somebody to ask. Give the capability an
`ask_callback`, or set `ask_fallback="deny"` to refuse instead:

```python
async def approve(operation: str, target: str, reason: str) -> bool:
    return await my_ui.confirm(f"Allow {operation} on {target}?")


capability = ConsoleCapability(permissions=DEFAULT_RULESET, ask_callback=approve)
```

Without either, an "ask" raises `PermissionAskError` — which is what you want in
a batch job and not what you want in an interactive one. Every shipped preset
except `PERMISSIVE_RULESET` has at least one operation defaulting to `"ask"`.

## Constructor Parameters

[`ConsoleCapability`][pydantic_ai_backends.ConsoleCapability] is a dataclass with
these fields:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `include_execute` | `bool` | `True` | Whether to register the `execute` shell tool. |
| `edit_format` | `"str_replace" \| "hashline"` | `"str_replace"` | File-editing format. `"hashline"` registers `hashline_edit` instead of `edit_file` and changes the injected instructions. See [Hashline Edit Format](console-toolset.md#hashline-edit-format). |
| `permissions` | `PermissionRuleset \| None` | `None` | Ruleset controlling which operations are allowed, asked, or denied. When `None`, all tools are exposed and no permission checks run. |
| `ask_callback` | `AskCallback \| None` | `None` | Async `(operation, target, reason) -> bool` answering an operation that resolves to `"ask"`. |
| `ask_fallback` | `"deny" \| "error"` | `"error"` | What an unanswerable `"ask"` does when there is no callback. |
| `image_support` / `document_support` | `bool` | `False` | Return images and PDFs from `read_file` as `BinaryContent` a multimodal model can see. |
| `descriptions`, `profile` | | | Tool text overrides and how much guidance it carries. See [What the Model Reads](console-toolset.md#what-the-model-reads). |

```python
from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import DEFAULT_RULESET

capability = ConsoleCapability(
    include_execute=True,
    edit_format="hashline",
    permissions=DEFAULT_RULESET,
)
```

## Tools Registered

The capability builds a console toolset via
[`create_console_toolset`][pydantic_ai_backends.create_console_toolset] and
exposes it through `get_toolset()`:

- `ls`, `read_file`, `write_file`, `glob`, `grep`
- `edit_file` (when `edit_format="str_replace"`) **or** `hashline_edit` (when `edit_format="hashline"`)
- `execute` (only when `include_execute=True`)

It also injects tool-usage instructions through `get_instructions()`, calling
[`get_console_system_prompt`][pydantic_ai_backends.get_console_system_prompt]
with the configured `edit_format` — so you do not need to add the console system
prompt to your agent manually.

## Where the Tools Work

In `ctx.workspace`, always. The workspace is chosen per run — from `workspace=`, from the
ref on the message history, or from the agent's workspace capabilities — so one agent
serves a different container per conversation without being rebuilt. See
[Choosing a run's workspace](https://pydantic.dev/docs/ai/core-concepts/workspace/#choosing-a-runs-workspace).

**On a read-only workspace** (`ReadOnlyWorkspace`, `LocalWorkspace(..., read_only=True)`)
`write_file`, the edit tool and `execute` are not offered to the model at all. **On a
workspace without commands** (`StateWorkspace`) `glob` and `grep` walk the files instead of
running `find` and `grep`, and `execute` answers with the workspace's refusal.

## Relationship to Other Features

- **Permissions** — the `permissions` ruleset drives both tool hiding
  (`prepare_tools`) and per-path/command checks (`before_tool_execute`). See
  [Permissions](permissions.md).
- **Edit format** — `edit_format` is forwarded to the underlying toolset and
  controls which edit tool is registered and which instructions are injected.
  See [Hashline Edit Format](console-toolset.md#hashline-edit-format).
- **Workspaces** — the environment comes from a workspace capability; see
  [Workspaces](workspaces.md).
