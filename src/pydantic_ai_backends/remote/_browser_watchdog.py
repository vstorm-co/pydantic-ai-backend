"""An independent hard deadline inside one dedicated Linux browser container.

This file is sent as stdlib-only source to the runtime. No daemon socket or host
privilege is available. A flock and immutable deadline prevent reconnects from
rearming it. It signals only the proven sleep-container init or recorded GUI
process identities, never every process owned by a Unix account.
"""

from __future__ import annotations

import json
import math
import os
import signal
import stat
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

STATE_PATH = Path("/tmp/sandboxd-browser-watchdog.json")
PROC = Path("/proc")
MAX_STATE_BYTES = 16384


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start: int
    executable: str
    uid: int

    def wire(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "start": self.start,
            "executable": self.executable,
            "uid": self.uid,
        }


def identity(pid: int, proc: Path = PROC) -> ProcessIdentity:
    if pid < 1:
        raise ValueError("A process identity requires a positive pid")
    directory = proc / str(pid)
    stat = (directory / "stat").read_text()
    fields = stat[stat.rindex(")") + 2 :].split()
    return ProcessIdentity(
        pid=pid,
        start=int(fields[19]),
        executable=(directory / "exe").resolve(strict=True).name,
        uid=directory.stat().st_uid,
    )


def _decode_identity(value: object) -> ProcessIdentity:
    if not isinstance(value, dict):
        raise ValueError("Invalid watchdog process identity")
    pid, start, executable, uid = (
        value.get("pid"),
        value.get("start"),
        value.get("executable"),
        value.get("uid"),
    )
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid < 1
        or not isinstance(start, int)
        or isinstance(start, bool)
        or start < 0
        or not isinstance(uid, int)
        or isinstance(uid, bool)
        or uid < 0
        or not isinstance(executable, str)
        or not executable
    ):
        raise ValueError("Invalid watchdog process identity")
    return ProcessIdentity(pid, start, executable, uid)


def _same_process(process: ProcessIdentity) -> bool:
    try:
        return process.uid == os.geteuid() and identity(process.pid) == process
    except (OSError, ValueError):
        return False


def _container_init() -> ProcessIdentity | None:
    try:
        process = identity(1)
        command = (PROC / "1" / "cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    if (
        process.uid == os.geteuid()
        and process.executable in {"docker-init", "tini"}
        and command[1:4] == [b"--", b"sleep", b"infinity"]
    ):
        return process
    return None


def _stop_gui(process: ProcessIdentity) -> bool:
    if process.pid <= 1 or not _same_process(process):
        return False
    try:
        group = os.getpgid(process.pid)
        if group == process.pid and group != os.getpgrp():
            os.killpg(group, signal.SIGKILL)
        else:
            os.kill(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def retire(
    init: ProcessIdentity | None, browser: ProcessIdentity, display: ProcessIdentity
) -> None:
    """Stop the exact dedicated container, with narrowly owned GUI fallback."""
    if init is not None and _same_process(init):
        with suppress(PermissionError, ProcessLookupError):
            os.kill(1, signal.SIGTERM)
    # An init may reject or delay termination. Recorded GUI groups are a second
    # independent stop path; PID+starttime checks refuse reused/unrelated pids.
    for process in (browser, display):
        _stop_gui(process)


def _write_state(fd: int, data: dict[str, object]) -> None:
    encoded = json.dumps(data, separators=(",", ":")).encode()
    if len(encoded) > MAX_STATE_BYTES:
        raise ValueError("Watchdog state exceeds its bound")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    offset = 0
    while offset < len(encoded):
        written = os.write(fd, encoded[offset:])
        if written <= 0:
            raise OSError("Watchdog state could not be written")
        offset += written
    os.fsync(fd)


def _check_state_file(fd: int) -> None:
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode):
        raise ValueError("Watchdog state is not a regular file owned by this runtime")


