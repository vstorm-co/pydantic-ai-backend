"""Multi-user web server with an isolated Docker container per session.

Each session is a Pydantic AI workspace: a container created on first use and
reached again by its ref. The file and command endpoints use the same workspace
the agent works in, so a user and their agent see the same files.

Requires: pip install "pydantic-ai-backend[console,docker]" fastapi uvicorn jinja2
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.workspaces import Workspace, WorkspaceRef, WorkspaceTimeoutError

from pydantic_ai_backends import ConsoleCapability, DockerWorkspace
from pydantic_ai_backends.permissions import PERMISSIVE_RULESET

# ============================================================================
# Sessions: one container each, remembered by its ref
# ============================================================================

containers = DockerWorkspace(image="python:3.12-slim", network_mode="none")
sessions: dict[str, WorkspaceRef | None] = {}
histories: dict[str, list[ModelMessage]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    # Containers outlive runs by design; this demo removes them on shutdown.
    for ref in sessions.values():
        if ref is not None:
            await containers.destroy(ref)


app = FastAPI(title="Multi-User Code Sandbox API", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Serve the frontend UI."""
    return templates.TemplateResponse("index.html", {"request": request})


# ============================================================================
# Request/Response Models
# ============================================================================


class CreateSessionResponse(BaseModel):
    session_id: str
    message: str


class WriteFileRequest(BaseModel):
    path: str
    content: str


class ExecuteRequest(BaseModel):
    command: str
    timeout: int = 30


class ExecuteResponse(BaseModel):
    output: str
    exit_code: int | None


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str


agent = Agent(
    "anthropic:claude-opus-5-5",
    instructions=(
        "You are a helpful coding assistant in an isolated sandbox. "
        "You can freely create, read and execute files; they persist within this session."
    ),
    # Each session is its own network-less container, so commands need no approval.
    capabilities=[containers, ConsoleCapability(permissions=PERMISSIVE_RULESET)],
)


def _workspace(session_id: str) -> Workspace:
    """The session's container, created on its first operation."""
    if session_id not in sessions:
        raise HTTPException(404, f"No such session: {session_id}")
    return Workspace(containers.backend(sessions[session_id]))


def _remember(session_id: str, workspace: Workspace) -> None:
    if workspace.ref is not None:
        sessions[session_id] = workspace.ref


# ============================================================================
# API Endpoints
# ============================================================================


@app.post("/sessions", response_model=CreateSessionResponse)
async def create_session() -> CreateSessionResponse:
    """Create a session; its container starts on first use."""
    session_id = uuid.uuid4().hex
    sessions[session_id] = None
    histories[session_id] = []
    return CreateSessionResponse(
        session_id=session_id, message="Session created. Your workspace is isolated."
    )


@app.delete("/sessions/{session_id}")
async def end_session(session_id: str) -> dict[str, str]:
    """End a session and remove its container."""
    if session_id not in sessions:
        raise HTTPException(404, f"No such session: {session_id}")
    ref = sessions.pop(session_id)
    histories.pop(session_id, None)
    if ref is not None:
        await containers.destroy(ref)
    return {"message": "Session ended successfully"}


@app.get("/sessions/{session_id}/files")
async def list_files(session_id: str, path: str = ".") -> dict[str, list[dict[str, object]]]:
    """List files in the session's workspace."""
    workspace = _workspace(session_id)
    entries = await workspace.list_dir(path)
    _remember(session_id, workspace)
    return {
        "files": [
            {"name": e.name, "path": e.path, "is_dir": e.is_dir, "size": e.size} for e in entries
        ]
    }


@app.get("/sessions/{session_id}/files/{path:path}")
async def read_file(session_id: str, path: str) -> dict[str, str]:
    """Read a file from the session's workspace."""
    workspace = _workspace(session_id)
    try:
        content = await workspace.read_text(path)
    except FileNotFoundError:
        raise HTTPException(404, f"File not found: {path}") from None
    _remember(session_id, workspace)
    return {"content": content}


@app.post("/sessions/{session_id}/files")
async def write_file(session_id: str, request: WriteFileRequest) -> dict[str, str]:
    """Write a file to the session's workspace."""
    workspace = _workspace(session_id)
    await workspace.write_text(request.path, request.content)
    _remember(session_id, workspace)
    return {"message": f"File written: {request.path}"}


@app.post("/sessions/{session_id}/execute", response_model=ExecuteResponse)
async def execute_command(session_id: str, request: ExecuteRequest) -> ExecuteResponse:
    """Run a shell command in the session's container."""
    workspace = _workspace(session_id)
    try:
        result = await workspace.run(request.command, shell=True, timeout=request.timeout)
    except WorkspaceTimeoutError as error:
        return ExecuteResponse(output=error.stdout + error.stderr, exit_code=None)
    finally:
        _remember(session_id, workspace)
    return ExecuteResponse(output=result.stdout + result.stderr, exit_code=result.exit_code)


@app.post("/sessions/{session_id}/chat", response_model=ChatResponse)
async def chat(session_id: str, request: ChatRequest) -> ChatResponse:
    """Chat with an agent working in the session's container."""
    workspace = _workspace(session_id)
    result = await agent.run(
        request.message, message_history=histories[session_id], workspace=workspace
    )
    histories[session_id] = result.all_messages()
    _remember(session_id, result.workspace)
    return ChatResponse(response=result.output)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
