"""Pydantic AI workspaces from self-hosted and hosted sandboxes.

A workspace is the environment an agent run works in, reached by every tool
through `ctx.workspace`. Each capability here supplies one, creates it on the
run's first operation, records it as the run's `WorkspaceRef` so a later run
attaches to the same environment, and never deletes it: `destroy(ref)` does.

| Capability | Environment | Extra |
|---|---|---|
| `DockerWorkspace` | A container on this host | `docker` |
| `SandboxdWorkspace` | A `sandboxd` session, so the agent's process holds no Docker socket | — |
| `KubernetesWorkspace` | A pod, reached through `pods/exec` | `kubernetes` |
| `DaytonaWorkspace` | A Daytona sandbox | `daytona` |
| `StateWorkspace` | A JSON document, files only | — |

Compose one with tools that use the workspace: this library's
`ConsoleCapability`, or the harness's `Coder`, `Shell` and `FileSystem`.
"""

from pydantic_ai_backends.workspaces._daytona import DaytonaWorkspace, DaytonaWorkspaceBackend
from pydantic_ai_backends.workspaces._docker import DockerWorkspace, DockerWorkspaceBackend
from pydantic_ai_backends.workspaces._kubernetes import (
    KubernetesWorkspace,
    KubernetesWorkspaceBackend,
)
from pydantic_ai_backends.workspaces._sandboxd import SandboxdWorkspace, SandboxdWorkspaceBackend
from pydantic_ai_backends.workspaces._state import StateWorkspace, StateWorkspaceBackend

__all__ = [
    "DaytonaWorkspace",
    "DaytonaWorkspaceBackend",
    "DockerWorkspace",
    "DockerWorkspaceBackend",
    "KubernetesWorkspace",
    "KubernetesWorkspaceBackend",
    "SandboxdWorkspace",
    "SandboxdWorkspaceBackend",
    "StateWorkspace",
    "StateWorkspaceBackend",
]
