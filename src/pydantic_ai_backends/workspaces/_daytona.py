"""A Daytona sandbox as a Pydantic AI workspace.

Commands run as synchronous session commands, which is the one Daytona API that
reports stdout and stderr apart along with the exit code. Each runs as its own
`sh` under the stoppable wrapper, so a session never carries a `cd` or an export
from one command into the next, and a command whose caller gave up is stopped
through a second one.

Not checked against a live Daytona account in this repository's CI: the unit
tests drive a fake SDK, and `tests/test_workspace_daytona_conformance.py` runs
Pydantic AI's conformance suite only when `DAYTONA_API_KEY` is set.
"""

from __future__ import annotations

import contextlib
import shlex
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio
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
from pydantic_ai_backends._optional import load
from pydantic_ai_backends.backends._runner import (
    PARTIAL_OUTPUT_BYTES,
    STOPPER,
    pid_file,
    wrapped_argv,
)
from pydantic_ai_backends.types import CommandOutcome
from pydantic_ai_backends.workspaces._commands import (
    check_timeout,
    command_argv,
    command_result,
    layered_env,
)

if TYPE_CHECKING:
    from daytona import (
        AsyncDaytona,
        AsyncSandbox,
        CreateSandboxFromImageParams,
        CreateSandboxFromSnapshotParams,
        DaytonaConfig,
    )

    CreateParams = CreateSandboxFromSnapshotParams | CreateSandboxFromImageParams

DAYTONA_PROVIDER = "daytona"
"""`WorkspaceRef.provider` of a Daytona workspace."""

STOP_GRACE_SECONDS = 5.0
"""Longest a stop may take under cancellation."""

RESUMABLE_STATES = frozenset({"stopped", "archived"})
"""States an attached sandbox is started from rather than refused in."""

GONE_STATES = frozenset({"destroyed", "destroying", "error", "build_failed"})
"""States in which a sandbox can no longer run anything."""

_CD_THEN_EXEC = 'cd "$1" && shift && exec "$@"'


def _state(sandbox: AsyncSandbox) -> str:
    state = getattr(sandbox, "state", None)
    return str(getattr(state, "value", state) or "").lower()


