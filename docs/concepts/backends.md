# Backends

Backends provide file storage for your pydantic-ai agents. All backends implement `BackendProtocol`, so you can swap them without changing your agent code.

## Quick Comparison

| Backend | Persistence | Execution | Best For |
|---------|-------------|-----------|----------|
| `LocalBackend` | Persistent | Yes | CLI tools, local development |
| `StateBackend` | Ephemeral | No | Unit testing, mocking |
| `DockerSandbox` | Ephemeral* | Yes | Safe execution, multi-user |
| `DaytonaSandbox` | Ephemeral | Yes | Cloud deployments, CI/CD, multi-user |
| `CompositeBackend` | Mixed | Depends | Route by path prefix |

## LocalBackend

Local filesystem with optional shell execution. Use for CLI tools and local development.

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import LocalBackend, create_console_toolset


@dataclass
class Deps:
    backend: LocalBackend


# Backend for local development
backend = LocalBackend(root_dir="./workspace")

# Create agent with file tools
toolset = create_console_toolset()
agent = Agent("openai:gpt-4o", deps_type=Deps).with_toolset(toolset)

# Agent can now work with local files
result = agent.run_sync(
    "Create a todo.py CLI app and test it",
    deps=Deps(backend=backend),
)
```

### Security Options

```python
# Restrict to specific directories
backend = LocalBackend(
    allowed_directories=["/home/user/project", "/home/user/data"],
    enable_execute=True,
)

# Read-only mode (no shell execution)
backend = LocalBackend(
    root_dir="/workspace",
    enable_execute=False,
)

# Corresponding toolset without execute
toolset = create_console_toolset(include_execute=False)
```

### Permission System

For fine-grained access control, use the permission system:

```python
from pydantic_ai_backends import LocalBackend
from pydantic_ai_backends.permissions import (
    DEFAULT_RULESET,
    READONLY_RULESET,
    PermissionRuleset,
    OperationPermissions,
    PermissionRule,
)

# Use pre-configured presets
backend = LocalBackend(root_dir="/workspace", permissions=DEFAULT_RULESET)

# Read-only permissions
backend = LocalBackend(root_dir="/workspace", permissions=READONLY_RULESET)

# Custom permissions
custom = PermissionRuleset(
    read=OperationPermissions(
        default="allow",
        rules=[
            PermissionRule(pattern="**/.env*", action="deny"),
        ],
    ),
    write=OperationPermissions(default="ask"),
    execute=OperationPermissions(
        default="deny",
        rules=[
            PermissionRule(pattern="git *", action="allow"),
            PermissionRule(pattern="python *", action="allow"),
        ],
    ),
)
backend = LocalBackend(root_dir="/workspace", permissions=custom)
```

See [Permissions](permissions.md) for full documentation.

### Features

- ✅ Python-native file operations (cross-platform)
- ✅ Optional shell execution via subprocess
- ✅ Directory restrictions with `allowed_directories`
- ✅ Fast grep using ripgrep (with Python fallback)
- ❌ No isolation - runs with your permissions

## StateBackend

In-memory storage - perfect for testing your pydantic-ai agents.

```python
import pytest
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai_backends import StateBackend, create_console_toolset


@dataclass
class Deps:
    backend: StateBackend


def test_agent_creates_file():
    """Test that agent can create files."""
    backend = StateBackend()
    toolset = create_console_toolset(include_execute=False)

    # Use TestModel for deterministic testing
    agent = Agent(TestModel(), deps_type=Deps).with_toolset(toolset)

    # Pre-populate files if needed
    backend.write("/data/input.txt", "test data")

    # Run agent
    result = agent.run_sync("Read input.txt", deps=Deps(backend=backend))

    # Verify files
    assert "/data/input.txt" in backend.files
```

### Features

- ✅ Fast - no disk I/O
- ✅ Isolated - no side effects
- ✅ Perfect for unit testing
- ✅ Access files via `backend.files`
- ❌ Data lost when process ends
- ❌ No command execution

## CompositeBackend

Route operations to different backends based on path prefix.

!!! warning "LocalBackend does not work in routes"
    `CompositeBackend` passes the **full virtual path** to whichever backend is
    selected. `LocalBackend` validates paths against its `root_dir` and will reject
    any virtual path that is not a real filesystem path within that directory.

    Use `LocalBackend` as the **default** backend, not inside `routes`.
    `StateBackend` and sandbox backends (`DockerSandbox`, `DaytonaSandbox`) accept
    virtual paths and are the right choice for route entries.

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import (
    CompositeBackend,
    StateBackend,
    LocalBackend,
    create_console_toolset,
)


@dataclass
class Deps:
    backend: CompositeBackend


# LocalBackend as default (real filesystem), StateBackend for ephemeral space
backend = CompositeBackend(
    default=LocalBackend(root_dir="/home/user/project"),
    routes={
        "/scratch/": StateBackend(),  # Ephemeral virtual space
    },
)

toolset = create_console_toolset()
agent = Agent("openai:gpt-4o", deps_type=Deps).with_toolset(toolset)

# Agent reads/writes real files via LocalBackend (paths relative to root_dir)
# Agent uses /scratch/ for temporary in-memory storage
result = agent.run_sync(
    "Analyse the project and write a summary to /scratch/summary.md",
    deps=Deps(backend=backend),
)
```

