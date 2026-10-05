"""`DaytonaWorkspace` against a fake Daytona client.

The real SDK is installed; only its `AsyncDaytona` is replaced, so the request
models and the error types are Daytona's own. Not a substitute for a live
account: `tests/test_workspace_daytona_conformance.py` runs the conformance suite
when `DAYTONA_API_KEY` is set.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

import anyio
import daytona
import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import (
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from pydantic_ai_backends._limits import MAX_RUN_OUTPUT_BYTES
from pydantic_ai_backends.backends._runner import STOPPER
from pydantic_ai_backends.workspaces import DaytonaWorkspace, DaytonaWorkspaceBackend


class _State(enum.Enum):
    STARTED = "started"
    STOPPED = "stopped"
    DESTROYED = "destroyed"


@dataclass
class _Response:
    stdout: str | None = ""
    stderr: str | None = ""
    exit_code: int | None = 0


@dataclass
class _Process:
    sandbox: _Sandbox
    sessions: list[str] = field(default_factory=list)
    requests: list[Any] = field(default_factory=list)
    stoppers: list[str] = field(default_factory=list)
    response: _Response = field(default_factory=_Response)
    hang: bool = False
    error: Exception | None = None

    async def create_session(self, session_id: str) -> None:
        self.sessions.append(session_id)

    async def execute_session_command(self, session_id: str, request: Any) -> _Response:
        assert session_id in self.sessions
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.hang:
            await anyio.sleep(30)
        return self.response

    async def exec(self, command: str, timeout: int | None = None) -> None:
        self.stoppers.append(command)
        raise RuntimeError("a failing stop must not matter")


class _Sandbox:
    def __init__(self, sandbox_id: str, state: _State = _State.STARTED) -> None:
        self.id = sandbox_id
        self.state = state
        self.started = 0
        self.process = _Process(self)

    async def start(self) -> None:
        self.started += 1
        self.state = _State.STARTED

    async def get_work_dir(self) -> str:
        return "/home/daytona"


@dataclass
class _Daytona:
    sandboxes: dict[str, _Sandbox] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    opened: int = 0
    closed: int = 0
    params: list[Any] = field(default_factory=list)

    def __call__(self, config: Any = None) -> _Daytona:
        return self

    async def __aenter__(self) -> _Daytona:
        self.opened += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed += 1

    async def create(self, params: Any = None) -> _Sandbox:
        self.params.append(params)
        sandbox = _Sandbox(f"sbx-{len(self.sandboxes)}")
        self.sandboxes[sandbox.id] = sandbox
        return sandbox

    async def get(self, sandbox_id: str) -> _Sandbox:
        if sandbox_id not in self.sandboxes:
            raise daytona.DaytonaNotFoundError(f"Sandbox {sandbox_id} not found")
        return self.sandboxes[sandbox_id]

    async def delete(self, sandbox: _Sandbox) -> None:
        self.deleted.append(sandbox.id)
        self.sandboxes.pop(sandbox.id, None)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Daytona:
    client = _Daytona()
    monkeypatch.setattr(daytona, "AsyncDaytona", client)
    return client


class TestDaytonaWorkspaceBackend:
    async def test_creates_a_sandbox_and_runs_in_its_work_dir(self, fake: _Daytona) -> None:
        backend = DaytonaWorkspace(env={"A": "1"}, create_params="params").backend()
        assert await backend.working_dir() == "/home/daytona"
        assert backend.ref == WorkspaceRef(provider="daytona", id="sbx-0")
        sandbox = fake.sandboxes["sbx-0"]
        sandbox.process.response = _Response(stdout="out", stderr="err", exit_code=3)

        result = await backend.run(["prog", "a b"], env={"B": "2"})

        assert (result.stdout, result.stderr, result.exit_code) == ("out", "err", 3)
        request = sandbox.process.requests[0]
        assert request.run_async is False and request.suppress_input_echo is True
        assert request.command.endswith("</dev/null")
        assert "/home/daytona" in request.command and "A=1 B=2" in request.command
        assert "prog 'a b'" in request.command
        assert len(sandbox.process.sessions) == 1
        assert fake.params == ["params"]
        assert fake.opened == fake.closed == 2

    async def test_a_ref_attaches_and_starts_a_stopped_sandbox(self, fake: _Daytona) -> None:
        fake.sandboxes["kept"] = _Sandbox("kept", _State.STOPPED)
        backend = DaytonaWorkspace().backend(WorkspaceRef(provider="daytona", id="kept"))
        await backend.run(["true"])
        assert fake.sandboxes["kept"].started == 1
        assert fake.params == []

    @pytest.mark.parametrize(
        ("sandbox", "message"),
        [(None, "no longer exists"), (_Sandbox("gone", _State.DESTROYED), "is destroyed")],
    )
    async def test_a_ref_whose_sandbox_is_gone_is_unavailable(
        self, fake: _Daytona, sandbox: _Sandbox | None, message: str
    ) -> None:
        if sandbox is not None:
            fake.sandboxes["gone"] = sandbox
        backend = DaytonaWorkspace().backend(WorkspaceRef(provider="daytona", id="gone"))
        with pytest.raises(WorkspaceUnavailableError, match=message):
            await backend.working_dir()

    async def test_a_sandbox_lost_mid_command_is_unavailable(self, fake: _Daytona) -> None:
        backend = DaytonaWorkspace().backend()
        await backend.working_dir()
        fake.sandboxes["sbx-0"].process.error = daytona.DaytonaNotFoundError("gone")
        with pytest.raises(WorkspaceUnavailableError, match="went away"):
            await backend.run(["true"])
        assert fake.sandboxes["sbx-0"].process.stoppers

    async def test_a_deadline_stops_the_command(self, fake: _Daytona) -> None:
        backend = DaytonaWorkspace().backend()
        await backend.working_dir()
        process = fake.sandboxes["sbx-0"].process
        process.hang = True
        with pytest.raises(WorkspaceTimeoutError, match="after 0.2 seconds"):
            await backend.run(["sleep", "30"], timeout=0.2)
        assert STOPPER in process.stoppers[0]

    async def test_cancellation_stops_the_command(self, fake: _Daytona) -> None:
        backend = DaytonaWorkspace().backend()
        await backend.working_dir()
        process = fake.sandboxes["sbx-0"].process
        process.hang = True
        with anyio.move_on_after(0.2):
            await backend.run(["sleep", "30"])
        assert process.stoppers

    async def test_output_over_the_limit_is_an_error(self, fake: _Daytona) -> None:
        backend = DaytonaWorkspace().backend()
        await backend.working_dir()
        fake.sandboxes["sbx-0"].process.response = _Response(
            stdout="x" * (MAX_RUN_OUTPUT_BYTES + 1), stderr=None
        )
        with pytest.raises(WorkspaceOutputLimitError):
            await backend.run(["yes"])

    async def test_a_shared_client_is_not_closed(self, fake: _Daytona) -> None:
        backend = DaytonaWorkspace(client=fake).backend()  # type: ignore[arg-type]
        await backend.run(["true"])
        assert fake.opened == fake.closed == 0

    async def test_purge(self, fake: _Daytona) -> None:
        await DaytonaWorkspace().backend().purge()
        backend = DaytonaWorkspace().backend()
        await backend.working_dir()
        await backend.purge()
        await backend.purge()
        assert fake.deleted == ["sbx-0"]

    def test_a_ref_from_another_provider_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'daytona' workspace ref"):
            DaytonaWorkspaceBackend(ref=WorkspaceRef(provider="docker", id="x"))


class TestDaytonaWorkspace:
    async def test_destroy(self, fake: _Daytona) -> None:
        fake.sandboxes["kept"] = _Sandbox("kept")
        await DaytonaWorkspace().destroy(WorkspaceRef(provider="daytona", id="kept"))
        assert fake.deleted == ["kept"]
        with pytest.raises(ValueError, match="'daytona' workspace ref"):
            await DaytonaWorkspace().destroy(WorkspaceRef(provider="docker", id="x"))

    def test_a_foreign_ref_is_left_to_another_capability(self) -> None:
        capability = DaytonaWorkspace()
        ctx: Any = None
        assert capability.get_workspace(ctx, ref=WorkspaceRef(provider="e2b", id="x")) is None
        assert isinstance(capability.get_workspace(ctx, ref=None), DaytonaWorkspaceBackend)

    def test_deferred_loading_is_refused(self) -> None:
        with pytest.raises(UserError, match="defer_loading"):
            DaytonaWorkspace(defer_loading=True)
