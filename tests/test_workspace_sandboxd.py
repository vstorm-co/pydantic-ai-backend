"""`SandboxdWorkspace` against a real `sandboxd`, with no Docker daemon.

The service runs under uvicorn in a thread, over real HTTP. Its sandboxes are
`FakeSandbox`es whose commands really run, in the session's host workspace,
through Pydantic AI's own local backend. That backend passes the conformance
suite itself, so a rule failing here is the HTTP layer's or the client's.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import (
    LocalWorkspaceBackend,
    WorkspaceBackend,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from pydantic_ai_backends.protocol import SandboxUnavailableError
from pydantic_ai_backends.remote import wire
from pydantic_ai_backends.remote.server import SandboxdConfig, create_app
from pydantic_ai_backends.types import CommandOutcome
from pydantic_ai_backends.workspaces import SandboxdWorkspace, SandboxdWorkspaceBackend
from tests.test_remote_sandbox import FakeSandbox, Harness, _open_session, _service_headers

TOKEN = "workspace-service-token"


class LocalRunnerSandbox(FakeSandbox):
    """A `FakeSandbox` that runs commands for real, in its session's directory."""

    def __init__(self, session_id: str, runtime: Any, directory: Path) -> None:
        super().__init__(session_id, runtime)
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.stopped_runs: list[str] = []
        self._runs: dict[str, anyio.CancelScope] = {}

    async def run_command(
        self,
        argv: Sequence[str],
        *,
        run_id: str,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        output_limit: int | None = None,
    ) -> CommandOutcome:
        del output_limit
        backend = LocalWorkspaceBackend(self.directory)
        with anyio.CancelScope() as scope:
            self._runs[run_id] = scope
            try:
                result = await backend.run(list(argv), env=env, timeout=timeout)
            except WorkspaceTimeoutError as error:
                return CommandOutcome(stdout=error.stdout, stderr=error.stderr, timed_out=True)
            except WorkspaceOutputLimitError as error:
                return CommandOutcome(stdout=error.stdout, stderr=error.stderr, output_limited=True)
            except WorkspaceUnavailableError as error:
                raise SandboxUnavailableError(str(error)) from error
            finally:
                self._runs.pop(run_id, None)
        if scope.cancelled_caught:
            return CommandOutcome(stdout="", stderr="", exit_code=143)
        return CommandOutcome(
            stdout=result.stdout, stderr=result.stderr, exit_code=result.exit_code
        )

    async def stop_command(self, run_id: str) -> None:
        self.stopped_runs.append(run_id)
        scope = self._runs.get(run_id)
        if scope is not None:
            scope.cancel()


@dataclass
class Service:
    """A running `sandboxd` and the sandboxes it built."""

    url: str
    root: Path
    built: dict[str, LocalRunnerSandbox] = field(default_factory=dict)

    def capability(self, **kwargs: Any) -> SandboxdWorkspace:
        return SandboxdWorkspace(service_url=self.url, token=TOKEN, **kwargs)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    import uvicorn

    root = Path(tmp_path_factory.mktemp("sandboxd")).resolve()
    port = _free_port()
    running = Service(url=f"http://127.0.0.1:{port}", root=root)

    def build(session_id: str, runtime: Any) -> LocalRunnerSandbox:
        sandbox = LocalRunnerSandbox(session_id, runtime, root / session_id / "workspace")
        running.built[session_id] = sandbox
        return sandbox

    config = SandboxdConfig(
        token=TOKEN,
        runtimes={"python": "python:3.12-slim", "node": "node:20-slim"},
        default_runtime="python",
        workspace_root=str(root),
        execute_timeout=30,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config, sandbox_builder=build),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "sandboxd did not start"
        time.sleep(0.02)
    try:
        yield running
    finally:
        server.should_exit = True
        thread.join(timeout=30)


