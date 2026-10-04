<p align="center">
  <img src="assets/social-preview.png" alt="Pydantic AI Backend" width="100%">
</p>

<h1 align="center">Pydantic AI Backend</h1>

<p align="center"><em>Sandboxed execution & file tools for agents.</em></p>

<p align="center">
  <a href="https://pypi.org/project/pydantic-ai-backend/"><img src="https://img.shields.io/pypi/v/pydantic-ai-backend.svg" alt="PyPI version"></a>
  <a href="https://pepy.tech/projects/pydantic-ai-backend"><img src="https://static.pepy.tech/badge/pydantic-ai-backend/month" alt="PyPI Downloads"></a>
  <a href="https://github.com/vstorm-co/pydantic-ai-backend/stargazers"><img src="https://img.shields.io/github/stars/vstorm-co/pydantic-ai-backend?style=flat&logo=github&color=yellow" alt="GitHub Stars"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://coveralls.io/github/vstorm-co/pydantic-ai-backend?branch=main"><img src="https://coveralls.io/repos/github/vstorm-co/pydantic-ai-backend/badge.svg?branch=main" alt="Coverage Status"></a>
  <a href="https://github.com/vstorm-co/pydantic-ai-backend/actions/workflows/ci.yml"><img src="https://github.com/vstorm-co/pydantic-ai-backend/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/pydantic/pydantic-ai"><img src="https://img.shields.io/badge/Powered%20by-Pydantic%20AI-E92063?logo=pydantic&logoColor=white" alt="Pydantic AI"></a>
</p>

---

!!! tip "Part of Pydantic Deep Agents"
    **Pydantic AI Backend** is one library in [Pydantic Deep Agents](https://github.com/vstorm-co/pydantic-deepagents) — the open-source
    Claude Code alternative & Python agent framework. Use it standalone, or get every
    library wired together in a single `create_deep_agent()` call.

**pydantic-ai-backend** gives [pydantic-ai](https://ai.pydantic.dev/) agents somewhere to
work and the tools to work there: [workspaces](concepts/workspaces.md) Pydantic AI does not
ship — a Docker container, a `sandboxd` session, a Kubernetes pod, a Daytona sandbox, a JSON
document — and file and shell tools that run in whichever workspace the run has.

<div class="grid cards" markdown>

- :material-docker: **Self-hosted sandboxes**

    A container on this host, or behind `sandboxd` so your app never holds the Docker socket

- :material-console: **Console tools**

    ls, read, write, edit, glob, grep, execute — in any Pydantic AI workspace

- :material-shield-lock: **Permission system**

    Per-path and per-command rules, approvals and presets

- :material-check-decagram: **Checked against the contract**

    Pydantic AI's own workspace conformance suite, on a real Docker daemon

</div>

## Quick Start

```python
from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, DockerWorkspace

agent = Agent(
    "anthropic:claude-opus-5-5",
    capabilities=[DockerWorkspace(image="python:3.12-slim"), ConsoleCapability()],
)
result = agent.run_sync("Write fizzbuzz.py and run it.")
```

The container is created on the first tool call and kept after the run; pass the message
history to the next run and it works in the same container.

### With Permissions

```python
from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import READONLY_RULESET

# Read-only agent — write/edit/execute tools hidden from the model
capability = ConsoleCapability(permissions=READONLY_RULESET)
```

## Choose Your Workspace

Same tools, different environments:

=== "Local Development"

    ```python
    from pydantic_ai.capabilities import LocalWorkspace

    workspace = LocalWorkspace("./workspace")  # Pydantic AI's own; isolates nothing
    ```

=== "Testing"

    ```python
    from pydantic_ai_backends import StateWorkspace

    workspace = StateWorkspace()  # a JSON document, no side effects
    ```

=== "Production (Docker)"

    ```python
    from pydantic_ai_backends import DockerWorkspace

    workspace = DockerWorkspace(runtime="python-datascience")
    ```

=== "Containerised App"

    ```python
    from pydantic_ai_backends import SandboxdWorkspace

    workspace = SandboxdWorkspace(service_url="http://sandboxd:8080", token="...")
    ```

## Available Tools

| Tool | Description |
|------|-------------|
| `ls` | List files in a directory |
| `read_file` | Read file content with line numbers, or an image or PDF the model can see |
| `write_file` | Create or overwrite a file |
| `edit_file` | Replace strings in a file |
| `glob` | Find files matching a pattern |
| `grep` | Search for patterns in files |
| `execute` | Run shell commands (optional) |

## Workspace Comparison

| Workspace | Kept between runs | Commands | Best For |
|---------|-------------|-----------|----------|
| `StateWorkspace` | In your store | No | Tests, files kept in a database |
| `DockerWorkspace` | Until destroyed | Yes | Safe execution on one host |
| `SandboxdWorkspace` | Until destroyed, or the service's TTL | Yes | Containerised apps, many users |
| `KubernetesWorkspace` | Until destroyed | Yes | Cluster-scheduled sandboxes |
| `DaytonaWorkspace` | Until destroyed, or Daytona's auto-delete | Yes | Hosted sandboxes |

## Related Projects

| Package | Description |
|---------|-------------|
| [Pydantic Deep Agents](https://github.com/vstorm-co/pydantic-deepagents) | Full agent framework (uses this library) |
| [pydantic-ai-todo](https://github.com/vstorm-co/pydantic-ai-todo) | Task planning toolset |
| [subagents-pydantic-ai](https://github.com/vstorm-co/subagents-pydantic-ai) | Multi-agent orchestration |
| [summarization-pydantic-ai](https://github.com/vstorm-co/summarization-pydantic-ai) | Context management |
| [pydantic-ai](https://github.com/pydantic/pydantic-ai) | The foundation — agent framework by Pydantic |

## Next Steps

<div class="grid cards" markdown>

- :material-download: **[Installation](installation.md)**

    Get started with pip or uv

- :material-book-open-variant: **[Concepts](concepts/index.md)**

    Learn about backends and toolsets

- :material-code-tags: **[Examples](examples/index.md)**

    See real-world usage patterns

- :material-api: **[API Reference](api/index.md)**

    Full API documentation

</div>
