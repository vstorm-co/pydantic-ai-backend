# ConsoleCapability

`ConsoleCapability` is the recommended way to add filesystem tools to a Pydantic AI agent.
It's a [pydantic-ai capability](https://ai.pydantic.dev/capabilities/) that bundles console
tools, instructions, and permission enforcement.

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
from pydantic_ai_backends import ConsoleCapability

agent = Agent("openai:gpt-4.1", capabilities=[ConsoleCapability()])
```

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
   With `READONLY_RULESET`, the model never sees `write_file`, `edit_file`,
   `execute`, or any of the background shell tools.

3. **`before_tool_execute`** — checks per-path permissions before each tool call.
   If a specific path is denied (e.g., `.env` files), the call is blocked even if
   the operation is generally allowed.

A denied `execute` removes **every** shell tool — `execute`, `run_in_background`,
`read_output`, `kill_shell` and `list_shells` — because they are one operation
reached different ways. Leaving the background ones behind meant a read-only
agent could still run arbitrary commands.

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
| `backend` | `BackendProtocol \| AsyncBackendProtocol \| None` | `None` | Backend the tools operate on. When `None`, each call reads `ctx.deps.backend`. See [Where the Backend Comes From](#where-the-backend-comes-from). |
| `include_execute` | `bool` | `True` | Whether to register the `execute` shell tool. |
| `include_background` | `bool` | `True` | Whether to register the background-shell tools (`run_in_background`, `read_output`, `kill_shell`, `list_shells`). |
| `edit_format` | `"str_replace" \| "hashline"` | `"str_replace"` | File-editing format. `"hashline"` registers `hashline_edit` instead of `edit_file` and changes the injected instructions. See [Hashline Edit Format](console-toolset.md#hashline-edit-format). |
| `permissions` | `PermissionRuleset \| None` | `None` | Ruleset controlling which operations are allowed, asked, or denied. When `None`, all tools are exposed and no permission checks run. |
| `ask_callback` | `AskCallback \| None` | `None` | Async `(operation, target, reason) -> bool` answering an operation that resolves to `"ask"`. |
| `ask_fallback` | `"deny" \| "error"` | `"error"` | What an unanswerable `"ask"` does when there is no callback. |

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

## Where the Backend Comes From

Two ways, and the choice is about who owns the deps type.

### From the agent's deps (default)

With no `backend`, the console tools read `ctx.deps.backend` at runtime, so your
dependencies object must satisfy the
[`ConsoleDeps`](console-toolset.md#consoledeps-protocol) protocol (any class
exposing a `backend` attribute). One agent then serves a different backend per
run — a different
[`DockerSandbox`][pydantic_ai_backends.backends.docker.sandbox.DockerSandbox]
per user, say — without rebuilding the agent.

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import ConsoleCapability, LocalBackend


@dataclass
class Deps:
    backend: LocalBackend


agent = Agent("openai:gpt-4.1", deps_type=Deps, capabilities=[ConsoleCapability()])

result = agent.run_sync(
    "List the Python files",
    deps=Deps(backend=LocalBackend(root_dir="/workspace")),
)
```

### A capability-owned backend

Pass `backend=` and the capability carries it, leaving your deps type alone. Use
this when the deps type is not yours to change — a platform assembling agents
from configuration will have its own — or when this agent is meant to hold one
particular sandbox.

```python
from pydantic_ai import Agent
from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.remote import RemoteSandbox

sandbox = RemoteSandbox("http://sandboxd:8080", token=token, session_id=session_id)
sandbox.start()

# Deps can be anything, including None — the tools never look at them.
agent = Agent("openai:gpt-4.1", capabilities=[ConsoleCapability(backend=sandbox)])
```

The same applies to
[`create_console_toolset(backend=...)`][pydantic_ai_backends.create_console_toolset]
if you are using the toolset directly.

## Relationship to Other Features

- **Permissions** — the `permissions` ruleset drives both tool hiding
  (`prepare_tools`) and per-path/command checks (`before_tool_execute`). See
  [Permissions](permissions.md).
- **Edit format** — `edit_format` is forwarded to the underlying toolset and
  controls which edit tool is registered and which instructions are injected.
  See [Hashline Edit Format](console-toolset.md#hashline-edit-format).
- **Image support** — `image_support` is a `create_console_toolset` option, not
  a `ConsoleCapability` field. If you need multimodal image reading, build the
  toolset directly with
  [`create_console_toolset(image_support=True)`][pydantic_ai_backends.create_console_toolset]
  instead of using the capability.
