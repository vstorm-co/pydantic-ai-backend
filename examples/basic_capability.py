"""Basic ConsoleCapability usage: file and shell tools in a container."""

import asyncio

from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, DockerWorkspace


async def main() -> None:
    workspace = DockerWorkspace(image="python:3.12-slim")
    agent = Agent("anthropic:claude-opus-5-5", capabilities=[workspace, ConsoleCapability()])

    result = await agent.run("Create a hello.py file that prints 'Hello World', then run it.")
    print(result.output)

    # The container outlives the run; remove it when you are done with it.
    await workspace.destroy(result.workspace.ref)


if __name__ == "__main__":
    asyncio.run(main())
