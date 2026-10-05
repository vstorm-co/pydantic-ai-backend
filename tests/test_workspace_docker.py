"""The container command primitive and `DockerWorkspace`, against a fake daemon.

The real daemon is covered by `test_workspace_docker_conformance.py`, which needs
one; these pin every branch without it — the daemon's errors above all, which a
healthy daemon will not produce on request.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio
import docker.errors
import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import (
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from pydantic_ai_backends.backends.docker import _exec
from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox
from pydantic_ai_backends.protocol import CommandRunner, SandboxUnavailableError
from pydantic_ai_backends.types import CommandOutcome
from pydantic_ai_backends.workspaces import _docker
from pydantic_ai_backends.workspaces._commands import check_timeout, command_argv, command_result
from pydantic_ai_backends.workspaces._docker import (
    CONTAINER_PREFIX,
    DockerWorkspace,
    DockerWorkspaceBackend,
)

KEPT = f"{CONTAINER_PREFIX}{'b' * 16}"
"""A name `DockerWorkspaceBackend` could have given a container it created."""


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.reason = "fake"
        self.url = "http://docker/fake"


def _api_error(status: int) -> docker.errors.APIError:
    return docker.errors.APIError("refused", response=_Response(status))  # type: ignore[arg-type]


class _Stream:
    """An exec's output: chunks, then optionally a block until released."""

    def __init__(self, chunks: Sequence[tuple[bytes | None, bytes | None]], hold: bool) -> None:
        self._chunks = list(chunks)
        self.hold = hold
        self.released = threading.Event()
        self.closed = False

    def __iter__(self) -> Iterator[tuple[bytes | None, bytes | None]]:
        yield from self._chunks
        if self.hold:
            self.released.wait(timeout=10)
            yield (b"late", None)

    def close(self) -> None:
        self.closed = True
        raise OSError("Socket is not connected")


@dataclass
class _Api:
    chunks: list[tuple[bytes | None, bytes | None]] = field(default_factory=list)
    exit_code: int | None = 0
    hold: bool = False
    create_error: Exception | None = None
    start_error: Exception | None = None
    inspect_error: Exception | None = None
    created: list[dict[str, Any]] = field(default_factory=list)
    stream: _Stream | None = None

    def exec_create(self, container_id: str, cmd: list[str], **kwargs: Any) -> dict[str, str]:
        if self.create_error is not None:
            raise self.create_error
        self.created.append({"container": container_id, "cmd": cmd, **kwargs})
        return {"Id": "exec-1"}

    def exec_start(self, exec_id: str, *, stream: bool, demux: bool) -> _Stream:
        assert (exec_id, stream, demux) == ("exec-1", True, True)
        if self.start_error is not None:
            raise self.start_error
        self.stream = _Stream(self.chunks, self.hold)
        return self.stream

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        if self.inspect_error is not None:
            raise self.inspect_error
        return {"ExitCode": self.exit_code}


@dataclass
class _Container:
    api: _Api
    id: str = "container-1"
    status: str = "running"
    reload_error: Exception | None = None
    stops: list[list[str]] = field(default_factory=list)
    removed: bool = False

    def exec_run(self, cmd: list[str], **kwargs: Any) -> Any:
        self.stops.append(cmd)
        if self.api.stream is not None:
            self.api.stream.released.set()
        raise RuntimeError("a stopper that fails must not matter")

    def reload(self) -> None:
        if self.reload_error is not None:
            raise self.reload_error

    def remove(self, *, force: bool) -> None:
        assert force
        self.removed = True


@dataclass
class _Containers:
    known: dict[str, _Container] = field(default_factory=dict)

    def get(self, name: str) -> _Container:
        if name not in self.known:
            raise docker.errors.NotFound("no such container")
        return self.known[name]


@dataclass
class _Client:
    api: _Api
    containers: _Containers = field(default_factory=_Containers)


@pytest.fixture
def api() -> _Api:
    return _Api()


