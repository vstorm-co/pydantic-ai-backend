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

## Next Steps

- [Core Concepts](concepts/index.md) - Learn the fundamentals
- [Local Backend Example](examples/local-backend.md) - Start with local files
- [API Reference](api/index.md) - Complete API documentation
