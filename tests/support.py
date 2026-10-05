"""Workspaces and run contexts for driving the console tools in tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    LocalWorkspaceBackend,
    Workspace,
    WorkspaceCommand,
    WorkspaceRef,
)

from pydantic_ai_backends import StateBackend
from pydantic_ai_backends.workspaces import StateWorkspaceBackend


def local(path: Path) -> Workspace:
    """A real directory, commands included."""
    return Workspace(LocalWorkspaceBackend(path))


def document(files: Mapping[str, str | bytes] | None = None) -> Workspace:
    """A `StateBackend` document holding `files`: files only, no commands."""
    state = StateBackend()
    for path, content in (files or {}).items():
        state.write_bytes(path, content.encode() if isinstance(content, str) else content)
    backend = StateWorkspaceBackend({"doc": state}, ref=WorkspaceRef(provider="state", id="doc"))
    return Workspace(backend)


def ctx(workspace: Workspace, **fields: Any) -> RunContext[Any]:
    """A run context whose tools reach `workspace`."""
    return RunContext(deps=None, model=TestModel(), usage=RunUsage(), workspace=workspace, **fields)


class Raising:
    """A workspace backend whose every operation raises `error` — a provider transport gone."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    @property
    def ref(self) -> WorkspaceRef | None:
        return WorkspaceRef(provider="raising", id="1")

    async def working_dir(self) -> str:
        return "/work"

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        raise self.error

    async def read_bytes(self, path: str) -> bytes:
        raise self.error

    async def write_bytes(self, path: str, data: bytes) -> None:
        raise self.error

    async def stat(self, path: str) -> FileEntry:
        raise self.error

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        raise self.error

    async def make_dir(self, path: str) -> None:
        raise self.error

    async def remove(self, path: str) -> None:
        raise self.error

    async def exists(self, path: str) -> bool:
        raise self.error


async def call(toolset: Any, tool: str, context: RunContext[Any], **args: Any) -> Any:
    """Invoke one registered tool function directly."""
    return await toolset.tools[tool].function_schema.function(context, **args)
