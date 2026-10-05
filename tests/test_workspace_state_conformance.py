"""Pydantic AI's workspace conformance suite, against a `StateBackend` document."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from pydantic_ai_backends.workspaces import StateWorkspace


class TestStateWorkspaceConformance(WorkspaceBackendSuite):
    @pytest.fixture(scope="class")
    def capability(self) -> StateWorkspace:
        return StateWorkspace()

    @pytest.fixture(scope="class")
    def backend(self, capability: StateWorkspace) -> WorkspaceBackend:
        return capability.backend()

    @pytest.fixture
    def has_real_posix_shell(self) -> bool:
        return False

    @pytest.fixture
    def fresh_backend(self, capability: StateWorkspace) -> Callable[[], WorkspaceBackend]:
        return capability.backend

    @pytest.fixture
    def attach_backend(
        self, capability: StateWorkspace
    ) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return capability.backend

    @pytest.fixture
    def destroy_environment(
        self, capability: StateWorkspace
    ) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        async def destroy(backend: WorkspaceBackend) -> None:
            assert backend.ref is not None
            await capability.destroy(backend.ref)

        return destroy
