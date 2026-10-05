"""Pydantic AI's workspace conformance suite, against real Daytona sandboxes.

Creates and deletes sandboxes on the account `DAYTONA_API_KEY` names:
`uv run pytest -m daytona tests/test_workspace_daytona_conformance.py`.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterator

import anyio
import pytest
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from pydantic_ai_backends.workspaces import DaytonaWorkspace, DaytonaWorkspaceBackend

pytestmark = [
    pytest.mark.daytona,
    pytest.mark.skipif(not os.environ.get("DAYTONA_API_KEY"), reason="needs DAYTONA_API_KEY"),
]

CAPABILITY = DaytonaWorkspace()


def _purge(backend: DaytonaWorkspaceBackend) -> None:
    anyio.run(backend.purge)


class TestDaytonaWorkspaceConformance(WorkspaceBackendSuite):
    @pytest.fixture(scope="class")
    def anyio_backend(self) -> str:
        return "asyncio"

    @pytest.fixture(scope="class")
    def backend(self) -> Iterator[WorkspaceBackend]:
        backend = CAPABILITY.backend()
        yield backend
        _purge(backend)

    @pytest.fixture
    def fresh_backend(self) -> Iterator[Callable[[], WorkspaceBackend]]:
        created: list[DaytonaWorkspaceBackend] = []

        def build() -> WorkspaceBackend:
            created.append(CAPABILITY.backend())
            return created[-1]

        yield build
        for backend in created:
            _purge(backend)

    @pytest.fixture
    def attach_backend(self) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return CAPABILITY.backend

    @pytest.fixture
    def destroy_environment(self) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        async def destroy(backend: WorkspaceBackend) -> None:
            assert backend.ref is not None
            await CAPABILITY.destroy(backend.ref)

        return destroy