class DaytonaWorkspaceBackend(WorkspaceBackend, SupportsCommands):
    """One Daytona sandbox, as the environment an agent run works in.

    Commands only: `Workspace` derives the file operations through the shell.
    The first operation creates the sandbox, or attaches to the one the ref
    names, starting it when it was stopped or archived; a sandbox that is gone
    fails with `WorkspaceUnavailableError`. It lives until
    :meth:`DaytonaWorkspace.destroy` or Daytona's own auto-delete.

    Args:
        config: A `daytona.DaytonaConfig`; the environment's `DAYTONA_*`
            variables when `None`.
        create_params: Parameters for a new sandbox, such as
            `CreateSandboxFromSnapshotParams`; Daytona's default when `None`.
        ref: The workspace to attach to; `None` creates one on first use.
        env: Variables every command gets, under any a call passes.
        client: An `AsyncDaytona` to share, owned and closed by the caller;
            without one each operation opens and closes its own.
        sandbox_name: A sandbox name chosen by whoever configures the workspace
            rather than by Daytona: with no ref, the first operation creates the
            sandbox under it, or attaches to the one that already has it. A ref
            is still attach-only, so a caller that knows the sandbox existed
            learns that it is gone instead of starting over in a new one.
    """

    def __init__(
        self,
        *,
        config: DaytonaConfig | None = None,
        create_params: CreateParams | None = None,
        ref: WorkspaceRef | None = None,
        env: Mapping[str, str] | None = None,
        client: AsyncDaytona | None = None,
        sandbox_name: str | None = None,
    ) -> None:
        if ref is not None and ref.provider != DAYTONA_PROVIDER:
            raise ValueError(f"expected a {DAYTONA_PROVIDER!r} workspace ref, got {ref.provider!r}")
        self._sandbox_name = sandbox_name
        self._config = config
        self._create_params = create_params
        self._ref = ref
        self._env = dict(env) if env else None
        self._client = client
        self._working_dir: str | None = None
        self._session_id = f"pab-{uuid.uuid4().hex}"
        self._session_open = False
        self._lock = anyio.Lock()

    @property
    def ref(self) -> WorkspaceRef | None:
        """The sandbox's id once it exists; `None` before the first operation."""
        return self._ref

    @contextlib.asynccontextmanager
    async def _daytona(self) -> AsyncIterator[AsyncDaytona]:
        if self._client is not None:
            yield self._client
            return
        daytona = load("daytona", purpose="DaytonaWorkspace")
        async with daytona.AsyncDaytona(self._config) as client:
            yield client

    async def _sandbox(self, client: AsyncDaytona) -> AsyncSandbox:
        """The sandbox, created or attached under the lock, with a session open in it."""
        async with self._lock:
            if self._ref is not None:
                sandbox = await _attach(client, self._ref.id)
            elif self._sandbox_name is not None:
                sandbox = await self._open_named(client, self._sandbox_name)
            else:
                # Shielded so a caller cancelled mid-create still leaves the ref
                # of a sandbox Daytona has already made.
                with anyio.CancelScope(shield=True):
                    sandbox = await client.create(self._create_params)
                    self._ref = WorkspaceRef(provider=DAYTONA_PROVIDER, id=sandbox.id)
            if self._working_dir is None:
                self._working_dir = await sandbox.get_work_dir()
            if not self._session_open:
                await sandbox.process.create_session(self._session_id)
                self._session_open = True
            return sandbox

    async def _open_named(self, client: AsyncDaytona, name: str) -> AsyncSandbox:
        """The sandbox called `name`: the existing one, or a new one created under it."""
        daytona = load("daytona", purpose="DaytonaWorkspace")
        with anyio.CancelScope(shield=True):
            try:
                sandbox = await _attach(client, name)
            except WorkspaceUnavailableError as gone:
                if not isinstance(gone.__cause__, daytona.DaytonaNotFoundError):
                    raise
                params = self._create_params or daytona.CreateSandboxFromSnapshotParams()
                try:
                    sandbox = await client.create(params.model_copy(update={"name": name}))
                except daytona.DaytonaConflictError:
                    # Another client created it between our lookup and our create.
                    sandbox = await _attach(client, name)
            # The name, not the id: it is what a later client knows to look for,
            # and `get` takes either.
            self._ref = WorkspaceRef(provider=DAYTONA_PROVIDER, id=name)
        return sandbox

    async def working_dir(self) -> str:
        """The sandbox's working directory, as Daytona reports it."""
        async with self._daytona() as client:
            await self._sandbox(client)
        assert self._working_dir is not None
        return self._working_dir

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command in the sandbox, stdin at EOF; see `SupportsCommands.run`.

        Raises:
            WorkspaceUnavailableError: The sandbox is gone.
            WorkspaceTimeoutError: The command reached `timeout`. Daytona answers
                a session command only once it ends, so no partial output comes
                with it.
            WorkspaceOutputLimitError: Its combined output passed 10 MiB.
        """
        argv = command_argv(command, shell)
        check_timeout(timeout)
        daytona = load("daytona", purpose="DaytonaWorkspace")
        run_id = uuid.uuid4().hex
        assignments = [f"{k}={v}" for k, v in (layered_env(self._env, env) or {}).items()]
        async with self._daytona() as client:
            sandbox = await self._sandbox(client)
            assert self._working_dir is not None
            line = shlex.join(
                [
                    "sh",
                    "-c",
                    _CD_THEN_EXEC,
                    "sh",
                    self._working_dir,
                    "env",
                    *assignments,
                    *wrapped_argv(argv, run_id),
                ]
            )
            request = daytona.SessionExecuteRequest(
                command=f"{line} </dev/null", run_async=False, suppress_input_echo=True
            )
            answered = False
            try:
                with anyio.move_on_after(timeout) as deadline:
                    response = await sandbox.process.execute_session_command(
                        self._session_id, request
                    )
                    answered = True
            except daytona.DaytonaNotFoundError as error:
                raise WorkspaceUnavailableError(
                    f"Daytona sandbox {sandbox.id!r} went away during the command"
                ) from error
            finally:
                if not answered:
                    await self._stop(sandbox, run_id)
        if deadline.cancelled_caught:
            return command_result(
                CommandOutcome(stdout="", stderr="", timed_out=True),
                timeout=timeout,
                limit=MAX_RUN_OUTPUT_BYTES,
            )
        stdout, stderr = response.stdout or "", response.stderr or ""
        limited = len(stdout.encode()) + len(stderr.encode()) > MAX_RUN_OUTPUT_BYTES
        outcome = CommandOutcome(
            stdout=stdout[:PARTIAL_OUTPUT_BYTES] if limited else stdout,
            stderr=stderr[:PARTIAL_OUTPUT_BYTES] if limited else stderr,
            exit_code=None if limited else response.exit_code,
            output_limited=limited,
        )
        return command_result(outcome, timeout=timeout, limit=MAX_RUN_OUTPUT_BYTES)

    async def _stop(self, sandbox: AsyncSandbox, run_id: str) -> None:
        """Stop a command whose caller stopped waiting, and everything it started."""
        stopper = shlex.join(["sh", "-c", STOPPER, "sh", pid_file(run_id)])
        with (
            anyio.CancelScope(shield=True),
            anyio.move_on_after(STOP_GRACE_SECONDS),
            contextlib.suppress(Exception),
        ):
            await sandbox.process.exec(stopper, timeout=int(STOP_GRACE_SECONDS))

    async def purge(self) -> None:
        """Delete this backend's sandbox. Already gone is fine."""
        if self._ref is None:
            return
        daytona = load("daytona", purpose="DaytonaWorkspace")
        async with self._daytona() as client:
            try:
                sandbox = await client.get(self._ref.id)
            except daytona.DaytonaNotFoundError:
                return
            await client.delete(sandbox)


