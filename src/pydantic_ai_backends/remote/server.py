"""sandboxd — an HTTP service that owns Docker and rents out sandboxes.

Run this next to the Docker socket and point applications at it with
:class:`~pydantic_ai_backends.remote.RemoteSandbox`. The application never needs
Docker access, which is the whole point: a containerised app that mounted the
socket to start sandboxes would be handing itself host root.

Security model
--------------
A process that can talk to the Docker daemon can start a privileged container
that bind-mounts `/`, so it is root-equivalent on the host. This service is
therefore the most sensitive process in the deployment, and:

- **Clients choose nothing about the container.** Image, mounts, network mode
  and every resource ceiling come from :class:`SandboxdConfig`. A request
  carries at most a runtime *alias*, validated against the allowlist.
- **Session ids are pattern-checked** (:data:`~.wire.SESSION_ID_PATTERN`) before
  reaching a path or a container, so they cannot traverse directories.
- **Each session gets its own token**, so one tenant cannot reach another's
  sandbox. The service token is the only one that may open or enumerate
  sessions.
- Bind it to a private network. It has no TLS and no rate limiting of its own.

Example:
    ```python
    from pydantic_ai_backends.remote.server import SandboxdConfig, create_app

    app = create_app(
        SandboxdConfig(
            token="...",
            runtimes={"python": "python:3.12-slim"},
            mem_limit="1g",
            cpus=2.0,
        )
    )
    ```
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import inspect
import logging
import os
import re
import secrets
import shutil
import stat
import time
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypeVar, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse

from pydantic_ai_backends.adapter import ensure_async
from pydantic_ai_backends.backends.docker._image import buildkit_available
from pydantic_ai_backends.backends.docker.runtimes import get_runtime
from pydantic_ai_backends.backends.docker.session import (
    SessionLimitExceeded,
    SessionManager,
    alive_of,
    last_activity_of,
)
from pydantic_ai_backends.remote import wire
from pydantic_ai_backends.remote._workspace import (
    WorkspacePathError,
    list_workspace,
    read_workspace,
    read_workspace_bytes,
    relative_request_path,
    workspace_root_for,
)
from pydantic_ai_backends.types import RuntimeConfig, SandboxUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from pydantic_ai_backends.protocol import AsyncSandboxProtocol

_logger = logging.getLogger(__name__)

_T = TypeVar("_T")

SandboxBuilder = Callable[[str, "SandboxRuntime"], Any]
"""Builds a sandbox for the service: `(session_id, runtime) -> sandbox`."""


@dataclass(frozen=True, slots=True)
class SandboxRuntime:
    """One entry in the service's runtime allowlist.

    A client names the *alias* this is registered under and nothing else, so
    everything here is the operator's decision. Give it either a ready-made
    `image`, or a `runtime` whose packages are built into an image on first use.

    The ceilings are per runtime, because they are not one number for a whole
    service: a notebook-style data runtime needs several gigabytes where a plain
    shell needs a few hundred megabytes, and forcing one value on both either
    starves the first or over-commits the host for the second. A ceiling left
    `None` takes the service-wide default from :class:`SandboxdConfig`, which is
    a *default* rather than a maximum — a runtime may name more.

    Attributes:
        image: Ready-made image, started as-is.
        runtime: `RuntimeConfig` (or the name of a built-in one) whose packages
            are installed into an image the first time this runtime is used.
            Later sessions hit the build cache.
        description: What this environment is for, shown in the dashboard.
        mem_limit: Memory ceiling in Docker syntax, e.g. `"2g"`.
        memswap_limit: Ceiling on memory and swap combined. `None` pins it to
            `mem_limit`, denying the sandbox swap. Only worth raising on a host
            whose swap is `zram`, where the pages stay in compressed RAM and the
            alternative to a little swapping is an OOM kill.
        cpus: Hard CPU ceiling in cores. A sandbox never exceeds it — and so
            cannot use cores that are idle.
        cpu_shares: Relative CPU weight, which applies only under contention. On
            a small host this is usually the better knob: one active sandbox may
            take the whole machine, and several are still divided fairly.
        pids_limit: Process ceiling.
        network_mode: Docker network mode. Naming anything but `"none"` here is
            how one runtime is allowed the network while others are not.
        oci_runtime: Low-level runtime the daemon starts this runtime's
            containers with — `"runsc"` for gVisor, `"kata"` for a microVM per
            container. Unlike the ceilings above this changes the *isolation
            boundary*, and it is per runtime for the same reason they are: the
            runtime that installs arbitrary packages off the network is the one
            worth paying gVisor's I/O overhead for, while a plain shell is not.
            Must be registered with the daemon; `None` takes its default.
    """

    image: str | None = None
    runtime: RuntimeConfig | str | None = None
    description: str = ""
    mem_limit: str | None = None
    memswap_limit: str | None = None
    cpus: float | None = None
    cpu_shares: int | None = None
    pids_limit: int | None = None
    network_mode: str | None = None
    oci_runtime: str | None = None

    def __post_init__(self) -> None:
        if (self.image is None) == (self.runtime is None):
            raise ValueError("A SandboxRuntime needs exactly one of image or runtime")

    @property
    def builds(self) -> bool:
        """Whether first use builds an image rather than pulling a ready one."""
        return self.runtime is not None

    def resolved_runtime(self) -> RuntimeConfig | None:
        """The `RuntimeConfig` this entry builds, looked up when named."""
        if self.runtime is None:
            return None
        if isinstance(self.runtime, str):
            return get_runtime(self.runtime)
        return self.runtime

    def describes(self) -> str:
        """Human-readable summary of what will run, for the policy view."""
        if self.description:
            return self.description
        built = self.resolved_runtime()
        if built is not None:
            return built.description or f"built from {built.base_image or built.image}"
        return self.image or ""

    def image_label(self) -> str:
        """The image, or what it is built from when there is not one yet."""
        if self.image is not None:
            return self.image
        built = self.resolved_runtime()
        assert built is not None
        return built.image or f"{built.base_image} + {len(built.packages)} package(s)"


def _as_runtime(entry: str | SandboxRuntime) -> SandboxRuntime:
    """Accept a bare image string wherever a runtime entry is expected."""
    return SandboxRuntime(image=entry) if isinstance(entry, str) else entry


DEFAULT_RUNTIMES: dict[str, SandboxRuntime] = {
    "coding": SandboxRuntime(
        runtime="coding",
        # Needs the network for the reason it exists: an agent working on code
        # installs what the project declares.
        network_mode="bridge",
    ),
    "python": SandboxRuntime(
        image="python:3.12-slim",
        description="Python 3.12, standard library only",
    ),
    "node": SandboxRuntime(
        image="node:20-slim",
        description="Node.js 20, no extra packages",
    ),
}
"""What a service allows when its operator names nothing.

No ceilings of their own, because a default that raised them would size the host
on the operator's behalf. Richer catalogues are opt-in — see
:data:`SUGGESTED_RUNTIMES`.

`coding` is the exception to "nothing here builds an image", and it is the
default because a sandbox for an agent working on code without `git` is not one.
Measured at 99.7 MB and eleven seconds to build; `prewarm` — on by default —
does it as the service starts rather than inside somebody's first request. A
host that cannot reach a Debian mirror will fail to build it, which `prewarm`
logs and skips; `python` and `node` stay here as ready-made fallbacks for
exactly that case.
"""

SUGGESTED_RUNTIMES: dict[str, SandboxRuntime] = {
    "coding": SandboxRuntime(
        runtime="coding",
        mem_limit="1g",
        cpus=2.0,
        network_mode="bridge",
    ),
    "polyglot": SandboxRuntime(
        runtime="polyglot",
        # The generalist: an agent that writes a script, a page and a stylesheet,
        # fetches something and installs what it is missing. Needs the network for
        # exactly that, which is why it is the entry to read before enabling.
        mem_limit="1g",
        cpus=2.0,
        network_mode="bridge",
    ),
    "python": SandboxRuntime(
        image="python:3.12-slim",
        description="Python 3.12, standard library only",
        mem_limit="1g",
        cpus=1.0,
    ),
    "python-analytics": SandboxRuntime(
        runtime="python-analytics",
        # DuckDB and Polars stream rather than materialise, so the ceiling is
        # sized for the task. Measured: a groupby over a 188 MB CSV peaks at
        # 312 MB here where pandas needs over 570 MB for the same answer.
        mem_limit="1g",
        cpus=2.0,
    ),
    "python-datascience": SandboxRuntime(
        runtime="python-datascience",
        # Four gigabytes is pandas' appetite, not the task's: it materialises the
        # whole frame and copies freely, so the same CSV that analytics handles in
        # 312 MB is killed here under anything less than about 600 MB. Kept
        # because most model-written code reaches for pandas first.
        mem_limit="4g",
        cpus=2.0,
    ),
    "python-documents": SandboxRuntime(
        runtime="python-documents",
        mem_limit="2g",
    ),
    "python-scraping": SandboxRuntime(
        runtime="python-scraping",
        mem_limit="1g",
        network_mode="bridge",
    ),
    "node": SandboxRuntime(
        image="node:20-slim",
        description="Node.js 20, no extra packages",
        mem_limit="1g",
    ),
    "node-typescript": SandboxRuntime(
        runtime="node-typescript",
        mem_limit="2g",
    ),
    "go": SandboxRuntime(runtime="go", mem_limit="2g", cpus=2.0),
    "rust": SandboxRuntime(runtime="rust", mem_limit="4g", cpus=2.0),
}
"""A fuller catalogue an operator can adopt or copy from.

Not the default, because each entry is a commitment the operator makes on their
own host: `python-datascience` builds an image on first use and is given four
gigabytes, and `python-scraping` is the one runtime here allowed the network,
which is exactly the sort of decision that should be read before it is enabled.

