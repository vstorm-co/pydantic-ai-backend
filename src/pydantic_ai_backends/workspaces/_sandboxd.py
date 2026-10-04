"""A `sandboxd` session as a Pydantic AI workspace."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field

import anyio
import httpx
from pydantic import BaseModel, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import (
    CommandResult,
    SupportsCommands,
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

SANDBOXD_PROVIDER = "sandboxd"
"""Default `WorkspaceRef.provider` of a `sandboxd` workspace."""

DEFAULT_REQUEST_TIMEOUT = 30.0
"""Seconds to wait for a request that runs no command: opening a session, a stop."""

TRANSPORT_SLACK_SECONDS = 10.0
"""Added to a command's deadline for the HTTP request carrying it, so the service
reports its own timeout instead of the client giving up on a running command."""

STOP_GRACE_SECONDS = 5.0
"""Longest a cancelled command's stop request may hold up the cancellation."""

_GONE = frozenset({404, 410})
"""Statuses meaning the session or its sandbox no longer exists."""


@dataclass(frozen=True)
class _Session:
    """An open session: its id and the token scoped to it."""

    session_id: str
    token: str
    execute_timeout: float


class SandboxdWorkspaceBackend(WorkspaceBackend, SupportsCommands):
    """One `sandboxd` session, as the environment an agent run works in.

    Commands go through the service's `/run`, which keeps stdout and stderr
    apart, reports a vanished sandbox as one and can be stopped; `Workspace`
    derives file operations through them. The session's files live on the
    service host, so a ref still attaches after the container was reaped, for as
    long as the service keeps the workspace (its `workspace_ttl`).

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
    ) -> None:
        if ref is not None and ref.provider != provider:
            raise ValueError(f"expected a {provider!r} workspace ref, got {ref.provider!r}")
        self._service_url = service_url.rstrip("/")
        self._token = token
        self._ref = ref
        self._provider = provider
        self._runtime = runtime
        self._tenant = tenant
        self._env = dict(env) if env else None
        self._client = client
        self._request_timeout = request_timeout
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

    async def _open(self) -> _Session:
        """Open or attach to the session, then learn the service's command ceiling."""
        request = wire.CreateSessionRequest(
            session_id=None if self._ref is None else self._ref.id,
            runtime=self._runtime,
            tenant=self._tenant,
            attach=self._ref is not None,
        )
        response = await self._post(
            "/sessions", request, token=self._token, timeout=self._request_timeout
        )
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
        try:
            response = await self._post(
                f"{path}/run",
                request,
                token=session.token,
                timeout=deadline + TRANSPORT_SLACK_SECONDS,
            )
        except BaseException:
            await self._stop(f"{path}/runs/{request.run_id}/stop", session.token)
            raise
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
    this library's `ConsoleCapability(use_workspace=True)`.

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
        )

    def get_workspace(
        self, ctx: RunContext[object], *, ref: WorkspaceRef | None
    ) -> WorkspaceBackend | None:
        """This run's backend, or `None` for a ref another provider owns."""
        del ctx
        if ref is not None and ref.provider != self.provider:
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
