"""Pydantic AI's workspace conformance suite, run against a real Docker daemon."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator

import pytest
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from pydantic_ai_backends.workspaces import DockerWorkspace, DockerWorkspaceBackend
from pydantic_ai_backends.workspaces._docker import _remove_container

pytestmark = pytest.mark.docker

CAPABILITY = DockerWorkspace(image="python:3.12-slim")


def _remove(backend: DockerWorkspaceBackend) -> None:
    if backend.ref is not None:
        _remove_container(backend.ref.id)


class TestDockerWorkspaceConformance(WorkspaceBackendSuite):
    @pytest.fixture(scope="class")
    def anyio_backend(self) -> str:
        return "asyncio"

    # Synchronous fixtures: building a backend does no I/O, and pytest-asyncio's
    # auto mode would otherwise claim a class-scoped async fixture for itself.
    @pytest.fixture(scope="class")
    def backend(self) -> Iterator[WorkspaceBackend]:
        backend = CAPABILITY.backend()
        yield backend
        _remove(backend)

    @pytest.fixture
    def fresh_backend(self) -> Iterator[Callable[[], WorkspaceBackend]]:
        created: list[DockerWorkspaceBackend] = []

        def build() -> WorkspaceBackend:
            backend = CAPABILITY.backend()
            created.append(backend)
            return backend

        yield build
        for backend in created:
            _remove(backend)

    @pytest.fixture
    def attach_backend(self) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return CAPABILITY.backend

    @pytest.fixture
    def destroy_environment(self) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        async def destroy(backend: WorkspaceBackend) -> None:
            assert backend.ref is not None
            await CAPABILITY.destroy(backend.ref)

        return destroy
