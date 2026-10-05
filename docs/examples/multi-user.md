# Multi-User App

A web app where every conversation gets its own sandbox, and a user's next message finds
the files the agent left behind.

The pattern is the same whichever workspace you use: store the message history (it carries
the workspace ref) with the conversation, pass it to the next run, and destroy the
workspace when the conversation is deleted. `sandboxd` adds what a fleet needs — idle
reaping, a ceiling on running containers, per-tenant capacity.

```python
import os

from fastapi import FastAPI
from pydantic_ai import Agent, ModelMessagesTypeAdapter

from pydantic_ai_backends import ConsoleCapability, SandboxdWorkspace

app = FastAPI()
conversations: dict[str, bytes] = {}  # conversation id -> stored message history

sandbox = SandboxdWorkspace(
    service_url=os.environ["SANDBOXD_URL"], token=os.environ["SANDBOXD_TOKEN"]
)
agent = Agent("anthropic:claude-opus-5-5", capabilities=[sandbox, ConsoleCapability()])


@app.post("/conversations/{conversation_id}/messages")
async def send(conversation_id: str, text: str, tenant: str) -> dict[str, str]:
    stored = conversations.get(conversation_id)
    history = ModelMessagesTypeAdapter.validate_json(stored) if stored else None
    result = await agent.run(text, message_history=history)
    conversations[conversation_id] = result.all_messages_json()
    return {"answer": result.output}


@app.delete("/conversations/{conversation_id}")
async def delete(conversation_id: str) -> None:
    stored = conversations.pop(conversation_id, None)
    if stored is None:
        return
    history = ModelMessagesTypeAdapter.validate_json(stored)
    refs = [m.workspace_ref for m in history if getattr(m, "workspace_ref", None)]
    if refs:
        await sandbox.destroy(refs[-1])
```

Notes:

- **Keep refs on the server.** Pydantic AI's UI adapters strip workspace refs from history
  a client sends, so a client cannot choose the environment your agent works in. Store the
  history server-side, as above.
- **Tenants.** `SandboxdWorkspace(tenant=...)` labels sessions for the service's
  per-tenant ceiling, so one busy tenant cannot fill the pool. Build one capability per
  tenant, or pass the backend per run: `agent.run(..., workspace=sandbox.backend(ref))`.
- **Who shares what.** A ref per conversation means everyone in a group chat shares one
  sandbox; a ref per user means a personal workspace across conversations. That is a
  data-sharing decision — see [sandboxd](../concepts/remote.md#choosing-what-shares-a-session).
