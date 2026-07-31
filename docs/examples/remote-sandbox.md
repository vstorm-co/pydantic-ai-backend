# Remote Sandbox

A web application that gives every user their own Docker sandbox, without the
application container ever touching Docker.

## The shape of it

```
┌──────────────┐   HTTP + token    ┌──────────────┐   docker.sock   ┌───────────┐
│  your app    │ ────────────────► │   sandboxd   │ ──────────────► │ container │
│ (no docker)  │                   │ (owns socket)│                 │ per user  │
└──────────────┘                   └──────────────┘                 └───────────┘
```

Only `sandboxd` holds the Docker socket. Your application holds a token.

## 1. The service

```python title="sandboxd_app.py"
import os

from pydantic_ai_backends.remote.server import SandboxdConfig, create_app

app = create_app(
    SandboxdConfig(
        token=os.environ["SANDBOXD_TOKEN"],
        runtimes={
            "python": "python:3.12-slim",
            "datascience": "my-registry/python-ds:v3",
        },
        default_runtime="python",
        # Ceilings the client cannot raise.
        max_sessions=20,
        mem_limit="1g",
        cpus=2.0,
        pids_limit=512,
        network_mode="none",
        execute_timeout=300,
        # Files survive the container, so a reaped session does not lose work.
        workspace_root="/workspaces",
        # Installed packages survive it too — the container is stopped, not discarded.
        persist_containers=True,
        # Nobody has to remember to clean up abandoned workspaces.
        workspace_ttl=14 * 24 * 3600,
        idle_timeout=1800,
        # One tenant cannot take the whole pool.
        max_sessions_per_tenant=5,
    )
)
```

```dockerfile title="sandboxd/Dockerfile"
FROM python:3.12-slim
RUN pip install --no-cache-dir "pydantic-ai-backend[server]" uvicorn
COPY sandboxd_app.py .
CMD ["uvicorn", "sandboxd_app:app", "--host", "0.0.0.0", "--port", "8080"]
```

## 2. Compose

```yaml title="docker-compose.yml"
services:
  app:
    build: ./app
    environment:
      SANDBOXD_URL: http://sandboxd:8080
      SANDBOXD_TOKEN: ${SANDBOXD_TOKEN}
    networks: [backend]
    # Deliberately no docker.sock mount.

  sandboxd:
    build: ./sandboxd
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - sandbox_workspaces:/workspaces
    environment:
      SANDBOXD_TOKEN: ${SANDBOXD_TOKEN}
    networks: [backend]
    # Deliberately no `ports:` — the service token can start containers.

networks:
  backend:

volumes:
  sandbox_workspaces:
```

## 3. The agent

```python title="app/agent.py"
import os

from pydantic_ai import Agent
from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import DEFAULT_RULESET
from pydantic_ai_backends.remote import RemoteSandbox

SERVICE_URL = os.environ["SANDBOXD_URL"]
SERVICE_TOKEN = os.environ["SANDBOXD_TOKEN"]


def sandbox_for(session_id: str, tenant: str) -> RemoteSandbox:
    """One sandbox per session, reattached on later turns.

    No I/O happens here: the session — and the container behind it — opens on the
    first tool call, so a turn the model answers from memory costs nothing.
    """
    return RemoteSandbox(
        SERVICE_URL,
        token=SERVICE_TOKEN,
        session_id=session_id,
        tenant=tenant,  # capacity label; grants nothing
        runtime="python",
        reuse=True,
    )


def agent_for(session_id: str, tenant: str) -> Agent[None, str]:
    return Agent(
        "openai:gpt-4.1",
        capabilities=[
            ConsoleCapability(
                backend=sandbox_for(session_id, tenant),
                permissions=DEFAULT_RULESET,
            )
        ],
    )
```

Because the capability carries the sandbox, the agent's deps type stays yours —
nothing has to grow a `backend` field.

## Session ids

The id is the only thing your application chooses, so it is the only place
isolation can go wrong. Two rules:

1. **Make it unguessable.** Derive it from a UUID you generated, never from a
   sequential id or a user-supplied string.
2. **Keep it inside 64 characters** and within
   `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` — the service rejects anything else, which
   is also what stops an id from traversing a workspace path.

```python
session_id = f"{tenant_id.hex[:8]}-{conversation_id.hex}"  # 41 chars
```

The tenant prefix here is for reading the dashboard, not for isolation — that
comes from `conversation_id` being a UUID4.

## Choosing a session scope

What you key the id on decides who shares the sandbox — and therefore who reads
whose files:

