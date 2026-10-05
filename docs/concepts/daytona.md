# Daytona

`DaytonaWorkspace` gives an agent a [Daytona](https://www.daytona.io) sandbox as its
[workspace](workspaces.md).

!!! warning "Not checked against a live account in CI"
    The unit tests drive a fake client. `tests/test_workspace_daytona_conformance.py` runs
    Pydantic AI's conformance suite against real sandboxes when `DAYTONA_API_KEY` is set:
    `uv run pytest -m daytona tests/test_workspace_daytona_conformance.py`.

```bash
pip install "pydantic-ai-backend[console,daytona]"
```

The extra installs the `daytona` package, whose module is `daytona`. The older
`daytona-sdk` package installs `daytona_sdk` instead, which is why the previous
`DaytonaSandbox` failed to import with the extra it declared.

## Basic Usage

```python
from daytona import DaytonaConfig
from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, DaytonaWorkspace

sandboxes = DaytonaWorkspace(config=DaytonaConfig(api_key="dtn_..."))
agent = Agent("anthropic:claude-opus-5-5", capabilities=[sandboxes, ConsoleCapability()])
```

Without `config`, the SDK reads `DAYTONA_API_KEY`, `DAYTONA_API_URL` and `DAYTONA_TARGET`.
`create_params` (`CreateSandboxFromSnapshotParams` or `CreateSandboxFromImageParams`)
shapes a new sandbox.

The first operation creates a sandbox and records its id as the run's ref. A later run
attaches to it, starting it when Daytona stopped or archived it; one that is destroyed or
gone fails with `WorkspaceUnavailableError`. `destroy(ref)` deletes it. Daytona's own
auto-stop and auto-delete still apply.

`sandbox_name` names the sandbox instead: a run with no ref creates it under that name, or
attaches to the sandbox that already has it, so an application can key a sandbox on its
own record without storing a ref first. Its ref carries the name. Two clients creating
the same name at once end up in one sandbox. A ref is still attach-only, so a sandbox
deleted meanwhile is `WorkspaceUnavailableError` rather than a new one.

```python
sandboxes = DaytonaWorkspace(config=config, sandbox_name=f"conv-{conversation_id}")
```

## How a command runs

As a synchronous session command, the one Daytona API that reports stdout and stderr apart
along with the exit code. Each command runs as its own `sh`, so nothing a command does to
the session's shell carries into the next one, with stdin at `/dev/null`. A command whose
caller times out or is cancelled is stopped by a second command that signals its process
group. Daytona answers a session command only once it ends, so a timed-out command comes
back with no partial output.

Without `client=`, each operation opens and closes its own `AsyncDaytona`; pass one you own
to share its connection across runs.
