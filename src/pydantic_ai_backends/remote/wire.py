"""Wire protocol shared by the remote sandbox client and server.

These models are the single source of truth for the HTTP contract. The
operation endpoints (`/exec`, `/read`, `/write`, `/ls`, `/glob`) keep the field
names that :class:`~pydantic_ai_backends.backends.kubernetes.KubernetesPodSandbox`
already sends in `mode="http"`, so one server can serve both clients; `/edit`,
`/grep`, `/exists` and `/read_bytes` are additions.

Binary-capable payloads travel base64-encoded (`content_b64`) because JSON
cannot carry arbitrary bytes, and a sandbox holds real files.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TOKEN_HEADER = "X-Sandbox-Token"
"""Header carrying either the service token or a session's own token."""

SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
"""Session ids appear in URLs and in on-disk workspace paths, so they are
restricted to characters that cannot traverse a directory or confuse a path."""

TENANT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
"""Tenant labels are reported back in listings, so they carry the same
restriction as session ids rather than being free text."""


class ExecRequest(BaseModel):
    """Run a shell command inside the sandbox."""

    command: str
    timeout_seconds: int | None = None


class ExecResponse(BaseModel):
    """Result of a command, mirroring `ExecuteResponse`."""

    output: str
    exit_code: int | None = None
    truncated: bool = False


class ReadRequest(BaseModel):
    """Read a slice of a text file."""

    path: str
    offset: int = Field(default=0, ge=0)
    """First line to return. Bounded because a negative offset would silently
    slice from the end of the file rather than being rejected."""

    limit: int = Field(default=2000, ge=1)


class ReadResponse(BaseModel):
    """Text content of the requested slice."""

    content: str


class ReadBytesRequest(BaseModel):
    """Read a whole file as bytes."""

    path: str


class ReadBytesResponse(BaseModel):
    """Raw file content, base64-encoded. Empty when the file is unreadable."""

    content_b64: str


class WriteRequest(BaseModel):
    """Write a file, creating parent directories as needed."""

    path: str
    content_b64: str


class WriteResponse(BaseModel):
    """Result of a write, mirroring `WriteResult`."""

    path: str | None = None
    error: str | None = None


class EditRequest(BaseModel):
    """Replace a string inside an existing file."""

    path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class EditResponse(BaseModel):
    """Result of an edit, mirroring `EditResult`."""

    path: str | None = None
    error: str | None = None
    occurrences: int | None = None


class LsRequest(BaseModel):
    """List one directory."""

    path: str


class GlobRequest(BaseModel):
    """Match files by glob pattern under a root."""

    pattern: str
    path: str = "/"


class FileEntry(BaseModel):
    """One directory entry, mirroring `FileInfo`."""

    name: str
    path: str
    is_dir: bool
    size: int | None = None


class GrepRequest(BaseModel):
    """Search file contents by regular expression."""

    pattern: str
    path: str | None = None
    glob: str | None = None
    ignore_hidden: bool = True


class GrepMatchEntry(BaseModel):
    """One grep hit, mirroring `GrepMatch`."""

    path: str
    line_number: int
    line: str


class GrepResponse(BaseModel):
    """Grep hits, or `error` when the search itself failed.

    Models `grep_raw`'s `list[GrepMatch] | str` return: `error` set means the
    string branch, and `matches` is then empty.
    """

    matches: list[GrepMatchEntry] = Field(default_factory=list)
    error: str | None = None


class ExistsRequest(BaseModel):
    """Test whether a path is a regular file."""

    path: str


class ExistsResponse(BaseModel):
    """Whether the path is a regular file."""

    exists: bool


class CreateSessionRequest(BaseModel):
    """Open a sandbox session.

    Deliberately carries no container settings. Image, mounts, network mode and
    every resource ceiling come from the server's configuration, because a
    process holding the Docker socket can start a privileged container that
    mounts the host — a client that could name its own image or volumes would
    own the machine.
    """

    session_id: str | None = Field(default=None, pattern=SESSION_ID_PATTERN)
    runtime: str | None = None
    """Alias of a server-allowed runtime. `None` selects the server default."""

    tenant: str | None = Field(default=None, pattern=TENANT_PATTERN)
    """Who this session is opened on behalf of, for the per-tenant ceiling.

    Declared by the client rather than derived from `session_id`, so the service
    imposes no naming convention on ids. Only a caller holding the service token
    can open a session at all, so this is a capacity label from a trusted
    application — not an authorization claim, and it grants nothing.
    """

    reuse: bool = False
    """Attach to the session under `session_id` when one is already open.

    Off by default, so an id collision is reported rather than silently sharing
    somebody else's sandbox. Turn it on for a session that spans several runs —
    one conversation, say — where the second run has to reach the files the
    first one wrote. The runtime of an attached session is whatever it was
    opened with; a `runtime` that disagrees is rejected rather than ignored.
    """


class SessionEvent(BaseModel):
    """One operation performed against a session.

    Records what was asked for and how it went — never file contents or command
    output, which would turn an audit trail into a data leak and grow without
    bound.
    """

    seq: int
    """Monotonic per-session sequence number, for incremental polling."""
    at: float
    op: str
    """Operation name: `exec`, `read`, `write`, `edit`, `ls`, `glob`, `grep`, `exists`."""
    target: str
    """Command or path the operation addressed, truncated."""
    ok: bool
    detail: str = ""
    """Short outcome summary, e.g. `exit 0` or `14 entries`."""
    duration_ms: float = 0.0