```python
SandboxdConfig(token=..., runtimes=SUGGESTED_RUNTIMES, default_runtime="python")
```
"""

_UNAUTHORIZED = "Invalid or missing sandbox token"


def _token_matches(supplied: str, expected: str) -> bool:
    """Compare two tokens in constant time, whatever bytes the header carried.

    Header values reach a handler latin-1 decoded, so a client is free to send a
    byte over 127 — and `secrets.compare_digest` refuses a `str` that is not
    ASCII outright rather than returning `False`. Comparing the encoded forms
    keeps a malformed token a 401 instead of an unauthenticated 500.
    """
    return secrets.compare_digest(
        supplied.encode("utf-8", "surrogateescape"), expected.encode("utf-8")
    )


_CONTAINER_PREFIX = "sandboxd-"
"""Name prefix every persisted sandbox container carries, so a sweep can find
them without a record of its own — after a restart, Docker is the only source."""

_UI_FILE = Path(__file__).parent / "ui" / "index.html"
"""The bundled dashboard — one self-contained file, no build step, no CDN."""


@functools.cache
def _ui_html() -> str:
    """The dashboard's markup, read from disk once rather than per request."""
    return _UI_FILE.read_text(encoding="utf-8")


@dataclass(slots=True)
class SandboxdConfig:
    """Everything the service decides on a client's behalf.

    Attributes:
        token: Service token. Required to open, list or inspect any session.
        runtimes: Allowlist mapping a client-visible alias to what may run
            under it — a bare image string, or a :class:`SandboxRuntime` carrying
            its own ceilings and, if it needs them, packages to build in. A
            request naming anything else is rejected; this is what stops a client
            from running an image of its choosing.
        default_runtime: Alias used when a request names none. Empty — the
            default — takes the first entry in `runtimes`, so the shipped
            allowlist defaults to `coding` while an operator who supplies their
            own gets whichever they listed first. Naming a specific alias here
            still wins; what it no longer does is force every custom allowlist
            to contain a particular key.
        max_sessions: Ceiling on *resident sandboxes* across the service, or
            `None` for no ceiling. Together with the largest per-runtime memory
            ceiling this is what bounds the worst case an operator has to size
            the host for, so `None` is for hosts where something else does the
            bounding. It does not bound how many sessions may exist — see
            `max_open_sessions`.
        max_open_sessions: Ceiling on sessions that *exist*, resident or
            hibernated, or `None` for no ceiling. A hibernated session costs
            disk and a few kilobytes of bookkeeping rather than memory, so this
            number is properly much larger than `max_sessions` — that gap is the
            difference between the sessions a host can carry and the ones it can
            run at once. Only meaningful with `evict_idle_after`, which is what
            demotes a session to hibernated in the first place.
        evict_idle_after: Seconds of inactivity after which a session at the
            ceiling may be hibernated to make room for a new one, instead of the
            new one being refused with `429`. This is what turns `max_sessions`
            from a hard cap on how many sessions may *exist* into a working-set
            size: the hibernated session keeps its token, its event log and its
            files, and its next request wakes it where it left off. `None`
            refuses instead of evicting. Requires `workspace_root` — hibernating
            a session whose files live only in its container would discard them
            silently, which is not a trade a config should make quietly.
        max_sessions_per_tenant: Ceiling on simultaneous sessions carrying one
            `tenant` label. Without it, one tenant of the calling application can
            occupy the whole pool and every other tenant gets `429`.
        mem_limit: Default memory ceiling per sandbox, in Docker syntax. A
            runtime naming its own overrides this, upwards or downwards.
        memswap_limit: Default ceiling on memory and swap combined. `None` pins
            it to `mem_limit`, so a sandbox at its ceiling is killed rather than
            allowed to swap — which is right when swap is a disk, because one
            swapping sandbox slows every other one on the host.

            Raise it only where swap is `zram`. There the pages stay in RAM
            compressed at roughly 3:1, so a sandbox briefly over its ceiling
            costs CPU instead of the whole host's responsiveness, and on a small
            box that trade is what turns an OOM kill into a slow command.
        cpus: Default hard CPU ceiling per sandbox, in cores. `None` leaves
            sandboxes free to use whatever is idle, bounded only by `cpu_shares`.
        cpu_shares: Default relative CPU weight, applied only under contention.
            Preferable to a hard `cpus` on a small host, where capping each
            sandbox to one core leaves the others unused while a single agent
            waits.
        tmpfs_size: Size of an in-memory `/tmp` given to every sandbox, in Docker
            syntax. Scratch writes then never reach the container's write layer,
            which is both faster and the difference between a busy sandbox
            growing on disk and not. `None` leaves `/tmp` on the overlay.

            Sized against `mem_limit` rather than on top of it, because tmpfs
            pages are charged to the container's own memory cgroup — so this is
            memory taken away from the workload, and worth keeping small when
            many sessions are open at once.
        pids_limit: Default process ceiling per sandbox.
        network_mode: Default Docker network mode. `"none"` because a service
            handing sandboxes to untrusted code should not give them the network
            unless that is a deliberate choice, per runtime or service-wide.
        oci_runtime: Default low-level runtime for every sandbox, overridable per
            runtime. `None` takes the daemon's default — which is the right
            default for us to ship, because naming a runtime the host has not
            registered turns every session into a `502`. Set it to `"runsc"` once
            gVisor is installed to put every sandbox behind a userspace syscall
            layer; `crun` belongs in the daemon's own config instead, since it is
            a faster `runc` rather than a different isolation model.
        work_dir: Working directory inside every sandbox. Applied to built
            runtimes as well, overriding whatever their `RuntimeConfig` says, so
            that one directory is where the workspace volume is mounted, what
            the archive endpoints read, and what a client's paths resolve
            against. Three places disagreeing about it is how files go missing.
        workspace_root: Host directory backing each sandbox's work directory, as
            `{workspace_root}/{session_id}/workspace`. Without it a sandbox's
            files live only in the container's write layer, so idle reaping
            destroys them — which a session spanning several runs will notice.
            Session ids are pattern-checked before they reach here, so one
            cannot traverse out of this root.
        prewarm: Pull and build the whole allowlist in the background at
            startup. Without it the first session on a built runtime pays for the
            image build — a measured ten seconds and upwards — and pays it in the
            middle of somebody's request. Only applies to the default Docker
            builder, since nothing else knows how to warm a custom one.
        persist_containers: Give each sandbox a stable container name, so a
            reaped session keeps its filesystem and the next attach restarts the
            same container. `workspace_root` alone preserves only the work
            directory — packages installed with `pip` or `apt` live in the
            container's write layer, and an agent that reinstalls them after
            every idle timeout is not working in "the same" machine. The cost is
            that stopped containers accumulate until a session is closed with
            `purge`, so this is off by default.
        idle_timeout: Seconds of inactivity before a session is reaped.
        cleanup_interval: Seconds between reaping passes.
        workspace_ttl: Seconds an unused workspace directory is kept before it is
            swept, timed from when its session was last opened. `None` — the
            default — keeps a session's **files** for ever, which is usually what
            an agent's user expects: the notes and scripts are the work. Set it
            only where a retention policy says otherwise. Only meaningful with
            `workspace_root`.
        container_ttl: Seconds a *stopped* persisted container is kept before it
            is removed, timed from when it stopped. This reclaims what a session
            installed inside its container — the build, the wheels, the
            `node_modules` — while leaving its workspace untouched, so the files
            survive and only the rebuildable part goes. `None` keeps them.
            Only meaningful with `persist_containers`.
        sandbox_uid: Run every built runtime's sandbox as this unprivileged
            user instead of root, and give each session's workspace to it.
            `None` — the default — keeps the current behaviour, where a sandbox
            runs as root.

            Worth turning on. A container escape starts from whoever the
            container runs as, and every file an agent writes into its
            bind-mounted workspace is owned by that user on the host — as root,
            a `sandboxd` running unprivileged cannot clean up after its own
            sessions. The image is built around the uid: a real account, a home
            directory, and a virtualenv it owns first on `PATH`, without which
            an agent's `pip install` fails and `uv` — having no `--user` mode —
            fails with no way forward.

            Two things it asks of the deployment. The service must be able to
            `chown` each session's workspace to this uid, which means either
            running with the privilege or running *as* that uid so the
            directories are created owned by it; a service that can do neither
            is told so when the session opens rather than handing an agent a
            workspace it cannot write to. And ready-made runtimes are left as
            root, since an image nobody built for this has no such user and no
            virtualenv, so an agent inside one could install nothing.
        max_read_bytes: Largest single file a client may read out of a sandbox.
        execute_timeout: Hard ceiling applied to every command, so one client
            cannot occupy a worker indefinitely.
        max_workers: Threads dedicated to blocking sandbox calls. Sandbox
            commands are long, so this pool is kept separate from asyncio's
            shared default one.
        ui_enabled: Serve the bundled dashboard at `/ui`. Off by default: the
            page asks a human for the service token, and that token can start
            containers on the host, so it belongs on localhost or a private
            network and never on a public listener. The HTTP API is unaffected
            either way.
    """

    token: str
    runtimes: Mapping[str, str | SandboxRuntime] = field(
        default_factory=lambda: dict(DEFAULT_RUNTIMES)
    )
    default_runtime: str = ""
    max_sessions: int | None = 20
    max_open_sessions: int | None = None
    max_sessions_per_tenant: int | None = None
    evict_idle_after: int | None = None
    mem_limit: str | None = "1g"
    memswap_limit: str | None = None
    cpus: float | None = 2.0
    cpu_shares: int | None = None
    pids_limit: int | None = 512
    tmpfs_size: str | None = "64m"
    network_mode: str | None = "none"
    oci_runtime: str | None = None
    work_dir: str = "/workspace"
    workspace_root: str | None = None
    persist_containers: bool = False
    prewarm: bool = True
    idle_timeout: int = 1800
    cleanup_interval: int = 300
    workspace_ttl: int | None = None
    container_ttl: int | None = None
    sandbox_uid: int | None = None
    max_read_bytes: int = 8 * 1024 * 1024
    execute_timeout: int = 300
    max_workers: int = 32
    ui_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("SandboxdConfig.token must not be empty")
        if not self.runtimes:
            raise ValueError("SandboxdConfig.runtimes must allow at least one image")
        if not self.default_runtime:
            self.default_runtime = next(iter(self.runtimes))
        if self.default_runtime not in self.runtimes:
            raise ValueError(
                f"default_runtime {self.default_runtime!r} is not in runtimes "
                f"({', '.join(sorted(self.runtimes))})"
            )
        if self.evict_idle_after is not None and self.workspace_root is None:
            raise ValueError(
                "evict_idle_after needs workspace_root: hibernating a session whose "
                "files live only in its container would discard them silently"
            )
        if (
            self.max_open_sessions is not None
            and self.max_sessions is not None
            and self.max_open_sessions < self.max_sessions
        ):
            raise ValueError(
                f"max_open_sessions ({self.max_open_sessions}) is below max_sessions "
                f"({self.max_sessions}): a session has to exist to be resident"
            )

    def resolve_runtime(self, alias: str | None) -> tuple[str, SandboxRuntime]:
        """Resolve a client-supplied alias to an allowed runtime.

        Args:
            alias: Runtime alias from the request, or `None` for the default.

        Returns:
            The `(alias, runtime)` pair to use.

        Raises:
            KeyError: If the alias is not on the allowlist.
        """
        chosen = alias or self.default_runtime
        return chosen, _as_runtime(self.runtimes[chosen])

    def limits_for(self, runtime: SandboxRuntime) -> dict[str, Any]:
        """The ceilings one runtime actually runs under.

        Its own values where it names them, the service defaults otherwise.
        """
        return {
            "mem_limit": runtime.mem_limit if runtime.mem_limit is not None else self.mem_limit,
            "memswap_limit": (
                runtime.memswap_limit if runtime.memswap_limit is not None else self.memswap_limit
            ),
            "cpus": runtime.cpus if runtime.cpus is not None else self.cpus,
            "cpu_shares": (
                runtime.cpu_shares if runtime.cpu_shares is not None else self.cpu_shares
            ),
            "pids_limit": (
                runtime.pids_limit if runtime.pids_limit is not None else self.pids_limit
            ),
            "network_mode": (
                runtime.network_mode if runtime.network_mode is not None else self.network_mode
            ),
            "oci_runtime": (
                runtime.oci_runtime if runtime.oci_runtime is not None else self.oci_runtime
            ),
        }


