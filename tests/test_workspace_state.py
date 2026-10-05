"""`StateWorkspace` beyond the conformance suite: refs, the store, refusals."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import Workspace, WorkspaceRef, WorkspaceUnavailableError

from pydantic_ai_backends import StateBackend
from pydantic_ai_backends.workspaces import StateWorkspace, StateWorkspaceBackend


async def test_a_document_the_application_loaded_is_worked_in() -> None:
    loaded = StateBackend()
    loaded.write_bytes("/notes.md", b"kept")
    capability = StateWorkspace(store={"row-7": loaded})
    workspace = Workspace(capability.backend(WorkspaceRef(provider="state", id="row-7")))
    assert await workspace.read_text("notes.md") == "kept"
    await workspace.write_text("new.md", "x")
    assert loaded.is_file("/new.md")


async def test_a_new_run_adds_a_document_to_the_store() -> None:
    capability = StateWorkspace()
    backend = capability.backend()
    assert backend.ref is None
    await Workspace(backend).make_dir("data")
    assert backend.ref is not None and backend.ref.id in capability.store
    assert capability.store[backend.ref.id].is_dir("/data")


async def test_a_removed_document_is_unavailable() -> None:
    capability = StateWorkspace()
    backend = capability.backend()
    await backend.working_dir()
    assert backend.ref is not None
    await capability.destroy(backend.ref)
    await capability.destroy(backend.ref)
    with pytest.raises(WorkspaceUnavailableError, match="no longer exists"):
        await capability.backend(backend.ref).working_dir()


def test_a_ref_from_another_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="'state' workspace ref"):
        StateWorkspaceBackend({}, ref=WorkspaceRef(provider="docker", id="x"))


async def test_destroy_refuses_another_providers_ref() -> None:
    with pytest.raises(ValueError, match="'state' workspace ref"):
        await StateWorkspace().destroy(WorkspaceRef(provider="docker", id="x"))


def test_a_foreign_ref_is_left_to_another_capability() -> None:
    capability = StateWorkspace()
    ctx: Any = None
    assert capability.get_workspace(ctx, ref=WorkspaceRef(provider="docker", id="x")) is None
    assert isinstance(capability.get_workspace(ctx, ref=None), StateWorkspaceBackend)


def test_deferred_loading_is_refused() -> None:
    with pytest.raises(UserError, match="defer_loading"):
        StateWorkspace(defer_loading=True)
