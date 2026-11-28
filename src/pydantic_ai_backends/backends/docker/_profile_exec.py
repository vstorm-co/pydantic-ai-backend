"""Binary profile transfer over private Docker exec, including runtime tmpfs mounts.

Docker archive endpoints see the container root filesystem rather than its live
/tmp mount. Exec keeps the entire plaintext transfer in container RAM and the
service process; neither command arguments nor host files contain profile data.
"""

from __future__ import annotations

import hashlib
import logging
import socket
import struct
import time
from typing import TYPE_CHECKING

from pydantic_ai_backends.backends.docker._browser_bridge import SocketOwner
from pydantic_ai_backends.remote._browser_profiles import MAX_PROFILE_BYTES, BrowserProfileError

if TYPE_CHECKING:
    from docker.models.containers import Container

logger = logging.getLogger(__name__)
MAGIC = b"PABX1"
HEADER_BYTES = 45
ACK = b"PABX1:OK"
TRANSFER_SECONDS = 30

_EXPORT = r"""
import hashlib, io, pathlib, struct, sys, tarfile
LIMIT=64*1024*1024
class Bounded(io.BytesIO):
    def write(self,data):
        if self.tell()+len(data)>LIMIT: raise RuntimeError('Profile archive exceeds its bound')
        return super().write(data)
count=0
def include(member):
    global count
    count+=1
    if count>20000 or member.size<0 or member.size>LIMIT:
        raise RuntimeError('Profile archive exceeds its bound')
    parts=pathlib.PurePosixPath(member.name).parts
    caches={'Cache','Code Cache','GPUCache','ShaderCache','BrowserMetrics'}
    if any(p.startswith('Singleton') or p in caches for p in parts): return None
    return member
buffer=Bounded()
with tarfile.open(fileobj=buffer,mode='w') as archive:
    archive.add('/tmp/sandboxd-browser-profile',arcname='sandboxd-browser-profile',filter=include)
payload=buffer.getvalue()
sys.stdout.buffer.write(b'PABX1'+struct.pack('>Q',len(payload))+hashlib.sha256(payload).digest())
sys.stdout.buffer.write(payload)
sys.stdout.buffer.flush()
"""

_IMPORT = r"""
import hashlib, io, os, pathlib, struct, sys, tarfile
LIMIT=64*1024*1024
header=sys.stdin.buffer.read(45)
if len(header)!=45 or header[:5]!=b'PABX1': raise RuntimeError('Invalid profile transfer header')
size=struct.unpack('>Q',header[5:13])[0]
if size<1 or size>LIMIT: raise RuntimeError('Profile transfer exceeds its bound')
payload=sys.stdin.buffer.read(size)
if len(payload)!=size or hashlib.sha256(payload).digest()!=header[13:]:
    raise RuntimeError('Incomplete profile transfer')
with tarfile.open(fileobj=io.BytesIO(payload),mode='r:') as archive:
    members=[]
    total=0
    for member in archive:
        members.append(member)
        total+=member.size
        parts=pathlib.PurePosixPath(member.name).parts
        if (len(members)>20000 or total>LIMIT or member.size<0 or not parts
            or pathlib.PurePosixPath(member.name).is_absolute() or '..' in parts
            or parts[0]!='sandboxd-browser-profile' or not(member.isfile() or member.isdir())):
            raise RuntimeError('Unsafe browser profile archive')
    for member in members:
        target=pathlib.Path('/tmp').joinpath(*pathlib.PurePosixPath(member.name).parts)
        if target.is_symlink() or any(p.is_symlink() for p in target.parents):
            raise RuntimeError('Browser profile destination contains a symbolic link')
        if member.isdir():
            target.mkdir(mode=0o700,parents=True,exist_ok=True)
        else:
            target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            source=archive.extractfile(member)
            if source is None: raise RuntimeError('Profile member is unreadable')
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as output, source:
                while True:
                    chunk=source.read(65536)
                    if not chunk: break
                    output.write(chunk)
sys.stdout.buffer.write(b'PABX1:OK')
sys.stdout.buffer.flush()
"""


def _open(container: Container, script: str) -> tuple[socket.socket, SocketOwner]:
    result: object = container.exec_run(["python3", "-u", "-c", script], stdin=True, socket=True)
    connection: object = getattr(result, "output", None)
    raw: object = getattr(connection, "_sock", connection)
    if not isinstance(raw, socket.socket):
        close = getattr(connection, "close", None)
        if callable(close):
            close()
        raise BrowserProfileError("Docker did not return a profile transfer socket")
    if not isinstance(connection, SocketOwner):
        raw.close()
        raise BrowserProfileError("Docker returned an unowned profile transfer socket")
    return raw, connection


def _read_exact(stream: socket.socket, size: int, deadline: float, *, eof: bool = False) -> bytes:
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserProfileError("Browser profile transfer exceeded its deadline")
        stream.settimeout(remaining)
        chunk = stream.recv(size - len(result))
        if not chunk:
            if eof and not result:
                return b""
            raise BrowserProfileError("Browser profile transfer ended before completion")
        result.extend(chunk)
    return bytes(result)


def _frames(stream: socket.socket, limit: int, deadline: float) -> bytes:
    result = bytearray()
    while True:
        header = _read_exact(stream, 8, deadline, eof=True)
        if not header:
            return bytes(result)
        length = struct.unpack(">I", header[4:])[0]
        if header[0] != 1 or header[1:4] != b"\0\0\0":
            # stderr may contain profile paths or values; never surface its body.
            raise BrowserProfileError("Browser profile transfer failed inside its container")
        if length > limit - len(result):
            raise BrowserProfileError("Browser profile transfer exceeded its bound")
        result.extend(_read_exact(stream, length, deadline))


def _close(stream: socket.socket, owner: SocketOwner) -> None:
    for resource in (stream, owner):
        try:
            resource.close()
        except Exception as error:
            logger.warning("Browser profile transport cleanup failed (%s)", type(error).__name__)


def export_archive(container: Container) -> bytes:
    """Read a complete length/digest-checked in-memory archive from live tmpfs."""
    deadline = time.monotonic() + TRANSFER_SECONDS
    stream, owner = _open(container, _EXPORT)
    try:
        data = _frames(stream, MAX_PROFILE_BYTES + HEADER_BYTES, deadline)
        if len(data) < HEADER_BYTES or data[:5] != MAGIC:
            raise BrowserProfileError("Browser profile transfer has an invalid header")
        size = struct.unpack(">Q", data[5:13])[0]
        payload = data[HEADER_BYTES:]
        if size != len(payload) or hashlib.sha256(payload).digest() != data[13:HEADER_BYTES]:
            raise BrowserProfileError("Browser profile transfer did not complete")
        return payload
    finally:
        _close(stream, owner)


def import_archive(container: Container, archive: bytes) -> None:
    """Send a validated bounded archive into tmpfs and require its completion ACK."""
    if not archive or len(archive) > MAX_PROFILE_BYTES:
        raise BrowserProfileError("Browser profile transfer exceeds its bound")
    header = MAGIC + struct.pack(">Q", len(archive)) + hashlib.sha256(archive).digest()
    deadline = time.monotonic() + TRANSFER_SECONDS
    stream, owner = _open(container, _IMPORT)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserProfileError("Browser profile transfer exceeded its deadline")
        stream.settimeout(remaining)
        stream.sendall(header + archive)
        if _frames(stream, len(ACK), deadline) != ACK:
            raise BrowserProfileError("Browser profile restore did not acknowledge completion")
    finally:
        _close(stream, owner)