@pytest.fixture
def client(api: _Api, monkeypatch: pytest.MonkeyPatch) -> _Client:
    fake = _Client(api)
    monkeypatch.setattr(_exec, "docker_client", lambda: fake)
    monkeypatch.setattr(_docker, "docker_client", lambda: fake)
    return fake


@pytest.fixture
def container(api: _Api, client: _Client) -> _Container:
    del client
    return _Container(api)


class TestRunInContainer:
    async def test_streams_come_back_apart(self, api: _Api, container: _Container) -> None:
        api.chunks = [(b"out", None), (None, b"err"), (b"\xff", None)]
        api.exit_code = 3
        outcome = await _exec.run_in_container(
            container,  # type: ignore[arg-type]
            ["prog", "a b"],
            run_id="r1",
            env={"K": "V"},
            workdir="/workspace",
        )
        assert outcome == CommandOutcome(stdout="out�", stderr="err", exit_code=3)
        created = api.created[0]
        assert created["cmd"] == _exec.wrapped_argv(["prog", "a b"], "r1")
        assert created["cmd"][-2:] == ["prog", "a b"]
        assert (created["environment"], created["workdir"], created["stdin"]) == (
            {"K": "V"},
            "/workspace",
            False,
        )

    async def test_no_env_sends_none(self, api: _Api, container: _Container) -> None:
        await _exec.run_in_container(container, ["true"], run_id="r1")  # type: ignore[arg-type]
        assert api.created[0]["environment"] is None

    async def test_output_over_the_limit_stops_the_command(
        self, api: _Api, container: _Container
    ) -> None:
        api.chunks = [(b"12", b"34"), (b"5", None), (b"never read", None)]
        outcome = await _exec.run_in_container(
            container,  # type: ignore[arg-type]
            ["yes"],
            run_id="r1",
            output_limit=4,
        )
        assert (outcome.output_limited, outcome.exit_code, outcome.stdout) == (True, None, "125")
        assert api.stream is not None and api.stream.closed
        assert container.stops[0][-1] == _exec.pid_file("r1")

    async def test_a_deadline_stops_the_command(self, api: _Api, container: _Container) -> None:
        api.chunks = [(b"partial", None)]
        api.hold = True
        outcome = await _exec.run_in_container(
            container,  # type: ignore[arg-type]
            ["sleep", "9"],
            run_id="r1",
            timeout=0.2,
        )
        assert (outcome.timed_out, outcome.stdout) == (True, "partial")
        assert container.stops

    async def test_cancellation_stops_the_command(self, api: _Api, container: _Container) -> None:
        api.hold = True
        with anyio.move_on_after(0.2):
            await _exec.run_in_container(container, ["sleep", "9"], run_id="r1")  # type: ignore[arg-type]
        assert container.stops

    @pytest.mark.parametrize(
        ("stage", "error"),
        [
            ("create_error", docker.errors.NotFound("gone")),
            ("create_error", _api_error(409)),
            ("start_error", docker.errors.NotFound("gone")),
            ("inspect_error", docker.errors.NotFound("gone")),
        ],
    )
    async def test_a_vanished_container(
        self, api: _Api, container: _Container, stage: str, error: Exception
    ) -> None:
        setattr(api, stage, error)
        with pytest.raises(SandboxUnavailableError):
            await _exec.run_in_container(container, ["true"], run_id="r1")  # type: ignore[arg-type]

    async def test_other_daemon_errors_propagate(self, api: _Api, container: _Container) -> None:
        api.create_error = _api_error(500)
        with pytest.raises(docker.errors.APIError):
            await _exec.run_in_container(container, ["true"], run_id="r1")  # type: ignore[arg-type]

    async def test_no_exit_code_means_the_container_went(
        self, api: _Api, container: _Container
    ) -> None:
        api.exit_code = None
        with pytest.raises(SandboxUnavailableError):
            await _exec.run_in_container(container, ["true"], run_id="r1")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("status", "reload_error", "gone"),
        [
            ("running", None, False),
            ("exited", None, True),
            ("running", docker.errors.NotFound("gone"), True),
        ],
    )
    async def test_a_signal_exit_is_a_result_only_in_a_live_container(
        self,
        api: _Api,
        container: _Container,
        status: str,
        reload_error: Exception | None,
        gone: bool,
    ) -> None:
        api.exit_code = 137
        container.status = status
        container.reload_error = reload_error
        if gone:
            with pytest.raises(SandboxUnavailableError):
                await _exec.run_in_container(container, ["sleep"], run_id="r1")  # type: ignore[arg-type]
        else:
            outcome = await _exec.run_in_container(container, ["sleep"], run_id="r1")  # type: ignore[arg-type]
            assert outcome.exit_code == 137

    async def test_an_empty_argv_is_refused(self, container: _Container) -> None:
        with pytest.raises(ValueError, match="name a program"):
            await _exec.run_in_container(container, [], run_id="r1")  # type: ignore[arg-type]


