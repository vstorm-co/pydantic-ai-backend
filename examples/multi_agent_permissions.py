"""Two agents, one workspace, different permissions."""

import asyncio
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace

from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import PERMISSIVE_RULESET, READONLY_RULESET


async def main() -> None:
    Path("/tmp/demo").mkdir(exist_ok=True)
    workspace = LocalWorkspace("/tmp/demo")

    # Agent 1: full access (can read + write + execute)
    writer = Agent(
        "anthropic:claude-opus-5-5",
        capabilities=[workspace, ConsoleCapability(permissions=PERMISSIVE_RULESET)],
    )

    # Agent 2: read-only (write/edit/execute tools are hidden)
    reader = Agent(
        "anthropic:claude-opus-5-5",
        capabilities=[workspace, ConsoleCapability(permissions=READONLY_RULESET)],
    )

    written = await writer.run("Write 'hello world' to test.txt")

    # The reader works in the same environment the writer used.
    result = await reader.run("Read the contents of test.txt", workspace=written.workspace)
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