class SessionEvents(BaseModel):
    """A slice of a session's activity log."""

    events: list[SessionEvent] = Field(default_factory=list)
    latest_seq: int = 0
    """Highest sequence number the service holds; pass it back as `after`."""


class SessionUsage(BaseModel):
    """Point-in-time resource usage for one sandbox."""

    memory_bytes: int | None = None
    memory_limit_bytes: int | None = None
    cpu_percent: float | None = None
    pids: int | None = None


class SessionInfo(BaseModel):
    """Observable state of one session."""

    session_id: str
    runtime: str
    tenant: str | None = None
    """Whoever the session was opened for, when the client said."""
    alive: bool
    state: Literal["running", "hibernated"] = "running"
    """Whether the session holds a sandbox, or was stopped to free a slot and is
    waiting for its next request to wake it. A hibernated session is never
    `alive`; it still has its token, its event log and its files."""
    created_at: float
    last_activity: float
    idle_seconds: float
    usage: SessionUsage | None = None


class SessionCreated(BaseModel):
    """A freshly opened session and the token scoped to it."""

    session: SessionInfo
    token: str
    """Grants access to this session only. The service token also works."""


class SessionList(BaseModel):
    """Every open session, for monitoring."""

    sessions: list[SessionInfo] = Field(default_factory=list)
    limit: int | None = None
    """Configured `max_sessions` — the ceiling on *resident* sandboxes."""
    open_limit: int | None = None
    """Configured `max_open_sessions`, or `None` when uncapped."""
    tenant_limit: int | None = None
    """Configured `max_sessions_per_tenant`, or `None` when uncapped."""


class ServiceHealth(BaseModel):
    """Liveness and capacity summary. Unauthenticated."""

    status: str = "ok"
    sessions: int = 0
    """Sessions holding a sandbox right now."""
    limit: int | None = None
    open_sessions: int = 0
    """Sessions that exist, resident and hibernated together."""
    open_limit: int | None = None
    runtimes: list[str] = Field(default_factory=list)


class RuntimePolicy(BaseModel):
    """One runtime a client may name, and what it gets.

    Ceilings are the *effective* ones — the runtime's own where it names them,
    the service defaults otherwise — because the number an operator needs is the
    one actually in force, not the one before the override.
    """

    alias: str
    """What a client puts in `CreateSessionRequest.runtime`."""
    image: str
    """The image, or what it is built from when nothing is built yet."""
    description: str = ""
    builds: bool = False
    """Whether the first session on this runtime builds an image."""
    mem_limit: str | None = None
    memswap_limit: str | None = None
    """Memory-plus-swap ceiling, or `None` when swap is pinned to `mem_limit`."""
    cpus: float | None = None
    cpu_shares: int | None = None
    pids_limit: int | None = None
    network_mode: str | None = None
    oci_runtime: str | None = None
    """Low-level runtime this one runs under, or `None` for the daemon's default."""


class ServicePolicy(BaseModel):
    """The ceilings and allowlist the service imposes on every sandbox.

    Authenticated, because it describes the host's configuration — though it
    holds nothing a caller with the service token is not already subject to.
    Exists so an operator can see the limits actually in force rather than
    inferring them from a config file.
    """

    runtimes: list[RuntimePolicy] = Field(default_factory=list)
    """Every allowed runtime, with the ceilings it actually runs under."""
    default_runtime: str = ""
    max_sessions: int | None = None
    """Ceiling on resident sandboxes."""
    max_open_sessions: int | None = None
    """Ceiling on sessions that exist, resident or hibernated."""
    max_sessions_per_tenant: int | None = None
    evict_idle_after: int | None = None
    """Seconds after which an idle session may be hibernated to free a slot."""
    mem_limit: str | None = None
    memswap_limit: str | None = None
    """Default memory-plus-swap ceiling, or `None` when swap is pinned to memory."""
    cpus: float | None = None
    cpu_shares: int | None = None
    pids_limit: int | None = None
    network_mode: str | None = None
    oci_runtime: str | None = None
    """Default low-level runtime, or `None` for whatever the daemon defaults to."""

    work_dir: str = ""
    idle_timeout: int = 0
    execute_timeout: int = 0
    max_read_bytes: int = 0
    persist_containers: bool = False
    """Whether a stopped sandbox keeps its filesystem for the next attach."""
    workspace_ttl: int | None = None
    """Seconds an unused workspace is kept before it is swept, or `None`."""
    container_ttl: int | None = None
    """Seconds a stopped container is kept before its build is reclaimed."""
    tmpfs_size: str | None = None
    """Size of the in-memory `/tmp` each sandbox gets, or `None` when it has none."""
    prewarm: bool = False
    """Whether the allowlist is pulled and built at startup rather than on demand."""
    buildkit: bool = False
    """Whether image builds use BuildKit, and so keep package caches between them."""


class ServiceIndex(BaseModel):
    """What this service is and where to find its endpoints.

    Served at `/` so that opening the base URL says something useful instead of
    a bare 404. Unauthenticated, and deliberately free of any session detail.
    """

    service: str = "sandboxd"
    health: ServiceHealth
    docs_url: str = "/docs"
    openapi_url: str = "/openapi.json"
    ui_url: str | None = None
    """Where the dashboard lives, or `None` when it is not enabled."""
    endpoints: list[str] = Field(default_factory=list)
    """Routed paths, derived from the app so the list cannot go stale."""
