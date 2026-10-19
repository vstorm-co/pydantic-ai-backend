"""A `sandboxd` session as a Pydantic AI workspace."""

from __future__ import annotations

import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field

import anyio
import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    SupportsCommands,
    SupportsFilesystem,
    SupportsRealpath,
    Workspace,
    WorkspaceBackend,
    WorkspaceCommand,
    WorkspaceRef,
    WorkspaceUnavailableError,
)

from pydantic_ai_backends._limits import MAX_RUN_OUTPUT_BYTES
from pydantic_ai_backends.remote import wire
from pydantic_ai_backends.types import CommandOutcome
from pydantic_ai_backends.workspaces._commands import (
    check_timeout,
    command_argv,
    command_result,
    layered_env,
)

logger = logging.getLogger(__name__)

SANDBOXD_PROVIDER = "sandboxd"
"""Default `WorkspaceRef.provider` of a `sandboxd` workspace."""

DEFAULT_REQUEST_TIMEOUT = 30.0
"""Seconds to wait for a request that runs no command: opening a session, a stop."""

TRANSPORT_SLACK_SECONDS = 10.0
"""Added to a command's deadline for the HTTP request carrying it, so the service
reports its own timeout instead of the client giving up on a running command."""

STOP_GRACE_SECONDS = 5.0
"""Longest a cancelled command's stop request may hold up the cancellation."""

OPENING_RETRY_SECONDS = 0.2
"""Pause before asking again for a named session another client is opening."""

DEFAULT_OPENING_TIMEOUT = 600.0
"""Seconds opening a session may take. It can include building the runtime's
image on the service host, which is minutes on a cold cache - far past a
request that runs no command."""

WAIT_NOTICE_SECONDS = 3.0
"""How long opening may take before `on_wait` is told it is waiting."""

_GONE = frozenset({404, 410})
"""Statuses meaning the session or its sandbox no longer exists."""


@dataclass(frozen=True)
class _Session:
    """An open session: its id and the token scoped to it."""

    session_id: str
    token: str
    execute_timeout: float
    native_reads: bool = False
    native_writes: bool = False
    native_realpath: bool = False


_PATH_ERRORS: dict[str, type[Exception]] = {
    "FileNotFoundError": FileNotFoundError,
    "IsADirectoryError": IsADirectoryError,
    "NotADirectoryError": NotADirectoryError,
    "FileExistsError": FileExistsError,
    "ValueError": ValueError,
}
"""The path errors a native file route reports, by the name it sends."""


_ENTRIES: TypeAdapter[list[wire.FileEntry]] = TypeAdapter(list[wire.FileEntry])


def _entry(row: wire.FileEntry) -> FileEntry:
    return FileEntry(name=row.name, path=row.path, is_dir=row.is_dir, size=row.size)


def _path_error(response: httpx.Response) -> Exception | None:
    """The builtin error a native route reported, if it reported one."""
    if response.status_code not in (400, 404):
        return None
    try:
        detail = response.json().get("detail")
    except ValueError:
        return None
    if not isinstance(detail, dict):
        return None
    kind = _PATH_ERRORS.get(str(detail.get("error")))
    return None if kind is None else kind(str(detail.get("message", "")))


class _CommandsOnly:
    """A backend seen through its commands alone, for the shell-derived file operations.

    `Workspace` derives every file operation from `run` when its backend has no
    native filesystem. Wrapped in this, the sandboxd backend is such a backend
    again, which is the path a native route hands an operation back to.
    """

    def __init__(self, backend: SandboxdWorkspaceBackend) -> None:
        self._backend = backend

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._backend.ref

    async def working_dir(self) -> str:
        return await self._backend.working_dir()

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        return await self._backend.run(command, shell=shell, env=env, timeout=timeout)


