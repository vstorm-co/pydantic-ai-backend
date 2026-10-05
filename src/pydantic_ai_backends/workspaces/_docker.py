"""A Docker container as a Pydantic AI workspace."""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import anyio
import anyio.to_thread
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef

from pydantic_ai_backends.backends.docker._client import docker_client
from pydantic_ai_backends.backends.docker.sandbox import REATTACHABLE_STATUSES, DockerSandbox
from pydantic_ai_backends.protocol import SandboxUnavailableError
from pydantic_ai_backends.types import RuntimeConfig
from pydantic_ai_backends.workspaces._container import ContainerWorkspaceBackend, RunnerSandbox

DOCKER_PROVIDER = "docker"
"""`WorkspaceRef.provider` of a container workspace."""

CONTAINER_PREFIX = "pydantic-ai-workspace-"
"""Name prefix of the containers a :class:`DockerWorkspace` creates."""

_CONTAINER_NAME = re.compile(rf"{re.escape(CONTAINER_PREFIX)}[0-9a-f]{{16}}")
"""Exactly the names :class:`DockerWorkspaceBackend` gives the containers it creates."""

SandboxFactory = Callable[[str], DockerSandbox]
"""Builds the sandbox for a container name, without starting it."""


def _created_here(name: str) -> bool:
    """Whether `name` is one this library gives a container it creates.

    A ref arrives with the message history, which an application may have taken
    from its client. Without this check a ref naming any container on the host -
    a database, the application itself - would have the model's commands run in
    it, and `destroy` would remove it.
    """
    return _CONTAINER_NAME.fullmatch(name) is not None


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


class DockerWorkspaceBackend(ContainerWorkspaceBackend):
    """One Docker container, as the environment an agent run works in.

    The container is named after the ref and never auto-removed, so a later run
    can attach to it and find its files, installed packages included; a stopped
    one is started again. It lives until :meth:`DockerWorkspace.destroy`. A ref
    attaches only to a container named the way this class names the ones it
    creates, never to any other container on the host.

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
        async def open_container(name: str | None) -> tuple[str, RunnerSandbox]:
            if name is None:
                name = f"{CONTAINER_PREFIX}{uuid.uuid4().hex[:16]}"
            elif not _created_here(name):
                raise SandboxUnavailableError(
                    f"container {name!r} was not created by a DockerWorkspace"
                )
            else:
                status = await anyio.to_thread.run_sync(_container_status, name)
                if status != "running" and status not in REATTACHABLE_STATUSES:
                    raise SandboxUnavailableError(
                        f"container {name!r} no longer exists"
                        if status is None
                        else f"container {name!r} is {status}"
                    )
            sandbox = sandbox_factory(name)
            await anyio.to_thread.run_sync(sandbox.start)
            return name, sandbox

        super().__init__(provider=DOCKER_PROVIDER, opener=open_container, ref=ref, env=env)


@dataclass(kw_only=True)
class DockerWorkspace(AbstractCapability[object]):
    """Supply a Docker container on this host as the run's workspace.

    A run with no ref creates a container; one carrying a `"docker"` ref attaches
    to that container, starting it if it was stopped. Containers are kept after
    the run — the ref is how to come back to them, and :meth:`destroy` is how to
    remove one.

    This supplies the environment only. Compose it with something that uses the
    workspace: `Coder`, `Shell` or `FileSystem` from the Pydantic AI harness, or
    this library's `ConsoleCapability`.

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
            ValueError: `ref` belongs to another provider, or names a container
                no `DockerWorkspace` created.
        """
        if ref.provider != DOCKER_PROVIDER:
            raise ValueError(f"expected a {DOCKER_PROVIDER!r} workspace ref, got {ref.provider!r}")
        if not _created_here(ref.id):
            raise ValueError(f"container {ref.id!r} was not created by a DockerWorkspace")
        await anyio.to_thread.run_sync(_remove_container, ref.id)
