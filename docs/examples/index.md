# Examples

Complete examples showing how to use **pydantic-ai-backend** with [pydantic-ai](https://ai.pydantic.dev/) agents.

## Getting Started

| Example | Description |
|---------|-------------|
| [CLI Agent](cli-agent.md) | Interactive CLI coding assistant |
| [Local Backend](local-backend.md) | File operations with LocalBackend |

## Production

| Example | Description |
|---------|-------------|
| [Docker Sandbox](docker-sandbox.md) | Safe code execution in Docker |
| [Multi-User Web App](multi-user.md) | FastAPI server with user isolation |
| [Remote Sandbox](remote-sandbox.md) | Docker sandboxes without Docker in your app container |

## Full Examples

Complete working examples in the [`examples/`](https://github.com/vstorm-co/pydantic-ai-backend/tree/main/examples) directory:

| Directory | Description |
|-----------|-------------|
| `local_cli/` | CLI coding assistant with pydantic-ai |
| `web_production/` | Multi-user web app with Docker |

## Quick Reference

### CLI Agent (LocalBackend)

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import LocalBackend, create_console_toolset

@dataclass
class Deps:
    backend: LocalBackend

backend = LocalBackend(root_dir=".")
toolset = create_console_toolset()

agent = Agent("openai:gpt-4o", deps_type=Deps)
agent = agent.with_toolset(toolset)

result = agent.run_sync(
    "Create a hello.py script and run it",
    deps=Deps(backend=backend),
)
```

### Safe Code Execution (DockerSandbox)

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import DockerSandbox, create_console_toolset

@dataclass
class Deps:
    backend: DockerSandbox

sandbox = DockerSandbox(runtime="python-datascience")

try:
    toolset = create_console_toolset()
    agent = Agent("openai:gpt-4o", deps_type=Deps)
    agent = agent.with_toolset(toolset)

    result = agent.run_sync(
        "Analyze data with pandas and create a chart",
        deps=Deps(backend=sandbox),
    )
finally:
    sandbox.stop()
```

### Multi-User Web App (SessionManager)

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import SessionManager, DockerSandbox, create_console_toolset

@dataclass
class UserDeps:
    backend: DockerSandbox
    user_id: str

manager = SessionManager(workspace_root="/app/workspaces")
toolset = create_console_toolset()
agent = Agent("openai:gpt-4o", deps_type=UserDeps).with_toolset(toolset)

async def handle_request(user_id: str, message: str):
    sandbox = await manager.get_or_create(user_id)
    result = await agent.run(message, deps=UserDeps(backend=sandbox, user_id=user_id))
    return result.output
```

### Unit Testing (StateBackend)

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai_backends import StateBackend, create_console_toolset

@dataclass
class Deps:
    backend: StateBackend

def test_agent_file_operations():
    backend = StateBackend()
    toolset = create_console_toolset(include_execute=False)

    agent = Agent(TestModel(), deps_type=Deps)
    agent = agent.with_toolset(toolset)

    # Pre-populate test files
    backend.write("/input.txt", "test data")

    result = agent.run_sync("Read input.txt", deps=Deps(backend=backend))

    # Verify agent behavior
    assert "/input.txt" in backend.files
```
