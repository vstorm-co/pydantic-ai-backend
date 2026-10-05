# Core Concepts

**pydantic-ai-backend** gives [pydantic-ai](https://ai.pydantic.dev/) agents somewhere to
work and the tools to work there. Two halves, joined by Pydantic AI's `ctx.workspace`:

![pydantic-ai-backend by layer: the agent's tools call ctx.workspace, Pydantic AI's contract, and a workspace from this library or from Pydantic AI answers it](../assets/architecture.png)

## 1. Workspaces

A workspace is the environment a run works in. A workspace capability supplies one, creates
it on first use, and records it as the run's ref so the next run comes back to it:

```python
from pydantic_ai_backends import (
    DaytonaWorkspace,
    DockerWorkspace,
    KubernetesWorkspace,
    SandboxdWorkspace,
    StateWorkspace,
)

DockerWorkspace(runtime="python-datascience")  # a container on this host
SandboxdWorkspace(service_url="http://sandboxd:8080", token="...")  # behind a service
KubernetesWorkspace(image="python:3.12-slim", namespace="agents")  # a pod
DaytonaWorkspace()  # a Daytona sandbox
StateWorkspace()  # a JSON document, files only
```

[Learn more about Workspaces →](workspaces.md)

## 2. Console tools

`ConsoleCapability` gives the model `ls`, `read_file`, `write_file`, `edit_file`, `glob`,
`grep` and `execute`, working in whichever workspace the run has:

```python
from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, DockerWorkspace

agent = Agent(
    "anthropic:claude-opus-5-5",
    capabilities=[DockerWorkspace(runtime="python-datascience"), ConsoleCapability()],
)
result = agent.run_sync("Create a Python script that calculates pi and run it")
```

[Learn more about the Capability →](capability.md) ·
[What the tools say and do →](console-toolset.md)

## 3. Permissions

Fine-grained access control for file operations and shell commands, enforced by the tools
on every call:

```python
from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import DEFAULT_RULESET, READONLY_RULESET

ConsoleCapability(permissions=DEFAULT_RULESET)  # reads allowed, writes and commands ask
ConsoleCapability(permissions=READONLY_RULESET)  # nothing may change or run
```

Available presets: `DEFAULT_RULESET`, `PERMISSIVE_RULESET`, `READONLY_RULESET`, `STRICT_RULESET`

[Learn more about Permissions →](permissions.md)

## Choosing a Workspace

| Use Case | Workspace |
|----------|-----------|
| CLI tools, your own trusted work | Pydantic AI's `LocalWorkspace` |
| Tests, or files kept in your database | `StateWorkspace` |
| Model-written code on one host | `DockerWorkspace` |
| An app that runs in a container, many users | [`SandboxdWorkspace`](remote.md) |
| Sandboxes scheduled by a cluster | [`KubernetesWorkspace`](kubernetes.md) |
| Hosted sandboxes | [`DaytonaWorkspace`](daytona.md), or the harness's E2B, Modal, Sprites |
