"""Retire only this display's isolated GUI process groups inside its sandbox."""

from __future__ import annotations

import os
import signal
import time
from contextlib import suppress
from pathlib import Path

_GUI = frozenset({"xfce4-session", "xfce4-panel", "xfwm4", "xfdesktop"})


def groups(display: str, proc: Path = Path("/proc")) -> set[int]:
    result: set[int] = set()
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            executable = Path(os.fsdecode(argv[0])).name
            if executable not in _GUI and executable != "Xvfb":
                continue
            if (entry / "exe").resolve(strict=True).name != executable:
                continue
            owned_display = (
                len(argv) > 1 and argv[1] == display.encode()
                if executable == "Xvfb"
                else b"DISPLAY=" + display.encode() in (entry / "environ").read_bytes().split(b"\0")
            )
            if not owned_display:
                continue
            group = os.getpgid(int(entry.name))
            if entry.stat().st_uid != os.geteuid() or group <= 1 or group == os.getpgrp():
                raise RuntimeError("The desktop does not own an isolated GUI process group")
            result.add(group)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def retire(display: str) -> None:
    owned = groups(display)
    for group in owned:
        with suppress(ProcessLookupError):
            os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while any(alive(group) for group in owned):
        if time.monotonic() >= deadline:
            raise RuntimeError("The previous desktop session did not stop")
        time.sleep(0.1)
