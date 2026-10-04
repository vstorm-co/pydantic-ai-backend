# Local Workspace

A coding assistant that works on files in a directory on your machine.

`LocalWorkspace` is Pydantic AI's own workspace: commands run as you, with your
permissions, so use it for your own trusted work. For code you did not write, use an
isolated workspace such as [`DockerWorkspace`](docker-sandbox.md).

```python
import asyncio

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace

from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import DEFAULT_RULESET


async def approve(operation: str, target: str, reason: str) -> bool:
    return input(f"Allow {operation} on {target!r}? [y/N] ").strip().lower() == "y"


agent = Agent(
    "anthropic:claude-opus-5-5",
    instructions="You are a careful coding assistant.",
    capabilities=[
        LocalWorkspace("./project"),
        ConsoleCapability(permissions=DEFAULT_RULESET, ask_callback=approve),
    ],
)


async def main() -> None:
    result = await agent.run("Find the TODOs in this project and summarise them.")
    print(result.output)


asyncio.run(main())
```

`DEFAULT_RULESET` lets the agent read anything but secrets and asks before a write or a
command; `approve` is how it asks. Secrets such as `.env` are refused outright, and a
command naming one is refused too.

## Read-only

```python
from pydantic_ai_backends.permissions import READONLY_RULESET

ConsoleCapability(permissions=READONLY_RULESET)
# or: LocalWorkspace("./project", read_only=True) — the write and execute tools are not offered
```
