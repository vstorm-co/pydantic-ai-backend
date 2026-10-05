# API Reference

Complete API documentation for pydantic-ai-backend.

## Quick Example

```python
from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, DockerWorkspace

agent = Agent(
    "anthropic:claude-opus-5-5",
    capabilities=[DockerWorkspace(runtime="python-minimal"), ConsoleCapability()],
)

result = agent.run_sync("Create hello.py and run it")
```

## Modules

| Module | Description |
|--------|-------------|
| [Capability](capability.md) | `ConsoleCapability`: the console tools as one capability |
| [Workspaces](workspaces.md) | Docker, sandboxd, Kubernetes, Daytona and state-document workspaces |
| [Permissions](permissions.md) | Rulesets, presets and the permission checker |
| [Docker](docker.md) | `DockerSandbox`, `SessionManager`, built-in runtimes |
| [Kubernetes](kubernetes.md) | `KubernetesPodSandbox` |
| [sandboxd](remote.md) | The sandbox service, its configuration, wire protocol and archive |
| [Toolsets](toolsets.md) | `create_console_toolset` and the tool text |
| [Types](types.md) | Type definitions, `StateBackend` |

## Import Reference

```python
# Console tools (requires the [console] extra)
from pydantic_ai_backends import (
    ConsoleCapability,
    create_console_toolset,
    get_console_system_prompt,
)

# Workspaces (requires the [workspaces] extra, plus the provider's own)
from pydantic_ai_backends import (
    DaytonaWorkspace,
    DockerWorkspace,
    KubernetesWorkspace,
    SandboxdWorkspace,
    StateWorkspace,
)

# Sandboxes the workspaces run on
from pydantic_ai_backends import (
    BUILTIN_RUNTIMES,
    DockerSandbox,
    KubernetesPodSandbox,
    RuntimeConfig,
    SessionManager,
)

# A filesystem kept as a JSON document
from pydantic_ai_backends import StateBackend

# Types
from pydantic_ai_backends import (
    CommandOutcome,
    EditResult,
    ExecuteResponse,
    FileInfo,
    GrepMatch,
    WriteResult,
)
```

## Protocols

### CommandRunner

What a sandbox implements for the container workspaces and `sandboxd` to run commands
in it under the workspace contract.

::: pydantic_ai_backends.protocol.CommandRunner
    options:
      show_root_heading: true
      members:
        - run_command
        - stop_command

### SandboxUnavailableError

::: pydantic_ai_backends.protocol.SandboxUnavailableError
    options:
      show_root_heading: true
