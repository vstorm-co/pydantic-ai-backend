# Remote Sandbox

An application that runs in a container and gives its agents sandboxes, without ever
holding the Docker socket.

## The service

```yaml
# docker-compose.yml
services:
  sandboxd:
    image: ghcr.io/vstorm-co/sandboxd:latest  # pin the version you deploy
    environment:
      SANDBOXD_TOKEN: ${SANDBOXD_TOKEN}
      SANDBOXD_WORKSPACE_ROOT: /var/lib/sandboxd/workspaces
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      # Same path on both sides: the daemon resolves the mount on the host.
      - /var/lib/sandboxd/workspaces:/var/lib/sandboxd/workspaces

  app:
    build: .
    environment:
      SANDBOXD_URL: http://sandboxd:8080
      SANDBOXD_TOKEN: ${SANDBOXD_TOKEN}
```

The token can start containers on the host: treat it as the socket it stands in front of.

## The agent

```bash
pip install "pydantic-ai-backend[console]"
```

```python
import os

from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, SandboxdWorkspace

sandbox = SandboxdWorkspace(
    service_url=os.environ["SANDBOXD_URL"], token=os.environ["SANDBOXD_TOKEN"]
)
agent = Agent("anthropic:claude-opus-5-5", capabilities=[sandbox, ConsoleCapability()])
```

The session opens on the first tool call and is kept on the service's host; the next run
with the same message history attaches to it. See [sandboxd](../concepts/remote.md) for
capacity, reaping and the security model.
