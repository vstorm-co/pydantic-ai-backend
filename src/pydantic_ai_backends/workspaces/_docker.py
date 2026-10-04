"""A Docker container as a Pydantic AI workspace."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import anyio
import anyio.to_thread
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import (
    CommandResult,
    SupportsCommands,
    WorkspaceBackend,
    WorkspaceCommand,
    WorkspaceRef,
    WorkspaceUnavailableError,
)

from pydantic_ai_backends._limits import MAX_RUN_OUTPUT_BYTES
from pydantic_ai_backends.backends.docker._client import docker_client
from pydantic_ai_backends.backends.docker.sandbox import REATTACHABLE_STATUSES, DockerSandbox
from pydantic_ai_backends.protocol import SandboxUnavailableError
from pydantic_ai_backends.types import RuntimeConfig
from pydantic_ai_backends.workspaces._commands import (
    check_timeout,
    command_argv,
    command_result,
    layered_env,
)

DOCKER_PROVIDER = "docker"
"""`WorkspaceRef.provider` of a container workspace."""

CONTAINER_PREFIX = "pydantic-ai-workspace-"
"""Name prefix of the containers a :class:`DockerWorkspace` creates."""

SandboxFactory = Callable[[str], DockerSandbox]
"""Builds the sandbox for a container name, without starting it."""


def _container_status(name: str) -> str | None:
    """The status of the container called `name`, or `None` when there is none."""
    import docker.errors

    try:
        container = docker_client().containers.get(name)
    except docker.errors.NotFound:
        return None
    status: str = container.status
    return status


def _remove_container(name: str) -> None:
    """Remove the container called `name` with everything in it; gone already is fine."""
    import docker.errors

    with contextlib.suppress(docker.errors.NotFound):
        docker_client().containers.get(name).remove(force=True)


class DockerWorkspaceBackend(WorkspaceBackend, SupportsCommands):
    """One Docker container, as the environment an agent run works in.

    Commands only: `Workspace` derives the file operations through the shell,
    which keeps the one failure contract for both. The container is named after
    the ref and is never auto-removed, so a later run can attach to it and find
    its files, installed packages included; it lives until :meth:`DockerWorkspace.destroy`.

    Args:
        sandbox_factory: Builds the `DockerSandbox` for a container name. Holds
            the image, runtime and limits, and must not start the container.
        ref: The workspace to attach to; `None` creates one on first use.
        env: Variables every command gets, under any a call passes.
    """

    def __init__(
        self,
        *,
        sandbox_factory: SandboxFactory,
        ref: WorkspaceRef | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if ref is not None and ref.provider != DOCKER_PROVIDER:
            raise ValueError(f"expected a {DOCKER_PROVIDER!r} workspace ref, got {ref.provider!r}")
        self._factory = sandbox_factory
        self._ref = ref
        self._env = dict(env) if env else None
        self._sandbox: DockerSandbox | None = None
        self._lock = anyio.Lock()

    @property
    def ref(self) -> WorkspaceRef | None:
        """The container's name once it exists; `None` before the first operation."""
        return self._ref

    async def _connect(self) -> DockerSandbox:
        """The started sandbox, creating or attaching on the first call."""
        async with self._lock:
            if self._sandbox is not None:
                return self._sandbox
            if self._ref is None:
                name = f"{CONTAINER_PREFIX}{uuid.uuid4().hex[:16]}"
                sandbox = self._factory(name)
                # Shielded: the thread creating the container runs to completion
                # either way, and a caller cancelled meanwhile must still leave
                # the ref behind, or nothing could ever remove that container.
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(sandbox.start)
                    self._ref = WorkspaceRef(provider=DOCKER_PROVIDER, id=name)
            else:
                status = await anyio.to_thread.run_sync(_container_status, self._ref.id)
                if status != "running" and status not in REATTACHABLE_STATUSES:
                    raise WorkspaceUnavailableError(
                        f"container {self._ref.id!r} no longer exists"
                        if status is None
                        else f"container {self._ref.id!r} is {status}"
                    )
                sandbox = self._factory(self._ref.id)
                await anyio.to_thread.run_sync(sandbox.start)
            self._sandbox = sandbox
            return sandbox

    async def working_dir(self) -> str:
        """The sandbox's work directory, which commands start in."""
        return (await self._connect()).work_dir

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command in the container, stdin at EOF; see `SupportsCommands.run`.

        Raises:
            WorkspaceUnavailableError: The container is gone, before or during it.
            WorkspaceTimeoutError: The command reached `timeout`.
            WorkspaceOutputLimitError: Its combined output passed 10 MiB.
        """
        argv = command_argv(command, shell)
        check_timeout(timeout)
        sandbox = await self._connect()
        try:
            outcome = await sandbox.run_command(
                argv,
                run_id=uuid.uuid4().hex,
                env=layered_env(self._env, env),
                timeout=timeout,
            )
        except SandboxUnavailableError as error:
            raise WorkspaceUnavailableError(str(error)) from error
        return command_result(outcome, timeout=timeout, limit=MAX_RUN_OUTPUT_BYTES)


@dataclass(kw_only=True)
class DockerWorkspace(AbstractCapability[object]):
    """Supply a Docker container on this host as the run's workspace.

    A run with no ref creates a container; one carrying a `"docker"` ref attaches
    to that container, starting it if it was stopped. Containers are kept after
    the run — the ref is how to come back to them, and :meth:`destroy` is how to
    remove one.

    This supplies the environment only. Compose it with something that uses the
    workspace: `Coder`, `Shell` or `FileSystem` from the Pydantic AI harness, or
    this library's `ConsoleCapability(use_workspace=True)`.

    A container is only as isolated as its runtime: Docker's default `runc`
    shares the host kernel. See `oci_runtime`.

    Example:
        ```python
        from pydantic_ai import Agent
        from pydantic_ai_harness.coder import Coder

        from pydantic_ai_backends.workspaces import DockerWorkspace

        agent = Agent("anthropic:claude-opus-5-5", capabilities=[DockerWorkspace(), Coder()])
        ```
    """

    image: str = "python:3.12-slim"
    """Image for a new container. Ignored when `runtime` is given."""

    runtime: RuntimeConfig | str | None = None
    """A `RuntimeConfig` or the name of a built-in runtime, which also sets the work directory."""

    work_dir: str = "/workspace"
    """Directory commands start in. Ignored when `runtime` is given."""

    network_mode: str | None = None
    """Docker network mode; `"none"` keeps the container off the network."""

    mem_limit: str | None = None
    """Memory ceiling in Docker syntax, such as `"512m"`."""

    cpus: float | None = None
    """Hard CPU ceiling in cores."""

    oci_runtime: str | None = None
    """Low-level runtime, Docker's `--runtime`: `"runsc"` (gVisor) for a stronger boundary."""

    env: Mapping[str, str] | None = field(default=None, repr=False)
    """Variables every command gets. Nothing is read from the host's environment."""

    def __post_init__(self) -> None:
        if self.defer_loading:
            raise UserError(
                "`DockerWorkspace` does not support `defer_loading=True`: "
                "the workspace is selected before deferred capabilities load."
            )

    def _sandbox(self, name: str) -> DockerSandbox:
        return DockerSandbox(
            image=self.image,
            runtime=self.runtime,
            work_dir=self.work_dir,
            container_name=name,
            network_mode=self.network_mode,
            mem_limit=self.mem_limit,
            cpus=self.cpus,
            oci_runtime=self.oci_runtime,
        )

    def backend(self, ref: WorkspaceRef | None = None) -> DockerWorkspaceBackend:
        """A backend for `ref`, or for a new container; no I/O until its first operation."""
        return DockerWorkspaceBackend(sandbox_factory=self._sandbox, ref=ref, env=self.env)

    def get_workspace(
        self, ctx: RunContext[object], *, ref: WorkspaceRef | None
    ) -> WorkspaceBackend | None:
        """This run's backend, or `None` for a ref another provider owns."""
        del ctx
        if ref is not None and ref.provider != DOCKER_PROVIDER:
            return None
        return self.backend(ref)

    async def destroy(self, ref: WorkspaceRef) -> None:
        """Remove the container `ref` names, files and all. Already gone is fine.

        Raises:
            ValueError: `ref` belongs to another provider.
        """
        if ref.provider != DOCKER_PROVIDER:
            raise ValueError(f"expected a {DOCKER_PROVIDER!r} workspace ref, got {ref.provider!r}")
        await anyio.to_thread.run_sync(_remove_container, ref.id)
