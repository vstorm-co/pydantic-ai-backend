"""The parts of a workspace's command contract every backend here shares."""

from __future__ import annotations

import math
from collections.abc import Mapping

from pydantic_ai.workspaces import (
    CommandResult,
    WorkspaceCommand,
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
)

from pydantic_ai_backends.types import CommandOutcome


def command_argv(command: WorkspaceCommand, shell: bool) -> list[str]:
    """The argv that runs `command`: a shell string under `/bin/sh -c`, an argv as is.

    Raises:
        TypeError: The form does not match `shell` — a string needs `shell=True`
            and a sequence needs `shell=False` — or `command` is bytes.
        ValueError: An empty argv, which a shell would run as a successful no-op.
    """
    if isinstance(command, str):
        if not shell:
            raise TypeError("a string command requires shell=True; pass an argv sequence otherwise")
        return ["/bin/sh", "-c", command]
    if isinstance(command, bytes):
        raise TypeError("a bytes command is not supported; pass a string or an argv sequence")
    if shell:
        raise TypeError("an argv sequence requires shell=False; pass a string to use the shell")
    argv = list(command)
    if not argv:
        raise ValueError("an argv command must name a program")
    return argv


def check_timeout(timeout: float | None) -> None:
    """Refuse a timeout that is not a positive, finite number of seconds."""
    if timeout is None:
        return
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a positive finite number or None")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number or None")


def layered_env(
    base: Mapping[str, str] | None, extra: Mapping[str, str] | None
) -> dict[str, str] | None:
    """`extra` over `base`, or `None` when neither adds anything."""
    merged = {**(base or {}), **(extra or {})}
    return merged or None


def command_result(outcome: CommandOutcome, *, timeout: float | None, limit: int) -> CommandResult:
    """A finished command as a result; a stopped one as the error that stopped it.

    Raises:
        WorkspaceTimeoutError: The command reached its deadline. With `timeout`
            unset that deadline was the sandbox's own ceiling.
        WorkspaceOutputLimitError: The command produced more than `limit` bytes.
    """
    if outcome.timed_out:
        reason = (
            f"Command timed out after {timeout:g} seconds"
            if timeout is not None
            else "Command reached the sandbox's time limit"
        )
        raise WorkspaceTimeoutError(reason, stdout=outcome.stdout, stderr=outcome.stderr)
    if outcome.output_limited:
        raise WorkspaceOutputLimitError(
            f"Command output exceeded {limit} bytes; redirect large output to a file",
            limit=limit,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
        )
    if outcome.exit_code is None:
        raise ValueError("a finished command must carry an exit code")
    return CommandResult(exit_code=outcome.exit_code, stdout=outcome.stdout, stderr=outcome.stderr)
