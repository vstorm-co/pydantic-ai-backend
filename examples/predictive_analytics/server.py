"""FastAPI server with WebSocket streaming for the predictive analytics demo.

Run with:
    uvicorn examples.predictive_analytics.server:app --host 0.0.0.0 --port 8000

Or:
    python -m examples.predictive_analytics.server
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic_ai import (
    Agent,
    FinalResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
    ToolCallPartDelta,
)
from pydantic_ai._agent_graph import End, UserPromptNode
from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent, ModelMessage
from pydantic_ai.workspaces import Workspace

from pydantic_ai_backends import DockerWorkspace

from .agent import CHART_DATA_PREFIX, analytics_agent
from .models import AnalyticsDeps

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DATA_PATH = BASE_DIR / "data" / "sales_data.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

containers = DockerWorkspace(image="python:3.12-slim")
workspace: Workspace | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start a container on startup, remove it on shutdown."""
    global workspace
    logger.info("Starting the Docker container...")
    workspace = Workspace(containers.backend())
    result = await workspace.run(
        "pip install -q pandas numpy scikit-learn 2>&1 | tail -1", shell=True, timeout=120
    )
    logger.info(f"Packages installed: {result.stdout.strip()}")
    yield
    if workspace.ref is not None:
        await containers.destroy(workspace.ref)
        logger.info("Docker container removed")


app = FastAPI(title="Predictive Analytics Demo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the frontend."""
    return HTMLResponse((STATIC_DIR / "index.html").read_text())


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "container": workspace.ref.id if workspace is not None and workspace.ref else None,
    }


# ---------------------------------------------------------------------------
# WebSocket streaming
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_chat(websocket: WebSocket):
    """Main chat endpoint with real-time streaming."""
    await websocket.accept()

    message_history: list[ModelMessage] = []

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            user_message = data.get("message", "").strip()
            if not user_message:
                continue

            assert workspace is not None
            deps = AnalyticsDeps(workspace=workspace, data_path=str(DATA_PATH))

            await websocket.send_json({"type": "start"})

            try:
                async with analytics_agent.iter(
                    user_message,
                    deps=deps,
                    message_history=message_history,
                ) as run:
                    async for node in run:
                        await _process_node(websocket, node, run)

                    result = run.result

                message_history = result.all_messages()

                await websocket.send_json({"type": "response", "content": str(result.output)})
            except Exception as e:
                logger.exception("Agent error")
                await websocket.send_json({"type": "error", "content": str(e)})

            await websocket.send_json({"type": "done"})

    except WebSocketDisconnect:
        logger.info("Client disconnected")


async def _process_node(websocket: WebSocket, node: Any, run: Any) -> None:
    """Dispatch node to appropriate streaming handler."""
    if isinstance(node, UserPromptNode):
        pass  # No status bar needed — typing indicator handles this
    elif Agent.is_model_request_node(node):
        await _stream_model_request(websocket, node, run)
    elif Agent.is_call_tools_node(node):
        await _stream_tool_calls(websocket, node, run)
    elif isinstance(node, End):
        pass


async def _stream_model_request(websocket: WebSocket, node: Any, run: Any) -> None:
    """Stream text and tool call deltas from model."""
    current_tool_name: str | None = None

    async with node.stream(run.ctx) as request_stream:
        final_result_found = False

        async for event in request_stream:
            if isinstance(event, PartStartEvent):
                if hasattr(event.part, "tool_name"):
                    current_tool_name = event.part.tool_name
                    await websocket.send_json(
                        {
                            "type": "tool_call_start",
                            "tool_name": current_tool_name,
                        }
                    )
            elif isinstance(event, PartDeltaEvent):
                if isinstance(event.delta, TextPartDelta):
                    await websocket.send_json(
                        {"type": "text_delta", "content": event.delta.content_delta}
                    )
                elif isinstance(event.delta, ToolCallPartDelta):
                    await websocket.send_json(
                        {
                            "type": "tool_args_delta",
                            "tool_name": current_tool_name,
                            "args_delta": event.delta.args_delta,
                        }
                    )
            elif isinstance(event, FinalResultEvent):
                final_result_found = True
                break

        if final_result_found:
            previous = ""
            async for cumulative in request_stream.stream_text():
                delta = cumulative[len(previous) :]
                if delta:
                    await websocket.send_json({"type": "text_delta", "content": delta})
                previous = cumulative


async def _stream_tool_calls(websocket: WebSocket, node: Any, run: Any) -> None:
    """Stream tool execution events; intercept chart data."""
    tool_names: dict[str, str] = {}

    async with node.stream(run.ctx) as handle_stream:
        async for event in handle_stream:
            if isinstance(event, FunctionToolCallEvent):
                tool_name = event.part.tool_name
                tool_args = event.part.args
                tool_call_id = event.part.tool_call_id

                if tool_call_id:
                    tool_names[tool_call_id] = tool_name

                await websocket.send_json(
                    {
                        "type": "tool_start",
                        "tool_name": tool_name,
                        "args": tool_args if isinstance(tool_args, dict) else str(tool_args),
                    }
                )

            elif isinstance(event, FunctionToolResultEvent):
                tool_call_id = event.tool_call_id
                tool_name = tool_names.get(tool_call_id, "unknown")
                result_str = str(event.result.content)

                # Intercept chart data
                if result_str.startswith(CHART_DATA_PREFIX):
                    chart_json = result_str[len(CHART_DATA_PREFIX) :]
                    try:
                        await websocket.send_json(
                            {"type": "chart_data", "data": json.loads(chart_json)}
                        )
                    except json.JSONDecodeError:
                        logger.warning("Failed to parse chart data")

                # Send tool output (truncated for display)
                display_output = result_str
                if display_output.startswith(CHART_DATA_PREFIX):
                    display_output = "Chart data generated"
                elif len(display_output) > 500:
                    display_output = display_output[:500] + "..."

                await websocket.send_json(
                    {
                        "type": "tool_output",
                        "tool_name": tool_name,
                        "output": display_output,
                    }
                )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