async def _attach(client: AsyncDaytona, id_or_name: str) -> AsyncSandbox:
    """The existing sandbox `id_or_name` names, started when it was stopped or archived.

    Raises:
        WorkspaceUnavailableError: It does not exist, or is gone for good.
    """
    daytona = load("daytona", purpose="DaytonaWorkspace")
    try:
        sandbox = await client.get(id_or_name)
    except daytona.DaytonaNotFoundError as error:
        raise WorkspaceUnavailableError(
            f"Daytona sandbox {id_or_name!r} no longer exists"
        ) from error
    state = _state(sandbox)
    if state in GONE_STATES:
        raise WorkspaceUnavailableError(f"Daytona sandbox {id_or_name!r} is {state}")
    if state in RESUMABLE_STATES:
        await sandbox.start()
    return sandbox


@dataclass(kw_only=True)
class DaytonaWorkspace(AbstractCapability[object]):
    """Supply a Daytona sandbox as the run's workspace.

    A run with no ref creates a sandbox; one carrying a `"daytona"` ref attaches
    to it, starting it when Daytona stopped or archived it. Sandboxes are kept
    after the run, subject to Daytona's own auto-stop and auto-delete; the ref is
    how to come back, and :meth:`destroy` deletes one.

    Not checked against a live Daytona account in this repository's CI.

    Example:
        ```python
        from daytona import DaytonaConfig
        from pydantic_ai import Agent

        from pydantic_ai_backends import ConsoleCapability
        from pydantic_ai_backends.workspaces import DaytonaWorkspace

        sandboxes = DaytonaWorkspace(config=DaytonaConfig(api_key="dtn_..."))
        agent = Agent("anthropic:claude-opus-5-5", capabilities=[sandboxes, ConsoleCapability()])
        ```
    """

    config: DaytonaConfig | None = field(default=None, repr=False)
    """A `daytona.DaytonaConfig`; the environment's `DAYTONA_*` variables when `None`."""

    create_params: CreateParams | None = None
    """Parameters for a new sandbox, such as `CreateSandboxFromSnapshotParams`."""

    env: Mapping[str, str] | None = field(default=None, repr=False)
    """Variables every command gets. Nothing is read from the host's environment."""

    client: AsyncDaytona | None = field(default=None, repr=False, compare=False)
    """An `AsyncDaytona` to share across runs, owned and closed by the caller."""

    sandbox_name: str | None = None
    """One sandbox for every run, named by you: created on first use, attached after.

    Without it each run without a ref gets a new sandbox and only the ref leads
    back to it. With it a later process finds the same sandbox by name. A ref
    naming another sandbox is left to another capability.
    """

    def __post_init__(self) -> None:
        if self.defer_loading:
            raise UserError(
                "`DaytonaWorkspace` does not support `defer_loading=True`: "
                "the workspace is selected before deferred capabilities load."
            )

    def backend(self, ref: WorkspaceRef | None = None) -> DaytonaWorkspaceBackend:
        """A backend for `ref`, or for a new sandbox; no API call until its first operation."""
        return DaytonaWorkspaceBackend(
            config=self.config,
            create_params=self.create_params,
            ref=ref,
            env=self.env,
            client=self.client,
            sandbox_name=self.sandbox_name,
        )

    def get_workspace(
        self, ctx: RunContext[object], *, ref: WorkspaceRef | None
    ) -> WorkspaceBackend | None:
        """This run's backend, or `None` for a ref another provider owns."""
        del ctx
        if ref is not None and ref.provider != DAYTONA_PROVIDER:
            return None
        if ref is not None and self.sandbox_name is not None and ref.id != self.sandbox_name:
            return None
        return self.backend(ref)

    async def destroy(self, ref: WorkspaceRef) -> None:
        """Delete the sandbox `ref` names. Already gone is fine.

        Raises:
            ValueError: `ref` belongs to another provider.
        """
        if ref.provider != DAYTONA_PROVIDER:
            raise ValueError(f"expected a {DAYTONA_PROVIDER!r} workspace ref, got {ref.provider!r}")
        await self.backend(ref).purge()