| Scope | `session_id` from | Who shares it | Costs |
|---|---|---|---|
| Per run | the run id | nobody | a fresh container per turn |
| Per conversation | the conversation id | everyone in that chat, group chats included | a slot while the thread stays warm |
| Per user | the user id | that person, across all their chats | a slot per active user |
| Shared | the agent id | **everyone who talks to that agent** | one slot, and one set of files for all |

```python
SCOPE_KEYS = {
    "run": lambda ctx: ctx.run_id,
    "conversation": lambda ctx: ctx.conversation_id,
    "user": lambda ctx: ctx.user_id,
    "agent": lambda ctx: ctx.agent_id,
}


def session_id_for(scope: str, ctx) -> str:
    return f"{ctx.tenant_id.hex[:8]}-{SCOPE_KEYS[scope](ctx).hex}"
```

The last two rows are data-sharing decisions, not technical ones. A shared
sandbox means one user sees files another user wrote, so surface it wherever the
choice is made rather than treating it as one more option.

Two traps worth naming:

- **Per-user scope in a group chat is incoherent.** Three people in a channel get
  three sandboxes, the agent writes a file for one of them, and the other two are
  told about a file they cannot see.
- **`max_sessions=20` is nothing for per-conversation scope** in a busy Slack
  workspace. Size it for the concurrency you actually have, set
  `max_sessions_per_tenant` so one organization cannot starve the rest, and
  prefer per-run where continuity is not needed.

## Showing the files to your users

The files are the point, and users want to see them — including in a conversation
from last week, long after its sandbox was reaped.

**Do not hand the session token to the browser.** It authorizes `/exec` as well as
`/ls` and `/read`, so anyone who opens DevTools gets a shell in the sandbox. The
service token is worse. Proxy instead:

```python title="app/files.py"
from fastapi import APIRouter, HTTPException

from pydantic_ai_backends.remote import WorkspaceArchive, WorkspaceArchiveError

router = APIRouter()
archive = WorkspaceArchive(SERVICE_URL, token=SERVICE_TOKEN)


@router.get("/conversations/{conversation_id}/files")
async def list_files(conversation_id: str, user=CurrentUser) -> list[dict]:
    await authorize(user, conversation_id)  # your rules, not sandboxd's
    try:
        return list(archive.ls(session_id_for("conversation", conversation_id)))
    except WorkspaceArchiveError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=str(e)) from e


@router.get("/conversations/{conversation_id}/files/{path:path}")
async def read_file(conversation_id: str, path: str, user=CurrentUser) -> str:
    await authorize(user, conversation_id)
    try:
        return archive.read(session_id_for("conversation", conversation_id), path)
    except WorkspaceArchiveError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=str(e)) from e
```

`WorkspaceArchive` reads the host volume directly, so **no container starts** —
browsing an old conversation is as cheap as reading a file, and it works whether
or not that session is still open. It raises rather than degrading precisely so
this route can pass a real status through instead of showing an empty folder when
the service is misconfigured.

One UI note that follows from the scope table above: under shared or per-user
scope, "this conversation's files" is the wrong label — the files belong to the
sandbox, not the chat. Say whose environment it is, or a user will see files they
never created and read it as a leak.

## Watching what happens

```python
import httpx

headers = {"X-Sandbox-Token": SERVICE_TOKEN}

# Capacity at a glance, unauthenticated.
print(httpx.get(f"{SERVICE_URL}/healthz").json())

# Live sessions with memory against each ceiling.
print(httpx.get(f"{SERVICE_URL}/sessions", params={"usage": True}, headers=headers).json())

# What an agent did in one session, newest last.
print(httpx.get(f"{SERVICE_URL}/sessions/{session_id}/events", headers=headers).json())
```

For a browser view, set `SandboxdConfig(ui_enabled=True)` and open `/ui` — but
only on localhost or a private network, since the page asks for the service
token.

## Cleaning up

```python
sandbox.stop()  # end the turn, keep the files for the next one
sandbox.stop(purge=True)  # the conversation is gone: drop container and files
```

A plain `stop()` is best-effort by design: `sandboxd` reaps idle sessions on its
own, so a missed call costs an idle timeout rather than a leaked container. The
service owns the lifecycle, not your application.

Purging is the part your application *does* own, because only it knows that a
conversation was deleted or a user left:

```python
async def on_conversation_deleted(conversation_id) -> None:
    RemoteSandbox(
        SERVICE_URL, token=SERVICE_TOKEN, session_id=session_id_for("conversation", ...)
    ).stop(purge=True)
```

And `workspace_ttl` is the net under all of it: a workspace whose session nobody
has opened for that long is swept, so abandoned chats do not fill the disk.
