# Docker Sandbox

Model-written code in a container, continued across turns.

```bash
pip install "pydantic-ai-backend[console,docker]"
```

```python
import asyncio

from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, DockerWorkspace

workspace = DockerWorkspace(runtime="python-datascience", network_mode="none")
agent = Agent(
    "anthropic:claude-opus-5-5",
    instructions="Write Python to answer questions about data. Run it before answering.",
    capabilities=[workspace, ConsoleCapability(image_support=True)],
)


async def main() -> None:
    first = await agent.run("Load the iris dataset and plot sepal length by species to plot.png.")
    print(first.output)

    # Same container: plot.png and every installed package are still there.
    second = await agent.run(
        "Look at plot.png and describe what it shows.", message_history=first.all_messages()
    )
    print(second.output)

    # The container outlives the runs; remove it when the conversation is over.
    await workspace.destroy(second.workspace.ref)


asyncio.run(main())
```

- `runtime="python-datascience"` starts from an image with pandas, numpy, matplotlib and
  scikit-learn installed; see [built-in runtimes](../concepts/docker.md#built-in-runtimes).
- `network_mode="none"` keeps the container off the network.
- `image_support=True` lets the model see the PNG it rendered.