class TestSandboxdWorkspaceConformance(WorkspaceBackendSuite):
    @pytest.fixture(scope="class")
    def anyio_backend(self) -> str:
        return "asyncio"

    # Synchronous fixtures: building a backend does no I/O, and pytest-asyncio's
    # auto mode would otherwise claim a class-scoped async fixture for itself.
    @pytest.fixture(scope="class")
    def backend(self, service: Service) -> SandboxdWorkspaceBackend:
        return service.capability().backend()

    @pytest.fixture
    def fresh_backend(self, service: Service) -> Callable[[], WorkspaceBackend]:
        return service.capability().backend

    @pytest.fixture
    def attach_backend(self, service: Service) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return service.capability().backend

    @pytest.fixture
    def destroy_environment(
        self, service: Service
    ) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        async def destroy(backend: WorkspaceBackend) -> None:
            assert backend.ref is not None
            await service.capability().destroy(backend.ref)

        return destroy


class TestSandboxdWorkspaceBackend:
    async def test_a_new_session_carries_the_runtime_and_tenant(self, service: Service) -> None:
        backend = service.capability(runtime="node", tenant="acme").backend()
        assert backend.ref is None
        await backend.working_dir()
        assert backend.ref is not None and backend.ref.provider == "sandboxd"
        sandbox = service.built[backend.ref.id]
        assert sandbox.image == "node:20-slim"

    async def test_env_layers_the_call_over_the_capability(self, service: Service) -> None:
        backend = service.capability(env={"BASE": "base", "BOTH": "base"}).backend()
        result = await backend.run(
            ["sh", "-c", 'printf "%s %s" "$BASE" "$BOTH"'], env={"BOTH": "call"}
        )
        assert result.stdout == "base call"

    async def test_a_ref_from_another_provider_is_refused(self, service: Service) -> None:
        with pytest.raises(ValueError, match="'sandboxd' workspace ref"):
            service.capability().backend(WorkspaceRef(provider="docker", id="x"))

    async def test_a_ref_naming_nothing_is_unavailable(self, service: Service) -> None:
        backend = service.capability().backend(WorkspaceRef(provider="sandboxd", id="never-opened"))
        with pytest.raises(WorkspaceUnavailableError, match="no longer exists"):
            await backend.working_dir()

    async def test_a_closed_session_with_kept_files_attaches(self, service: Service) -> None:
        first = service.capability().backend()
        await first.run(["sh", "-c", "printf kept > note.txt"])
        assert first.ref is not None
        # Closed without purging, as an idle reaper would: the files stay.
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{service.url}/sessions/{first.ref.id}", headers={wire.TOKEN_HEADER: TOKEN}
            )
        assert response.status_code == 204
        again = service.capability().backend(first.ref)
        assert (await again.run(["cat", "note.txt"])).stdout == "kept"

    async def test_a_timeout_reports_its_own_seconds(self, service: Service) -> None:
        backend = service.capability().backend()
        with pytest.raises(WorkspaceTimeoutError, match="after 0.5 seconds"):
            await backend.run(["sh", "-c", "printf partial; sleep 30"], timeout=0.5)

    async def test_an_unexpected_status_propagates(self, service: Service) -> None:
        backend = service.capability(runtime="perl").backend()
        with pytest.raises(httpx.HTTPStatusError):
            await backend.working_dir()

    async def test_a_cancelled_command_is_stopped_on_the_service(self, service: Service) -> None:
        backend = service.capability().backend()
        await backend.working_dir()
        assert backend.ref is not None
        with anyio.move_on_after(0.5):
            await backend.run(["sleep", "30"])
        sandbox = service.built[backend.ref.id]
        assert len(sandbox.stopped_runs) == 1

    async def test_a_shared_client_is_used_and_left_open(self, service: Service) -> None:
        async with httpx.AsyncClient() as client:
            backend = service.capability(client=client).backend()
            assert (await backend.run(["printf", "shared"])).stdout == "shared"
            assert not client.is_closed

    async def test_purging_twice_is_fine(self, service: Service) -> None:
        capability = service.capability()
        backend = capability.backend()
        await backend.working_dir()
        assert backend.ref is not None
        await capability.destroy(backend.ref)
        await capability.destroy(backend.ref)
        assert not (service.root / backend.ref.id).exists()

    async def test_destroy_refuses_another_providers_ref(self, service: Service) -> None:
        with pytest.raises(ValueError, match="'sandboxd' workspace ref"):
            await service.capability().destroy(WorkspaceRef(provider="docker", id="x"))


