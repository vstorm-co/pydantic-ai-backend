# Pydantic AI Workspaces

Pydantic AI 2.52 added [workspaces](https://pydantic.dev/docs/ai/core-concepts/workspace/):
the environment an agent run works in, which every tool reaches through
`ctx.workspace` without knowing whether it is a directory on the host or a
sandbox somewhere else. The harness's `Coder`, `Shell` and `FileSystem` are
built on it, and so is any tool you write against `ctx.workspace`.

This library supplies workspaces from its own sandboxes, and goes the other way
too:

| | What it is |
|---|---|
| `DockerWorkspace` | A container on this host, built from the same `DockerSandbox` the rest of the library uses |
| `SandboxdWorkspace` | A session on a [`sandboxd`](remote.md) service, so the agent's process holds no Docker socket |
| `ConsoleCapability(use_workspace=True)` | This library's console tools, running in whatever workspace the run has — one of the above, or the harness's E2B, Modal or Sprites |

## Installation

```bash
pip install "pydantic-ai-backend[workspaces,docker]"   # DockerWorkspace
pip install "pydantic-ai-backend[workspaces]"          # SandboxdWorkspace
```

The `workspaces` extra needs Pydantic AI 2.52 or newer.

## A container on this host

```python
from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder

from pydantic_ai_backends.workspaces import DockerWorkspace

agent = Agent("anthropic:claude-opus-5-5", capabilities=[DockerWorkspace(), Coder()])
result = agent.run_sync("Write fizzbuzz.py and run it.")
```

The first operation creates a container named `pydantic-ai-workspace-…` and
records it as the run's `WorkspaceRef`. Pass the message history to the next run
and it attaches to the same container, files and installed packages included; a
stopped container is started again. A container that has been removed is
reported as `WorkspaceUnavailableError` rather than replaced with an empty one —
pass `workspace="new"` to start over on purpose.

`image`, `runtime`, `network_mode`, `mem_limit`, `cpus` and `oci_runtime` mean
what they mean on [`DockerSandbox`](docker.md). The isolation is the container's:
under Docker's default `runc` it shares the host's kernel, which is what
`oci_runtime="runsc"` (gVisor) is for.

## A session on `sandboxd`

```python
from pydantic_ai_backends.workspaces import SandboxdWorkspace

sandbox = SandboxdWorkspace(service_url="http://sandboxd:8080", token=SANDBOXD_TOKEN)
agent = Agent("anthropic:claude-opus-5-5", capabilities=[sandbox, Coder()])
```

Each workspace is one `sandboxd` session. Commands go through the service's
`/run`, which exists for this and differs from the `/exec` the tool-path
`RemoteSandbox` uses:

| | `/exec` (`RemoteSandbox`) | `/run` (`SandboxdWorkspace`) |
|---|---|---|
| Command | A shell string | An argv, or `/bin/sh -c` spelled out |
| Output | One stream | stdout and stderr apart |
| A dead sandbox | `Error: …`, exit 1 | `410`, raised as `WorkspaceUnavailableError` |
| Environment | The container's | The container's, plus `env` per command |
| A caller that stops waiting | The command runs on | `/runs/{run_id}/stop` stops its process group |

Opening a workspace from a ref uses `attach`, which never creates: when the
session is closed and the service no longer holds its workspace, the answer is
`404` and the run fails with `WorkspaceUnavailableError` instead of carrying on
in an empty directory. A session the idle reaper closed still attaches, for as
long as the service keeps its files (`workspace_ttl`).

**Every command is bounded by the service's `execute_timeout`**, including one
that asks for no timeout. The service enforces its ceiling on every caller, and a
workspace cannot opt out of it.

### More than one service

A ref is a provider and an id, and an id means something only on the service
that issued it. When an agent can reach several services, give each capability
its own `provider`, so a ref from one is never offered to another:

```python
capabilities = [
    SandboxdWorkspace(service_url=EU_URL, token=EU_TOKEN, provider="sandboxd:eu"),
    SandboxdWorkspace(service_url=US_URL, token=US_TOKEN, provider="sandboxd:us"),
]
```

### Sharing an HTTP client

Without `client=` each request opens a connection of its own. A host making many
runs can pass an `httpx.AsyncClient` it owns and closes itself.

## The console tools in any workspace

`ConsoleCapability(use_workspace=True)` points `ls`, `read_file`, `write_file`,
`edit_file`, `glob`, `grep` and `execute` at `ctx.workspace` instead of a backend
of their own — the same tools, text and permission rules, in whichever
environment the run was given:

```python
from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.workspaces import SandboxdWorkspace

agent = Agent(
    "anthropic:claude-opus-5-5",
    capabilities=[
        SandboxdWorkspace(service_url=URL, token=TOKEN),
        ConsoleCapability(use_workspace=True, image_support=True),
    ],
)
```

File operations go through the workspace's own file methods, so a policy wrapped
around the workspace — `ReadOnlyWorkspace`, or a `WrapperWorkspace` of your own —
applies to every one of them. `glob` and `grep` run `find` and `grep` in the
workspace, so they need one that runs commands. A workspace has no background
shells, so those tools answer that they are unsupported; pass
`include_background=False` to leave them out.

`WorkspaceSandbox` is the adapter underneath, for code that wants this library's
`AsyncSandboxProtocol` over any `Workspace`.

## Removing a workspace

Pydantic AI never deletes an environment, and neither does anything here: a
workspace lives until whoever holds its ref removes it.

```python
ref = result.workspace.ref
await sandbox.destroy(ref)  # removes the session and its files; gone already is fine
```

A failed run returns no result, so delete its environment in an `on_run_error`
hook, where `ctx.workspace.ref` names it.

## What was checked

Both backends pass Pydantic AI's own `WorkspaceBackendSuite`, the same rules its
built-in and provider backends pass, against a real Docker daemon:
`tests/test_workspace_docker_conformance.py` and
`tests/test_workspace_sandboxd_conformance.py` (marked `docker`). The `sandboxd`
client also passes it in ordinary CI, over real HTTP, with commands running
through Pydantic AI's local backend in place of a container.

Known limits, stated rather than implied:

- **File operations go through the shell.** Neither backend implements
  `SupportsFilesystem`, so `Workspace` derives reads and writes from commands —
  one round trip each, and chunked for large files. Correct, and slower than a
  native file API would be.
- **A backgrounded child delays a finished command by about two seconds.**
  Docker closes an exec's streams that long after the command exits when a child
  still holds them. Redirect a background job's output to a file.
- **`realpath` comes from the shell fallback.** It follows links with `readlink`
  in one command; there is no native implementation.
- **Durable execution rebuilds the backend from its ref on every call**, so each
  workspace call under Temporal, DBOS or Prefect on `sandboxd` pays an attach
  request first.
