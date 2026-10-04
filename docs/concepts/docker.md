# Docker

`DockerWorkspace` gives an agent a Docker container on this host as its
[workspace](workspaces.md): every command and file operation of the run happens in the
container, and a later run attaches to the same one by its ref.

!!! warning "Requires Docker"
    ```bash
    pip install "pydantic-ai-backend[console,docker]"
    ```
    Ensure Docker is installed and the daemon is running. For an application that must
    not hold the Docker socket itself, run [`sandboxd`](remote.md) and use
    `SandboxdWorkspace` instead.

## Basic Usage

```python
from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, DockerWorkspace

workspace = DockerWorkspace(runtime="python-datascience")
agent = Agent("anthropic:claude-opus-5-5", capabilities=[workspace, ConsoleCapability()])

result = agent.run_sync(
    "Load the iris dataset with sklearn, analyze it with pandas, "
    "and create a visualization with matplotlib"
)
print(result.output)

# The container outlives the run; remove it when the conversation is over.
await workspace.destroy(result.workspace.ref)
```

The first operation creates a container named `pydantic-ai-workspace-…`. Pass the message
history to the next run and it attaches to the same container — files, installed packages
and all; a stopped one is started again, and one that was removed fails with
`WorkspaceUnavailableError` rather than continuing in an empty container.

## Runtime Configurations

Pre-configured environments with packages pre-installed:

```python
from pydantic_ai_backends import DockerWorkspace, RuntimeConfig

# Use built-in runtime
workspace = DockerWorkspace(runtime="python-datascience")

# Or define custom runtime for your use case
runtime = RuntimeConfig(
    name="ml-env",
    base_image="python:3.12-slim",
    packages=["torch", "transformers", "pandas"],
)
workspace = DockerWorkspace(runtime=runtime)
```

### Built-in Runtimes

| Runtime | Image | What it adds |
|---|---|---|
| `coding` | built on python:3.12-slim | git, ripgrep, fd, jq, less, procps, uv |
| `polyglot` | built on python:3.12-slim | Python and Node together, curl, git, numpy, duckdb, polars, httpx |
| `python-minimal` | python:3.12-slim | standard library only |
| `python-datascience` | built on python:3.12-slim | pandas, numpy, matplotlib, scikit-learn, seaborn |
| `python-analytics` | built on python:3.12-slim | duckdb, polars, pyarrow |
| `python-web` | built on python:3.12-slim | fastapi, uvicorn, sqlalchemy, httpx |
| `python-scraping` | built on python:3.12-slim | httpx, beautifulsoup4, lxml, markdownify |
| `python-documents` | built on python:3.12-slim | pypdf, python-docx, openpyxl, pillow |
| `node-minimal` | node:20-slim | nothing |
| `node-typescript` | built on node:20-slim | typescript, tsx, vitest |
| `node-react` | built on node:20-slim | typescript, vite, react, react-dom, @types/react |
| `bun` | oven/bun:1-slim | Bun's own bundler, test runner and package manager |
| `deno` | denoland/deno:alpine | TypeScript with no install step |
| `go` | golang:1.23-alpine | Go toolchain |
| `rust` | rust:1-slim | Rust toolchain with cargo |

A runtime naming an `image` starts as fast as a pull. One naming a `base_image`
plus `packages` builds an image on first use and hits the cache afterwards, which
is worth it when installing them per session would dominate.

**`coding` is the one to reach for when the agent's job is code.** Measured at
99.7 MB and eleven seconds to build: `git` is 33.1 MB of that and unavoidable,
while `ripgrep`, `fd`, `jq`, `less` and `procps` come to 4.3 MB between them.
`uv` is there because an agent installs packages inside its own turn — measured
5–7× faster than pip on the same package set. What is deliberately absent is
`build-essential`: 94 MB to compile wheels that manylinux already ships built.

### What every sandbox gets, whatever its runtime

Some settings are applied to the container rather than baked into an image, so
they reach the ready-made runtimes too — `bun`, `deno`, `go` and `rust` build
nothing, so a Dockerfile could never have carried them. A runtime overrides any
of it through its own `env_vars`.

- **git is configured through `GIT_CONFIG_*`.** Without it, every git command in
  a bind-mounted workspace fails with `detected dubious ownership` — the
  directory belongs to whoever the service runs as, and the container does not —
  and a commit fails again with `Author identity unknown`. Both measured.
- **An init process reaps orphans.** `sleep infinity` as PID 1 never calls
  `wait()`, so a backgrounded server or anything the command timeout kills stays
  a zombie for the life of the container. Measured: ten orphans left ten
  permanent zombies, accumulating against `pids_limit` until the session could
  not fork. The reaper costs 488 kB.
- **Output stays readable**: `PYTHONUNBUFFERED` so a command killed by the
  timeout still returns what it printed rather than an empty string, and
  `NO_COLOR` / `PAGER=cat` so escape sequences do not fill the model's context.
- **`uv` is capped at two concurrent downloads.** Its parallelism is memory:
  measured installing pandas, uncapped uv is OOM-killed by a 128 MB ceiling that
  pip survives. Capped it fits, and is still 6.6× faster than pip.
- **`LANG=C.UTF-8`**, because `node:20-slim` ships no locale at all.

## One Container per Conversation

The ref is the conversation's container. Store the message history (or `result.workspace.ref`)
with the conversation, and every turn reaches the same environment; a different conversation
gets a container of its own. Call `destroy(ref)` when a conversation is deleted — nothing
removes a container for you.

What `DockerWorkspace` does not do is manage a fleet: idle reaping, a ceiling on running
containers, per-tenant capacity, hibernation. That is what [`sandboxd`](remote.md) is for,
built on the same `DockerSandbox` and the same command path.

## Options

`image`, `runtime`, `work_dir`, `network_mode`, `mem_limit`, `cpus` and `oci_runtime` are
fields of `DockerWorkspace`. For a container option it does not expose — volumes, a tmpfs,
a pids limit — build the backend yourself with a factory that returns the `DockerSandbox`
you want:

```python
from pydantic_ai_backends import DockerSandbox, DockerWorkspaceBackend


def sandbox(name: str) -> DockerSandbox:
    return DockerSandbox(
        image="python:3.12-slim",
        container_name=name,
        volumes={"/host/data": "/workspace/data"},
        tmpfs={"/tmp": "size=64m"},
        pids_limit=256,
    )


result = await agent.run("...", workspace=DockerWorkspaceBackend(sandbox_factory=sandbox))
```

## Security

- Each workspace is its own container; conversations cannot see each other's files.
- `no-new-privileges`, an init process and a pids limit are on by default; memory and CPU
  ceilings are fields.
- `network_mode="none"` keeps a container off the network.
- Under Docker's default `runc` a container shares the host kernel. `oci_runtime="runsc"`
  (gVisor) or `"kata"` moves that boundary, which is the right trade for model-written code.

## Next Steps

- [Workspaces](workspaces.md) - refs, removal, the conformance results
- [sandboxd](remote.md) - the same containers behind an HTTP service
- [API Reference](../api/docker.md) - Complete API