class TestSandboxdWorkspaceCapability:
    def test_a_foreign_ref_is_left_to_another_capability(self, service: Service) -> None:
        capability = service.capability(provider="sandboxd:eu")
        ctx: Any = None
        assert capability.get_workspace(ctx, ref=WorkspaceRef(provider="sandboxd", id="x")) is None
        own = capability.get_workspace(ctx, ref=WorkspaceRef(provider="sandboxd:eu", id="x"))
        assert isinstance(own, SandboxdWorkspaceBackend)
        assert isinstance(capability.get_workspace(ctx, ref=None), SandboxdWorkspaceBackend)

    def test_deferred_loading_is_refused(self) -> None:
        with pytest.raises(UserError, match="defer_loading"):
            SandboxdWorkspace(service_url="http://x", token="t", defer_loading=True)

    def test_the_token_stays_out_of_its_repr(self) -> None:
        assert "secret" not in repr(SandboxdWorkspace(service_url="http://x", token="secret"))


def _answering(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _opened(request: httpx.Request) -> httpx.Response | None:
    """The two answers every session open gets, or `None` for anything else."""
    if request.url.path == "/sessions" and request.method == "POST":
        created = wire.SessionCreated(
            session=wire.SessionInfo(
                session_id="s-1",
                runtime="python",
                alive=True,
                created_at=0,
                last_activity=0,
                idle_seconds=0,
            ),
            token="session-token",
        )
        return httpx.Response(200, content=created.model_dump_json())
    if request.url.path == "/policy":
        return httpx.Response(200, content=wire.ServicePolicy(execute_timeout=60).model_dump_json())
    return None


class TestSandboxdWorkspaceAnswers:
    """What the client makes of answers a healthy service would not give."""

    async def test_a_malformed_run_answer_is_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _opened(request) or httpx.Response(200, json={"unexpected": True})

        async with _answering(handler) as client:
            backend = SandboxdWorkspaceBackend("http://sandboxd", token="t", client=client)
            with pytest.raises(ValueError, match="answered /run"):
                await backend.run(["true"])

    async def test_a_vanished_sandbox_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _opened(request) or httpx.Response(410, text="sandbox gone")

        async with _answering(handler) as client:
            backend = SandboxdWorkspaceBackend("http://sandboxd", token="t", client=client)
            with pytest.raises(WorkspaceUnavailableError, match="sandbox gone"):
                await backend.run(["true"])

    async def test_output_over_the_limit_is_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            ran = wire.RunResponse(stdout="x", stderr="", output_limited=True, output_limit=8)
            return _opened(request) or httpx.Response(200, content=ran.model_dump_json())

        async with _answering(handler) as client:
            backend = SandboxdWorkspaceBackend("http://sandboxd", token="t", client=client)
            with pytest.raises(WorkspaceOutputLimitError) as caught:
                await backend.run(["yes"])
        assert caught.value.limit == 8

    async def test_the_service_ceiling_names_itself(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            ran = wire.RunResponse(stdout="", stderr="", timed_out=True)
            return _opened(request) or httpx.Response(200, content=ran.model_dump_json())

        async with _answering(handler) as client:
            backend = SandboxdWorkspaceBackend("http://sandboxd", token="t", client=client)
            with pytest.raises(WorkspaceTimeoutError, match="sandbox's time limit"):
                await backend.run(["sleep", "999"])

    async def test_a_timeout_past_the_ceiling_names_the_ceiling(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            ran = wire.RunResponse(stdout="", stderr="", timed_out=True)
            return _opened(request) or httpx.Response(200, content=ran.model_dump_json())

        async with _answering(handler) as client:
            backend = SandboxdWorkspaceBackend("http://sandboxd", token="t", client=client)
            # The service's ceiling is 60 seconds, so the 120 asked for never applied.
            with pytest.raises(WorkspaceTimeoutError, match="sandbox's time limit"):
                await backend.run(["sleep", "999"], timeout=120)

    async def test_an_unresolvable_working_dir_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            ran = wire.RunResponse(stdout="", stderr="pwd: broken", exit_code=1)
            return _opened(request) or httpx.Response(200, content=ran.model_dump_json())

        async with _answering(handler) as client:
            backend = SandboxdWorkspaceBackend("http://sandboxd", token="t", client=client)
            with pytest.raises(WorkspaceUnavailableError, match="pwd: broken"):
                await backend.working_dir()

    async def test_a_refused_purge_propagates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _opened(request) or httpx.Response(500)

        async with _answering(handler) as client:
            backend = SandboxdWorkspaceBackend("http://sandboxd", token="t", client=client)
            with pytest.raises(httpx.HTTPStatusError):
                await backend.purge()


class TestRunRoutesOnTheService:
    """What `/run`, `/runs/{id}/stop` and `attach` answer, in-process."""

    @pytest.fixture
    def harness(self) -> Harness:
        return Harness()

    @pytest.fixture
    def client(self, harness: Harness) -> Iterator[TestClient]:
        with harness.client() as client:
            yield client

    def test_attach_needs_a_session_id(self, client: TestClient) -> None:
        response = client.post("/sessions", json={"attach": True}, headers=_service_headers())
        assert response.status_code == 400

    def test_attach_without_kept_workspaces_finds_nothing(self, client: TestClient) -> None:
        response = client.post(
            "/sessions", json={"session_id": "never", "attach": True}, headers=_service_headers()
        )
        assert response.status_code == 404

    @pytest.fixture
    def runnerless(self) -> Iterator[TestClient]:
        """A service whose custom builder returns sandboxes that cannot run commands."""

        class Runnerless:
            def __init__(self, session_id: str, runtime: Any) -> None:
                self.session_id = session_id
                self.image = runtime.image_label()

            def start(self) -> None: ...

            def is_alive(self) -> bool:
                return True

            def stop(self, remove: bool = False) -> None: ...

        config = SandboxdConfig(token=TOKEN, runtimes={"python": "python:3.12-slim"})
        with TestClient(create_app(config, sandbox_builder=Runnerless)) as client:
            yield client

    def test_a_sandbox_without_a_runner_cannot_run(self, runnerless: TestClient) -> None:
        headers = {wire.TOKEN_HEADER: TOKEN}
        created = runnerless.post("/sessions", json={}, headers=headers).json()
        session_id = created["session"]["session_id"]
        response = runnerless.post(
            f"/sessions/{session_id}/run",
            json={"argv": ["true"], "run_id": "0" * 32},
            headers=headers,
        )
        assert response.status_code == 501
        assert "Runnerless cannot run commands" in response.text
        stopped = runnerless.post(f"/sessions/{session_id}/runs/{'0' * 32}/stop", headers=headers)
        assert stopped.status_code == 204

    def test_stopping_a_run_reaches_the_sandbox(self, client: TestClient, harness: Harness) -> None:
        session_id, token = _open_session(client)
        response = client.post(
            f"/sessions/{session_id}/runs/{'0' * 32}/stop", headers={wire.TOKEN_HEADER: token}
        )
        assert response.status_code == 204
        assert harness.built[session_id].stopped_runs == ["0" * 32]

    def test_a_run_id_must_be_a_uuid_hex(self, client: TestClient) -> None:
        session_id, token = _open_session(client)
        # It names a pid file inside the sandbox, so nothing else gets through.
        response = client.post(
            f"/sessions/{session_id}/runs/not-a-run/stop", headers={wire.TOKEN_HEADER: token}
        )
        assert response.status_code == 422


class TestPurgeRaces:
    async def test_a_session_closed_meanwhile_counts_as_purged(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _opened(request) or httpx.Response(404, text="No such session")

        async with _answering(handler) as client:
            await SandboxdWorkspaceBackend("http://sandboxd", token="t", client=client).purge()
