"""The parts of running a stoppable command that every container sandbox shares.

A command runs behind a small `sh` wrapper that records its pid, so a second
command can stop it — and every process it started — when the caller times out
or gives up. The process a container runtime's exec starts leads its own process
group, which is what makes `kill -TERM -<pid>` reach the whole tree. Measured on
Docker with dash and busybox: the group is gone within 0.2 seconds.
"""

from __future__ import annotations

from collections.abc import Sequence

PARTIAL_OUTPUT_BYTES = 64 * 1024
"""How much of each stream a timed-out or over-limit command reports."""

SIGNAL_EXIT_BASE = 128
"""A shell reports a child killed by signal N as exit status 128 + N."""

_PID_DIR = "/tmp"

WRAPPER = 'p=$1; shift; echo $$ > "$p"; "$@"; s=$?; rm -f "$p"; exit $s'
"""Record the group leader's pid, run the command as its child, keep its status.

The command is a child rather than `exec`'d so the pid file can be removed
afterwards. A program that does not exist still exits 127, from `sh` itself.
"""

STOPPER = (
    'p=$1; i=0; while [ ! -s "$p" ] && [ $i -lt 20 ]; do sleep 0.05; i=$((i+1)); done; '
    '[ -s "$p" ] || exit 0; g=$(cat "$p"); '
    'kill -TERM "-$g" 2>/dev/null || kill -TERM "$g" 2>/dev/null; i=0; '
    'while { kill -0 "-$g" || kill -0 "$g"; } 2>/dev/null && [ $i -lt 20 ]; '
    "do sleep 0.1; i=$((i+1)); done; "
    'kill -KILL "-$g" 2>/dev/null || kill -KILL "$g" 2>/dev/null; rm -f "$p"; exit 0'
)
"""TERM the recorded process group, then KILL whatever is left after two seconds.

Waits up to a second for the pid file, because a cancellation can arrive between
the command starting and the wrapper writing it. `kill -TERM -<pid>` rather than
`kill -TERM -- -<pid>`: dash rejects the `--`. Where the wrapper does not lead a
process group of its own, the group kill fails and the wrapper alone is signalled.
"""


def pid_file(run_id: str) -> str:
    """Where the wrapper for `run_id` records its process group."""
    return f"{_PID_DIR}/.pydantic-ai-run-{run_id}"


def wrapped_argv(argv: Sequence[str], run_id: str) -> list[str]:
    """`argv` behind the wrapper that makes it stoppable by `run_id`."""
    return ["sh", "-c", WRAPPER, "sh", pid_file(run_id), *argv]


class Sink:
    """Both streams of one command, filled from the thread draining them.

    Appended to by one thread and read by the event loop only after that thread
    has finished or the command has been stopped. Copying a `bytearray` holds the
    GIL, so a read that races a final append sees one consistent prefix.
    """

    def __init__(self, limit: int) -> None:
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.limit = limit
        self.over_limit = False

    def add(self, out: bytes | None, err: bytes | None) -> bool:
        """Record a chunk; `False` once the combined output is over the limit."""
        if out:
            self.stdout += out
        if err:
            self.stderr += err
        if len(self.stdout) + len(self.stderr) > self.limit:
            self.over_limit = True
        return not self.over_limit

    def text(self, *, partial: bool) -> tuple[str, str]:
        """Both streams decoded, undecodable bytes replaced rather than dropped."""
        end = PARTIAL_OUTPUT_BYTES if partial else None
        return (
            bytes(self.stdout[:end]).decode("utf-8", errors="replace"),
            bytes(self.stderr[:end]).decode("utf-8", errors="replace"),
        )