class SandboxdWorkspaceBackend(
    WorkspaceBackend, SupportsCommands, SupportsFilesystem, SupportsRealpath
):
    """One `sandboxd` session, as the environment an agent run works in.

    Commands go through the service's `/run`, which keeps stdout and stderr
    apart, reports a vanished sandbox as one and can be stopped. The session's
    files live on the service host, so a ref still attaches after the container
    was reaped, for as long as the service keeps the workspace (its
    `workspace_ttl`).

    **File operations are served off that host directory** where the service
    says it can (`ServicePolicy.native_files`): a read or a write is then one
    request handled in a millisecond, instead of the one to nine `docker exec`s
    the shell derivation costs. A path the service hands back - a symlink, a
    special file, anything outside the work directory - and every operation on
    a service that does not offer this, goes through the shell as before.

    Every command is bounded by the service's `execute_timeout`, including one
    asking for no timeout: the service enforces its ceiling on every caller.

    Args:
        service_url: Base URL of the service.
        token: The service token. It can open a session on the service's host,
            so treat it as the Docker socket it sits in front of.
        ref: The workspace to attach to; `None` opens a new session on first use.
        provider: Provider name in refs. Give each service its own when an agent
            can reach more than one, or a ref from one would attach on another.
        runtime: Runtime alias for a new session; the service default when `None`.
        tenant: Who the session is opened for, counted against the service's
            per-tenant ceiling.
        env: Variables every command gets, under any a call passes.
        client: An `httpx.AsyncClient` to share. Owned by the caller, who
            closes it; without one each request uses a client of its own.
        request_timeout: Seconds for a request that runs no command.
        opening_timeout: Seconds opening the session may take, waiting on the
            service included. Longer than `request_timeout` because an open can
            build the runtime's image first.
        on_wait: Awaited once when opening takes longer than a few seconds -
            the service is building the runtime, or another client is opening
            the same name - so a caller can say why nothing is happening yet.
            A failure in it is logged and does not stop the open.
        session_name: A session id chosen by whoever configures the workspace
            rather than by the service: with no ref, the first operation opens
            the session under it, or attaches when it is open or its files are
            kept. A ref is still attach-only, so a caller that knows the
            session existed learns that it is gone instead of starting over in
            an empty one.
    """

    def __init__(
        self,
        service_url: str,
        *,
        token: str,
        ref: WorkspaceRef | None = None,
        provider: str = SANDBOXD_PROVIDER,
        runtime: str | None = None,
        tenant: str | None = None,
        env: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        session_name: str | None = None,
        opening_timeout: float = DEFAULT_OPENING_TIMEOUT,
        on_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if ref is not None and ref.provider != provider:
            raise ValueError(f"expected a {provider!r} workspace ref, got {ref.provider!r}")
        if session_name is not None and not re.fullmatch(wire.SESSION_ID_PATTERN, session_name):
            raise ValueError(
                f"session_name {session_name!r} is not a session id the service accepts"
            )
        self._service_url = service_url.rstrip("/")
        self._token = token
        self._ref = ref
        self._provider = provider
        self._runtime = runtime
        self._tenant = tenant
        self._env = dict(env) if env else None
        self._client = client
        self._request_timeout = request_timeout
        self._session_name = session_name
        self._opening_timeout = max(opening_timeout, request_timeout)
        self._on_wait = on_wait
        self._session: _Session | None = None
        self._working_dir: str | None = None
        self._lock = anyio.Lock()

    @property
    def ref(self) -> WorkspaceRef | None:
        """The session id once it exists; `None` before the first operation."""
        return self._ref

    @contextlib.asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(base_url=self._service_url) as client:
            yield client

    def _url(self, path: str) -> str:
        # A shared client may carry its own base URL or none, so every request
        # names the service in full.
        return f"{self._service_url}{path}"

    async def _post(
        self, path: str, body: BaseModel | None, *, token: str, timeout: float
    ) -> httpx.Response:
        async with self._http() as client:
            return await client.post(
                self._url(path),
                json=None if body is None else body.model_dump(mode="json"),
                headers={wire.TOKEN_HEADER: token},
                timeout=timeout,
            )

    async def _create(self) -> httpx.Response:
        """Ask the service for the session, waiting for as long as opening may take.

        The request itself gets `opening_timeout`, because the service builds a
        runtime's image inside the first open that needs it.
        """
        named = self._ref is None and self._session_name is not None
        request = wire.CreateSessionRequest(
            session_id=self._session_name if self._ref is None else self._ref.id,
            runtime=self._runtime,
            tenant=self._tenant,
            reuse=named,
            attach=self._ref is not None,
        )
        give_up = anyio.current_time() + self._opening_timeout
        response = await self._post(
            "/sessions", request, token=self._token, timeout=self._opening_timeout
        )
        # With `reuse` a 409 means another client is opening the same name right
        # now - two first runs of one conversation, say, or one of them waiting on
        # an image build. It is theirs a moment later, so ask again rather than
        # fail one of them.
        while named and response.status_code == 409 and anyio.current_time() < give_up:
            await anyio.sleep(OPENING_RETRY_SECONDS)
            response = await self._post(
                "/sessions", request, token=self._token, timeout=self._opening_timeout
            )
        return response

    @staticmethod
    async def _announce_wait(on_wait: Callable[[], Awaitable[None]]) -> None:
        """Tell `on_wait` once that opening is taking a while; cancelled if it is not."""
        await anyio.sleep(WAIT_NOTICE_SECONDS)
        try:
            await on_wait()
        except Exception:
            # A notice is decoration: the session it is about must still open.
            logger.warning("sandboxd_wait_notice_failed", exc_info=True)

    async def _open(self) -> _Session:
        """Open or attach to the session, then learn the service's command ceiling."""
        # Kept out of the task group: an exception leaving its body comes out as an
        # `ExceptionGroup`, and a caller of `run` catches `httpx.HTTPError`.
        failure: Exception | None = None
        response: httpx.Response | None = None
        async with anyio.create_task_group() as group:
            if self._on_wait is not None:
                group.start_soon(self._announce_wait, self._on_wait)
            try:
                response = await self._create()
            except Exception as exc:
                failure = exc
            group.cancel_scope.cancel()
        if failure is not None:
            raise failure
        assert response is not None
        if self._ref is not None and response.status_code == 404:
            raise WorkspaceUnavailableError(f"sandboxd session {self._ref.id!r} no longer exists")
        response.raise_for_status()
        created = wire.SessionCreated.model_validate_json(response.content)
        if self._ref is None:
            # Recorded before anything else can fail, so a caller holding this
            # backend can always remove what it opened.
            self._ref = WorkspaceRef(provider=self._provider, id=created.session.session_id)

        async with self._http() as client:
            policy_response = await client.get(
                self._url("/policy"),
                headers={wire.TOKEN_HEADER: self._token},
                timeout=self._request_timeout,
            )
        policy_response.raise_for_status()
        policy = wire.ServicePolicy.model_validate_json(policy_response.content)
        return _Session(
            session_id=created.session.session_id,
            token=created.token,
            execute_timeout=float(policy.execute_timeout),
            native_reads=policy.native_files,
            native_writes=policy.native_writes,
            native_realpath=policy.native_realpath,
        )

    async def _connect(self) -> _Session:
        async with self._lock:
            if self._session is not None:
                return self._session
            # Shielded so a caller cancelled mid-open still records the ref of a
            # session the service has already opened. Bounded by the request
            # timeout of the two requests inside.
            with anyio.CancelScope(shield=True):
                opened = await self._open()
                self._session = opened
            return opened

    async def working_dir(self) -> str:
        """The directory commands start in, as the sandbox resolves it."""
        if self._working_dir is None:
            result = await self.run(["sh", "-c", "pwd -P"])
            if result.exit_code != 0:
                raise WorkspaceUnavailableError(
                    f"could not resolve the sandbox's working directory: {result.stderr.strip()}"
                )
            self._working_dir = result.stdout.rstrip("\n")
        return self._working_dir

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command in the session's sandbox; see `SupportsCommands.run`.

        Raises:
            WorkspaceUnavailableError: The session or its sandbox is gone.
            WorkspaceTimeoutError: The command reached `timeout`, or the
                service's own ceiling first.
            WorkspaceOutputLimitError: Its combined output passed the limit.
            httpx.HTTPError: The service could not be reached or answered with
                an unexpected status — transient, and left for a caller to retry.
        """
        argv = command_argv(command, shell)
        check_timeout(timeout)
        session = await self._connect()
        request = wire.RunRequest(
            argv=argv,
            env=layered_env(self._env, env) or {},
            timeout_seconds=timeout,
            run_id=uuid.uuid4().hex,
        )
        deadline = (
            session.execute_timeout if timeout is None else min(timeout, session.execute_timeout)
        )
        path = f"/sessions/{session.session_id}"
        answered = False
        try:
            response = await self._post(
                f"{path}/run",
                request,
                token=session.token,
                timeout=deadline + TRANSPORT_SLACK_SECONDS,
            )
            answered = True
        finally:
            # A caller cancelled mid-command, or a request that failed in
            # transit, may have left the command running on the service.
            if not answered:
                await self._stop(f"{path}/runs/{request.run_id}/stop", session.token)
        if response.status_code in _GONE:
            raise WorkspaceUnavailableError(
                f"sandboxd session {session.session_id!r} is gone: {response.text}"
            )
        response.raise_for_status()
        try:
            ran = wire.RunResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise ValueError(f"sandboxd answered /run with something else: {error}") from error
        outcome = CommandOutcome(
            stdout=ran.stdout,
            stderr=ran.stderr,
            exit_code=ran.exit_code,
            timed_out=ran.timed_out,
            output_limited=ran.output_limited,
        )
        # A timeout past the service's ceiling never applied: the ceiling did.
        applied = timeout if timeout is not None and timeout <= session.execute_timeout else None
        return command_result(
            outcome, timeout=applied, limit=ran.output_limit or MAX_RUN_OUTPUT_BYTES
        )

    async def read_bytes(self, path: str) -> bytes:
        """A file's contents; see `SupportsFilesystem.read_bytes`."""
        response = await self._native("read", path, writes=False)
        return (await self._shell().read_bytes(path)) if response is None else response.content

    async def write_bytes(self, path: str, data: bytes) -> None:
        """Write a file, creating its parents; see `SupportsFilesystem.write_bytes`."""
        if await self._native("write", path, writes=True, content=data) is None:
            await self._shell().write_bytes(path, data)

    async def stat(self, path: str) -> FileEntry:
        """A path's metadata; see `SupportsFilesystem.stat`."""
        response = await self._native("stat", path, writes=False)
        if response is None:
            return await self._shell().stat(path)
        return _entry(wire.FileEntry.model_validate_json(response.content))

    async def list_dir(self, path: str) -> list[FileEntry]:
        """A directory's entries; see `SupportsFilesystem.list_dir`."""
        response = await self._native("list", path, writes=False)
        if response is None:
            return list(await self._shell().list_dir(path))
        rows = _ENTRIES.validate_json(response.content)
        return [_entry(row) for row in rows]

    async def make_dir(self, path: str) -> None:
        """`mkdir -p`; see `SupportsFilesystem.make_dir`."""
        if await self._native("mkdir", path, writes=True) is None:
            await self._shell().make_dir(path)

    async def remove(self, path: str) -> None:
        """Remove a file or a tree; see `SupportsFilesystem.remove`."""
        if await self._native("remove", path, writes=True) is None:
            await self._shell().remove(path)

    async def exists(self, path: str) -> bool:
        """Whether something is at `path`; see `SupportsFilesystem.exists`."""
        try:
            await self.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True

    async def realpath(self, path: str) -> str:
        """`path` with its links resolved; see `SupportsRealpath.realpath`.

        A path check - the console's guard, a confined workspace - asks this
        before every read and write. Answered by the service for a path with no
        link in it; a link, and a service that does not offer this, resolve in
        the sandbox as before.
        """
        session = await self._connect()
        if session.native_realpath:
            response = await self._native("realpath", path, writes=False)
            if response is not None:
                return wire.RealpathResponse.model_validate_json(response.content).path
        return await self._shell().realpath(path)

    def _shell(self) -> Workspace:
        """The shell-derived file operations, for what the service hands back."""
        return Workspace(_CommandsOnly(self))

    async def _native(
        self, op: str, path: str, *, writes: bool, content: bytes | None = None
    ) -> httpx.Response | None:
        """Ask the service to do one file operation itself.

        Returns:
            The answer, or `None` when the service does not do this one natively
            - it said so in its policy, or handed this path back - and the shell
            has to.

        Raises:
            The builtin path error the service reported, or
            `WorkspaceUnavailableError` for a session that is gone.
        """
        session = await self._connect()
        if not (session.native_writes if writes else session.native_reads):
            return None
        route = f"/sessions/{session.session_id}/files/{op}"
        headers = {wire.TOKEN_HEADER: session.token}
        async with self._http() as client:
            if content is None:
                response = await client.post(
                    self._url(route),
                    json={"path": path},
                    headers=headers,
                    timeout=self._request_timeout,
                )
            else:
                response = await client.post(
                    self._url(route),
                    params={"path": path},
                    content=content,
                    headers={**headers, "content-type": "application/octet-stream"},
                    timeout=self._request_timeout,
                )
        if response.status_code == wire.NOT_NATIVE_STATUS:
            return None
        error = _path_error(response)
        if error is not None:
            raise error
        if response.status_code in _GONE:
            raise WorkspaceUnavailableError(
                f"sandboxd session {session.session_id!r} is gone: {response.text}"
            )
        response.raise_for_status()
        return response

    async def purge(self) -> None:
        """Close this backend's session and delete its files. Already gone is fine.

        Attaches first when the session is closed but its workspace is kept, since
        the service deletes a workspace only through its session.

        Raises:
            httpx.HTTPError: The service could not be reached or refused.
        """
        try:
            session = await self._connect()
        except WorkspaceUnavailableError:
            return
        async with self._http() as client:
            response = await client.delete(
                self._url(f"/sessions/{session.session_id}"),
                params={"purge": "true"},
                headers={wire.TOKEN_HEADER: self._token},
                timeout=self._request_timeout,
            )
        if response.status_code not in _GONE:
            response.raise_for_status()

    async def _stop(self, path: str, token: str) -> None:
        """Ask the service to stop a run whose caller stopped waiting, if it can.

        Best effort: the caller is leaving with its own error or cancellation,
        which a failed stop must not replace.
        """
        with (
            anyio.CancelScope(shield=True),
            anyio.move_on_after(STOP_GRACE_SECONDS),
            contextlib.suppress(httpx.HTTPError),
        ):
            await self._post(path, None, token=token, timeout=STOP_GRACE_SECONDS)


@dataclass(kw_only=True)
class SandboxdWorkspace(AbstractCapability[object]):
    """Supply a `sandboxd` session as the run's workspace.

    A run with no ref opens a session; one carrying this capability's provider
    attaches to that session, or to the workspace the service kept after it was
    reaped. Sessions are kept after the run — the ref is how to come back, and
    :meth:`destroy` is how to remove one with its files.

    This supplies the environment only. Compose it with something that uses the
    workspace: `Coder`, `Shell` or `FileSystem` from the Pydantic AI harness, or
    this library's `ConsoleCapability`.

    Example:
        ```python
        from pydantic_ai import Agent
        from pydantic_ai_harness.coder import Coder

        from pydantic_ai_backends.workspaces import SandboxdWorkspace

        sandbox = SandboxdWorkspace(service_url="http://sandboxd:8080", token="...")
        agent = Agent("anthropic:claude-opus-5-5", capabilities=[sandbox, Coder()])
        ```
    """

    service_url: str
    """Base URL of the service."""

    token: str = field(repr=False)
    """The service token: it can open sessions on the host, so keep it out of logs."""

    provider: str = SANDBOXD_PROVIDER
    """Provider name in refs; distinct per service when an agent can reach several."""

    runtime: str | None = None
    """Runtime alias for new sessions; the service default when `None`."""

    tenant: str | None = None
    """Who sessions are opened for, against the service's per-tenant ceiling."""

    env: Mapping[str, str] | None = field(default=None, repr=False)
    """Variables every command gets. Nothing is read from the host's environment."""

    client: httpx.AsyncClient | None = field(default=None, repr=False, compare=False)
    """An `httpx.AsyncClient` to share across runs, owned and closed by the caller."""

    opening_timeout: float = DEFAULT_OPENING_TIMEOUT
    """Seconds opening a session may take; it can include building the runtime's image."""

    on_wait: Callable[[], Awaitable[None]] | None = field(default=None, repr=False, compare=False)
    """Awaited once when opening a session takes more than a few seconds, so a caller
    can say why nothing is happening yet. A failure in it does not stop the open."""

    session_name: str | None = None
    """One session for every run, named by you: opened on first use, attached after.

    Without it each run without a ref gets a new session and only the ref leads
    back to it. With it a later process finds the same session - or the files the
    service kept after reaping it - by name. A ref naming another session is left
    to another capability.
    """

    def __post_init__(self) -> None:
        if self.defer_loading:
            raise UserError(
                "`SandboxdWorkspace` does not support `defer_loading=True`: "
                "the workspace is selected before deferred capabilities load."
            )

    def backend(self, ref: WorkspaceRef | None = None) -> SandboxdWorkspaceBackend:
        """A backend for `ref`, or for a new session; no I/O until its first operation."""
        return SandboxdWorkspaceBackend(
            self.service_url,
            token=self.token,
            ref=ref,
            provider=self.provider,
            runtime=self.runtime,
            tenant=self.tenant,
            env=self.env,
            client=self.client,
            session_name=self.session_name,
            opening_timeout=self.opening_timeout,
            on_wait=self.on_wait,
        )

    def get_workspace(
        self, ctx: RunContext[object], *, ref: WorkspaceRef | None
    ) -> WorkspaceBackend | None:
        """This run's backend, or `None` for a ref another provider owns."""
        del ctx
        if ref is not None and ref.provider != self.provider:
            return None
        if ref is not None and self.session_name is not None and ref.id != self.session_name:
            return None
        return self.backend(ref)

    async def destroy(self, ref: WorkspaceRef) -> None:
        """Close the session `ref` names and delete its files. Already gone is fine.

        Raises:
            ValueError: `ref` belongs to another provider.
            httpx.HTTPError: The service could not be reached or refused.
        """
        if ref.provider != self.provider:
            raise ValueError(f"expected a {self.provider!r} workspace ref, got {ref.provider!r}")
        await self.backend(ref).purge()