class TestDockerSandboxRunsCommands:
    def test_it_is_a_command_runner(self) -> None:
        sandbox = DockerSandbox(work_dir="/srv")
        assert isinstance(sandbox, CommandRunner)
        assert sandbox.work_dir == "/srv"

    async def test_runs_in_its_work_dir(self, api: _Api, container: _Container) -> None:
        sandbox = DockerSandbox(work_dir="/srv")
        sandbox._container = container  # type: ignore[assignment]
        api.chunks = [(b"ok", None)]
        outcome = await sandbox.run_command(["true"], run_id="r1", output_limit=100)
        assert outcome.stdout == "ok"
        assert api.created[0]["workdir"] == "/srv"
        await sandbox.stop_command("r1")
        assert container.stops

    async def test_stopping_before_a_container_exists_does_nothing(self) -> None:
        await DockerSandbox().stop_command("r1")


@dataclass
class _FakeSandbox:
    """What `DockerWorkspaceBackend` needs of a `DockerSandbox`."""

    name: str
    outcome: CommandOutcome = field(
        default_factory=lambda: CommandOutcome(stdout="ok", stderr="", exit_code=0)
    )
    error: Exception | None = None
    started: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    work_dir: str = "/workspace"

    def start(self) -> None:
        self.started += 1

    async def run_command(
        self,
        argv: Sequence[str],
        *,
        run_id: str,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandOutcome:
        self.calls.append({"argv": list(argv), "run_id": run_id, "env": env, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.outcome


@dataclass
class _Factory:
    built: list[_FakeSandbox] = field(default_factory=list)

    def __call__(self, name: str) -> Any:
        sandbox = _FakeSandbox(name)
        self.built.append(sandbox)
        return sandbox


class TestDockerWorkspaceBackend:
    async def test_creates_a_named_container_on_first_use(self) -> None:
        factory = _Factory()
        backend = DockerWorkspaceBackend(sandbox_factory=factory, env={"A": "1", "B": "1"})
        assert backend.ref is None
        assert await backend.working_dir() == "/workspace"
        assert backend.ref is not None and backend.ref.id.startswith(CONTAINER_PREFIX)
        result = await backend.run("echo hi", shell=True, env={"B": "2"}, timeout=5)
        assert result.stdout == "ok"
        call = factory.built[0].calls[0]
        assert call["argv"] == ["/bin/sh", "-c", "echo hi"]
        assert (call["env"], call["timeout"]) == ({"A": "1", "B": "2"}, 5)
        assert len(factory.built) == 1 and factory.built[0].started == 1

    @pytest.mark.parametrize("status", ["running", "exited"])
    async def test_attaches_to_a_container_that_is_there(
        self, client: _Client, status: str
    ) -> None:
        client.containers.known[KEPT] = _Container(client.api, status=status)
        factory = _Factory()
        backend = DockerWorkspaceBackend(
            sandbox_factory=factory, ref=WorkspaceRef(provider="docker", id=KEPT)
        )
        await backend.run(["true"])
        assert factory.built[0].name == KEPT
        assert backend.ref == WorkspaceRef(provider="docker", id=KEPT)

    @pytest.mark.parametrize(
        ("status", "message"), [(None, "no longer exists"), ("dead", "is dead")]
    )
    async def test_refuses_to_replace_a_container_that_is_gone(
        self, client: _Client, status: str | None, message: str
    ) -> None:
        if status is not None:
            client.containers.known[KEPT] = _Container(client.api, status=status)
        backend = DockerWorkspaceBackend(
            sandbox_factory=_Factory(), ref=WorkspaceRef(provider="docker", id=KEPT)
        )
        with pytest.raises(WorkspaceUnavailableError, match=message):
            await backend.working_dir()

    @pytest.mark.parametrize(
        "name", ["postgres", f"{CONTAINER_PREFIX}../x", f"{CONTAINER_PREFIX}{'a' * 16}-more"]
    )
    async def test_a_ref_never_reaches_a_container_it_did_not_create(
        self, client: _Client, name: str
    ) -> None:
        client.containers.known[name] = _Container(client.api)
        factory = _Factory()
        backend = DockerWorkspaceBackend(
            sandbox_factory=factory, ref=WorkspaceRef(provider="docker", id=name)
        )
        with pytest.raises(WorkspaceUnavailableError, match="not created by a DockerWorkspace"):
            await backend.run(["true"])
        assert factory.built == []

    async def test_a_ref_from_another_provider_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'docker' workspace ref"):
            DockerWorkspaceBackend(
                sandbox_factory=_Factory(), ref=WorkspaceRef(provider="sandboxd", id="x")
            )

    async def test_errors_take_the_workspace_types(self) -> None:
        factory = _Factory()
        backend = DockerWorkspaceBackend(sandbox_factory=factory)
        await backend.working_dir()
        sandbox = factory.built[0]

        sandbox.error = SandboxUnavailableError("gone")
        with pytest.raises(WorkspaceUnavailableError, match="gone"):
            await backend.run(["true"])

        sandbox.error = None
        sandbox.outcome = CommandOutcome(stdout="p", stderr="", timed_out=True)
        with pytest.raises(WorkspaceTimeoutError, match="after 2 seconds"):
            await backend.run(["sleep", "9"], timeout=2)

        sandbox.outcome = CommandOutcome(stdout="", stderr="", output_limited=True)
        with pytest.raises(WorkspaceOutputLimitError):
            await backend.run(["yes"])


class TestDockerWorkspace:
    def test_builds_the_sandbox_it_describes(self) -> None:
        capability = DockerWorkspace(
            image="alpine:3.20", work_dir="/srv", network_mode="none", mem_limit="256m"
        )
        sandbox = capability._sandbox("name")
        assert (sandbox.work_dir, sandbox._container_name, sandbox._network_mode) == (
            "/srv",
            "name",
            "none",
        )
        assert (sandbox._image, sandbox._mem_limit) == ("alpine:3.20", "256m")

    def test_a_foreign_ref_is_left_to_another_capability(self) -> None:
        capability = DockerWorkspace()
        ctx: Any = None
        assert capability.get_workspace(ctx, ref=WorkspaceRef(provider="e2b", id="x")) is None
        own = capability.get_workspace(ctx, ref=WorkspaceRef(provider="docker", id="x"))
        assert isinstance(own, DockerWorkspaceBackend)
        assert isinstance(capability.get_workspace(ctx, ref=None), DockerWorkspaceBackend)

    def test_deferred_loading_is_refused(self) -> None:
        with pytest.raises(UserError, match="defer_loading"):
            DockerWorkspace(defer_loading=True)

    async def test_destroy_removes_the_container(self, client: _Client) -> None:
        kept = _Container(client.api)
        client.containers.known[KEPT] = kept
        await DockerWorkspace().destroy(WorkspaceRef(provider="docker", id=KEPT))
        assert kept.removed
        # Gone already is fine.
        await DockerWorkspace().destroy(
            WorkspaceRef(provider="docker", id=f"{CONTAINER_PREFIX}{'0' * 16}")
        )

    async def test_destroy_never_removes_a_container_it_did_not_create(
        self, client: _Client
    ) -> None:
        database = _Container(client.api)
        client.containers.known["postgres"] = database
        with pytest.raises(ValueError, match="not created by a DockerWorkspace"):
            await DockerWorkspace().destroy(WorkspaceRef(provider="docker", id="postgres"))
        assert not database.removed

    async def test_destroy_refuses_another_providers_ref(self) -> None:
        with pytest.raises(ValueError, match="'docker' workspace ref"):
            await DockerWorkspace().destroy(WorkspaceRef(provider="e2b", id="x"))


class TestCommandContract:
    def test_the_command_form_must_match_shell(self) -> None:
        assert command_argv("ls -la", True) == ["/bin/sh", "-c", "ls -la"]
        assert command_argv(("ls", "-la"), False) == ["ls", "-la"]
        with pytest.raises(TypeError):
            command_argv("ls", False)
        with pytest.raises(TypeError):
            command_argv(["ls"], True)
        with pytest.raises(TypeError):
            command_argv(b"ls", True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            command_argv([], False)

    @pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "5"])
    def test_a_timeout_must_be_a_positive_finite_number(self, timeout: Any) -> None:
        with pytest.raises(ValueError):
            check_timeout(timeout)

    def test_a_valid_timeout_passes(self) -> None:
        check_timeout(None)
        check_timeout(0.5)

    def test_a_finished_outcome_needs_an_exit_code(self) -> None:
        with pytest.raises(ValueError, match="exit code"):
            command_result(CommandOutcome(stdout="", stderr=""), timeout=None, limit=1)


class TestAConfiguredContainer:
    """A container named by whoever configures the workspace, for every run."""

    async def test_is_created_under_its_name_on_first_use(self, client: _Client) -> None:
        factory = _Factory()
        backend = DockerWorkspaceBackend(sandbox_factory=factory, container_name="project-box")
        await backend.run(["true"])
        assert factory.built[0].name == "project-box"
        assert backend.ref == WorkspaceRef(provider="docker", id="project-box")

    async def test_a_ref_to_it_attaches_while_it_is_there(self, client: _Client) -> None:
        client.containers.known["project-box"] = _Container(client.api, status="exited")
        factory = _Factory()
        backend = DockerWorkspaceBackend(
            sandbox_factory=factory,
            ref=WorkspaceRef(provider="docker", id="project-box"),
            container_name="project-box",
        )
        await backend.run(["true"])
        assert factory.built[0].name == "project-box"

    async def test_a_ref_to_it_once_it_is_gone_is_unavailable(self, client: _Client) -> None:
        backend = DockerWorkspaceBackend(
            sandbox_factory=_Factory(),
            ref=WorkspaceRef(provider="docker", id="project-box"),
            container_name="project-box",
        )
        with pytest.raises(WorkspaceUnavailableError, match="no longer exists"):
            await backend.working_dir()

    async def test_no_ref_reaches_another_container(self, client: _Client) -> None:
        client.containers.known["postgres"] = _Container(client.api)
        backend = DockerWorkspaceBackend(
            sandbox_factory=_Factory(),
            ref=WorkspaceRef(provider="docker", id="postgres"),
            container_name="project-box",
        )
        with pytest.raises(WorkspaceUnavailableError, match="not created by a DockerWorkspace"):
            await backend.working_dir()

    def test_the_capability_leaves_other_refs_alone(self) -> None:
        capability = DockerWorkspace(container_name="project-box")
        ctx: Any = None
        assert capability.get_workspace(ctx, ref=WorkspaceRef(provider="docker", id="x")) is None
        own = capability.get_workspace(ctx, ref=WorkspaceRef(provider="docker", id="project-box"))
        assert isinstance(own, DockerWorkspaceBackend)

    async def test_destroy_removes_it(self, client: _Client) -> None:
        box = _Container(client.api)
        client.containers.known["project-box"] = box
        await DockerWorkspace(container_name="project-box").destroy(
            WorkspaceRef(provider="docker", id="project-box")
        )
        assert box.removed

    def test_volumes_reach_the_sandbox(self) -> None:
        sandbox = DockerWorkspace(volumes={"/host/project": "/workspace"})._sandbox("name")
        assert sandbox._volumes == {"/host/project": "/workspace"}
