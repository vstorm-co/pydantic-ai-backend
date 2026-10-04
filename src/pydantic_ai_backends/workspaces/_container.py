"""A workspace over any sandbox that runs commands: create or attach, then run."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import anyio
from pydantic_ai.workspaces import (
    CommandResult,
    SupportsCommands,
    WorkspaceBackend,
    WorkspaceCommand,
    WorkspaceRef,
    WorkspaceUnavailableError,
)

from pydantic_ai_backends._limits import MAX_RUN_OUTPUT_BYTES
from pydantic_ai_backends.protocol import CommandRunner, SandboxUnavailableError
from pydantic_ai_backends.workspaces._commands import (
    check_timeout,
    command_argv,
    command_result,
    layered_env,
)


class RunnerSandbox(CommandRunner, Protocol):
    """A sandbox a container workspace runs in: commands, and where they start."""

    @property
    def work_dir(self) -> str: ...


Opener = Callable[[str | None], Awaitable[tuple[str, RunnerSandbox]]]
"""Opens the environment a ref id names, or a new one for `None`.

Returns the id the ref carries and the started sandbox. Raises
`SandboxUnavailableError` for an id whose environment is gone, and never creates
a replacement for it.
"""


class ContainerWorkspaceBackend(WorkspaceBackend, SupportsCommands):
    """One sandbox that runs commands, as the environment an agent run works in.

    Commands only: `Workspace` derives the file operations through the shell,
    which keeps one failure contract for both. The environment is opened on the
    first operation and kept after the run; whoever holds the ref removes it.

    Args:
        provider: The provider name refs carry.
        opener: Opens the environment; see :data:`Opener`.
        ref: The workspace to attach to; `None` creates one on first use.
        env: Variables every command gets, under any a call passes.
    """

    def __init__(
        self,
        *,
        provider: str,
        opener: Opener,
        ref: WorkspaceRef | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if ref is not None and ref.provider != provider:
            raise ValueError(f"expected a {provider!r} workspace ref, got {ref.provider!r}")
        self._provider = provider
        self._opener = opener
        self._ref = ref
        self._env = dict(env) if env else None
        self._sandbox: RunnerSandbox | None = None
        self._lock = anyio.Lock()

    @property
    def ref(self) -> WorkspaceRef | None:
        """The environment's id once it exists; `None` before the first operation."""
        return self._ref

    async def _connect(self) -> RunnerSandbox:
        async with self._lock:
            if self._sandbox is not None:
                return self._sandbox
            # Shielded: a caller cancelled while the environment is being created
            # must still leave its ref behind, or nothing could ever remove it.
            with anyio.CancelScope(shield=True):
                try:
                    opened_id, sandbox = await self._opener(
                        None if self._ref is None else self._ref.id
                    )
                except SandboxUnavailableError as error:
                    raise WorkspaceUnavailableError(str(error)) from error
                if self._ref is None:
                    self._ref = WorkspaceRef(provider=self._provider, id=opened_id)
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
        """Run a command in the sandbox, stdin at EOF; see `SupportsCommands.run`.

        Raises:
            WorkspaceUnavailableError: The environment is gone, before or during it.
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
