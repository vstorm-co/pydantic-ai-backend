"""A model that gets the same call wrong twice must not end the run."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.workspaces import WorkspaceRef

from pydantic_ai_backends import StateBackend, create_console_toolset
from pydantic_ai_backends.workspaces import StateWorkspace


def _wrong_twice_then_stop(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Two identical ambiguous edits, then an answer — the shape that used to be fatal."""
    attempts = sum(
        1
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    )
    if attempts < 2:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "edit_file",
                    {"path": "/f.py", "old_string": "x = 1", "new_string": "x = 2"},
                )
            ]
        )
    return ModelResponse(parts=[TextPart("I could not make that edit uniquely.")])


async def test_a_second_ambiguous_edit_is_answered_not_fatal():
    """`ModelRetry` past the budget raises `UnexpectedModelBehavior` and kills the run.

    The floor in `_failures.steer` is what stops that, and only a real run
    exercises it: `max_retries` reaches the tool from the toolset, and the
    exception is raised by pydantic-ai rather than by anything here.
    """
    state = StateBackend()
    state.write_bytes("/f.py", b"x = 1\nx = 1\n")
    workspace = StateWorkspace(store={"doc": state})
    agent = Agent(
        FunctionModel(_wrong_twice_then_stop),
        capabilities=[workspace],
        toolsets=[create_console_toolset()],
    )

    result = await agent.run("Change x to 2.", workspace=WorkspaceRef(provider="state", id="doc"))

    assert result.output == "I could not make that edit uniquely."

    kinds = [
        part.part_kind
        for message in result.all_messages()
        for part in message.parts
        if getattr(part, "part_kind", None) in {"tool-return", "retry-prompt"}
    ]
    contents = [
        str(part.content)
        for message in result.all_messages()
        for part in message.parts
        if getattr(part, "part_kind", None) in {"tool-return", "retry-prompt"}
    ]

    # The first wrong edit steered the model; the second, out of budget, came
    # back as an ordinary result. Either half missing is a different bug: no
    # retry prompt means nothing steers, and no tool return means the run died.
    assert kinds == ["retry-prompt", "tool-return"]
    assert all("found 2 times" in content for content in contents)
