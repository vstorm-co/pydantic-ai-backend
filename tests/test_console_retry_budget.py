"""A model that gets the same call wrong twice must not end the run."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ToolCallPart,
)
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


def _answer(part: ModelRequestPart | ModelResponsePart) -> str | None:
    """`retried` or `returned` for a tool's answer to the model, `None` for anything else.

    pydantic-ai 2.60 moved a `ModelRetry` from a `RetryPromptPart` of its own to a
    `ToolReturnPart` with `outcome="retried"`, and the `console` extra supports both.
    """
    kind = getattr(part, "part_kind", None)
    if kind == "retry-prompt":
        return "retried"
    if kind == "tool-return":
        return "retried" if getattr(part, "outcome", None) == "retried" else "returned"
    return None


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

    parts = [part for message in result.all_messages() for part in message.parts]
    answers = [answer for part in parts if (answer := _answer(part)) is not None]
    contents = [str(getattr(part, "content", "")) for part in parts if _answer(part) is not None]

    # The first wrong edit steered the model; the second, out of budget, came
    # back as an ordinary result. Either half missing is a different bug: no
    # retry means nothing steers, and no plain return means the run died.
    assert answers == ["retried", "returned"]
    assert all("found 2 times" in content for content in contents)
