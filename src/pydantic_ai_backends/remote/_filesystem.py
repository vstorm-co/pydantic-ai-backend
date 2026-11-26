"""Trusted NFS export leases; clients never choose images, URLs or host paths."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from pydantic_ai_backends.remote.wire import FilesystemBinding

if TYPE_CHECKING:
    from docker import DockerClient
    from docker.models.containers import Container

MOUNT_PATH: Literal["/project"] = "/project"
STATE_DIRECTORY = ".filesystem-bindings"
_LABEL = "io.pydantic-ai-backend.filesystem"


class FilesystemMountError(RuntimeError):
    """A mount was refused or unavailable; its message contains no credential."""


class BindingProof(BaseModel):
    binding_id: UUID
    session_name: str
    tenant: str
    mount_path: Literal["/project"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_name: str
    tenant: str
    uid: int = Field(ge=0)
    binding: FilesystemBinding = Field(repr=False)
    gateway_url: str
    image: str
    exporter_epoch: tuple[str, str, int] | None = None
    exporter_ip: str | None = None


class _Health(BaseModel):
    status: str = Field(alias="Status")


class _ContainerState(BaseModel):
    status: str = Field(alias="Status")
    health: _Health | None = Field(default=None, alias="Health")
    started_at: str = Field(alias="StartedAt")


class _NetworkEndpoint(BaseModel):
    address: str = Field(alias="IPAddress")


class _Networks(BaseModel):
    networks: dict[str, _NetworkEndpoint] = Field(alias="Networks")


class _ContainerInfo(BaseModel):
    id: str = Field(alias="Id")
    restarts: int = Field(default=0, alias="RestartCount")
    state: _ContainerState = Field(alias="State")
    network: _Networks = Field(alias="NetworkSettings")


class FilesystemMountBroker:
    """One immutable binding per sandbox session, with operator-owned infrastructure.

    All blocking calls belong on sandboxd's worker pool. The metadata directory
    is outside every agent's mounted workspace. Docker access already grants
    host authority; labels and stable names keep cleanup within this broker.
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        image: str,
        workspace_root: str,
        client: Callable[[], DockerClient],
    ) -> None:
        parsed = urlsplit(gateway_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("filesystem_gateway_url must be a credential-free HTTP(S) URL")
        if not image.strip():
            raise ValueError("filesystem_export_image must not be empty")
        self.gateway_url = gateway_url.rstrip("/")
        self.image = image
        self.root = Path(workspace_root) / STATE_DIRECTORY
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("The filesystem binding directory must not be a symlink")
        self.root.chmod(0o700)
        self._owner = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:20]
        self._client = client
        self._lock = threading.RLock()
        self._volumes: dict[str, str] = {}

    def _names(self, session_name: str) -> tuple[str, str, str]:
        identity = hashlib.sha256(f"{self._owner}:{session_name}".encode()).hexdigest()[:24]
        return (
            f"projectfs-{identity}-export",
            f"projectfs-{identity}-net",
            f"projectfs-{identity}-volume",
        )

    def _path(self, session_name: str) -> Path:
        # Session names are wire-validated before reaching this internal API.
        if (
            not session_name
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for c in session_name
            )
            or session_name in {".", ".."}
        ):
            raise FilesystemMountError("Invalid filesystem session identity")
        return self.root / f"{session_name}.json"

    def _read(self, session_name: str) -> _Record | None:
        path = self._path(session_name)
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
                return _Record.model_validate_json(stream.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise FilesystemMountError("Filesystem binding metadata is unavailable") from None

    def _write(self, record: _Record) -> None:
        payload = record.model_dump(mode="json")
        payload["binding"]["token"] = record.binding.token.get_secret_value()
        path = self._path(record.session_name)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{record.session_name}.", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _verify(self, record: _Record) -> None:
        try:
            with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                response = client.get(
                    f"{self.gateway_url}/bindings/{record.binding.id}",
                    headers={"Authorization": f"Bearer {record.binding.token.get_secret_value()}"},
                )
            response.raise_for_status()
            proof = BindingProof.model_validate_json(response.content)
        except (httpx.HTTPError, ValueError):
            raise FilesystemMountError("The filesystem gateway refused this binding") from None
        if (proof.binding_id, proof.session_name, proof.tenant) != (
            record.binding.id,
            record.session_name,
            record.tenant,
        ):
            raise FilesystemMountError("The filesystem binding does not match this session")

    def volume_for(self, session_name: str) -> str | None:
        with self._lock:
            return self._volumes.get(session_name)

    def prepare(
        self,
        session_name: str,
        tenant: str | None,
        binding: FilesystemBinding | None,
        *,
        existing: bool,
        uid: int,
    ) -> None:
        """Verify before admission and establish a mount, refusing implicit migration."""
        from docker.errors import DockerException

        with self._lock:
            stored = self._read(session_name)
            if binding is None:
                if stored is not None:
                    raise FilesystemMountError(
                        "This session requires its existing filesystem binding"
                    )
                return
            if tenant is None:
                raise FilesystemMountError("A filesystem binding requires a named tenant")
            record = _Record(
                session_name=session_name,
                tenant=tenant,
                uid=uid,
                binding=binding,
                gateway_url=self.gateway_url,
                image=self.image,
            )
            if stored is not None:
                if (
                    stored.gateway_url != self.gateway_url
                    or stored.uid != uid
                    or stored.image != self.image
                    or stored.tenant != tenant
                    or stored.binding.id != binding.id
                    or not secrets.compare_digest(
                        stored.binding.token.get_secret_value(), binding.token.get_secret_value()
                    )
                ):
                    raise FilesystemMountError(
                        "Filesystem bindings are immutable; explicitly reset this session "
                        "before changing one"
                    )
            elif existing:
                raise FilesystemMountError(
                    "This existing session has no filesystem mount; explicitly recreate it "
                    "without discarding its workspace"
                )
            record = stored if stored is not None else record
            self._verify(record)
            try:
                if stored is None:
                    self._write(record)
                self._ensure(record)
            except (DockerException, OSError, ValueError, KeyError, FilesystemMountError):
                # Docker errors can include request payloads. Do not expose them.
                raise FilesystemMountError("The filesystem export could not be prepared") from None

    def check(self, session_name: str) -> None:
        """A missing, stopped or revoked helper must never degrade into local files."""
        from docker.errors import DockerException

        with self._lock:
            record = self._read(session_name)
            if record is None:
                return
            try:
                self._verify(record)
                exporter = self._client().containers.get(self._names(session_name)[0])
                exporter.reload()
                info = _ContainerInfo.model_validate(exporter.attrs)
                self._assert_epoch(record, info)
            except (DockerException, ValueError, KeyError, FilesystemMountError):
                self._stop_agent(session_name)
                raise FilesystemMountError(
                    "The filesystem export changed or is unavailable; restart the sandbox "
                    "filesystem explicitly. Execution was refused."
                ) from None

    def _ensure(self, record: _Record) -> None:
        from docker.errors import NotFound

        client = self._client()
        export_name, network_name, volume_name = self._names(record.session_name)
        labels = {
            _LABEL: self._owner,
            f"{_LABEL}.session": record.session_name,
            f"{_LABEL}.binding": str(record.binding.id),
        }
        try:
            exporter = client.containers.get(export_name)
        except NotFound:
            # Replacing a missing helper under a retained agent would leave its
            # kernel NFS mount pointing at another server/handle namespace.
            try:
                retained = client.containers.get(f"sandboxd-{record.session_name}")
            except NotFound:
                pass
            else:
                retained.stop(timeout=5)
                raise FilesystemMountError(
                    "The retained session lost its export; explicit recreation is required"
                )
            try:
                network = client.networks.get(network_name)
            except NotFound:
                network = client.networks.create(
                    network_name, driver="bridge", internal=False, labels=labels
                )
            if network.attrs.get("Labels") != labels:
                raise FilesystemMountError(
                    "Refusing an unowned filesystem control network"
                ) from None
            exporter = client.containers.run(
                self.image,
                entrypoint=["/usr/local/bin/projectfs-export"],
                command=[],
                healthcheck={
                    "test": ["CMD", "/usr/local/bin/projectfs-export", "healthcheck"],
                    "interval": 1_000_000_000,
                    "timeout": 3_000_000_000,
                    "start_period": 1_000_000_000,
                    "retries": 5,
                },
                name=export_name,
                network=network_name,
                detach=True,
                labels=labels,
                environment={
                    "PROJECTFS_RPC_URL": f"{self.gateway_url}/bindings/{record.binding.id}/fs",
                    "PROJECTFS_TOKEN": record.binding.token.get_secret_value(),
                    "PROJECTFS_UID": str(record.uid),
                    "PROJECTFS_GID": str(record.uid),
                },
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                read_only=True,
                mem_limit="128m",
                pids_limit=64,
                extra_hosts={"host.docker.internal": "host-gateway"},
            )
        if any(exporter.labels.get(key) != value for key, value in labels.items()):
            raise FilesystemMountError("Filesystem export ownership does not match")
        exporter.reload()
        info = _ContainerInfo.model_validate(exporter.attrs)
        if record.exporter_epoch is not None:
            try:
                self._assert_epoch(record, info)
            except FilesystemMountError:
                self._stop_agent(record.session_name)
                raise
        address = self._wait(exporter, network_name)
        options = {
            "type": "nfs",
            "device": ":/",
            "o": (
                f"addr={address},nfsvers=3,tcp,port=2049,mountport=2049,"
                "nolock,noac,lookupcache=none,sync,soft,timeo=20,retrans=1"
            ),
        }
        try:
            volume = client.volumes.get(volume_name)
        except NotFound:
            volume = client.volumes.create(
                volume_name, driver="local", driver_opts=options, labels=labels
            )
        if volume.attrs.get("Labels") != labels or volume.attrs.get("Options") != options:
            raise FilesystemMountError(
                "Filesystem volume identity changed; explicit recreation is required"
            )
        self._write(
            record.model_copy(
                update={
                    "exporter_epoch": (info.id, info.state.started_at, info.restarts),
                    "exporter_ip": address,
                }
            )
        )
        self._volumes[record.session_name] = volume_name

    def _stop_agent(self, session_name: str) -> None:
        from docker.errors import NotFound

        try:
            agent = self._client().containers.get(f"sandboxd-{session_name}")
        except NotFound:
            return
        agent.stop(timeout=5)

    @staticmethod
    def _assert_epoch(record: _Record, info: _ContainerInfo) -> None:
        if (
            info.state.status != "running"
            or info.state.health is None
            or info.state.health.status != "healthy"
            or record.exporter_epoch != (info.id, info.state.started_at, info.restarts)
            or len(info.network.networks) != 1
            or record.exporter_ip != next(iter(info.network.networks.values())).address
        ):
            raise FilesystemMountError(
                "The filesystem export epoch changed; explicitly restart its sandbox"
            )

    @staticmethod
    def _wait(exporter: Container, network_name: str) -> str:
        for _ in range(100):
            exporter.reload()
            info = _ContainerInfo.model_validate(exporter.attrs)
            if info.state.status != "running":
                raise FilesystemMountError("The filesystem export stopped during startup")
            if info.state.health is None:
                raise FilesystemMountError(
                    "The filesystem export image must define its readiness healthcheck"
                )
            if info.state.health.status == "healthy":
                return info.network.networks[network_name].address
            time.sleep(0.1)
        raise FilesystemMountError("The filesystem export did not become ready")

    def suspend(self, session_name: str, *, purge: bool = False) -> None:
        """Clean only owned resources, retaining mounts referenced by a saved container."""
        from docker.errors import NotFound

        with self._lock:
            self._volumes.pop(session_name, None)
            client = self._client()
            export_name, network_name, volume_name = self._names(session_name)
            try:
                retained = client.containers.get(f"sandboxd-{session_name}")
            except NotFound:
                retained = None
            try:
                exporter = client.containers.get(export_name)
            except NotFound:
                exporter = None
            if retained is not None:
                return
            if exporter is not None:
                if exporter.labels.get(_LABEL) != self._owner:
                    raise FilesystemMountError("Refusing to stop an unowned filesystem export")
                exporter.stop(timeout=5)
            if exporter is not None:
                exporter.remove()
            for collection, name in [
                (client.volumes, volume_name),
                (client.networks, network_name),
            ]:
                try:
                    resource = collection.get(name)
                except NotFound:
                    continue
                labels = resource.attrs.get("Labels") or {}
                if (
                    labels.get(_LABEL) != self._owner
                    or labels.get(f"{_LABEL}.session") != session_name
                ):
                    raise FilesystemMountError("Refusing to remove unowned filesystem resources")
                resource.remove()
            if purge:
                self._path(session_name).unlink(missing_ok=True)

    def sweep(self, active: set[str]) -> None:
        """Reconcile idle/restarted sessions through the normal sandboxd sweep."""
        for path in self.root.glob("*.json"):
            name = path.stem
            if name not in active:
                self.suspend(name, purge=not (self.root.parent / name).exists())
