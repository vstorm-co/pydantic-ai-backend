# Docker Sandbox

`DockerSandbox` provides isolated code execution for your pydantic-ai agents. Run untrusted code safely in Docker containers.

!!! warning "Requires Docker"
    ```bash
    pip install pydantic-ai-backend[docker]
    ```
    Ensure Docker is installed and the daemon is running.

## Basic Usage with pydantic-ai

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import DockerSandbox, create_console_toolset


@dataclass
class Deps:
    backend: DockerSandbox


# Create sandbox with pre-configured runtime
sandbox = DockerSandbox(runtime="python-datascience")

try:
    # Add console tools to your agent
    toolset = create_console_toolset()
    agent = Agent("openai:gpt-4o", deps_type=Deps)
    agent = agent.with_toolset(toolset)

    # Agent can safely execute arbitrary code in Docker
    result = agent.run_sync(
        "Load the iris dataset with sklearn, analyze it with pandas, "
        "and create a visualization with matplotlib",
        deps=Deps(backend=sandbox),
    )
    print(result.output)
finally:
    sandbox.stop()  # Clean up container
```

## Runtime Configurations

Pre-configured environments with packages pre-installed:

```python
from pydantic_ai_backends import DockerSandbox, RuntimeConfig

# Use built-in runtime
sandbox = DockerSandbox(runtime="python-datascience")

# Or define custom runtime for your use case
runtime = RuntimeConfig(
    name="ml-env",
    base_image="python:3.12-slim",
    packages=["torch", "transformers", "pandas"],
)
sandbox = DockerSandbox(runtime=runtime)
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

## SessionManager for Multi-User

For web apps where each user needs isolated execution:

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import SessionManager, DockerSandbox, create_console_toolset


@dataclass
class UserDeps:
    backend: DockerSandbox
    user_id: str


# Create session manager
manager = SessionManager(
    default_runtime="python-datascience",
    workspace_root="/app/workspaces",  # Persistent storage per user
)


async def handle_user_request(user_id: str, message: str):
    # Get or create sandbox for this user
    sandbox = await manager.get_or_create(user_id)

    # Create agent with user's isolated sandbox
    toolset = create_console_toolset()
    agent = Agent("openai:gpt-4o", deps_type=UserDeps)
    agent = agent.with_toolset(toolset)

    result = await agent.run(
        message,
        deps=UserDeps(backend=sandbox, user_id=user_id),
    )
    return result.output


# Each user's code runs in isolated container
# User A cannot see User B's files
```

### Architecture

```
                    ┌─────────────────┐
                    │ SessionManager  │
                    └────────┬────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────▼───────┐   ┌───────▼───────┐   ┌───────▼───────┐
│ DockerSandbox │   │ DockerSandbox │   │ DockerSandbox │
│   (User A)    │   │   (User B)    │   │   (User C)    │
│  pydantic-ai  │   │  pydantic-ai  │   │  pydantic-ai  │
│    Agent      │   │    Agent      │   │    Agent      │
└───────────────┘   └───────────────┘   └───────────────┘
```

## Persistent Storage

By default, files are lost when container stops. Use volumes for persistence:

```python
sandbox = DockerSandbox(
    runtime="python-datascience",
    volumes={"/host/data": "/workspace/data"},  # Mount host directory
)
```

### Named Containers (Reusable)

Use `container_name` to create containers that persist between sessions.
Installed packages, caches, and filesystem state survive restarts:

```python
sandbox = DockerSandbox(
    image="python:3.12-slim",
    container_name="my-dev-env",  # implies auto_remove=False
    volumes={"/my/project": "/workspace"},
)
# First run: creates container "my-dev-env"
# Next run: finds it, restarts if stopped, reattaches
```

With SessionManager, each user gets their own persistent directory:

```python
manager = SessionManager(
    workspace_root="/app/workspaces",  # Creates /app/workspaces/{user_id}/
)
```

### Custom Sandbox Factory

`SessionManager` accepts a `sandbox_factory` callable to use any sandbox
backend (Daytona, custom implementations, etc.):

```python
from pydantic_ai_backends import SessionManager, DaytonaSandbox


def daytona_factory(session_id: str) -> DaytonaSandbox:
    return DaytonaSandbox(sandbox_id=session_id)


manager = SessionManager(sandbox_factory=daytona_factory)
sandbox = await manager.get_or_create("user-123")
```

When no factory is provided, `SessionManager` defaults to creating
`DockerSandbox` instances (fully backward compatible).

## Security

- Each user gets a separate Docker container
- Users cannot access each other's files
- Containers can have resource limits (CPU, memory)
- Network isolation available via Docker networking
- No host filesystem access (unless explicitly mounted)

## Next Steps

- [Multi-User Example](../examples/multi-user.md) - Web app with SessionManager
- [Docker Sandbox Example](../examples/docker-sandbox.md) - Full example
- [API Reference](../api/docker.md) - Complete API