### Path Prefix Matching

CompositeBackend matches paths using **longest prefix first**. Routes are sorted by length at initialization, so more specific prefixes take priority over shorter ones:

```python
from pydantic_ai_backends import CompositeBackend, StateBackend, DockerSandbox

backend = CompositeBackend(
    default=StateBackend(),
    routes={
        "/sandbox/": DockerSandbox(runtime="python-minimal"),
        "/sandbox/data/": StateBackend(),  # Longer prefix wins
    },
)

# Matches "/sandbox/data/" (longer prefix wins)
backend.read("/sandbox/data/input.csv")

# Matches "/sandbox/"
backend.write("/sandbox/script.py", "print('hello')")

# No prefix matches - falls back to default StateBackend
backend.write("/temp/scratch.txt", "temporary data")
```

Paths are matched with exact-or-child semantics: `/foo` matches route `/foo/` and
`/foo/bar` matches route `/foo/`, but `/foobar` does not. Trailing slashes on route
keys are optional and normalised internally.

### Aggregated Operations

When you call `ls`, `glob`, or `grep` at the root level (`/` or `""`), CompositeBackend aggregates results from **all** backends:

```python
from pydantic_ai_backends import CompositeBackend, StateBackend

backend = CompositeBackend(
    default=StateBackend(),
    routes={
        "/cache/": StateBackend(),
        "/output/": StateBackend(),
    },
)

# ls at root shows virtual directories for each route prefix
# plus any files in the default backend
entries = backend.ls_info("/")
# Returns: [/cache, /output, ...any default backend entries...]

# glob from root searches ALL backends
matches = backend.glob_info("**/*.py", "/")

# grep from root searches ALL backends
results = backend.grep_raw("TODO", "/")
```

When targeting a specific path, only the matching backend is queried:

```python
# Only searches the /output/ backend
results = backend.grep_raw("error", "/output/")

# Only searches the /cache/ backend
entries = backend.glob_info("*.json", "/cache/")
```

!!! note "Error handling in aggregated operations"
    During aggregated `grep` operations, errors from individual backends are silently
    ignored. This prevents a failing backend from blocking results from other backends.

### Common Patterns

#### Real filesystem + ephemeral scratch space

```python
backend = CompositeBackend(
    default=LocalBackend(root_dir="/home/user/my-app"),  # Persistent real files
    routes={
        "/scratch/": StateBackend(),  # Ephemeral in-memory space
    },
)

# Agent reads and writes real project files via LocalBackend
# Agent uses /scratch/ for intermediate or throwaway data
```

#### Real filesystem + isolated sandbox execution

```python
from pydantic_ai_backends import CompositeBackend, LocalBackend, DockerSandbox

backend = CompositeBackend(
    default=LocalBackend(root_dir="/home/user/project"),
    routes={
        "/sandbox/": DockerSandbox(runtime="python-datascience"),
    },
)

# Read/write real project files by default
# Run untrusted code safely inside /sandbox/
```

#### Multiple ephemeral namespaces

```python
backend = CompositeBackend(
    default=StateBackend(),
    routes={
        "/plans/": StateBackend(),
        "/output/": StateBackend(),
    },
)

# Logically separate namespaces within a fully in-memory backend
# Useful for testing or agents that need internal partitioning
```

### Use Cases

- Real project files + ephemeral scratch space
- Real filesystem + isolated Docker/Daytona execution
- Logical namespace partitioning with `StateBackend`

## Backend Protocol

All backends implement the
[`BackendProtocol`][pydantic_ai_backends.protocol.BackendProtocol] interface:
`ls_info`, `read_bytes`, `read`, `write`, `edit`, `glob_info`, and `grep_raw`.

### Execute (sandbox backends)

Sandbox backends such as [`DockerSandbox`][pydantic_ai_backends.backends.docker.sandbox.DockerSandbox]
and [`DaytonaSandbox`][pydantic_ai_backends.backends.daytona.DaytonaSandbox]
additionally implement
[`SandboxProtocol`][pydantic_ai_backends.protocol.SandboxProtocol], which extends
`BackendProtocol` with an `execute(command, timeout)` method and an `id` property.

See the [API Reference](../api/index.md#protocols) for the full rendered
protocol signatures.

## Path Security

All backends validate paths to prevent directory traversal:

```python
# These will fail:
backend.read("../etc/passwd")  # Parent directory
backend.read("~/secrets")  # Home expansion
backend.read("C:\\Windows\\...")  # Windows paths
```

## Next Steps

- [Permissions](permissions.md) - Fine-grained access control
- [Docker Sandbox](docker.md) - Local isolated execution
- [Daytona Sandbox](daytona.md) - Cloud isolated execution
- [Console Toolset](console-toolset.md) - Ready-to-use tools
- [API Reference](../api/backends.md) - Complete API
