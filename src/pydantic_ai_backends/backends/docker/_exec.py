"""Running one command in a container, with the failure contract a workspace needs.

A workspace needs more from a command than its text: a container that is gone
must not look like a command that printed "Error:" and exited 1, stdout and
stderr are two streams rather than one, and a command must stop when its caller
stops waiting. So this runs the command through the low-level exec API:

- the streams are demultiplexed and decoded separately;
- `env` is passed to the exec itself, layered over the container's own;
- the deadline is kept here rather than by the `timeout` utility, whose exit
  code 124 a command can also produce on its own;
- a caller that times out or is cancelled stops the command's whole process
  group. A `docker exec` process leads its own group, so the wrapper records its
  pid and `kill -TERM -<pid>` reaches every child it started. Measured on dash
  and on busybox: the group is gone within 0.2 seconds.

Docker itself closes an exec's streams about two seconds after the command exits
even when a background child still holds them, so a backgrounded server delays a
finished command by that grace and no longer.
"""

from __future__ import annotations

import contextlib
import functools
import threading
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread

from pydantic_ai_backends._limits import MAX_RUN_OUTPUT_BYTES
from pydantic_ai_backends.backends._runner import (
    SIGNAL_EXIT_BASE,
    STOPPER,
    Sink,
    pid_file,
    wrapped_argv,
)
from pydantic_ai_backends.backends.docker._client import docker_client
from pydantic_ai_backends.protocol import SandboxUnavailableError
from pydantic_ai_backends.types import CommandOutcome

if TYPE_CHECKING:
    from docker.models.containers import Container

STOP_GRACE_SECONDS = 5.0
"""Longest a stop may take, its TERM-then-KILL included, under cancellation."""


def _translate(error: BaseException) -> BaseException:
    """A daemon error that means the container is gone, as that; anything else as is."""
    import docker.errors

    if isinstance(error, docker.errors.NotFound):
        return SandboxUnavailableError(f"container is gone: {error}")
    if isinstance(error, docker.errors.APIError) and error.status_code == 409:
        # "Container ... is not running" — paused, stopped or being removed.
        return SandboxUnavailableError(f"container is not running: {error}")
    return error


def _drain(api: Any, exec_id: str, sink: Sink, stopped: threading.Event) -> None:
    """Start the exec and copy its output into `sink` until it ends or overflows.

    Leaving early closes the stream, which otherwise holds its socket until the
    garbage collector finds it. One that ran to its end is closed already, and
    closing it again fails with "Socket is not connected".
    """
    stream = api.exec_start(exec_id, stream=True, demux=True)
    for out, err in stream:
        if stopped.is_set() or not sink.add(out, err):
            with contextlib.suppress(OSError):
                stream.close()
            return


async def _running(container: Container) -> bool:
    """Whether `container` still exists and is running, asked of the daemon."""
    import docker.errors

    try:
        await anyio.to_thread.run_sync(container.reload)
    except docker.errors.NotFound:
        return False
    status: str = container.status
    return status == "running"


async def stop_in_container(container: Container, run_id: str) -> None:
    """Stop the command started under `run_id`, and every process it started.

    Best effort, and quiet about it: the caller is already on its way out with a
    timeout, an over-limit result or a cancellation, and a failed stop must not
    replace that with a less useful error. A container that is gone has nothing
    left to stop.
    """
    stopper = functools.partial(
        container.exec_run, ["sh", "-c", STOPPER, "sh", pid_file(run_id)], stdout=False
    )
    with (
        anyio.CancelScope(shield=True),
        anyio.move_on_after(STOP_GRACE_SECONDS),
        contextlib.suppress(Exception),
    ):
        await anyio.to_thread.run_sync(stopper, abandon_on_cancel=True)


async def run_in_container(
    container: Container,
    argv: Sequence[str],
    *,
    run_id: str,
    env: Mapping[str, str] | None = None,
    workdir: str | None = None,
    timeout: float | None = None,
    output_limit: int = MAX_RUN_OUTPUT_BYTES,
) -> CommandOutcome:
    """Run `argv` in `container` with stdin at EOF and wait for it.

    Args:
        container: A running container.
        argv: The program and its arguments, passed through literally.
        run_id: Names this run, so :func:`stop_in_container` can stop it from
            another request. Must be unique among the container's live runs.
        env: Variables layered over the container's own environment.
        workdir: Directory the command starts in; the container's own when `None`.
        timeout: Seconds before the command is stopped. Measured from the start
            of the exec, so a slow daemon counts against it.
        output_limit: Combined bytes of output before the command is stopped.

    Returns:
        The result; a non-zero exit is a result, not an error.

    Raises:
        SandboxUnavailableError: The container is gone or not running, before
            or during the command.
    """
    if not argv:
        raise ValueError("argv must name a program")
    # The process-wide client created the container, so its low-level API reaches
    # the same daemon.
    api = docker_client().api
    create = functools.partial(
        api.exec_create,
        container.id,
        wrapped_argv(argv, run_id),
        environment=dict(env) if env else None,
        workdir=workdir,
        stdout=True,
        stderr=True,
        stdin=False,
    )
    try:
        exec_id: str = (await anyio.to_thread.run_sync(create))["Id"]
    except Exception as error:
        raise _translate(error) from error

    sink = Sink(output_limit)
    stopped = threading.Event()
    drain = functools.partial(_drain, api, exec_id, sink, stopped)
    finished = False
    try:
        with anyio.move_on_after(timeout) as deadline:
            await anyio.to_thread.run_sync(drain, abandon_on_cancel=True)
        finished = not (deadline.cancelled_caught or sink.over_limit)
        if not finished:
            stdout, stderr = sink.text(partial=True)
            return CommandOutcome(
                stdout=stdout,
                stderr=stderr,
                timed_out=deadline.cancelled_caught,
                output_limited=not deadline.cancelled_caught,
            )
    except Exception as error:
        raise _translate(error) from error
    finally:
        # Every way out but a finished command leaves one running: stopped at
        # its deadline or its limit, abandoned by a cancelled caller, or cut off
        # by a broken stream.
        if not finished:
            stopped.set()
            await stop_in_container(container, run_id)

    try:
        info = await anyio.to_thread.run_sync(api.exec_inspect, exec_id)
    except Exception as error:
        raise _translate(error) from error
    exit_code = info.get("ExitCode")
    if exit_code is None:
        # The stream ended without the command finishing: its container went
        # away underneath it.
        raise SandboxUnavailableError("the container stopped while the command was running")
    if exit_code > SIGNAL_EXIT_BASE and not await _running(container):
        # Killed by a signal, and the container with it: removing or stopping a
        # container kills its processes, so their exit is the container's end
        # rather than the command's own. A signal in a live container is a result.
        raise SandboxUnavailableError("the container stopped while the command was running")
    stdout, stderr = sink.text(partial=False)
    return CommandOutcome(stdout=stdout, stderr=stderr, exit_code=exit_code)
