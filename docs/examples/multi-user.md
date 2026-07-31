# Multi-User Web App Example

Build a multi-user web application where each user gets isolated storage and execution.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Web UI (templates/)                      │
├─────────────────────────────────────────────────────────────┤
│                      FastAPI Server                         │
├─────────────────────────────────────────────────────────────┤
│                    SessionManager                           │
├───────────────┬───────────────┬───────────────┬────────────┤
│ DockerSandbox │ DockerSandbox │ DockerSandbox │    ...     │
│   (User A)    │   (User B)    │   (User C)    │            │
└───────────────┴───────────────┴───────────────┴────────────┘
```

## SessionManager

```python
from pydantic_ai_backends import SessionManager

# Create manager. By default it creates DockerSandbox instances.
manager = SessionManager(
    default_runtime="python-minimal",
    workspace_root="/app/workspaces",  # Persistent storage
    default_idle_timeout=3600,  # 1 hour
)

# Get (or lazily create) the sandbox for a user. Returns the same
# sandbox on subsequent calls and resets the idle timer.
sandbox = await manager.get_or_create("alice")

# User operations are isolated
sandbox.write("/workspace/secret.txt", "Alice's private data")
sandbox.execute("python script.py")

# Release a session and stop its container
await manager.release("alice")
```

`SessionManager` keys sandboxes by `session_id`. The
[`get_or_create`][pydantic_ai_backends.backends.docker.session.SessionManager.get_or_create] method is
the single entry point: it returns an existing live sandbox or starts a new one.
Pass a `sandbox_factory` callable to back sessions with a different sandbox type
(e.g. [`DaytonaSandbox`][pydantic_ai_backends.backends.daytona.DaytonaSandbox])
instead of the default Docker path.

## FastAPI Server

```python
from contextlib import asynccontextmanager
from dataclasses import dataclass
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai_backends import SessionManager, DockerSandbox, create_console_toolset

# Session manager
session_manager: SessionManager | None = None


@asynccontextmanager
async def lifespan(app):
    global session_manager
    session_manager = SessionManager(
        default_runtime="python-minimal",
        workspace_root="/tmp/workspaces",
    )
    yield
    # Cleanup on shutdown: stop the cleanup loop and all containers
    await session_manager.shutdown()


app = FastAPI(lifespan=lifespan)


# Agent setup
@dataclass
class UserDeps:
    backend: DockerSandbox
    user_id: str


toolset = create_console_toolset()
agent = Agent("openai:gpt-4o", deps_type=UserDeps).with_toolset(toolset)


# Request models
class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str
    session_id: str


# Endpoints
@app.post("/sessions/{session_id}/chat")
async def chat(session_id: str, request: ChatRequest):
    # get_or_create lazily starts a sandbox on the first request and
    # reuses it (resetting the idle timer) on later ones.
    sandbox = await session_manager.get_or_create(session_id)

    deps = UserDeps(backend=sandbox, user_id=session_id)
    result = await agent.run(request.message, deps=deps)

    return ChatResponse(response=result.output, session_id=session_id)


@app.delete("/sessions/{session_id}")
async def end_session(session_id: str):
    released = await session_manager.release(session_id)
    if not released:
        raise HTTPException(404, "Session not found")
    return {"message": "Session ended"}


@app.get("/sessions/{session_id}/files")
async def list_files(session_id: str, path: str = "/workspace"):
    if session_id not in session_manager:
        raise HTTPException(404, "Session not found")
    sandbox = await session_manager.get_or_create(session_id)
    return {"files": sandbox.ls_info(path)}
```

## Client Usage

```python
import httpx


async def main():
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        # Pick a session id (e.g. the user id). The sandbox is created
        # automatically on the first chat request.
        session_id = "alice"

        # Chat with AI
        r = await client.post(
            f"/sessions/{session_id}/chat",
            json={"message": "Create a hello world script and run it"},
        )
        print(r.json()["response"])

        # List files
        r = await client.get(f"/sessions/{session_id}/files")
        print(r.json()["files"])

        # Cleanup
        await client.delete(f"/sessions/{session_id}")
```

## Auto-Cleanup of Idle Sessions

SessionManager can automatically clean up sessions that have been idle, preventing container sprawl in production.

### Background Cleanup Loop

Start a background task that periodically checks for and removes idle sessions:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic_ai_backends import SessionManager

session_manager: SessionManager | None = None


@asynccontextmanager
async def lifespan(app):
    global session_manager
    session_manager = SessionManager(
        default_runtime="python-datascience",
        workspace_root="/app/workspaces",
        default_idle_timeout=1800,  # 30 minutes
    )

    # Start background cleanup every 5 minutes
    session_manager.start_cleanup_loop(interval=300)

    yield

    # Graceful shutdown: stop cleanup loop and all containers
    stopped = await session_manager.shutdown()
    print(f"Stopped {stopped} sessions on shutdown")


app = FastAPI(lifespan=lifespan)
```

### Manual Cleanup

You can also trigger cleanup manually, for example in a scheduled endpoint or health check:

```python
@app.post("/admin/cleanup")
async def cleanup_idle_sessions(max_idle: int = 1800):
    """Remove sessions idle for more than max_idle seconds."""
    cleaned = await session_manager.cleanup_idle(max_idle=max_idle)
    return {
        "cleaned": cleaned,
        "active": session_manager.session_count,
    }
```

### Per-User Session Lifecycle

SessionManager handles the full lifecycle of each user's sandbox:

```python
# 1. First request from user - creates a new container
sandbox = await session_manager.get_or_create("user-alice")

# 2. Subsequent requests - reuses the existing container
#    (also resets the idle timer)
sandbox = await session_manager.get_or_create("user-alice")

# 3. If container dies between requests, a new one is created automatically
sandbox = await session_manager.get_or_create("user-alice")

# 4. Explicit release when user logs out
await session_manager.release("user-alice")

# 5. Or let idle cleanup handle it automatically
```

### Monitoring Active Sessions

```python
@app.get("/admin/sessions")
async def list_sessions():
    return {
        "count": session_manager.session_count,
        "session_ids": list(session_manager.sessions.keys()),
    }


@app.get("/admin/sessions/{session_id}")
async def check_session(session_id: str):
    return {"exists": session_id in session_manager}
```

## Security Features

- **User Isolation**: Each user's container is separate
- **No Cross-Access**: Users cannot see other users' files
- **Persistent Storage**: Files stored on host, mounted into containers
- **Automatic Cleanup**: Containers removed when sessions end or go idle

## Full Example

See [`examples/web_production/`](https://github.com/vstorm-co/pydantic-ai-backend/tree/main/examples/web_production) for a complete implementation with:

- FastAPI server
- HTML/JS frontend
- Session management
- AI chat integration

```bash
cd examples/web_production
pip install pydantic-ai-backend[docker] fastapi uvicorn pydantic-ai jinja2
python server.py
# Open http://localhost:8000
```
