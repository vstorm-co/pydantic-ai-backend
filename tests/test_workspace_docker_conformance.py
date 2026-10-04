"""Pydantic AI's workspace conformance suite, run against a real Docker daemon."""

from __future__ import annotations

import gc
from collections.abc import Awaitable, Callable, Iterator

import anyio
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


class TestDockerWorkspaceLifetime:
    """What the conformance suite does not exercise: one backend dropped while another works."""

    @pytest.fixture
    def anyio_backend(self) -> str:
        return "asyncio"

    @pytest.mark.anyio
    async def test_dropping_a_backend_leaves_the_container_running(self) -> None:
        first = CAPABILITY.backend()
        started = await first.run(
            "nohup sleep 300 >/dev/null 2>&1 & echo $! > /tmp/background.pid", shell=True
        )
        assert started.exit_code == 0
        ref = first.ref
        assert ref is not None
        second = CAPABILITY.backend(ref)
        try:
            async with anyio.create_task_group() as group:

                async def in_flight() -> None:
                    result = await second.run(["sh", "-c", "sleep 2; echo done"])
                    assert result.stdout == "done\n"

                group.start_soon(in_flight)
                await anyio.sleep(0.5)
                del first
                gc.collect()

            background = await second.run(["sh", "-c", 'kill -0 "$(cat /tmp/background.pid)"'])
            assert background.exit_code == 0
        finally:
            _remove_container(ref.id)
