"""Read-only agent: write/edit/execute tools are hidden from the model."""

import asyncio
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace

from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import READONLY_RULESET


async def main() -> None:
    Path("/tmp/demo").mkdir(exist_ok=True)
    # With READONLY_RULESET the model cannot see write_file, edit_file or execute.
    # It can only read, glob, grep and ls.
    agent = Agent(
        "anthropic:claude-opus-5-5",
        capabilities=[
            LocalWorkspace("/tmp/demo"),
            ConsoleCapability(permissions=READONLY_RULESET),
        ],
    )

    result = await agent.run("List all Python files and show me the contents of the first one.")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
