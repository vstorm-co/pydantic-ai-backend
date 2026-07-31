# Local Backend Example

Build a pydantic-ai agent that works with local files using `LocalBackend`.

## Quick Start

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import LocalBackend, create_console_toolset


@dataclass
class Deps:
    backend: LocalBackend


# Create backend for local filesystem
backend = LocalBackend(root_dir="./workspace")

# Add file tools to your agent
toolset = create_console_toolset()
agent = Agent("openai:gpt-4o", deps_type=Deps)
agent = agent.with_toolset(toolset)

# Your agent can now work with files!
result = agent.run_sync(
    "Create a fibonacci.py script that calculates fib(10) and run it",
    deps=Deps(backend=backend),
)
print(result.output)
```

## Complete CLI Agent

Interactive coding assistant that works with your local project:

```python
import asyncio
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import LocalBackend, create_console_toolset, get_console_system_prompt


@dataclass
class Deps:
    backend: LocalBackend


# Create backend with security restrictions
backend = LocalBackend(
    root_dir=".",
    allowed_directories=["./"],  # Restrict to current dir
    enable_execute=True,
)

# Create agent with file tools
toolset = create_console_toolset()
agent = Agent(
    "openai:gpt-4o",
    system_prompt=f"""You are a helpful coding assistant.
{get_console_system_prompt()}
""",
    deps_type=Deps,
)
agent = agent.with_toolset(toolset)


async def main():
    deps = Deps(backend=backend)
    print("CLI Agent ready! Type 'quit' to exit.\n")

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit"):
            break

        result = await agent.run(user_input, deps=deps)
        print(f"\nAgent: {result.output}\n")


asyncio.run(main())
```

## Security Options

### Restricted Directories

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import LocalBackend, create_console_toolset


@dataclass
class Deps:
    backend: LocalBackend


# Only allow access to specific directories
backend = LocalBackend(
    allowed_directories=[
        "/home/user/project",
        "/home/user/data",
    ],
)

toolset = create_console_toolset()
agent = Agent("openai:gpt-4o", deps_type=Deps).with_toolset(toolset)

# Agent can access /home/user/project but NOT /etc/passwd
```

### Disable Execution

```python
# Read-only mode - no shell access
backend = LocalBackend(
    root_dir="/workspace",
    enable_execute=False,
)

# Toolset without execute command
toolset = create_console_toolset(include_execute=False)
```

## Example Session

```
CLI Agent ready! Type 'quit' to exit.

You: Show me the project structure

Agent: Let me list the files in the current directory.

Contents of .:
  src/
  tests/
  README.md
  pyproject.toml

The project has:
- `src/` - Source code
- `tests/` - Test files
- `README.md` - Documentation
- `pyproject.toml` - Project config

You: Create a fibonacci function

Agent: I'll create a fibonacci.py file with the function.

Created fibonacci.py with a fibonacci function that calculates
the nth Fibonacci number using recursion with memoization.

You: Run it with n=10

Agent: Running the script...

Output: 55

The 10th Fibonacci number is 55.
```

## Full Example

See [`examples/local_cli/`](https://github.com/vstorm-co/pydantic-ai-backend/tree/main/examples/local_cli) for a complete implementation:

```bash
cd examples/local_cli
pip install pydantic-ai-backend[console]
python cli_agent.py --dir ~/myproject
```
