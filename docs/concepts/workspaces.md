# Workspaces

Pydantic AI 2.52 added [workspaces](https://pydantic.dev/docs/ai/core-concepts/workspace/):
the environment an agent run works in, which every tool reaches through `ctx.workspace`
without knowing whether it is a directory on the host or a sandbox somewhere else. The
harness's `Coder`, `Shell` and `FileSystem` are built on it, and so is this library's
[`ConsoleCapability`](capability.md).

This library supplies the workspaces Pydantic AI does not ship:

| Capability | Environment | Extra | Checked against the real thing |
|---|---|---|---|
| `DockerWorkspace` | A container on this host | `docker` | Yes — conformance suite on a real daemon |
| `SandboxdWorkspace` | A [`sandboxd`](remote.md) session, so the agent's process holds no Docker socket | — | Yes — real daemon, and real HTTP in CI |
| `KubernetesWorkspace` | A pod, through `pods/exec` | `kubernetes` | No — a fake API in CI; a cluster suite behind `-m kubernetes` |
| `DaytonaWorkspace` | A Daytona sandbox | `daytona` | No — a fake client in CI; a live suite behind `-m daytona` |
| `StateWorkspace` | A JSON document, files only | — | Yes — the conformance suite, in CI |

Pydantic AI's own `LocalWorkspace` covers a directory on the host, and the harness covers
E2B, Modal and Sprites. `ConsoleCapability` works in any of them.

## How a workspace is used

```python
from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, SandboxdWorkspace

sandbox = SandboxdWorkspace(service_url="http://sandboxd:8080", token=SANDBOXD_TOKEN)
agent = Agent("anthropic:claude-opus-5-5", capabilities=[sandbox, ConsoleCapability()])

first = await agent.run("Clone the repo and run its tests.")
# Same environment: the ref travels on the message history.
second = await agent.run("Fix the failing test.", message_history=first.all_messages())
```

Every capability here follows the same rules:

- **Created on first use.** Nothing starts until a tool touches the workspace; the run then
  records the environment as a `WorkspaceRef` on its responses.
- **Attached by ref.** A later run with that history — or `workspace=ref` — attaches to the
  same environment. One whose environment is gone fails with `WorkspaceUnavailableError`
  rather than carrying on in an empty one; pass `workspace="new"` to start over on purpose.
- **Never deleted for you.** An environment lives until whoever holds its ref removes it:
  `await capability.destroy(ref)`. A failed run returns no result, so delete its environment
  in an `on_run_error` hook, where `ctx.workspace.ref` names it.
- **Refs are per provider.** A capability attaches only to refs carrying its provider name.
  Give each service or cluster its own `provider` when an agent can reach several, so a ref
  from one is never offered to another.

## Commands and their failures

Every command-capable workspace here runs commands the same way. A small `sh` wrapper
records the command's process-group leader, so a caller that times out or is cancelled can
stop the command *and everything it started* with a second command. stdout and stderr come
back apart, `env` is layered per command, a command that does not exist exits 127, and the
failures the contract names are raised as such: `WorkspaceTimeoutError` (with partial
output where the provider streams it), `WorkspaceOutputLimitError` past 10 MiB, and
`WorkspaceUnavailableError` for an environment that is gone — including one destroyed while
a command was running.

None of these backends implements `SupportsFilesystem`: `Workspace` derives file
operations through the shell, one round trip each, chunked for large files. Correct, and
slower than a native file API would be.

## sandboxd

```python
sandbox = SandboxdWorkspace(service_url="http://sandboxd:8080", token=SANDBOXD_TOKEN)
```

Each workspace is one `sandboxd` session, run through the service's `/run`. Opening a
workspace from a ref uses `attach`, which never creates: a session that is closed and whose
workspace the service no longer holds answers `404`. A session the idle reaper closed still
attaches for as long as the service keeps its files (`workspace_ttl`).

**Every command is bounded by the service's `execute_timeout`**, including one that asks for
no timeout. A timeout past that ceiling reports the sandbox's limit rather than the number
asked for. Without `client=` each request opens a connection; a host making many runs passes
an `httpx.AsyncClient` it owns.

## State documents

```python
from pydantic_ai_backends import StateBackend, StateWorkspace

workspace = StateWorkspace()  # an in-process store
```

A `StateBackend` is a filesystem kept as a JSON document — `files` and the `directories`
made empty — so a host can keep a workspace in a database row. `StateWorkspace` serves such
documents by id from `store`, a mapping the application owns: fill it with documents loaded
for the run, and save `files` and `sorted(directories)` afterwards. There is nothing to run a
command in, so `execute` answers with the workspace's refusal, and `glob` and `grep` walk
the files instead.

## The console tools in any workspace

`ConsoleCapability` points `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep` and
`execute` at `ctx.workspace` — the same tools, text and permission rules in whichever
environment the run was given. File operations go through the workspace's own methods, so a
policy wrapped around it — `ReadOnlyWorkspace`, or a `WrapperWorkspace` of your own —
applies to every one of them, and on a read-only workspace the write and execute tools are
not offered at all.

## What was checked

`DockerWorkspace` and `SandboxdWorkspace` pass Pydantic AI's own `WorkspaceBackendSuite`
against a real Docker daemon (`tests/test_workspace_*_conformance.py`, marked `docker`); the
`sandboxd` client also passes it in ordinary CI, over real HTTP, with commands run by
Pydantic AI's local backend. `StateWorkspace` passes every filesystem rule in CI. The
Kubernetes and Daytona suites exist and are skipped without a cluster or an API key.

Known limits, stated rather than implied:

- **A backgrounded child delays a finished Docker command by about two seconds.** Docker
  closes an exec's streams that long after the command exits when a child still holds them.
- **`realpath` comes from the shell fallback.** It follows links with `readlink` in one
  command; there is no native implementation.
- **Durable execution rebuilds the backend from its ref on every call**, so each workspace
  call under Temporal, DBOS or Prefect pays an attach first.
