"""Registry of long-lived processes started by a backend.

Unlike `execute`, which runs a command to completion and reaps its tree, these
processes keep running after the call returns: dev servers, watchers, log tails.
Their output is streamed to files so it can be drained by byte offset without
ever blocking on a pipe.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai_backends.types import (
    BackgroundHandle,
    BackgroundOutput,
    BackgroundProcessInfo,
)

KILL_GRACE_SECONDS = 2
"""How long to wait for a killed process to be reaped before moving on."""


@dataclass
class _Tracked:
    """One running process and how far its output has been drained."""

    shell_id: str
    command: str
    popen: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path
    stdout_pos: int = 0
    stderr_pos: int = 0


class BackgroundProcesses:
    """Starts, drains and stops the background processes of one backend.

    Args:
        cwd: Working directory the processes are started in.
    """

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd
        self._tracked: dict[str, _Tracked] = {}
        self._counter = 0
        self._output_dir: Path | None = None

    def __bool__(self) -> bool:
        """Whether any process is being tracked."""
        return bool(self._tracked)

    def start(self, argv: Sequence[str], command: str) -> BackgroundHandle:
        """Spawn `argv` detached and return a handle to it immediately.

        Args:
            argv: Argument vector to spawn, already wrapped for the platform.
            command: The original command string, kept for listings.
        """
        if self._output_dir is None:
            self._output_dir = Path(tempfile.mkdtemp(prefix="pad_bg_"))

        self._counter += 1
        shell_id = f"bg_{self._counter}"
        stdout_path = self._output_dir / f"{shell_id}.out"
        stderr_path = self._output_dir / f"{shell_id}.err"

        # The child writes straight to these files, so the parent's handles can
        # be closed right after the spawn — the child keeps its own dup'd ones.
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            popen = subprocess.Popen(
                argv,
                cwd=self._cwd,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=(sys.platform != "win32"),
            )

        self._tracked[shell_id] = _Tracked(
            shell_id=shell_id,
            command=command,
            popen=popen,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        return BackgroundHandle(shell_id=shell_id, pid=popen.pid, command=command)

    def read(self, shell_id: str) -> BackgroundOutput:
        """Return output produced since the previous read, plus run status."""
        process = self._tracked.get(shell_id)
        if process is None:
            return BackgroundOutput(
                shell_id=shell_id,
                stdout="",
                stderr=f"No such background shell: {shell_id}",
                running=False,
                exit_code=None,
            )

        exit_code = process.popen.poll()
        stdout, process.stdout_pos = _drain(process.stdout_path, process.stdout_pos)
        stderr, process.stderr_pos = _drain(process.stderr_path, process.stderr_pos)
        return BackgroundOutput(
            shell_id=shell_id,
            stdout=stdout,
            stderr=stderr,
            running=exit_code is None,
            exit_code=exit_code,
        )

    def kill(self, shell_id: str) -> bool:
        """Stop one process. Returns whether it was still running."""
        process = self._tracked.get(shell_id)
        if process is None:
            return False

        was_running = process.popen.poll() is None
        _kill_tree(process.popen)
        with contextlib.suppress(Exception):
            process.popen.wait(timeout=KILL_GRACE_SECONDS)
        return was_running

    def list(self) -> list[BackgroundProcessInfo]:
        """Status of every tracked process."""
        infos: list[BackgroundProcessInfo] = []
        for process in self._tracked.values():
            exit_code = process.popen.poll()
            infos.append(
                BackgroundProcessInfo(
                    shell_id=process.shell_id,
                    command=process.command,
                    pid=process.popen.pid,
                    running=exit_code is None,
                    exit_code=exit_code,
                )
            )
        return infos

    def kill_all(self) -> None:
        """Stop every process and remove its on-disk output."""
        for process in list(self._tracked.values()):
            _kill_tree(process.popen)
            with contextlib.suppress(Exception):
                process.popen.wait(timeout=KILL_GRACE_SECONDS)

        self._tracked.clear()
        if self._output_dir is not None:
            shutil.rmtree(self._output_dir, ignore_errors=True)
            self._output_dir = None


def _drain(path: Path, position: int) -> tuple[str, int]:
    """Read new bytes from `path` starting at `position`."""
    try:
        with open(path, "rb") as f:
            f.seek(position)
            data = f.read()
    except OSError:  # pragma: no cover - the file was removed mid-read
        return "", position
    return data.decode("utf-8", errors="replace"), position + len(data)


def _kill_tree(popen: subprocess.Popen[bytes]) -> None:
    """Kill a process and, on Unix, the whole group it was given.

    On Windows `cmd /c` already terminates its children when it dies, so
    killing the process itself is enough.
    """
    if popen.poll() is not None:
        return
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        with contextlib.suppress(ProcessLookupError):
            popen.kill()
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
