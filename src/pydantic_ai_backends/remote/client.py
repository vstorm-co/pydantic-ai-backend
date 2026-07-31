"""RemoteSandbox — a sandbox that lives in another process.

Lets an application use sandboxes without holding the Docker socket itself,
which is what makes it safe to run the app in a container: the sandbox service
owns Docker, the app only speaks HTTP to it. No docker-in-docker.
"""

from __future__ import annotations

import base64
import contextlib
import threading
import uuid
from typing import TYPE_CHECKING, Any

from pydantic_ai_backends._optional import load
from pydantic_ai_backends.backends.base import BaseSandbox
from pydantic_ai_backends.remote import wire
from pydantic_ai_backends.types import (
    EditResult,
    ExecuteResponse,
    FileInfo,
    GrepMatch,
    SandboxUsage,
    WriteResult,
)

if TYPE_CHECKING:
    import httpx

TRANSPORT_SLACK_SECONDS = 10.0
"""Added to the HTTP timeout on top of a command's own, so the transport never
gives up before the command it is waiting for."""

DEFAULT_TIMEOUT_SECONDS = 60.0
"""Request timeout for operations that carry no timeout of their own."""


class RemoteSandbox(BaseSandbox):
    """Sandbox backed by a `sandboxd` service over HTTP.

    Implements the same synchronous surface as
    :class:`~pydantic_ai_backends.DockerSandbox`, so it drops into
    :class:`~pydantic_ai_backends.SessionManager` or a console toolset
    unchanged. Every file operation is served by the remote side rather than
    derived from shell commands, so `read` of a large file transfers only the
    requested slice.

    The session — and therefore the container behind it — is opened on the first
    operation, not on construction. An agent granted a sandbox it never uses
    costs nothing: no session, no container, not even a round trip. Call
    :meth:`start` explicitly only to pre-warm one.

    Failures are returned, never raised: a transport error surfaces the same way
    a missing file does (`b""`, `[]`, an `Error: ...` string), matching
    `LocalBackend` and `DockerSandbox`. A tool call must not take down an
    agent run because a socket blipped.

    Args:
        service_url: Base URL of the service, e.g. `"http://sandboxd:8080"`.
            Ignored when `client` is supplied.
        token: Service token, sent as the `X-Sandbox-Token` header. Used to open
            a session; afterwards the session's own token is used.
        session_id: Identifier to open or re-attach to. Generated when omitted.
        reuse: Attach to the session under `session_id` when the service already
            has one open, instead of failing. Off by default, so a colliding id
            is reported rather than silently sharing another caller's sandbox.
            Turn it on for a sandbox that spans several runs — one conversation,
            say — where a later run has to reach the files an earlier one wrote.
        runtime: Alias of a server-allowed runtime. `None` takes the server
            default. The server rejects anything not on its allowlist.
        tenant: Whoever this sandbox is for, against which the service applies
            its per-tenant session ceiling. A capacity label only — it grants
            nothing and authorizes nothing.
        timeout: Request timeout in seconds for operations without their own.
        client: Pre-built `httpx.Client`. Supply one to share a connection pool,
            set custom transport or TLS, or drive an in-process ASGI app.

    Example:
        ```python
        from pydantic_ai_backends.remote import RemoteSandbox

        sandbox = RemoteSandbox("http://sandboxd:8080", token="...")
        sandbox.start()
        print(sandbox.execute("python -c 'print(1+1)'").output)
        sandbox.stop()
        ```
    """

    def __init__(
        self,
        service_url: str = "http://localhost:8080",
        *,
        token: str = "",
        session_id: str | None = None,
        runtime: str | None = None,
        tenant: str | None = None,
        reuse: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(session_id or f"remote-{uuid.uuid4().hex[:12]}")
        self._service_token = token
        self._session_token = token
        self._runtime = runtime
        self._tenant = tenant
        self._reuse = reuse
        self._timeout = timeout
        self._started = False
        # Operations run on a thread pool, so two of them can arrive at an
        # unopened session at once; without this both would POST /sessions and
        # one would lose to its own 409.
        self._open_lock = threading.Lock()
        self.touch()

        if client is not None:
            self._http = client
            self._owns_client = False
        else:
            httpx_module = load("httpx", purpose="RemoteSandbox")
            self._http = httpx_module.Client(
                base_url=service_url.rstrip("/"),
                timeout=httpx_module.Timeout(timeout),
            )
            self._owns_client = True

    @property
    def session_id(self) -> str:
        """Alias for the sandbox id, for parity with `DockerSandbox`."""
        return self._id

    def _url(self, operation: str) -> str:
        return f"/sessions/{self._id}/{operation}"

    def _post(self, url: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
        """POST JSON with the session token, returning the response or `None`.

        Opens the session first if this is the first operation.

        Returns:
            The `httpx.Response`, or `None` when the request could not be made
            or the service answered with an error status.
        """
        if not self._ensure_session():
            return None

        self.touch()
        try:
            response = self._http.post(
                url,
                json=payload,
                headers={wire.TOKEN_HEADER: self._session_token},
                timeout=timeout if timeout is not None else self._timeout,
            )
        except Exception:
            return None
        return None if response.status_code >= 400 else response

    def _ensure_session(self) -> bool:
        """Open the session on first use.

        Returns:
            Whether a session is available. A failure here is reported the same
            way a failed operation is — the caller degrades instead of raising,
            because a tool call must not end an agent run.
        """
        if self._started:
            return True
        with self._open_lock:
            if self._started:
                return True
            try:
                self.start()
            except RuntimeError:
                return False
        return True

    def start(self) -> None:
        """Open the remote session, or attach to it when `reuse` is set.

        Idempotent within one instance: repeated calls do nothing once a session
        is open. With `reuse`, a *new* instance naming an existing session id
        attaches to it rather than failing — which is what makes a sandbox
        outlive the run that created it.

        Raises:
            RuntimeError: If the service refuses to open a session. Unlike the
                file operations this does raise, because a caller that cannot
                get a sandbox at all needs to know why.
        """
        if self._started:
            return

        body = wire.CreateSessionRequest(
            session_id=self._id,
            runtime=self._runtime,
            tenant=self._tenant,
            reuse=self._reuse,
        )
        try:
            response = self._http.post(
                "/sessions",
                json=body.model_dump(mode="json"),
                headers={wire.TOKEN_HEADER: self._service_token},
                timeout=self._timeout,
            )
        except Exception as exc:
            raise RuntimeError(f"Could not reach the sandbox service: {exc}") from exc

        if response.status_code >= 400:
            raise RuntimeError(
                f"Sandbox service refused to open a session "
                f"(HTTP {response.status_code}): {response.text}"
            )

        created = wire.SessionCreated.model_validate(response.json())
        self._id = created.session.session_id
        self._session_token = created.token
        self._started = True

    def is_alive(self) -> bool:
        """Whether the remote session exists and its sandbox is running."""
        try:
            response = self._http.get(
                f"/sessions/{self._id}",
                headers={wire.TOKEN_HEADER: self._session_token},
                timeout=self._timeout,
            )
        except Exception:
            return False
        if response.status_code >= 400:
            return False
        return wire.SessionInfo.model_validate(response.json()).alive

    def resource_usage(self) -> SandboxUsage | None:
        """Sample the remote sandbox's resource usage."""
        try:
            response = self._http.get(
                f"/sessions/{self._id}",
                params={"usage": "true"},
                headers={wire.TOKEN_HEADER: self._session_token},
                timeout=self._timeout,
            )
        except Exception:
            return None
        if response.status_code >= 400:
            return None
        usage = wire.SessionInfo.model_validate(response.json()).usage
        if usage is None:
            return None
        return SandboxUsage(
            memory_bytes=usage.memory_bytes,
            memory_limit_bytes=usage.memory_limit_bytes,
            cpu_percent=usage.cpu_percent,
            pids=usage.pids,
        )

    def stop(self, purge: bool = False) -> None:
        """Delete the remote session and close an owned HTTP client.

        Args:
            purge: Also discard what the session accumulated — its container's
                filesystem and its host workspace. Leave it off to end a turn
                while keeping the files for the next one; turn it on when the
                thing the session belonged to is gone for good.
        """
        if self._started:
            # Best effort: the service reaps idle sessions on its own, so a
            # failed teardown costs a timeout, not a leak.
            with contextlib.suppress(Exception):
                self._http.delete(
                    f"/sessions/{self._id}",
                    params={"purge": "true"} if purge else None,
                    headers={wire.TOKEN_HEADER: self._session_token},
                    timeout=self._timeout,
                )
            self._started = False

        if self._owns_client:
            with contextlib.suppress(Exception):
                self._http.close()

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Run a command in the remote sandbox."""
        body = wire.ExecRequest(command=command, timeout_seconds=timeout)
        transport_timeout = (
            timeout + TRANSPORT_SLACK_SECONDS if timeout is not None else self._timeout
        )
        response = self._post(self._url("exec"), body.model_dump(mode="json"), transport_timeout)
        if response is None:
            return ExecuteResponse(output="Error: sandbox service unavailable", exit_code=1)
        parsed = wire.ExecResponse.model_validate(response.json())
        return ExecuteResponse(
            output=parsed.output,
            exit_code=parsed.exit_code,
            truncated=parsed.truncated,
        )

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a slice of a text file."""
        body = wire.ReadRequest(path=path, offset=offset, limit=limit)
        response = self._post(self._url("read"), body.model_dump(mode="json"))
        if response is None:
            return f"Error: could not read '{path}'"
        return wire.ReadResponse.model_validate(response.json()).content

    def read_bytes(self, path: str) -> bytes:
        """Read a whole file as bytes, or `b""` when unreadable."""
        body = wire.ReadBytesRequest(path=path)
        response = self._post(self._url("read_bytes"), body.model_dump(mode="json"))
        if response is None:
            return b""
        encoded = wire.ReadBytesResponse.model_validate(response.json()).content_b64
        try:
            return base64.b64decode(encoded, validate=True)
        except Exception:
            return b""

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write a file."""
        raw = content if isinstance(content, bytes) else content.encode("utf-8")
        body = wire.WriteRequest(path=path, content_b64=base64.b64encode(raw).decode("ascii"))
        response = self._post(self._url("write"), body.model_dump(mode="json"))
        if response is None:
            return WriteResult(error=f"Error: could not write '{path}'")
        parsed = wire.WriteResponse.model_validate(response.json())
        return WriteResult(path=parsed.path, error=parsed.error)

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Replace a string inside a file."""
        body = wire.EditRequest(
            path=path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )
        response = self._post(self._url("edit"), body.model_dump(mode="json"))
        if response is None:
            return EditResult(error=f"Error: could not edit '{path}'")
        parsed = wire.EditResponse.model_validate(response.json())
        return EditResult(path=parsed.path, error=parsed.error, occurrences=parsed.occurrences)

    def exists(self, path: str) -> bool:
        """Whether the path is a regular file."""
        body = wire.ExistsRequest(path=path)
        response = self._post(self._url("exists"), body.model_dump(mode="json"))
        if response is None:
            return False
        return wire.ExistsResponse.model_validate(response.json()).exists

    def ls_info(self, path: str) -> list[FileInfo]:
        """List one directory, or `[]` when it cannot be listed."""
        body = wire.LsRequest(path=path)
        response = self._post(self._url("ls"), body.model_dump(mode="json"))
        return _to_file_infos(response)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Match files by glob, or `[]` when the search cannot be run."""
        body = wire.GlobRequest(pattern=pattern, path=path)
        response = self._post(self._url("glob"), body.model_dump(mode="json"))
        return _to_file_infos(response)

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search file contents, returning matches or an error string."""
        body = wire.GrepRequest(pattern=pattern, path=path, glob=glob, ignore_hidden=ignore_hidden)
        response = self._post(self._url("grep"), body.model_dump(mode="json"))
        if response is None:
            return f"Error: could not search for {pattern!r}"
        parsed = wire.GrepResponse.model_validate(response.json())
        if parsed.error is not None:
            return parsed.error
        return [
            GrepMatch(path=m.path, line_number=m.line_number, line=m.line) for m in parsed.matches
        ]


def _to_file_infos(response: Any) -> list[FileInfo]:
    """Parse a listing response into `FileInfo` rows, tolerating failure."""
    if response is None:
        return []
    entries = [wire.FileEntry.model_validate(row) for row in response.json()]
    return [FileInfo(name=e.name, path=e.path, is_dir=e.is_dir, size=e.size) for e in entries]


class WorkspaceArchiveError(Exception):
    """Raised when a stored workspace cannot be listed or read.

    Attributes:
        status_code: What the service answered, or `None` when it could not be
            reached at all. An application proxying file views to its own users
            maps this onto its own response.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class WorkspaceArchive:
    """Read-only view of the files sessions left behind.

    Reads the service's host volume directly, so a session reaped long ago is
    still browsable and listing a conversation's files costs no container start.

    Unlike :class:`RemoteSandbox`, this **raises** rather than degrading. Nothing
    here is in an agent's tool path — the caller is an application answering a
    user who asked to see some files, and it needs to tell "there are none" apart
    from "the service is misconfigured".

    Requires the service to be running with `SandboxdConfig.workspace_root` set,
    and the service token: a reaped session has no token of its own left, and the
    intended caller is a backend applying its own authorization first.

    Args:
        service_url: Base URL of the service. Ignored when `client` is supplied.
        token: Service token.
        timeout: Request timeout in seconds.
        client: Pre-built `httpx.Client`, to share a connection pool or drive an
            in-process ASGI app.

    Example:
        ```python
        from pydantic_ai_backends.remote import WorkspaceArchive

        archive = WorkspaceArchive("http://sandboxd:8080", token="...")
        for entry in archive.ls(session_id):
            print(entry["path"], entry["size"])
        print(archive.read(session_id, "report.md"))
        ```
    """

    def __init__(
        self,
        service_url: str = "http://localhost:8080",
        *,
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._timeout = timeout
        if client is not None:
            self._http = client
            self._owns_client = False
        else:
            httpx_module = load("httpx", purpose="WorkspaceArchive")
            self._http = httpx_module.Client(
                base_url=service_url.rstrip("/"),
                timeout=httpx_module.Timeout(timeout),
            )
            self._owns_client = True

    def ls(self, session_id: str, path: str = ".") -> list[FileInfo]:
        """List one directory of a stored workspace.

        Args:
            session_id: Session whose files are wanted.
            path: Directory to list. An absolute in-container path works, so a
                path taken from a live session's listing can be handed straight
                back.

        Raises:
            WorkspaceArchiveError: If the workspace or directory is absent, the
                path escapes the workspace, or the service cannot be reached.
        """
        payload = wire.LsRequest(path=path).model_dump(mode="json")
        response = self._post(f"/workspaces/{session_id}/ls", payload)
        entries = [wire.FileEntry.model_validate(row) for row in response.json()]
        return [FileInfo(name=e.name, path=e.path, is_dir=e.is_dir, size=e.size) for e in entries]

    def read(self, session_id: str, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a slice of a stored workspace file.

        Decoded exactly as a live session would decode it, so the archive and the
        sandbox never disagree about what a file says.

        Raises:
            WorkspaceArchiveError: If the file is absent, too large, not readable
                as text, outside the workspace, or the service is unreachable.
        """
        payload = wire.ReadRequest(path=path, offset=offset, limit=limit).model_dump(mode="json")
        response = self._post(f"/workspaces/{session_id}/read", payload)
        return wire.ReadResponse.model_validate(response.json()).content

    def close(self) -> None:
        """Close the HTTP client, when this object built it."""
        if self._owns_client:
            with contextlib.suppress(Exception):
                self._http.close()

    def _post(self, url: str, payload: dict[str, Any]) -> Any:
        """POST with the service token, raising on anything but success."""
        try:
            response = self._http.post(
                url,
                json=payload,
                headers={wire.TOKEN_HEADER: self._token},
                timeout=self._timeout,
            )
        except Exception as exc:
            raise WorkspaceArchiveError(f"Could not reach the sandbox service: {exc}") from exc

        if response.status_code >= 400:
            raise WorkspaceArchiveError(response.text, status_code=response.status_code)
        return response