_EVENT_HISTORY = 200
"""Activity entries kept per session, so a long-running one cannot grow the
service's memory without limit."""

_TARGET_MAX_CHARS = 200
"""Commands and paths are recorded for context, not archived verbatim."""

USAGE_CACHE_SECONDS = 5.0
"""How long a resource sample is served before the daemon is asked again.

Docker's stats endpoint waits for a second sample to compute a CPU delta, so one
call costs **over a second** — measured at 1–2s. A dashboard polling every few
seconds cannot pay that per sandbox per poll, and a slightly stale number is
exactly what a dashboard wants anyway.
"""


@dataclass(slots=True)
class _Session:
    """Service-side bookkeeping for one session.

    Outlives the session's sandbox. A session evicted to make room keeps this
    record — its token, its runtime and its event log — while its container is
    stopped, so the caller comes back to the same session rather than being told
    it no longer exists.
    """

    runtime: str
    token: str
    created_at: float
    tenant: str | None = None
    events: deque[wire.SessionEvent] = field(default_factory=lambda: deque(maxlen=_EVENT_HISTORY))
    next_seq: int = 1
    hibernated_at: float | None = None
    """When the sandbox was stopped to free a slot, or `None` while resident."""


@dataclass(slots=True)
class _Pending:
    """An id claimed for a session, from the request that claimed it onwards.

    Outlives the open itself: `_new_sandbox` reads the runtime again every time a
    dead container is healed, so this lives as long as the session does. Which is
    what makes it usable as the reservation too — an id present here is either
    open or being opened, and either way it is taken.
    """

    runtime: SandboxRuntime
    tenant: str | None


@dataclass(slots=True)
class _Outcome:
    """Mutable result slot filled in by an operation being observed."""

    ok: bool = False
    detail: str = ""


def _default_builder(config: SandboxdConfig) -> SandboxBuilder:
    """Build the Docker-backed sandbox factory described by `config`."""

    def build(session_id: str, runtime: SandboxRuntime) -> Any:
        from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox

        built = runtime.resolved_runtime()
        # A ready-made image was not built around the sandbox user and has no
        # virtualenv it owns, so running it unprivileged would leave an agent
        # unable to install anything. Only built runtimes take the uid.
        overrides: dict[str, Any] = {"work_dir": config.work_dir}
        if config.sandbox_uid is not None:
            overrides["run_as_uid"] = config.sandbox_uid
        return DockerSandbox(
            # One work directory service-wide: it is where the workspace volume
            # is mounted and what the archive endpoints read, so a runtime's own
            # `work_dir` is overridden rather than allowed to disagree.
            runtime=built.model_copy(update=overrides) if built else None,
            image=runtime.image or "",
            session_id=session_id,
            work_dir=config.work_dir,
            volumes=_session_volumes(config, session_id),
            container_name=_container_name(config, session_id),
            idle_timeout=config.idle_timeout,
            max_read_bytes=config.max_read_bytes,
            tmpfs={"/tmp": f"size={config.tmpfs_size}"} if config.tmpfs_size else None,
            **config.limits_for(runtime),
        )

    return build


def _container_name(config: SandboxdConfig, session_id: str) -> str | None:
    """Stable container name for a session, when containers are persisted.

    Session ids are already restricted to characters Docker accepts in a name,
    so the prefix is the only thing that has to be added.
    """
    if not config.persist_containers:
        return None
    return f"{_CONTAINER_PREFIX}{session_id}"


def _session_dir(config: SandboxdConfig, session_id: str) -> Path | None:
    """Host directory holding one session's workspace, when configured."""
    if config.workspace_root is None:
        return None
    return Path(config.workspace_root) / session_id


def _session_volumes(config: SandboxdConfig, session_id: str) -> dict[str, str] | None:
    """Host mount backing one session's work directory, when configured."""
    session_dir = _session_dir(config, session_id)
    if session_dir is None:
        return None
    workspace = workspace_root_for(session_dir.parent, session_id)
    workspace.mkdir(parents=True, exist_ok=True)
    if config.sandbox_uid is not None:
        _hand_workspace_to(workspace, config.sandbox_uid)
    # The sweep reads this mtime as "when the session was last opened", and
    # mkdir on an existing directory does not touch it.
    os.utime(session_dir)
    return {str(workspace.resolve()): config.work_dir}


def _hand_workspace_to(workspace: Path, uid: int) -> None:
    """Give one session's workspace to the user its sandbox will run as.

    A sandbox running unprivileged writes into this directory as `uid`, so the
    directory has to belong to `uid` — otherwise the agent's very first file
    write fails, which is a worse outcome than the privilege this is buying back.

    Raises:
        RuntimeError: When the service cannot change the ownership, naming what
            it would have to be able to do. Starting the sandbox anyway would
            hand an agent a workspace it cannot write to, and the cause would
            surface much later as an unexplained permission error inside a
            container.
    """
    try:
        # Owner only. The container writes as this uid, so the owner bits are
        # what decide; setting the group as well would additionally require the
        # service to be a member of a group it may well not have.
        os.chown(workspace, uid, -1)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot give {workspace} to uid {uid} for an unprivileged sandbox: {exc}. "
            "Either run sandboxd with the privilege to chown, or run it as that uid "
            "so the directory is created owned by it, or unset sandbox_uid."
        ) from exc


def sweep_containers(config: SandboxdConfig, client: Any, now: float) -> list[str]:
    """Remove stopped sandbox containers older than `container_ttl`.

    What a session installed inside its container is rebuildable; what it wrote
    to its workspace is not. So this reclaims the first and never touches the
    second — the disk equivalent of throwing away a build directory and keeping
    the source.

    Args:
        config: Service configuration; a `container_ttl` of `None` sweeps nothing.
        client: Docker client used to list and remove containers.
        now: Current wall clock, against which each container's stop time is aged.

    Returns:
        The container names actually removed.
    """
    if config.container_ttl is None or not config.persist_containers:
        return []

    removed: list[str] = []
    try:
        containers = client.containers.list(all=True, filters={"name": _CONTAINER_PREFIX})
    except Exception:  # pragma: no cover - listing failure must not break the loop
        return removed

    for container in containers:
        state = (container.attrs or {}).get("State") or {}
        if state.get("Running"):
            continue
        stopped_at = _stopped_at(state)
        if stopped_at is None or now - stopped_at <= config.container_ttl:
            continue
        try:
            container.remove(force=True)
        except Exception:
            continue
        removed.append(container.name)

    if removed:
        _logger.info("Removed %d stopped sandbox container(s)", len(removed))
    return removed


def remove_persisted_container(
    config: SandboxdConfig, client_factory: Callable[[], Any], session_id: str
) -> bool:
    """Remove one session's persisted container by name, running or not.

    The route a purge takes when the session is hibernated and so has no sandbox
    object to ask. Failure is not raised: the container may already be gone, and
    a purge that could not find one has nothing left to do either way.

    Returns:
        Whether a container was removed.
    """
    name = _container_name(config, session_id)
    if name is None:
        return False
    try:
        client_factory().containers.get(name).remove(force=True)
    except Exception:
        return False
    return True


def _stopped_at(state: dict[str, Any]) -> float | None:
    """When a container stopped, from Docker's RFC 3339 `FinishedAt`.

    Docker writes a zero timestamp for a container that never ran, which must not
    read as "stopped in 0001 and therefore ancient".
    """
    finished = state.get("FinishedAt")
    if not isinstance(finished, str) or finished.startswith("0001-"):
        return None
    try:
        # Docker's nanosecond precision is finer than fromisoformat accepts.
        cleaned = re.sub(r"\.(\d{6})\d+", r".\1", finished.replace("Z", "+00:00"))
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:  # pragma: no cover - defensive against a format change
        return None


