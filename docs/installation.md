# Installation

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Install with uv (recommended)

```bash
uv add pydantic-ai-backend
```

## Install with pip

```bash
pip install pydantic-ai-backend
```

## Optional Dependencies

Every workspace and the console tools need Pydantic AI 2.52 or newer.

| Extra | Gives you | Installs |
|---|---|---|
| `console` | `ConsoleCapability`, `create_console_toolset` | `pydantic-ai-slim` |
| `workspaces` | `StateWorkspace`, `SandboxdWorkspace` (no further extra needed) | `pydantic-ai-slim` |
| `docker` | `DockerWorkspace`, `DockerSandbox` | `docker` |
| `kubernetes` | `KubernetesWorkspace` | `kubernetes` |
| `daytona` | `DaytonaWorkspace` | `daytona` |
| `remote` | `WorkspaceArchive`, reading a `sandboxd` workspace without a sandbox | `httpx` |
| `server` | `sandboxd`, the service — install it in the *service* image, not your app | FastAPI, uvicorn, `docker` |
| `images` | Downscaling large images `read_file` returns | Pillow |

```bash
uv add "pydantic-ai-backend[console,docker]"
# or
pip install "pydantic-ai-backend[console,docker]"
```

## Environment Setup

### API Key (for console toolset)

If using the console toolset with pydantic-ai, set your model provider's API key:

=== "OpenAI"

    ```bash
    export OPENAI_API_KEY=your-api-key
    ```

=== "Anthropic"

    ```bash
    export ANTHROPIC_API_KEY=your-api-key
    ```

### Docker (for DockerWorkspace)

1. Install Docker: [Get Docker](https://docs.docker.com/get-docker/)
2. Ensure Docker daemon is running
3. Pull a base image:

```bash
docker pull python:3.12-slim
```

### Daytona (for DaytonaWorkspace)

Set your Daytona API key, or pass a `DaytonaConfig` to `DaytonaWorkspace(config=...)`:

```bash
export DAYTONA_API_KEY=your-api-key
```

## Verify Installation

```python
from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, DockerWorkspace

workspace = DockerWorkspace(image="python:3.12-slim")
agent = Agent("anthropic:claude-opus-5-5", capabilities=[workspace, ConsoleCapability()])

result = agent.run_sync("Write hello.py that prints a greeting, then run it")
print(result.output)
await workspace.destroy(result.workspace.ref)  # the container outlives the run
```

## Troubleshooting

### Import Errors

Ensure you have the correct Python version:

```bash
python --version  # Should be 3.10+
```

### Docker Permission Denied

On Linux, add your user to the docker group:

```bash
sudo usermod -aG docker $USER
```

Then log out and back in.

### pydantic-ai Not Found

If using console toolset, install with the `[console]` extra:

```bash
pip install pydantic-ai-backend[console]
```

## Choosing a low-level container runtime

Docker does not run containers itself. It hands each one to an **OCI runtime** —
by default `runc` — which is the process that actually creates the namespaces and
executes your code. That runtime is swappable, per container, and it is the only
knob that changes how strong a sandbox's isolation is rather than how much CPU or
memory it gets.

Register the ones you want with the daemon in `/etc/docker/daemon.json`, then name
one per sandbox or per runtime alias:

```json
{
  "runtimes": {
    "crun": { "path": "/usr/bin/crun" },
    "runsc": { "path": "/usr/local/bin/runsc" }
  }
}
```

```python
from pydantic_ai_backends import DockerWorkspace

# gVisor: syscalls handled in userspace, not by the host kernel.
workspace = DockerWorkspace(image="python:3.12-slim", oci_runtime="runsc")
```

Or service-wide and per runtime in `sandboxd`, where a runtime's own choice wins
over the service default:

```python
from pydantic_ai_backends.remote.server import SandboxRuntime, SandboxdConfig

config = SandboxdConfig(
    token="...",
    runtimes={
        "shell": SandboxRuntime(image="alpine:3"),
        # The runtime allowed to install packages off the network is the one
        # worth paying gVisor's I/O overhead for.
        "scraping": SandboxRuntime(
            runtime="python-scraping",
            network_mode="bridge",
            oci_runtime="runsc",
        ),
    },
    default_runtime="shell",
)
```

**A runtime the daemon does not know about makes it refuse to start the
container**, which surfaces as a `502` from `POST /sessions`. That is why the
default here is `None` — the daemon's own choice — rather than something we pick
on your behalf. `GET /policy` and the dashboard report which one is in force.

### What each one buys you

| Runtime | Isolation | Cost | Use when |
|---|---|---|---|
| `runc` (default) | Namespaces + cgroups, host kernel shared | none | Code you trust |
| `crun` | Same as `runc` | none — it is *faster* | Always, if available |
| `runsc` (gVisor) | Syscalls intercepted in userspace | ~10–30% on I/O-heavy work, near zero on compute | Untrusted, model-written code |
| `kata` | Own kernel per container, in a microVM | ~200ms start instead of milliseconds | Hard multi-tenant boundaries |

### `crun`: a faster `runc`, for free

`crun` is a drop-in reimplementation of `runc` in C rather than Go. It is not a
different isolation model — a `crun` container is exactly as isolated as a `runc`
one — it is the same thing with less overhead per container operation, because
there is no Go runtime to start and no garbage collector.

Published comparisons put the container lifecycle around 20% faster. Make it the
daemon's default and every sandbox gets it with no code change at all:

```json
{
  "default-runtime": "crun",
  "runtimes": { "crun": { "path": "/usr/bin/crun" } }
}
```

Install it with `apt install crun` or `dnf install crun`, then
`systemctl restart docker`.

Two caveats worth knowing before you size a host around it. The widely quoted
"~3 GB of RAM per node" figure comes from **Kubernetes** measurements comparing
CRI-O + `crun` against containerd + `runc`, so it includes the CRI layer and does
not transfer to a Docker deployment unchanged. And under Docker the persistent
per-container process is `containerd-shim-runc-v2`, which `crun` does not replace
— so expect the win to show up mainly in start and stop latency, and measure your
own host before promising anyone a density number.

## Next Steps

- [Core Concepts](concepts/index.md) - Learn the fundamentals
- [Local Workspace Example](examples/local-workspace.md) - Start with a directory on your machine
- [API Reference](api/index.md) - Complete API documentation
