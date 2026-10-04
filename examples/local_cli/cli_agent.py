"""A CLI coding assistant working in a directory on your machine.

The console tools run in Pydantic AI's `LocalWorkspace`, which runs commands as
you: use it on your own code. For code you did not write, swap in an isolated
workspace such as `DockerWorkspace`.

Requires: pip install pydantic-ai-backend[console]
"""

import argparse
import asyncio
import os
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.messages import ModelMessage

from pydantic_ai_backends import create_console_toolset

INSTRUCTIONS = """You are a helpful coding assistant that can read, write, and execute code.

## Guidelines

1. Always read a file before editing it
2. Use glob to find files when you don't know exact paths
3. Use grep to search for code patterns
4. Execute tests after making changes
5. Explain what you're doing and why
"""


def create_cli_agent(
    working_dir: str,
    model: str = "anthropic:claude-opus-5-5",
    enable_execute: bool = True,
    ignore_hidden: bool = True,
    read_only: bool = False,
) -> Agent[None, str]:
    """Create a CLI agent with console tools in `working_dir`.

    Args:
        working_dir: The directory the agent works in.
        model: The model to use.
        enable_execute: Whether to offer shell command execution.
        ignore_hidden: Default grep behavior for hidden files.
        read_only: Refuse every change and command in the workspace.
    """
    toolset = create_console_toolset(
        include_execute=enable_execute and not read_only,
        require_write_approval=False,
        require_execute_approval=False,
        default_ignore_hidden=ignore_hidden,
    )
    return Agent(
        model,
        instructions=INSTRUCTIONS,
        capabilities=[LocalWorkspace(working_dir, read_only=read_only)],
        toolsets=[toolset],
    )


async def run_single_task(agent: Agent[None, str], task: str) -> str:
    """Run a single task and return the result."""
    result = await agent.run(task)
    return result.output


async def run_interactive(agent: Agent[None, str], working_dir: str) -> None:
    """Run an interactive session; each turn sees the conversation so far."""
    print(f"CLI Agent ready! Working directory: {working_dir}")
    print("Type 'quit' to exit, 'help' for examples.\n")

    examples = """
Example commands:
- "List all Python files"
- "Read the README.md file"
- "Create a hello.py that prints 'Hello World'"
- "Find all TODO comments in the code"
- "Run the tests"
- "Show me the project structure"
"""
    history: list[ModelMessage] = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        if user_input.lower() == "help":
            print(examples)
            continue

        if not user_input:
            continue

        print("\nAgent: ", end="", flush=True)
        result = await agent.run(user_input, message_history=history)
        history = result.all_messages()
        print(f"{result.output}\n")


def main() -> None:
    """Main entry point."""

    parser = argparse.ArgumentParser(description="CLI Agent with file operations")
    parser.add_argument("--dir", "-d", default=".", help="Working directory (default: current)")
    parser.add_argument(
        "--model",
        "-m",
        default="anthropic:claude-opus-5-5",
        help="Model to use (default: anthropic:claude-opus-5-5)",
    )
    parser.add_argument("--no-execute", action="store_true", help="Disable shell commands")
    parser.add_argument("--task", "-t", help="Run a single task instead of interactive mode")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Refuse every change and command; only the read tools are offered",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include hidden files when searching with grep",
    )

    args = parser.parse_args()

    working_dir = str(Path(args.dir).resolve())
    if not os.path.isdir(working_dir):
        print(f"Error: {working_dir} is not a directory")
        return

    agent = create_cli_agent(
        working_dir,
        model=args.model,
        enable_execute=not args.no_execute,
        ignore_hidden=not args.include_hidden,
        read_only=args.read_only,
    )

    if args.task:
        print(asyncio.run(run_single_task(agent, args.task)))
    else:
        asyncio.run(run_interactive(agent, working_dir))


if __name__ == "__main__":
    main()