def sweep_workspaces(config: SandboxdConfig, keep: Iterable[str], now: float) -> list[str]:
    """Delete workspace directories nobody is using any more.

    Args:
        config: Service configuration; a `workspace_ttl` of `None` sweeps nothing.
        keep: Session ids with an open session, which are never swept whatever
            their age — an active session's directory may be older than the TTL
            simply because it has been open the whole time.
        now: Current wall clock, against which each directory's mtime is aged.

    Returns:
        The session ids whose workspaces were deleted.
    """
    root = Path(config.workspace_root) if config.workspace_root else None
    if root is None or config.workspace_ttl is None or not root.is_dir():
        return []

    live = set(keep)
    swept: list[str] = []
    for entry in sorted(root.iterdir()):
        if entry.name in live:
            continue
        try:
            # One stat rather than `is_dir()` followed by `stat()`. The two leave
            # a window for a concurrent purge to delete the directory between
            # them — both run on the same worker pool — and the second call would
            # then raise and abort the whole pass over one vanished entry.
            info = entry.stat()
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or now - info.st_mtime <= config.workspace_ttl:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        swept.append(entry.name)
    return swept


def _remove_sandbox(sandbox: Any) -> None:
    """Stop a sandbox and discard its container, when it can do that.

    `DockerSandbox.stop` takes `remove`; the base sandbox surface does not, and a
    sandbox that never persisted anything has nothing to remove. Calling it here
    rather than through `SessionManager.release` keeps the manager's contract at
    plain `stop()`, and the second `stop()` release makes is a no-op.
    """
    remover = getattr(sandbox, "stop", None)
    if remover is None:  # pragma: no cover - every sandbox has stop()
        return
    try:
        remover(remove=True)
    except TypeError:
        remover()


def _default_prewarm(config: SandboxdConfig) -> Callable[[], None]:
    """Pull and build everything on the allowlist, once, in the background.

    Sequential on purpose: several image builds at once would fight over the CPU
    and the disk of exactly the small host this is most worth doing on. A runtime
    that will not build is logged and skipped — one bad entry must not stop the
    others being ready, and the failure will surface again when a client asks for
    it.
    """

    def warm() -> None:
        from pydantic_ai_backends.backends.docker._client import docker_client
        from pydantic_ai_backends.backends.docker._image import pull_if_absent, resolve_image

        client = docker_client()
        for alias in sorted(config.runtimes):
            runtime = _as_runtime(config.runtimes[alias])
            started = time.monotonic()
            try:
                built = runtime.resolved_runtime()
                if built is None:
                    assert runtime.image is not None
                    pulled = pull_if_absent(client, runtime.image)
                    action = "pulled" if pulled else "already local"
                else:
                    at_work_dir = built.model_copy(update={"work_dir": config.work_dir})
                    resolve_image(client, at_work_dir, "")
                    action = "built"
            except Exception:
                _logger.exception("Could not prepare runtime %s; it will be built on demand", alias)
                continue
            _logger.info("Runtime %s %s in %.1fs", alias, action, time.monotonic() - started)

    return warm


def _default_docker_client() -> Any:
    """The Docker client the container sweep talks to, imported on demand."""
    from pydantic_ai_backends.backends.docker._client import docker_client

    return docker_client()


def _usage_of(sandbox: Any) -> wire.SessionUsage | None:
    """Sample a sandbox's usage synchronously, when it can report any."""
    sampler = getattr(sandbox, "resource_usage", None)
    if sampler is None:
        return None
    return _as_wire_usage(sampler())


def _as_wire_usage(sampled: Any) -> wire.SessionUsage | None:
    """Convert whatever a sandbox reported, or `None` when it reported nothing."""
    if not isinstance(sampled, SandboxUsage):
        return None
    return wire.SessionUsage(
        memory_bytes=sampled.memory_bytes,
        memory_limit_bytes=sampled.memory_limit_bytes,
        cpu_percent=sampled.cpu_percent,
        pids=sampled.pids,
    )


