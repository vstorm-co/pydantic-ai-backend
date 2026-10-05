# CLI Agent Example

Build an interactive CLI coding assistant: the console tools in Pydantic AI's `LocalWorkspace`.

## Quick Start

```bash
cd examples/local_cli
pip install pydantic-ai-backend[console]
export ANTHROPIC_API_KEY=your-key
python cli_agent.py

# Include hidden files (e.g., .env) when searching
python cli_agent.py --include-hidd## Basic Implementation

```python
import asyncio

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.messages import ModelMessage

from pydantic_ai_backends import create_console_toolset

agent = Agent(
    "anthropic:claude-opus-5-5",
    instructions="You are a helpful coding assistant.",
    capabilities=[LocalWorkspace(".")],
    toolsets=[create_console_toolset(require_execute_approval=False)],
)


async def main() -> None:
    history: list[ModelMessage] = []
    while (user_input := input("You: ").strip()) not in ("quit", "exit"):
        result = await agent.run(user_input, message_history=history)
        history = result.all_messages()
        print(f"Agent: {result.output}\n")


asyncio.run(main())
```

`LocalWorkspace` runs commands as you and isolates nothing; for code you did not write, use
an isolated workspace such as `DockerWorkspace`.

))
```

## Features

The agent can:

- **List files**: "Show me what's in this directory"
- **Read files**: "Read the main.py file"
- **Write files**: "Create a hello.py that prints Hello World"
- **Edit files**: "Change the function name from foo to bar"
- **Search**: "Find all files containing 'TODO'" (use `--include-hidden` to search dotfiles)
- **Execute**: "Run the tests"

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

## Command Line Options

The full example supports these options:

```bash
# Specify working directory
python cli_agent.py --dir /path/to/project

# Use different model
python cli_agent.py --model anthropic:claude-3-haiku

# Disable shell execution
python cli_agent.py --no-execute

# Restrict file access
python cli_agent.py --read-only

# Include hidden files in searches
python cli_agent.py --include-hidden

# Single task mode
python cli_agent.py --task "Create a hello world script"
```

## Full Example

See [`examples/local_cli/cli_agent.py`](https://github.com/vstorm-co/pydantic-ai-backend/tree/main/examples/local_cli) for the complete implementation with:

- Command line argument parsing
- Interactive and single-task modes
- Safety options (--read-only, --no-execute)
- Help command
