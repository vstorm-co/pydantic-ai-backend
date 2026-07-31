# API Reference

Complete API documentation for pydantic-ai-backend.

## Quick Example

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import LocalBackend, create_console_toolset

@dataclass
class Deps:
    backend: LocalBackend

backend = LocalBackend(root_dir=".")
toolset = create_console_toolset()
agent = Agent("openai:gpt-4o", deps_type=Deps).with_toolset(toolset)

result = agent.run_sync("Create hello.py and run it", deps=Deps(backend=backend))
```

## Modules

| Module | Description |
|--------|-------------|
| [Backends](backends.md) | LocalBackend, StateBackend, CompositeBackend |
| [Docker](docker.md) | DockerSandbox, SessionManager, RuntimeConfig |
| [Daytona](daytona.md) | DaytonaSandbox cloud sandbox |
| [Remote](remote.md) | RemoteSandbox client, sandboxd service, wire protocol |
| [Toolsets](toolsets.md) | Console toolset for pydantic-ai |
| [Types](types.md) | Type definitions |

## Import Reference

```python
# Toolset for pydantic-ai agents (requires [console] extra)
from pydantic_ai_backends import (
    create_console_toolset,
    get_console_system_prompt,
    ConsoleDeps,
)

# Backends
from pydantic_ai_backends import (
    LocalBackend,
    StateBackend,
    CompositeBackend,
)

# Docker (requires [docker] extra)
from pydantic_ai_backends import (
    DockerSandbox,
    SessionManager,
    RuntimeConfig,
    BUILTIN_RUNTIMES,
)

# Types
from pydantic_ai_backends import (
    FileInfo,
    WriteResult,
    EditResult,
    ExecuteResponse,
    GrepMatch,
)

# Protocols
from pydantic_ai_backends import (
    BackendProtocol,
    SandboxProtocol,
)
```

## Protocols

### BackendProtocol

All backends implement this interface.

::: pydantic_ai_backends.protocol.BackendProtocol
    options:
      show_root_heading: true
      members:
        - ls_info
        - read_bytes
        - read
        - write
        - edit
        - glob_info
        - grep_raw

### SandboxProtocol

Extends [`BackendProtocol`][pydantic_ai_backends.protocol.BackendProtocol] with command execution.

::: pydantic_ai_backends.protocol.SandboxProtocol
    options:
      show_root_heading: true
      members:
        - execute
        - id