class _Service:
    """Service state and behaviour shared by the route handlers."""

    def __init__(
        self,
        config: SandboxdConfig,
        build_sandbox: SandboxBuilder,
        prewarm: Callable[[], None] | None = None,
        docker_client: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self._build_sandbox = build_sandbox
        self._prewarm = prewarm
        self._docker_client = docker_client
        self._sessions: dict[str, _Session] = {}
        self._pending: dict[str, _Pending] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._prewarm_task: asyncio.Task[None] | None = None
        self._usage_cache: dict[str, tuple[float, wire.SessionUsage | None]] = {}
        # The clock the usage cache ages against, as an attribute so a test can
        # advance it. Patching `time.monotonic` in this module instead reaches
        # the *global* function, which is also what the event loop schedules
        # against — freezing it stalls timers and makes anything running
        # concurrently racy, which is how the expiry test came to fail only under
        # the newer stack.
        self.now: Callable[[], float] = time.monotonic
        # Operations in flight per session. `last_activity` is stamped when a
        # command *starts*, so a long exec looks idle while it runs — evicting on
        # that alone would kill work mid-command.
        self._inflight: Counter[str] = Counter()
        # Ids whose sandbox is being stopped to free a slot rather than closed.
        # `on_release` cannot tell the two apart on its own, and forgetting a
        # hibernating session would throw away the token its caller still holds.
        self._hibernating: set[str] = set()
        self.manager = SessionManager(
            sandbox_factory=self._new_sandbox,
            max_sessions=config.max_sessions,
            default_idle_timeout=config.idle_timeout,
            on_release=self._forget,
        )

    def _forget(self, session_id: str) -> None:
        """Drop the bookkeeping for a session whose sandbox has been stopped.

        Reached by idle reaping as well as an explicit close, which is the point:
        without it a reaped session left its token and its whole event log behind
        for the life of the process, and `reuse` would attach to a record with no
        sandbox under it.

        Hibernation goes through the same release path and must not do any of
        that: the session is coming back, and the only thing that stops being
        true about it is its usage sample.
        """
        self._usage_cache.pop(session_id, None)
        if session_id in self._hibernating:
            return
        self._sessions.pop(session_id, None)
        self._pending.pop(session_id, None)
        self._inflight.pop(session_id, None)

    def _new_sandbox(self, session_id: str) -> Any:
        return self._build_sandbox(session_id, self._pending[session_id].runtime)

    def startup(self) -> None:
        """Create the worker pool and begin reaping idle sessions and workspaces."""
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.max_workers, thread_name_prefix="sandboxd"
        )
        # Starting and stopping a sandbox blocks for seconds; the manager runs
        # both on this pool rather than on the loop every request shares.
        self.manager.executor = self._executor
        self.manager.start_cleanup_loop(interval=self.config.cleanup_interval)
        # Always: the manager only reaps sessions it holds a sandbox for, so
        # ending the hibernated ones is this loop's job whatever the TTLs say.
        self._sweep_task = asyncio.create_task(self._sweep_loop())
        if self.config.prewarm and self._prewarm is not None:
            # Off the event loop and un-awaited: the service serves requests
            # while the images arrive, which is the whole point of doing it here
            # rather than leaving it to the first client.
            self._prewarm_task = asyncio.create_task(self._prewarm_images())

    async def _prewarm_images(self) -> None:
        """Prepare the allowlist on the worker pool, logging how it went."""
        assert self._prewarm is not None
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._executor, self._prewarm)
        except Exception:
            # `CancelledError` is a BaseException, so a cancelled shutdown passes
            # straight through here rather than being logged as a failure.
            _logger.exception("Prewarming the runtime allowlist failed")

    async def _sweep_loop(self) -> None:
        """End long-asleep sessions and reclaim what nobody is using.

        Separate from the manager's idle reaping because it outlives it: a
        workspace is swept long after the session that wrote it stopped
        existing, which is the whole point of keeping one — and a hibernated
        session has no sandbox for the manager to notice at all.
        """
        while True:
            await asyncio.sleep(self.config.cleanup_interval)
            try:
                await self._reap_hibernated()
                await self._in_thread(self._sweep_once)
            except Exception:
                _logger.exception("Sweep failed; retrying next interval")

    async def _reap_hibernated(self) -> None:
        """End the sessions that have been asleep past `idle_timeout`.

        The manager reaps by sandbox, and a hibernated session has none — so
        without this an eviction meant to free a slot would instead keep the
        session's record and workspace for the life of the process.
        """
        now = time.time()
        # "Asleep for at least `idle_timeout`", so a timeout of zero means the
        # next sweep — rather than never, which a strict comparison against a
        # clock that has not visibly ticked would give.
        stale = [
            session_id
            for session_id, record in self._sessions.items()
            if record.hibernated_at is not None
            and now - record.hibernated_at >= self.config.idle_timeout
        ]
        for session_id in stale:
            await self.close_session(session_id)
        if stale:
            _logger.info("Closed %d session(s) asleep past the idle timeout", len(stale))

    def _sweep_once(self) -> None:
        """One reclaim pass, off the event loop.

        Both halves block — deleting a directory tree and asking the daemon to
        list and remove containers — and neither is urgent enough to hold the
        loop for.
        """
        now = time.time()
        # Hibernated sessions have no sandbox in the manager, and their
        # workspace is the only reason they are worth waking.
        keep = self._sessions.keys() | self.manager.sessions.keys()
        swept = sweep_workspaces(self.config, keep, now)
        if swept:
            _logger.info("Swept %d unused workspace(s)", len(swept))
        if self._docker_client is not None:
            sweep_containers(self.config, self._docker_client(), now)

    async def shutdown(self) -> None:
        """Stop every sandbox and release the worker pool."""
        for task in (self._sweep_task, self._prewarm_task):
            if task is not None:
                task.cancel()
        self._sweep_task = None
        self._prewarm_task = None
        await self.manager.shutdown()
        self._sessions.clear()
        self._pending.clear()
        self._usage_cache.clear()
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def check_service_token(self, token: str | None) -> None:
        """Reject anything but the service token.

        Raises:
            HTTPException: 401 when the token is absent or wrong.
        """
        if token is None or not _token_matches(token, self.config.token):
            raise HTTPException(status_code=401, detail=_UNAUTHORIZED)

    def check_session_token(self, session_id: str, token: str | None) -> None:
        """Authorize a caller for one session.

        The service token works everywhere; a session token works only for the
        session it was issued for, so one tenant cannot reach another's sandbox.
        The token is checked before existence is revealed, so an unauthenticated
        caller cannot enumerate session ids by watching 401 turn into 404.

        Raises:
            HTTPException: 401 for a bad token, 404 for an unknown session.
        """
        if token is None:
            raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
        record = self._sessions.get(session_id)
        candidates = [self.config.token] if record is None else [self.config.token, record.token]
        if not any(_token_matches(token, candidate) for candidate in candidates):
            raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No such session: {session_id}")

    def adapter(self, sandbox: Any) -> AsyncSandboxProtocol:
        """Async view of a sandbox, bound to the service's own thread pool."""
        # `ensure_async` is typed to the base backend protocol; anything with
        # `execute` — which every sandbox has — comes back as a sandbox adapter.
        return cast("AsyncSandboxProtocol", ensure_async(sandbox, executor=self._executor))

    async def sandbox(self, session_id: str) -> Any:
        """Live sandbox for an already-authorized session, healing a dead one.

        Used by the operation endpoints: a client asking to run a command wants
        a working sandbox, so a container that died since the last call is
        replaced rather than reported — and a session hibernated to free a slot
        is woken here, which is the only place it can be, since waking is what
        the next request means.

        Raises:
            HTTPException: 404 when the session has since disappeared, and 429
                when it is hibernated and every resident session is busy. The
                second is backpressure rather than an error: the work is still
                there, and the caller is being asked to come back.
        """
        try:
            if session_id not in self.manager.sessions:
                # Waking claims a slot, so somebody idle may have to give one up.
                await self.make_room()
            sandbox = await self.manager.get_or_create(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"No such session: {session_id}") from exc
        except SessionLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail=f"Cannot wake session {session_id}: {exc}",
            ) from exc

        record = self._sessions.get(session_id)
        if record is not None:  # pragma: no branch - an authorized caller has one
            record.hibernated_at = None
        return sandbox

    def peek(self, session_id: str) -> Any:
        """Existing sandbox for a session, without creating anything.

        Inspection must not have side effects: routing `GET /sessions/{id}`
        through :meth:`sandbox` would silently replace a dead container and then
        report it as alive, which is precisely the state an operator is looking
        for — and would wake a hibernated session merely for being looked at.

        Returns:
            The sandbox, or `None` when the session is hibernated and so has
            none. A hibernated session is still a session, and describing it is
            most of the point of keeping the record.

        Raises:
            HTTPException: 404 when there is no such session at all.
        """
        if session_id not in self._sessions:
            raise HTTPException(status_code=404, detail=f"No such session: {session_id}")
        return self.manager.sessions.get(session_id)

    def describe(
        self,
        session_id: str,
        sandbox: Any,
        usage: wire.SessionUsage | None = None,
        *,
        alive: bool,
    ) -> wire.SessionInfo:
        """Build the observable view of one session.

        Takes an already-taken `usage` sample and an already-resolved `alive`
        rather than fetching either: sampling is slow enough that the caller has
        to decide when and how many at a time, and `is_alive` may be a coroutine
        that this method — which runs in a worker thread — could not await.

        Raises:
            HTTPException: 404 when the session was reaped while this view was
                being built. The record is looked up again rather than trusted
                from the authorization check, because a usage sample is a
                second-long await and the idle reaper runs on its own timer.
        """
        record = self._sessions.get(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No such session: {session_id}")
        now = time.time()
        # A hibernated session has no sandbox to ask, and the moment it was put
        # to sleep is the last thing that happened to it.
        last_activity = (
            record.hibernated_at
            if record.hibernated_at is not None
            else getattr(sandbox, "last_activity", now)
        )
        return wire.SessionInfo(
            session_id=session_id,
            runtime=record.runtime,
            tenant=record.tenant,
            alive=alive,
            state="hibernated" if record.hibernated_at is not None else "running",
            created_at=record.created_at,
            last_activity=last_activity,
            idle_seconds=max(0.0, now - last_activity),
            usage=usage,
        )

    async def sampled_usage(self, session_id: str, sandbox: Any) -> wire.SessionUsage | None:
        """One sandbox's resource usage, cached for `USAGE_CACHE_SECONDS`.

        Taken on the worker pool, never on the event loop: a stats call is a
        second-long blocking round trip to the daemon, and holding the loop for
        it would stall every agent's command while a dashboard polls.
        """
        now = self.now()
        cached = self._usage_cache.get(session_id)
        if cached is not None and now - cached[0] < USAGE_CACHE_SECONDS:
            return cached[1]

        sampler = getattr(sandbox, "resource_usage", None)
        if inspect.iscoroutinefunction(sampler):
            # Awaited here rather than in a thread: a coroutine function handed to
            # the pool would return an un-awaited coroutine, which reports as no
            # usage at all and leaves a warning behind.
            sample = _as_wire_usage(await sampler())
        else:
            loop = asyncio.get_running_loop()
            sample = await loop.run_in_executor(self._executor, _usage_of, sandbox)
        self._usage_cache[session_id] = (now, sample)
        return sample

    async def described(self, session_id: str, sandbox: Any, usage: bool) -> wire.SessionInfo:
        """Describe one session, sampling its usage only when asked.

        `sandbox` is `None` for a hibernated session, which has nothing to
        sample and is not alive — both true without asking the daemon.
        """
        if sandbox is None:
            return self.describe(session_id, None, None, alive=False)
        sample = await self.sampled_usage(session_id, sandbox) if usage else None
        alive = await alive_of(sandbox)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            functools.partial(self.describe, session_id, sandbox, sample, alive=alive),
        )

    def health(self) -> wire.ServiceHealth:
        """Liveness and capacity summary."""
        return wire.ServiceHealth(
            status="ok",
            sessions=self.manager.session_count,
            limit=self.config.max_sessions,
            open_sessions=len(self._sessions),
            open_limit=self.config.max_open_sessions,
            runtimes=sorted(self.config.runtimes),
        )

    @contextmanager
    def observe(self, session_id: str, op: str, target: str) -> Iterator[_Outcome]:
        """Record one operation against a session's activity log.

        Wraps the call so the entry is written even when it raises, which is
        exactly the case an operator most wants to see. Nothing about the
        payload is stored — only what was addressed and how it went.

        Args:
            session_id: Session the operation belongs to.
            op: Operation name.
            target: Command or path, truncated on the way in.

        Yields:
            An :class:`_Outcome` for the caller to fill in.
        """
        outcome = _Outcome()
        started = time.perf_counter()
        self._inflight[session_id] += 1
        try:
            yield outcome
        finally:
            self._inflight[session_id] -= 1
            if self._inflight[session_id] <= 0:
                del self._inflight[session_id]
            record = self._sessions.get(session_id)
            if record is not None:
                trimmed = (
                    target
                    if len(target) <= _TARGET_MAX_CHARS
                    else (target[:_TARGET_MAX_CHARS] + "…")
                )
                record.events.append(
                    wire.SessionEvent(
                        seq=record.next_seq,
                        at=time.time(),
                        op=op,
                        target=trimmed,
                        ok=outcome.ok,
                        detail=outcome.detail,
                        duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    )
                )
                record.next_seq += 1

    def events(self, session_id: str, after: int) -> wire.SessionEvents:
        """Activity entries newer than `after`, for incremental polling.

        Raises:
            HTTPException: 404 when the session was reaped between the
                authorization check and this call, matching :meth:`describe` —
                a subscript here turned that race into a 500.
        """
        record = self._sessions.get(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No such session: {session_id}")
        return wire.SessionEvents(
            events=[event for event in record.events if event.seq > after],
            latest_seq=record.next_seq - 1,
        )

    def policy(self) -> wire.ServicePolicy:
        """The ceilings and allowlist currently in force."""
        config = self.config
        return wire.ServicePolicy(
            runtimes=[self._runtime_policy(alias) for alias in sorted(config.runtimes)],
            default_runtime=config.default_runtime,
            max_sessions=config.max_sessions,
            max_open_sessions=config.max_open_sessions,
            max_sessions_per_tenant=config.max_sessions_per_tenant,
            evict_idle_after=config.evict_idle_after,
            mem_limit=config.mem_limit,
            memswap_limit=config.memswap_limit,
            cpus=config.cpus,
            cpu_shares=config.cpu_shares,
            pids_limit=config.pids_limit,
            network_mode=config.network_mode,
            oci_runtime=config.oci_runtime,
            work_dir=config.work_dir,
            idle_timeout=config.idle_timeout,
            execute_timeout=config.execute_timeout,
            max_read_bytes=config.max_read_bytes,
            persist_containers=config.persist_containers,
            sandbox_uid=config.sandbox_uid,
            workspace_ttl=config.workspace_ttl,
            container_ttl=config.container_ttl,
            tmpfs_size=config.tmpfs_size,
            prewarm=config.prewarm,
            buildkit=buildkit_available(),
        )

    def _runtime_policy(self, alias: str) -> wire.RuntimePolicy:
        """One allowlist entry as the dashboard and an operator read it."""
        runtime = _as_runtime(self.config.runtimes[alias])
        limits = self.config.limits_for(runtime)
        return wire.RuntimePolicy(
            alias=alias,
            image=runtime.image_label(),
            description=runtime.describes(),
            builds=runtime.builds,
            mem_limit=limits["mem_limit"],
            memswap_limit=limits["memswap_limit"],
            cpus=limits["cpus"],
            cpu_shares=limits["cpu_shares"],
            pids_limit=limits["pids_limit"],
            network_mode=limits["network_mode"],
            oci_runtime=limits["oci_runtime"],
        )

    async def listing(self, usage: bool) -> wire.SessionList:
        """Describe every open session.

        Sampling runs concurrently on the worker pool rather than one sandbox
        after another: sequential stats calls made this endpoint take a second
        per session, on the event loop, which stalled everything else the service
        was doing.
        """
        # Driven by the service's own records rather than the manager's, so a
        # hibernated session — which has no sandbox and is exactly the one an
        # operator is looking for when the pool is full — still appears.
        resident = self.manager.sessions
        rows = [(session_id, resident.get(session_id)) for session_id in self._sessions]
        described = await asyncio.gather(
            *(self._described_or_gone(session_id, sandbox, usage) for session_id, sandbox in rows)
        )
        return wire.SessionList(
            sessions=[row for row in described if row is not None],
            limit=self.config.max_sessions,
            open_limit=self.config.max_open_sessions,
            tenant_limit=self.config.max_sessions_per_tenant,
        )

    async def _described_or_gone(
        self, session_id: str, sandbox: Any, usage: bool
    ) -> wire.SessionInfo | None:
        """Describe one session for a listing, or `None` if it has just gone.

        A listing samples every sandbox, each sample is a daemon round trip, and
        the idle reaper runs while that happens — so one session disappearing
        mid-listing must drop a row rather than fail the operator's whole view.
        """
        try:
            return await self.described(session_id, sandbox, usage)
        except HTTPException:
            return None

    async def open_session(self, body: wire.CreateSessionRequest) -> wire.SessionCreated:
        """Open a session and mint a token scoped to it.

        Raises:
            HTTPException: 400 for a runtime outside the allowlist, 409 when the
                id is already open or already being opened, 429 at capacity —
                the service's or the tenant's — and 502 when the sandbox won't
                start.
        """
        try:
            alias, runtime = self.config.resolve_runtime(body.runtime)
        except KeyError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown runtime {body.runtime!r}. "
                    f"Allowed: {', '.join(sorted(self.config.runtimes))}"
                ),
            ) from exc

        session_id = body.session_id or f"s-{uuid.uuid4().hex[:16]}"
        open_already = self._sessions.get(session_id)
        if open_already is not None:
            if not body.reuse:
                raise HTTPException(status_code=409, detail=f"Session exists: {session_id}")
            return await self._attach(session_id, open_already, body.runtime)
        if session_id in self._pending:
            raise HTTPException(status_code=409, detail=f"Session is opening: {session_id}")

        # Claimed before the first await. Starting a sandbox suspends, so without
        # this two requests naming one id both get past the check above, both are
        # handed the same sandbox, and the second overwrites the first's record —
        # which silently invalidates the token the first caller is holding.
        await self._make_open_room()
        self._pending[session_id] = _Pending(runtime=runtime, tenant=body.tenant)
        try:
            self._reject_tenant_at_capacity(body.tenant, session_id)
            await self.make_room()
            sandbox = await self.manager.get_or_create(session_id)
        except HTTPException:
            self._pending.pop(session_id, None)
            raise
        except SessionLimitExceeded as exc:
            self._pending.pop(session_id, None)
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except Exception as exc:
            self._pending.pop(session_id, None)
            raise HTTPException(status_code=502, detail=f"Could not start sandbox: {exc}") from exc

        self._sessions[session_id] = _Session(
            runtime=alias,
            token=secrets.token_urlsafe(32),
            created_at=time.time(),
            tenant=body.tenant,
        )
        return wire.SessionCreated(
            session=self.describe(session_id, sandbox, alive=await alive_of(sandbox)),
            token=self._sessions[session_id].token,
        )

    async def make_room(self) -> str | None:
        """Close the least recently used idle session when the pool is full.

        A session is a candidate only when nothing is in flight on it *and* it
        has been idle for at least `evict_idle_after`. The in-flight check is the
        load-bearing one: `last_activity` is stamped when a command begins, so a
        command still running after a minute looks a minute idle. The threshold
        adds a grace period on top, for the gap between an agent's turns.

        With every session working the caller still gets backpressure, which is
        the correct answer rather than a queue nobody bounded.

        Returns:
            The session id that was closed, or `None` when nothing could be.
        """
        ceiling = self.config.max_sessions
        threshold = self.config.evict_idle_after
        if ceiling is None or threshold is None:
            return None
        if self.manager.session_count < ceiling:
            return None

        now = time.time()
        candidates = [
            (now - last_activity_of(sandbox, now), session_id)
            for session_id, sandbox in self.manager.sessions.items()
            # A session with an operation in flight is working, whatever its
            # last-activity stamp says: that stamp is set when a command begins.
            if session_id in self._sessions and not self._inflight[session_id]
        ]
        candidates = [entry for entry in candidates if entry[0] >= threshold]
        if not candidates:
            return None

        _, victim = max(candidates)
        await self.hibernate(victim)
        _logger.info("Hibernated idle session %s to make room", victim)
        return victim

    async def _make_open_room(self) -> None:
        """Close the longest-asleep session when the open ceiling is full.

        Hibernated sessions cost disk rather than memory, which is what lets
        there be many more of them than resident ones — but not unboundedly
        many, since each keeps a workspace and possibly a stopped container. At
        the ceiling the one that has been asleep longest is the one least likely
        to be wanted again, and it is closed for good.

        Raises:
            HTTPException: 429 when every open session is resident, so nothing
                can be given up without killing work somebody is doing.
        """
        ceiling = self.config.max_open_sessions
        if ceiling is None or len(self._pending) < ceiling:
            return

        asleep = [
            (record.hibernated_at, session_id)
            for session_id, record in self._sessions.items()
            if record.hibernated_at is not None
        ]
        if not asleep:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Service holds its ceiling of {ceiling} open sessions and all of "
                    "them are in use. Close one before opening another."
                ),
            )

        _, longest_asleep = min(asleep)
        await self.close_session(longest_asleep)
        _logger.info("Closed hibernated session %s to make room", longest_asleep)

    async def hibernate(self, session_id: str) -> None:
        """Stop a session's sandbox without ending the session.

        What goes is everything resident: the container, the process supervising
        it, and the memory both hold. What stays is the record — the token its
        caller is holding, the runtime it was opened on, and its event log — plus
        the workspace on disk. The next request wakes it, finds its files, and
        never learns it was away.

        This is why eviction is not a close. Closing frees the same memory, but
        it also invalidates a token that a client has no way of knowing is gone,
        and it makes the number of sessions a host can carry the number it can
        run at once. Those are different numbers, and on a small host they differ
        by an order of magnitude.
        """
        self._hibernating.add(session_id)
        try:
            await self.manager.release(session_id)
        finally:
            self._hibernating.discard(session_id)
        record = self._sessions.get(session_id)
        if record is not None:  # pragma: no branch - the caller picked a live session
            record.hibernated_at = time.time()

    def _reject_tenant_at_capacity(self, tenant: str | None, opening: str) -> None:
        """Refuse a new session once its tenant holds its share of the pool.

        Counted over the service's own reservations rather than the manager's
        sessions, which covers two ways a tenant would otherwise slip past: a
        session whose sandbox has died but has not been reaped yet still counts
        against it, and so does an open still in flight — without that, a burst
        of concurrent requests all pass a check none of them has registered
        against yet.

        Args:
            tenant: Label the new session is being opened for.
            opening: Id this call is opening, whose own reservation is already in
                place and so must not be counted against its own ceiling.

        Raises:
            HTTPException: 429 when the tenant is already at its ceiling.
        """
        ceiling = self.config.max_sessions_per_tenant
        if ceiling is None or tenant is None:
            return

        held = sum(
            1
            for session_id, pending in self._pending.items()
            if pending.tenant == tenant and session_id != opening
        )
        if held >= ceiling:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Tenant {tenant!r} already holds {held} of {ceiling} sessions. "
                    "Release an idle session before opening another."
                ),
            )

    async def _attach(
        self, session_id: str, session: _Session, requested_runtime: str | None
    ) -> wire.SessionCreated:
        """Hand back an already-open session, with the token it was issued.

        The existing token is returned rather than a fresh one: only a caller
        holding the service token gets here, and that caller could open any
        session anyway — while re-minting would cut off whoever holds the token
        from the run that opened it.

        Raises:
            HTTPException: 409 if the caller asks for a different runtime than
                the session was opened with. Honouring it would mean replacing
                a live sandbox and discarding the files the caller came back for.
        """
        if requested_runtime is not None and requested_runtime != session.runtime:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Session {session_id} is open on runtime {session.runtime!r}, "
                    f"not {requested_runtime!r}. Close it before changing runtime."
                ),
            )
        sandbox = self.peek(session_id)
        if sandbox is None:
            # Hibernated. Re-opening a session is a caller saying it wants to
            # work again, which is exactly what wakes one.
            sandbox = await self.sandbox(session_id)
        return wire.SessionCreated(
            session=self.describe(session_id, sandbox, alive=await alive_of(sandbox)),
            token=session.token,
        )

    async def close_session(self, session_id: str, purge: bool = False) -> None:
        """Stop a session's sandbox and forget it.

        Args:
            session_id: Session to close.
            purge: Also discard everything the session accumulated — its
                container's write layer and its host workspace. Without it a
                persisted container stays stopped and a workspace stays on disk,
                which is what makes a later attach find the same files; with it,
                the session is gone for good.
        """
        sandbox = self.manager.sessions.get(session_id)
        if purge:
            if sandbox is not None:
                await self._discard(sandbox)
            elif self._docker_client is not None:
                # Hibernated: there is no sandbox object to stop, but a persisted
                # container is sitting stopped under the session's name and a
                # purge that left it there would not be one.
                await self._in_thread(
                    functools.partial(
                        remove_persisted_container, self.config, self._docker_client, session_id
                    )
                )

        if not await self.manager.release(session_id):
            # Hibernated, so the manager has nothing to release and `on_release`
            # never fires. The records that outlive a sandbox end here instead.
            self._forget(session_id)

        if purge:
            session_dir = _session_dir(self.config, session_id)
            if session_dir is not None:
                await self._in_thread(
                    functools.partial(shutil.rmtree, session_dir, ignore_errors=True)
                )

    async def _discard(self, sandbox: Any) -> None:
        """Stop a sandbox and throw its container away, sync or async.

        Discarding a container blocks, so a synchronous sandbox goes to the
        worker pool. An async one is awaited instead — in a thread its coroutine
        would never run, and the container would survive the purge.
        """
        remover = getattr(sandbox, "stop", None)
        if remover is None:  # pragma: no cover - every sandbox has stop()
            return
        if not inspect.iscoroutinefunction(remover):
            await self._in_thread(functools.partial(_remove_sandbox, sandbox))
            return
        try:
            pending = remover(remove=True)
        except TypeError:
            # The base sandbox surface takes no `remove`; it has nothing to
            # discard beyond stopping.
            pending = remover()
        await pending

    def workspace_of(self, session_id: str) -> Path:
        """The host directory holding a session's files.

        Raises:
            HTTPException: 409 when the service keeps no workspaces, so there is
                nothing to read without a running sandbox. 404 when this session
                never had one.
        """
        if self.config.workspace_root is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This service keeps no workspaces on disk. "
                    "Set SandboxdConfig.workspace_root to read files without a sandbox."
                ),
            )
        workspace = workspace_root_for(Path(self.config.workspace_root), session_id)
        if not workspace.is_dir():
            raise HTTPException(status_code=404, detail=f"No workspace for session: {session_id}")
        return workspace

    async def archive_ls(self, session_id: str, path: str) -> list[wire.FileEntry]:
        """List a stored workspace directory without starting a sandbox.

        Raises:
            HTTPException: 400 for a path outside the workspace, 404 when it is
                not a directory.
        """
        workspace = self.workspace_of(session_id)
        relative = relative_request_path(path, self.config.work_dir)
        rows = await self._off_loop(list_workspace, workspace, relative)
        return [wire.FileEntry(**row) for row in rows]

    async def archive_read(self, session_id: str, body: wire.ReadRequest) -> wire.ReadResponse:
        """Read a stored workspace file without starting a sandbox.

        Raises:
            HTTPException: 400 for a path outside the workspace or content that
                is not readable text, 404 when it is not a file.
        """
        workspace = self.workspace_of(session_id)
        relative = relative_request_path(body.path, self.config.work_dir)
        content = await self._off_loop(
            read_workspace,
            workspace,
            relative,
            body.offset,
            body.limit,
            self.config.max_read_bytes,
        )
        return wire.ReadResponse(content=content)

    async def archive_read_bytes(
        self, session_id: str, body: wire.ReadBytesRequest
    ) -> wire.ReadBytesResponse:
        """Read a stored workspace file as bytes, without starting a sandbox.

        The sibling of :meth:`archive_read`, for the files that are not text. A
        chart or a PDF read through `archive_read` comes back decoded and
        re-encoded, which is a corrupt file rather than an error - so a consumer
        serving downloads had to allowlist text suffixes and refuse everything
        an agent is most likely to have produced.

        Raises:
            HTTPException: 400 for a path outside the workspace or a file over
                the read ceiling, 404 when it is not a file.
        """
        workspace = self.workspace_of(session_id)
        relative = relative_request_path(body.path, self.config.work_dir)
        raw = await self._off_loop(
            read_workspace_bytes, workspace, relative, self.config.max_read_bytes
        )
        return wire.ReadBytesResponse(content_b64=base64.b64encode(raw).decode("ascii"))

    async def _in_thread(self, call: Callable[[], Any]) -> Any:
        """Run one blocking call on the service's worker pool."""
        return await asyncio.get_running_loop().run_in_executor(self._executor, call)

    async def _off_loop(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking filesystem call on the service's worker pool.

        Raises:
            HTTPException: 400 for a refused path or unreadable content, 404 for
                a missing one — mapped here so both archive routes agree.
        """
        try:
            return await self._in_thread(functools.partial(fn, *args))
        except WorkspacePathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def command_timeout(self, requested: int | None) -> int:
        """Clamp a requested command timeout to the service ceiling."""
        if requested is None:
            return self.config.execute_timeout
        return min(requested, self.config.execute_timeout)

    async def bounded(self, operation: Awaitable[_T]) -> _T:
        """Await one sandbox operation under the service's command ceiling.

        `execute_timeout` is documented as applying to every command, and it was
        applied to `/exec` alone. Everything else — `ls`, `glob`, `grep`, `read`,
        `write` — reaches the sandbox's shell too, and an uncapped one of those
        occupies a worker thread that nothing reclaims: a grep over a large tree
        is enough, and `max_workers` of them wedge the service. Enforced here
        rather than per backend so the promise is kept in the one place that
        makes it.

        The thread running the call cannot be interrupted, so this bounds how
        long a *client* waits rather than how long the command runs. The
        backend's own per-operation timeouts bound the command; this is what
        stops the request from outliving them.

        Raises:
            HTTPException: 504 when the operation outlasts the ceiling.
        """
        try:
            return await asyncio.wait_for(operation, timeout=self.config.execute_timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise HTTPException(
                status_code=504,
                detail=f"Operation exceeded the {self.config.execute_timeout}s service ceiling",
            ) from exc


def _service_of(request: Request) -> _Service:
    """Pull the service off application state."""
    service: _Service = request.app.state.service
    return service


ServiceDep = Annotated[_Service, Depends(_service_of)]
TokenHeader = Annotated[str | None, Header(alias=wire.TOKEN_HEADER)]


def _require_service_token(service: ServiceDep, token: TokenHeader = None) -> None:
    """Dependency admitting only the service token."""
    service.check_service_token(token)


def _authorized_session(
    service: ServiceDep,
    session_id: Annotated[str, PathParam(pattern=wire.SESSION_ID_PATTERN)],
    token: TokenHeader = None,
) -> str:
    """Dependency authorizing a caller for one session, returning its id."""
    service.check_session_token(session_id, token)
    return session_id


ServiceAuth = Depends(_require_service_token)
AuthorizedSession = Annotated[str, Depends(_authorized_session)]


def _register_session_routes(app: FastAPI, service: _Service) -> None:
    """Health, plus opening, listing, inspecting and closing sessions."""

    @app.get("/", response_model=wire.ServiceIndex)
    async def index() -> wire.ServiceIndex:
        """Say what this service is. Opening the base URL should not 404."""
        return wire.ServiceIndex(
            health=service.health(),
            ui_url="/ui" if service.config.ui_enabled else None,
            endpoints=sorted({getattr(route, "path", "") for route in app.routes} - {"", "/"}),
        )

    if service.config.ui_enabled:

        @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
        async def dashboard() -> HTMLResponse:
            """Serve the bundled single-file dashboard."""
            return HTMLResponse(_ui_html())

    @app.get("/healthz", response_model=wire.ServiceHealth)
    async def healthz() -> wire.ServiceHealth:
        """Liveness and capacity. Unauthenticated on purpose, for probes."""
        return service.health()

    @app.get("/policy", response_model=wire.ServicePolicy, dependencies=[ServiceAuth])
    async def get_policy() -> wire.ServicePolicy:
        """The ceilings and allowlist every sandbox is held to."""
        return service.policy()

    @app.post("/sessions", response_model=wire.SessionCreated, dependencies=[ServiceAuth])
    async def create_session(body: wire.CreateSessionRequest) -> wire.SessionCreated:
        """Open a sandbox session and mint a token scoped to it."""
        return await service.open_session(body)

    @app.get("/sessions", response_model=wire.SessionList, dependencies=[ServiceAuth])
    async def list_sessions(usage: Annotated[bool, Query()] = False) -> wire.SessionList:
        """Enumerate open sessions.

        Args:
            usage: Also sample resource usage. Off by default because each
                sample costs a daemon round trip per sandbox.
        """
        return await service.listing(usage)

    @app.get("/sessions/{session_id}", response_model=wire.SessionInfo)
    async def get_session(
        session_id: AuthorizedSession,
        usage: Annotated[bool, Query()] = False,
    ) -> wire.SessionInfo:
        """Inspect one session, without reviving a dead sandbox."""
        return await service.described(session_id, service.peek(session_id), usage)

    @app.get("/sessions/{session_id}/events", response_model=wire.SessionEvents)
    async def session_events(
        session_id: AuthorizedSession,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> wire.SessionEvents:
        """What has been done to this session, newest entries last.

        Args:
            after: Return only entries newer than this sequence number, so a
                watcher can poll without re-reading the whole log.
        """
        return service.events(session_id, after)

    @app.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(
        session_id: AuthorizedSession,
        purge: Annotated[bool, Query()] = False,
    ) -> None:
        """Stop a session's sandbox and forget it.

        Args:
            purge: Also delete the session's container and host workspace, so
                nothing is left for a later attach to find. Use it when the thing
                the session belonged to — a conversation, a user — is gone.
        """
        await service.close_session(session_id, purge=purge)


def _register_workspace_routes(app: FastAPI, service: _Service) -> None:
    """Reading a session's stored files, with no sandbox involved.

    Separate from the session operations on purpose: these serve the host volume
    directly, so they work for a session that was reaped long ago and cost no
    container start. They are read-only — a workspace is browsed here and written
    only by the sandbox that owns it.

    Service token only. A reaped session has no session token left to present,
    and the documented consumer is an application proxying file views to its own
    users after applying its own authorization.
    """

    @app.post(
        "/workspaces/{session_id}/ls",
        response_model=list[wire.FileEntry],
        dependencies=[ServiceAuth],
    )
    async def list_archived(
        session_id: Annotated[str, PathParam(pattern=wire.SESSION_ID_PATTERN)],
        body: wire.LsRequest,
    ) -> list[wire.FileEntry]:
        """List one directory of a stored workspace."""
        return await service.archive_ls(session_id, body.path)

    @app.post(
        "/workspaces/{session_id}/read",
        response_model=wire.ReadResponse,
        dependencies=[ServiceAuth],
    )
    async def read_archived(
        session_id: Annotated[str, PathParam(pattern=wire.SESSION_ID_PATTERN)],
        body: wire.ReadRequest,
    ) -> wire.ReadResponse:
        """Read a slice of a stored workspace file."""
        return await service.archive_read(session_id, body)

    @app.post(
        "/workspaces/{session_id}/read_bytes",
        response_model=wire.ReadBytesResponse,
        dependencies=[ServiceAuth],
    )
    async def read_archived_bytes(
        session_id: Annotated[str, PathParam(pattern=wire.SESSION_ID_PATTERN)],
        body: wire.ReadBytesRequest,
    ) -> wire.ReadBytesResponse:
        """Read a whole stored workspace file as base64.

        Beside `read` rather than replacing it: text wants the slicing, the line
        numbers and the encoding detection that `read` does, and a PNG wants none
        of it.
        """
        return await service.archive_read_bytes(session_id, body)


def _register_operation_routes(app: FastAPI, service: _Service) -> None:
    """The file and command operations, all scoped to one session."""

    @app.post("/sessions/{session_id}/exec", response_model=wire.ExecResponse)
    async def exec_command(
        session_id: AuthorizedSession, body: wire.ExecRequest
    ) -> wire.ExecResponse:
        """Run a shell command, capped by the service's `execute_timeout`."""
        sandbox = await service.sandbox(session_id)
        with service.observe(session_id, "exec", body.command) as outcome:
            # Both: the clamped timeout kills the command inside the sandbox,
            # and `bounded` stops a backend that ignores it from holding the
            # request — and its worker — for ever.
            result = await service.bounded(
                service.adapter(sandbox).execute(
                    body.command, service.command_timeout(body.timeout_seconds)
                )
            )
            outcome.ok = result.exit_code == 0
            outcome.detail = f"exit {result.exit_code}"
        return wire.ExecResponse(
            output=result.output, exit_code=result.exit_code, truncated=result.truncated
        )

    @app.post("/sessions/{session_id}/read", response_model=wire.ReadResponse)
    async def read_file(session_id: AuthorizedSession, body: wire.ReadRequest) -> wire.ReadResponse:
        """Read a slice of a text file."""
        sandbox = await service.sandbox(session_id)
        with service.observe(session_id, "read", body.path) as outcome:
            content = await service.bounded(
                service.adapter(sandbox).read(body.path, body.offset, body.limit)
            )
            outcome.ok = not content.startswith(("Error:", "[Error"))
            outcome.detail = f"{len(content)} chars"
        return wire.ReadResponse(content=content)

    @app.post("/sessions/{session_id}/read_bytes", response_model=wire.ReadBytesResponse)
    async def read_file_bytes(
        session_id: AuthorizedSession, body: wire.ReadBytesRequest
    ) -> wire.ReadBytesResponse:
        """Read a whole file as base64."""
        sandbox = await service.sandbox(session_id)
        with service.observe(session_id, "read_bytes", body.path) as outcome:
            raw = await service.bounded(service.adapter(sandbox).read_bytes(body.path))
            outcome.ok = bool(raw)
            outcome.detail = f"{len(raw)} bytes"
        return wire.ReadBytesResponse(content_b64=base64.b64encode(raw).decode("ascii"))

    @app.post("/sessions/{session_id}/write", response_model=wire.WriteResponse)
    async def write_file(
        session_id: AuthorizedSession, body: wire.WriteRequest
    ) -> wire.WriteResponse:
        """Write a file from base64 content."""
        try:
            raw = base64.b64decode(body.content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"content_b64 is not valid base64: {exc}"
            ) from exc

        sandbox = await service.sandbox(session_id)
        with service.observe(session_id, "write", body.path) as outcome:
            result = await service.bounded(service.adapter(sandbox).write(body.path, raw))
            outcome.ok = result.error is None
            outcome.detail = result.error or f"{len(raw)} bytes"
        return wire.WriteResponse(path=result.path, error=result.error)

    @app.post("/sessions/{session_id}/edit", response_model=wire.EditResponse)
    async def edit_file(session_id: AuthorizedSession, body: wire.EditRequest) -> wire.EditResponse:
        """Replace a string inside a file."""
        sandbox = await service.sandbox(session_id)
        with service.observe(session_id, "edit", body.path) as outcome:
            result = await service.bounded(
                service.adapter(sandbox).edit(
                    body.path, body.old_string, body.new_string, body.replace_all
                )
            )
            outcome.ok = result.error is None
            outcome.detail = result.error or f"{result.occurrences} replaced"
        return wire.EditResponse(
            path=result.path, error=result.error, occurrences=result.occurrences
        )

    @app.post("/sessions/{session_id}/exists", response_model=wire.ExistsResponse)
    async def path_exists(
        session_id: AuthorizedSession, body: wire.ExistsRequest
    ) -> wire.ExistsResponse:
        """Test whether a path is a regular file."""
        sandbox = await service.sandbox(session_id)
        with service.observe(session_id, "exists", body.path) as outcome:
            found = await service.bounded(service.adapter(sandbox).exists(body.path))
            outcome.ok = True
            outcome.detail = "found" if found else "missing"
        return wire.ExistsResponse(exists=found)

    @app.post("/sessions/{session_id}/ls", response_model=list[wire.FileEntry])
    async def list_dir(session_id: AuthorizedSession, body: wire.LsRequest) -> list[wire.FileEntry]:
        """List one directory."""
        sandbox = await service.sandbox(session_id)
        with service.observe(session_id, "ls", body.path) as outcome:
            rows = await service.bounded(service.adapter(sandbox).ls_info(body.path))
            outcome.ok = True
            outcome.detail = f"{len(rows)} entries"
        return [wire.FileEntry(**row) for row in rows]

    @app.post("/sessions/{session_id}/glob", response_model=list[wire.FileEntry])
    async def glob_files(
        session_id: AuthorizedSession, body: wire.GlobRequest
    ) -> list[wire.FileEntry]:
        """Match files by glob pattern."""
        sandbox = await service.sandbox(session_id)
        with service.observe(session_id, "glob", f"{body.pattern} in {body.path}") as outcome:
            rows = await service.bounded(
                service.adapter(sandbox).glob_info(body.pattern, body.path)
            )
            outcome.ok = True
            outcome.detail = f"{len(rows)} matches"
        return [wire.FileEntry(**row) for row in rows]

    @app.post("/sessions/{session_id}/grep", response_model=wire.GrepResponse)
    async def grep_files(
        session_id: AuthorizedSession, body: wire.GrepRequest
    ) -> wire.GrepResponse:
        """Search file contents."""
        sandbox = await service.sandbox(session_id)
        with service.observe(session_id, "grep", body.pattern) as outcome:
            found = await service.bounded(
                service.adapter(sandbox).grep_raw(
                    body.pattern, body.path, body.glob, body.ignore_hidden
                )
            )
            outcome.ok = not isinstance(found, str)
            outcome.detail = found if isinstance(found, str) else f"{len(found)} matches"
        if isinstance(found, str):
            return wire.GrepResponse(error=found)
        return wire.GrepResponse(matches=[wire.GrepMatchEntry(**match) for match in found])


def create_app(
    config: SandboxdConfig,
    *,
    sandbox_builder: SandboxBuilder | None = None,
) -> FastAPI:
    """Build the sandbox service.

    Args:
        config: Service policy. See :class:`SandboxdConfig`.
        sandbox_builder: Override for how a sandbox is constructed, receiving
            `(session_id, image)`. Defaults to :class:`DockerSandbox` configured
            from `config`. Supply one to embed a different sandbox type, or to
            test without a Docker daemon.

    Returns:
        A configured `FastAPI` application.
    """
    # Prewarming and the container sweep only make sense for the builder that
    # knows what an image is; an injected builder may have nothing to pull and no
    # daemon to ask.
    service = _Service(
        config,
        sandbox_builder or _default_builder(config),
        prewarm=None if sandbox_builder else _default_prewarm(config),
        docker_client=None if sandbox_builder else _default_docker_client,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        service.startup()
        try:
            yield
        finally:
            await service.shutdown()

    app = FastAPI(
        title="sandboxd",
        summary="Sandbox execution service for AI agents",
        lifespan=lifespan,
    )
    app.state.service = service
    _register_session_routes(app, service)
    _register_workspace_routes(app, service)
    _register_operation_routes(app, service)
    return app


def run() -> None:  # pragma: no cover - thin CLI wrapper around uvicorn
    """Serve the service described by the environment.

    Every field of :class:`SandboxdConfig` is `SANDBOXD_` plus its name in upper
    case; :mod:`pydantic_ai_backends.remote.env` documents the parsing and does
    all of it, so this is uvicorn and an exit code. A configuration problem is
    reported as one line rather than a traceback, because the reader is looking
    at container logs.

    ```bash
    SANDBOXD_TOKEN=... python -m pydantic_ai_backends.remote.server
    ```
    """
    import uvicorn

    from pydantic_ai_backends.remote.env import (
        SandboxdConfigError,
        bind_from_env,
        config_from_env,
    )

    try:
        config = config_from_env(os.environ)
        host, port = bind_from_env(os.environ)
    except SandboxdConfigError as e:
        raise SystemExit(str(e)) from e

    uvicorn.run(create_app(config), host=host, port=port)


__all__ = [
    "DEFAULT_RUNTIMES",
    "SUGGESTED_RUNTIMES",
    "SandboxBuilder",
    "SandboxRuntime",
    "SandboxdConfig",
    "create_app",
    "run",
]


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    run()
