"""The contract a sandbox implements to run commands for a Pydantic AI workspace.

`DockerSandbox` and the Kubernetes pod sandbox implement :class:`CommandRunner`;
`sandboxd` serves it as `/run`, and the workspaces in
:mod:`pydantic_ai_backends.workspaces` are built on it. It raises rather than
folding a failure into its output, because its callers are code that has to tell
a sandbox that is gone from a command that failed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic_ai_backends.types import CommandOutcome


class SandboxUnavailableError(RuntimeError):
    """The sandbox is gone or no longer running, so no command can reach it."""


@runtime_checkable
class CommandRunner(Protocol):
    """Commands under a workspace's failure contract.

    Raises rather than folding a failure into the output, keeps stdout and
    stderr apart, and can be stopped from elsewhere by the `run_id` its caller
    chose. Implemented by `DockerSandbox` and `KubernetesPodSandbox`, and what
    both `sandboxd`'s `/run` and the Pydantic AI workspaces in
    :mod:`pydantic_ai_backends.workspaces` are built on.
    """

    async def run_command(
        self,
        argv: Sequence[str],
        *,
        run_id: str,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        output_limit: int | None = None,
    ) -> CommandOutcome:
        """Run `argv` with stdin at EOF and wait for it.

        Args:
            argv: The program and its arguments, passed through literally.
            run_id: Names the run, so :meth:`stop_command` can stop it.
            env: Variables layered over the sandbox's environment.
            timeout: Seconds before the command is stopped; none when `None`.
            output_limit: Combined output bytes before it is stopped; the
                implementation's default when `None`.

        Raises:
            SandboxUnavailableError: The sandbox is gone or not running.
        """
        ...

    async def stop_command(self, run_id: str) -> None:
        """Stop the command started under `run_id` and everything it started.

        A run that already finished, or never started, has nothing to stop.
        """
        ...
