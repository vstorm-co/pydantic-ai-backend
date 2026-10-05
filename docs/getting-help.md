# Getting Help

## Documentation

This documentation is your primary resource. Use the search bar (press `/` or `s`) to find specific topics.

## GitHub Issues

For bugs, feature requests, or questions:

[:fontawesome-brands-github: Open an Issue](https://github.com/vstorm-co/pydantic-ai-backend/issues){ .md-button }

### Before Opening an Issue

1. **Search existing issues** - Your problem may already be reported
2. **Check the docs** - The answer might be here
3. **Prepare a minimal example** - Help us reproduce the issue

### Bug Report Template

```markdown
## Description
[Clear description of the bug]

## Steps to Reproduce
1. Create workspace with...
2. Call method...
3. Observe error...

## Expected Behavior
[What you expected to happen]

## Actual Behavior
[What actually happened]

## Environment
- pydantic-ai-backend version: X.X.X
- pydantic-ai version: X.X.X
- Python version: 3.XX
- OS: [e.g., macOS 14.0, Ubuntu 22.04]
- Docker version (if using DockerWorkspace or sandboxd): X.X.X
```

## Community Resources

### Pydantic AI

pydantic-ai-backend is designed for use with Pydantic AI. Their documentation is an excellent resource:

- [Pydantic AI Documentation](https://ai.pydantic.dev/)
- [Pydantic AI GitHub](https://github.com/pydantic/pydantic-ai)

### Related Projects

- [pydantic-deep](https://github.com/vstorm-co/pydantic-deepagents) - Full agent framework
- [pydantic-ai-todo](https://github.com/vstorm-co/pydantic-ai-todo) - Task planning toolset
- [subagents-pydantic-ai](https://github.com/vstorm-co/subagents-pydantic-ai) - Multi-agent orchestration
- [summarization-pydantic-ai](https://github.com/vstorm-co/summarization-pydantic-ai) - Context management

## FAQ

### Which workspace should I use?

| Use Case | Workspace |
|----------|-----------|
| Unit tests, or files kept in your database | `StateWorkspace` |
| Local CLI tools on your own code | Pydantic AI's `LocalWorkspace` |
| Model-written code on one host | `DockerWorkspace` |
| An app in a container serving many users | `SandboxdWorkspace` |
| Sandboxes scheduled by a cluster | `KubernetesWorkspace` |
| Hosted sandboxes | `DaytonaWorkspace` |

### How do I run without Docker?

Use Pydantic AI's `LocalWorkspace` for a directory on your machine:

```python
from pydantic_ai.capabilities import LocalWorkspace

from pydantic_ai_backends import ConsoleCapability

capabilities = [LocalWorkspace("./workspace"), ConsoleCapability()]
```

For tests, use `StateWorkspace`, which keeps files in memory and runs no commands:

```python
from pydantic_ai_backends import ConsoleCapability, StateWorkspace

capabilities = [StateWorkspace(), ConsoleCapability()]
```

### How do I disable shell execution?

```python
ConsoleCapability(include_execute=False)
```

### How do I restrict file access?

Use the permission system:

```python
from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import READONLY_RULESET

ConsoleCapability(permissions=READONLY_RULESET)
```

Or give the run a read-only workspace, which refuses every change at the workspace itself:

```python
from pydantic_ai.capabilities import LocalWorkspace

LocalWorkspace("./workspace", read_only=True)
```

### Docker container won't start

1. Ensure Docker is running: `docker info`
2. Check image exists: `docker images`
3. Pull if needed: `docker pull python:3.12-slim`
4. On Linux, check permissions: `sudo usermod -aG docker $USER`

## Contributing

We welcome contributions! See our [Contributing Guide](https://github.com/vstorm-co/pydantic-ai-backend/blob/main/CONTRIBUTING.md) for details.
