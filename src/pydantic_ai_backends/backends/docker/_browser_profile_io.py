"""Bounded Docker archive transport; plaintext profiles stay in memory or container."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic_ai_backends.remote._browser_profiles import (
    MAX_PROFILE_BYTES,
    BrowserProfileError,
    profile_archive,
)

if TYPE_CHECKING:
    from docker.models.containers import Container

logger = logging.getLogger(__name__)


@runtime_checkable
class ExecResult(Protocol):
    @property
    def exit_code(self) -> int | None: ...


class ProfileContainer(Protocol):
    def exec_run(self, cmd: list[str]) -> object: ...
    def get_archive(self, path: str) -> tuple[Iterable[bytes], object]: ...
    def put_archive(self, path: str, data: bytes) -> bool: ...


class DockerProfileContainer:
    """Keep Docker's dynamic return types at this narrow archive adapter."""

    def __init__(self, container: Container) -> None:
        self.container = container

    def exec_run(self, cmd: list[str]) -> object:
        return self.container.exec_run(cmd)

    def get_archive(self, path: str) -> tuple[Iterable[bytes], object]:
        from ._profile_exec import export_archive

        if path != "/tmp/sandboxd-browser-profile":
            raise BrowserProfileError("Only the managed browser profile may be archived")
        return [export_archive(self.container)], None

    def put_archive(self, path: str, data: bytes) -> bool:
        from ._profile_exec import import_archive

        if path != "/tmp":
            raise BrowserProfileError("Only the managed browser profile may be restored")
        import_archive(self.container, data)
        return True


_IDENTITY = r"""
import json, os, pathlib, signal, sys, time
state=json.loads(pathlib.Path('/tmp/sandboxd-browser-watchdog.json').read_text())
browser=state['browser']
pid=browser['pid']
assert isinstance(pid,int) and not isinstance(pid,bool) and pid>1
proc=pathlib.Path('/proc')/str(pid)
def same():
    try:
        raw=(proc/'stat').read_text()
        fields=raw[raw.rindex(')')+2:].split()
        return (fields[0]!='Z' and int(fields[19])==browser['start']
                and (proc/'exe').resolve(strict=True).name=='chromium'
                and proc.stat().st_uid==os.geteuid())
    except FileNotFoundError:
        return False
def group():
    pgid=os.getpgid(pid)
    if pgid!=pid or pgid<=1 or pgid==os.getpgrp():
        raise RuntimeError('Chromium does not own an isolated process group')
    return pgid
"""

_CONTROL = (
    _IDENTITY
    + r"""
mode=sys.argv[1]
if mode not in {'stop','resume'}: raise ValueError('Invalid profile control')
if same():
    os.killpg(group(),signal.SIGSTOP if mode=='stop' else signal.SIGCONT)
"""
)

_FLUSH = (
    _IDENTITY
    + r"""
import urllib.request
from urllib.parse import urlsplit
import websocket
deadline=time.monotonic()+15
if same():
    os.killpg(group(),signal.SIGCONT)
    with urllib.request.urlopen('http://127.0.0.1:9222/json/version',timeout=5) as response:
        info=json.load(response)
    path=urlsplit(info['webSocketDebuggerUrl']).path
    ws=websocket.create_connection('ws://127.0.0.1:9222'+path,timeout=5,suppress_origin=True)
    try:
        ws.send('{"id":1,"method":"Browser.close"}')
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0: raise RuntimeError('Browser close exceeded its deadline')
            ws.settimeout(remaining)
            try:
                message=ws.recv()
            except websocket.WebSocketConnectionClosedException:
                break
            if not message: break
            reply=json.loads(message)
            if reply.get('id')==1:
                if 'error' in reply: raise RuntimeError('Browser close was refused')
                break
    finally:
        ws.close()
def writers():
    for entry in pathlib.Path('/proc').iterdir():
        if not entry.name.isdigit(): continue
        try:
            raw=(entry/'stat').read_text()
            fields=raw[raw.rindex(')')+2:].split()
            if fields[0]!='Z' and int(fields[2])==pid: return True
        except FileNotFoundError:
            continue
    return False
while writers():
    if time.monotonic()>=deadline:
        raise RuntimeError('Chromium writers did not flush before their deadline')
    time.sleep(.05)
"""
)

_RESTORE = r"""
import pathlib, shutil
for proc in pathlib.Path('/proc').iterdir():
    if not proc.name.isdigit(): continue
    try:
        if (proc/'exe').resolve(strict=True).name=='chromium':
            raise RuntimeError('Cannot restore a live browser profile')
    except FileNotFoundError:
        pass
path=pathlib.Path('/tmp/sandboxd-browser-profile')
if path.is_symlink(): path.unlink()
elif path.exists(): shutil.rmtree(path)
path.mkdir(mode=0o700)
"""


def _control_writers(container: ProfileContainer, mode: str) -> None:
    result = container.exec_run(["python3", "-c", _CONTROL, mode])
    if not isinstance(result, ExecResult) or result.exit_code != 0:
        raise BrowserProfileError("Browser profile writers could not be controlled")


def export_profile(container: ProfileContainer, *, graceful: bool) -> bytes:
    """Flush orderly shutdown, or freeze writers for a crash-consistent checkpoint."""
    if graceful:
        result = container.exec_run(["python3", "-c", _FLUSH])
        if not isinstance(result, ExecResult) or result.exit_code != 0:
            raise BrowserProfileError("Chromium could not flush its profile")
    stopped = False
    deadline = time.monotonic() + 30
    failed = False
    try:
        if not graceful:
            stopped = True
            _control_writers(container, "stop")
        stream, _metadata = container.get_archive("/tmp/sandboxd-browser-profile")
        chunks = bytearray()
        try:
            for chunk in stream:
                if time.monotonic() >= deadline:
                    raise BrowserProfileError("Browser profile capture exceeded its deadline")
                if len(chunks) + len(chunk) > MAX_PROFILE_BYTES:
                    raise BrowserProfileError("Browser profile archive exceeds its bound")
                chunks.extend(chunk)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        return profile_archive(bytes(chunks))
    except BaseException:
        failed = True
        raise
    finally:
        if stopped:
            try:
                _control_writers(container, "resume")
            except Exception as error:
                if not failed:
                    raise
                logger.error("Browser profile writer resume failed (%s)", type(error).__name__)


def restore_profile(container: ProfileContainer, archive: bytes) -> None:
    """Install only a validated profile into an existing container before GUI startup."""
    archive = profile_archive(archive)
    result = container.exec_run(["python3", "-c", _RESTORE])
    if not isinstance(result, ExecResult) or result.exit_code != 0:
        raise BrowserProfileError("Browser profile cannot be restored into this container")
    if not container.put_archive("/tmp", archive):
        raise BrowserProfileError("Docker refused the browser profile restore")
