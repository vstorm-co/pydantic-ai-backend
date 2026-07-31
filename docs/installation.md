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

### Console Toolset

For the ready-to-use pydantic-ai toolset:

```bash
uv add pydantic-ai-backend[console]
# or
pip install pydantic-ai-backend[console]
```

### Docker Sandbox

For isolated code execution in Docker containers:

```bash
uv add pydantic-ai-backend[docker]
# or
pip install pydantic-ai-backend[docker]
```

### Daytona Sandbox

For isolated code execution in Daytona cloud sandboxes (installs `daytona-sdk`):

```bash
uv add pydantic-ai-backend[daytona]
# or
pip install pydantic-ai-backend[daytona]
```

### Remote Sandbox (client)

To use sandboxes that live in another process, so your application never needs
Docker access (installs `httpx` only):

```bash
uv add pydantic-ai-backend[remote]
# or
pip install pydantic-ai-backend[remote]
```

### sandboxd (service)

For the service that owns Docker and rents out sandboxes over HTTP. Install this
in the *sandbox service* image, not in your application:

```bash
uv add pydantic-ai-backend[server]
# or
pip install pydantic-ai-backend[server]
```

See [Remote Sandboxes](concepts/remote.md).

### All Dependencies

```bash
uv add pydantic-ai-backend[console,docker,daytona,remote]
# or
pip install pydantic-ai-backend[console,docker,daytona,remote]
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

### Docker (for DockerSandbox)

For using `DockerSandbox`:

1. Install Docker: [Get Docker](https://docs.docker.com/get-docker/)
2. Ensure Docker daemon is running
3. Pull a base image:

```bash
docker pull python:3.12-slim
```

### Daytona (for DaytonaSandbox)

For using `DaytonaSandbox`, set your Daytona API key (or pass `api_key=` to the
constructor):

```bash
export DAYTONA_API_KEY=your-api-key
```

## Verify Installation

### Basic (LocalBackend)

```python
from pydantic_ai_backends import LocalBackend

backend = LocalBackend(root_dir=".")
backend.write("test.txt", "Hello from pydantic-ai-backend!")
print(backend.read("test.txt"))
```

### With Console Toolset

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import LocalBackend, create_console_toolset


@dataclass
class Deps:
    backend: LocalBackend


backend = LocalBackend(root_dir=".", enable_execute=False)
toolset = create_console_toolset(include_execute=False)

agent = Agent("openai:gpt-4o-mini", deps_type=Deps)
agent = agent.with_toolset(toolset)

result = agent.run_sync("List files in current directory", deps=Deps(backend=backend))
print(result.output)
```

### With Docker

```python
from pydantic_ai_backends import DockerSandbox

sandbox = DockerSandbox(image="python:3.12-slim")
sandbox.write("/workspace/hello.py", "print('Hello from Docker!')")
result = sandbox.execute("python /workspace/hello.py")
print(result.output)  # "Hello from Docker!"
sandbox.stop()
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
from pydantic_ai_backends import DockerSandbox

# gVisor: syscalls handled in userspace, not by the host kernel.
sandbox = DockerSandbox(image="python:3.12-slim", oci_runtime="runsc")
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
- [Local Backend Example](examples/local-backend.md) - Start with local files
- [API Reference](api/index.md) - Complete API documentation
