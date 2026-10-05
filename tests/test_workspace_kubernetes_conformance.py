"""Pydantic AI's workspace conformance suite, against pods on a real cluster.

Needs a cluster the current kubeconfig reaches and `pods/exec` on its namespace:
`uv run pytest -m kubernetes tests/test_workspace_kubernetes_conformance.py`.
`KUBERNETES_TEST_NAMESPACE` picks the namespace; `default` otherwise.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterator

import pytest
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from pydantic_ai_backends.workspaces import KubernetesWorkspace, KubernetesWorkspaceBackend

pytestmark = pytest.mark.kubernetes

CAPABILITY = KubernetesWorkspace(
    image="python:3.12-slim", namespace=os.environ.get("KUBERNETES_TEST_NAMESPACE", "default")
)


class TestKubernetesWorkspaceConformance(WorkspaceBackendSuite):
    @pytest.fixture(scope="class")
    def anyio_backend(self) -> str:
        return "asyncio"

    @pytest.fixture(scope="class")
    def backend(self) -> Iterator[WorkspaceBackend]:
        backend = CAPABILITY.backend()
        yield backend
        if backend.ref is not None:
            CAPABILITY._pod(backend.ref.id).stop()

    @pytest.fixture
    def fresh_backend(self) -> Iterator[Callable[[], WorkspaceBackend]]:
        created: list[KubernetesWorkspaceBackend] = []

        def build() -> WorkspaceBackend:
            created.append(CAPABILITY.backend())
            return created[-1]

        yield build
        for backend in created:
            if backend.ref is not None:
                CAPABILITY._pod(backend.ref.id).stop()

    @pytest.fixture
    def attach_backend(self) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return CAPABILITY.backend

    @pytest.fixture
    def destroy_environment(self) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        async def destroy(backend: WorkspaceBackend) -> None:
            assert backend.ref is not None
            await CAPABILITY.destroy(backend.ref)

        return destroy
