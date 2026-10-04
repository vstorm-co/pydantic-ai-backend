"""Custom permission ruleset: read and execute allowed, writes denied."""

import asyncio
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace

from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import create_ruleset


async def main() -> None:
    Path("/tmp/demo").mkdir(exist_ok=True)
    ruleset = create_ruleset(
        allow_read=True,
        allow_write=False,
        allow_edit=False,
        allow_execute=True,
    )

    agent = Agent(
        "anthropic:claude-opus-5-5",
        capabilities=[LocalWorkspace("/tmp/demo"), ConsoleCapability(permissions=ruleset)],
    )

    result = await agent.run("Run pytest and tell me which tests pass.")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
