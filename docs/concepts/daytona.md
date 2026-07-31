# Daytona Sandbox

`DaytonaSandbox` provides cloud-based isolated code execution via [Daytona](https://daytona.io/) ephemeral sandboxes. Sub-90ms startup, no Docker daemon required.

!!! warning "Requires Daytona SDK"
    ```bash
    pip install pydantic-ai-backend[daytona]
    ```
    You also need a Daytona API key — set `DAYTONA_API_KEY` environment variable or pass `api_key=` directly.

## Basic Usage with pydantic-ai

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import DaytonaSandbox, create_console_toolset


@dataclass
class Deps:
    backend: DaytonaSandbox


# Create cloud sandbox (starts automatically)
sandbox = DaytonaSandbox(api_key="dtna_...")

try:
    toolset = create_console_toolset()
    agent = Agent("openai:gpt-4o", deps_type=Deps)
    agent = agent.with_toolset(toolset)

    result = agent.run_sync(
        "Write a Python script that fetches weather data and saves it to a CSV",
        deps=Deps(backend=sandbox),
    )
    print(result.output)
finally:
    sandbox.stop()  # Delete the cloud sandbox
```

## Authentication

Daytona requires an API key. You can provide it in two ways:

```python
# Option 1: Environment variable (recommended)
import os

os.environ["DAYTONA_API_KEY"] = "dtna_..."
sandbox = DaytonaSandbox()

# Option 2: Direct parameter
sandbox = DaytonaSandbox(api_key="dtna_...")
```

## Configuration

```python
sandbox = DaytonaSandbox(
    api_key="dtna_...",  # API key (or DAYTONA_API_KEY env var)
    work_dir="/home/daytona",  # Working directory (default)
    startup_timeout=180,  # Max seconds to wait for sandbox ready
)
```

## How It Works

`DaytonaSandbox` extends [`BaseSandbox`][pydantic_ai_backends.backends.base.BaseSandbox], inheriting shell-based implementations of `ls_info`, `read`, `glob_info`, and `grep_raw`. It overrides:

| Method | Implementation |
|--------|---------------|
| `execute()` | Daytona SDK `sandbox.process.exec()` |
| `_read_bytes()` | Daytona native file download API |
| `write()` | Daytona native file upload API |
| `edit()` | Read → Python string replace → write |

Native file APIs are more efficient than shell-based alternatives for binary content and large files.

## Daytona vs Docker

| Feature | `DaytonaSandbox` | `DockerSandbox` |
|---------|-----------------|----------------|
| Infrastructure | Cloud (Daytona platform) | Local Docker daemon |
| Startup time | Sub-90ms | Seconds (image pull + start) |
| Setup required | API key only | Docker installed + running |
| Isolation | Cloud VM | Container |
| Persistence | Ephemeral | Ephemeral (volumes optional) |
| Cost | Daytona pricing | Free (local resources) |
| Runtimes | Default environment | Custom `RuntimeConfig` |
| Best for | CI/CD, serverless, cloud deployments | Local development, self-hosted |

## Lifecycle Management

```python
sandbox = DaytonaSandbox(api_key="dtna_...")

# Check if sandbox is responsive
if sandbox.is_alive():
    result = sandbox.execute("python --version")
    print(result.output)

# Clean up when done
sandbox.stop()
```

The sandbox is also cleaned up automatically via `__del__` when the object is garbage collected.

## Error Handling

```python
from pydantic_ai_backends import DaytonaSandbox

# Missing API key
try:
    sandbox = DaytonaSandbox()  # No key in env either
except ValueError as e:
    print(e)  # "Daytona API key is required..."

# Sandbox startup timeout
try:
    sandbox = DaytonaSandbox(api_key="dtna_...", startup_timeout=5)
except RuntimeError as e:
    print(e)  # "Daytona sandbox failed to start within 5 seconds"
```

## Next Steps

- [Backends Overview](backends.md) - Compare all backends
- [Docker Sandbox](docker.md) - Local alternative with Docker
- [Console Toolset](console-toolset.md) - Ready-to-use tools for agents
