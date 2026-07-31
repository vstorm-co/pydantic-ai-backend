# Core Concepts

**pydantic-ai-backend** adds file operations and code execution to your [pydantic-ai](https://ai.pydantic.dev/) agents. Three main components work together:

## 1. Console Toolset

The console toolset gives your pydantic-ai agent file and execution capabilities:

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import LocalBackend, create_console_toolset

@dataclass
class Deps:
    backend: LocalBackend

# Create toolset with file + execution tools
toolset = create_console_toolset()

# Add to your pydantic-ai agent
agent = Agent("openai:gpt-4o", deps_type=Deps)
agent = agent.with_toolset(toolset)

# Run - agent can now read, write, search, and execute!
result = agent.run_sync(
    "Create a Python script that calculates pi and run it",
    deps=Deps(backend=LocalBackend(root_dir=".")),
)
```

Tools provided: `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `execute`

[Learn more about Console Toolset →](console-toolset.md)

## 2. Backends

Backends provide file storage. The same toolset works with any backend:

```python
from pydantic_ai_backends import LocalBackend, StateBackend, DockerSandbox

# Local filesystem (for CLI tools)
backend = LocalBackend(root_dir="/workspace")

# In-memory (for testing)
backend = StateBackend()

# Docker (for safe execution)
backend = DockerSandbox(runtime="python-datascience")
```

[Learn more about Backends →](backends.md)

## 3. Docker Sandbox

For production and multi-user scenarios, `DockerSandbox` provides isolated execution:

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import DockerSandbox, create_console_toolset

@dataclass
class Deps:
    backend: DockerSandbox

# Safe sandbox with data science packages
sandbox = DockerSandbox(runtime="python-datascience")

try:
    toolset = create_console_toolset()
    agent = Agent("openai:gpt-4o", deps_type=Deps)
    agent = agent.with_toolset(toolset)

    # Agent can run arbitrary code safely in Docker
    result = agent.run_sync(
        "Analyze the iris dataset with pandas and show statistics",
        deps=Deps(backend=sandbox),
    )
    print(result.output)
finally:
    sandbox.stop()
```

[Learn more about Docker →](docker.md)

## 4. Permissions

Fine-grained access control for file operations and shell commands:

```python
from pydantic_ai_backends import LocalBackend
from pydantic_ai_backends.permissions import DEFAULT_RULESET, READONLY_RULESET

# Safe defaults - allow reads, ask for writes/executes
backend = LocalBackend(root_dir="/workspace", permissions=DEFAULT_RULESET)

# Read-only mode - deny all writes and executes
backend = LocalBackend(root_dir="/workspace", permissions=READONLY_RULESET)
```

Available presets: `DEFAULT_RULESET`, `PERMISSIVE_RULESET`, `READONLY_RULESET`, `STRICT_RULESET`

[Learn more about Permissions →](permissions.md)

## Architecture

![Architecture](../assets/architecture.png)

## Choosing a Backend

| Use Case | Backend | Example |
|----------|---------|---------|
| CLI tools, local dev | `LocalBackend` | Personal coding assistant |
| Unit tests | `StateBackend` | Testing agent behavior |
| Safe code execution | `DockerSandbox` | Code interpreter |
| Multi-user web apps | `SessionManager` | SaaS product |
| App itself runs in a container | [`RemoteSandbox`](remote.md) | Self-hosted SaaS, no docker-in-docker |
| Mixed (project + temp) | `CompositeBackend` | Complex workflows |
