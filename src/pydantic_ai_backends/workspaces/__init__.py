"""Pydantic AI workspaces backed by this library's sandboxes.

A workspace is the environment an agent run works in, reached by every tool
through `ctx.workspace`. The capabilities here supply one: hand an agent
`DockerWorkspace()` and the Pydantic AI harness's `Coder`, `Shell` and
`FileSystem` run in a container instead of on the host.

The other direction is here too: `WorkspaceSandbox` puts any workspace behind
this library's sandbox protocol, which is how `ConsoleCapability(use_workspace=True)`
runs its tools in whatever workspace the run has.

Requires `pip install "pydantic-ai-backend[workspaces]"`.
"""

from pydantic_ai_backends.workspaces._console import WorkspaceSandbox
from pydantic_ai_backends.workspaces._docker import DockerWorkspace, DockerWorkspaceBackend
from pydantic_ai_backends.workspaces._sandboxd import SandboxdWorkspace, SandboxdWorkspaceBackend

__all__ = [
    "DockerWorkspace",
    "DockerWorkspaceBackend",
    "SandboxdWorkspace",
    "SandboxdWorkspaceBackend",
    "WorkspaceSandbox",
]