def existing(path: Path = STATE_PATH) -> float:
    """Attest the existing live watchdog without modifying its original deadline."""
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _check_state_file(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise ValueError("The original browser watchdog is no longer running")
        raw = os.read(fd, MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError("Watchdog state exceeds its bound")
        data: object = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Invalid browser watchdog state")
        deadline = data.get("deadline")
        if (
            not isinstance(deadline, (int, float))
            or isinstance(deadline, bool)
            or not math.isfinite(deadline)
            or deadline <= time.monotonic()
            or not _same_process(_decode_identity(data.get("watchdog")))
            or not _same_process(_decode_identity(data.get("browser")))
            or not _same_process(_decode_identity(data.get("display")))
        ):
            raise ValueError("The original browser watchdog or GUI has expired")
        return float(deadline)
    finally:
        os.close(fd)


def _interrupted(_signum: int, _frame: FrameType | None) -> None:
    raise InterruptedError("Browser watchdog stopped")


def _watch(
    fd: int,
    deadline: float,
    init: ProcessIdentity | None,
    browser: ProcessIdentity,
    display: ProcessIdentity,
) -> None:
    try:
        signal.signal(signal.SIGTERM, _interrupted)
        signal.signal(signal.SIGINT, _interrupted)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 1.0))
    finally:
        try:
            retire(init, browser, display)
        finally:
            os.close(fd)


def arm(
    lifetime_seconds: int, browser_pid: int, display_pid: int, path: Path = STATE_PATH
) -> float:
    """Fork one disowned watchdog after GUI readiness; never renew an older one."""
    import fcntl

    if (
        not isinstance(lifetime_seconds, int)
        or isinstance(lifetime_seconds, bool)
        or not 1 <= lifetime_seconds <= 86400
    ):
        raise ValueError("Browser lifetime must be 1–86400 integer seconds")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    parent_owns_fd = True
    try:
        _check_state_file(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return existing(path)
        if os.fstat(fd).st_size:
            raise ValueError("An earlier browser watchdog cannot be rearmed")
        browser, display = identity(browser_pid), identity(display_pid)
        if (
            browser.pid <= 1
            or display.pid <= 1
            or browser.executable != "chromium"
            or display.executable != "Xvfb"
            or browser.uid != os.geteuid()
            or display.uid != os.geteuid()
        ):
            raise ValueError("The runtime does not own the browser and its display")
        deadline = time.monotonic() + lifetime_seconds
        init = _container_init()
        child = os.fork()
        if child == 0:
            parent_owns_fd = False
            setup_complete = False
            status = 1
            try:
                os.setsid()
                null = os.open(os.devnull, os.O_RDWR)
                for target in (0, 1, 2):
                    os.dup2(null, target)
                os.close(null)
                setup_complete = True
                _watch(fd, deadline, init, browser, display)
                status = 0
            finally:
                try:
                    if not setup_complete:
                        try:
                            retire(init, browser, display)
                        finally:
                            os.close(fd)
                finally:
                    os._exit(status)
        _write_state(
            fd,
            {
                "deadline": deadline,
                "watchdog": identity(child).wire(),
                "browser": browser.wire(),
                "display": display.wire(),
            },
        )
        return deadline
    finally:
        if parent_owns_fd:
            os.close(fd)


def abort(browser_pid: int, display_pid: int) -> None:
    """Retire partial startup even when no live supervisor can receive its failure."""
    init = _container_init()
    if init is not None and _same_process(init):
        with suppress(PermissionError, ProcessLookupError):
            os.kill(1, signal.SIGTERM)
    for pid in (browser_pid, display_pid):
        if pid <= 1:
            continue
        try:
            process = identity(pid)
        except (OSError, ValueError):
            continue
        if process.executable in {"chromium", "Xvfb"}:
            _stop_gui(process)


def main(argv: list[str]) -> None:
    if argv == ["existing"]:
        existing()
    elif len(argv) == 4 and argv[0] == "arm":
        arm(int(argv[1]), int(argv[2]), int(argv[3]))
    elif len(argv) == 3 and argv[0] == "abort":
        abort(int(argv[1]), int(argv[2]))
    else:
        raise ValueError("Invalid browser watchdog command")


if __name__ == "__main__":
    main(sys.argv[1:])
