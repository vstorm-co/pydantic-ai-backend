"""Pydantic AI's workspace conformance suite, run against a real `sandboxd`.

The service runs in a thread under uvicorn on a free port, with its default
Docker builder, so every request crosses real HTTP into a real container.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from pydantic_ai_backends.remote.server import SandboxdConfig, create_app
from pydantic_ai_backends.workspaces import SandboxdWorkspace, SandboxdWorkspaceBackend

pytestmark = pytest.mark.docker

TOKEN = "conformance-service-token"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def capability(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SandboxdWorkspace]:
    import uvicorn

    workspaces = tmp_path_factory.mktemp("sandboxd-workspaces")
    config = SandboxdConfig(
        token=TOKEN,
        runtimes={"python": "python:3.12-slim"},
        workspace_root=str(Path(workspaces).resolve()),
        prewarm=False,
        execute_timeout=60,
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - only when the service hangs
            raise RuntimeError("sandboxd did not start")
        time.sleep(0.05)
    try:
        yield SandboxdWorkspace(service_url=f"http://127.0.0.1:{port}", token=TOKEN)
    finally:
        server.should_exit = True
        thread.join(timeout=30)


class TestSandboxdWorkspaceConformance(WorkspaceBackendSuite):
    @pytest.fixture(scope="class")
    def anyio_backend(self) -> str:
        return "asyncio"

    # Synchronous fixtures: building a backend does no I/O, and pytest-asyncio's
    # auto mode would otherwise claim a class-scoped async fixture for itself.
    # Sessions left behind go when the service shuts down with the module.
    @pytest.fixture(scope="class")
    def backend(self, capability: SandboxdWorkspace) -> SandboxdWorkspaceBackend:
        return capability.backend()

    @pytest.fixture
    def fresh_backend(self, capability: SandboxdWorkspace) -> Callable[[], WorkspaceBackend]:
        return capability.backend

    @pytest.fixture
    def attach_backend(
        self, capability: SandboxdWorkspace
    ) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return capability.backend

    @pytest.fixture
    def destroy_environment(
        self, capability: SandboxdWorkspace
    ) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        async def destroy(backend: WorkspaceBackend) -> None:
            assert backend.ref is not None
            await capability.destroy(backend.ref)

        return destroy
