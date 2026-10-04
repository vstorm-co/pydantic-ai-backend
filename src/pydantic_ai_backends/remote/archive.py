"""Reading what `sandboxd` sessions left behind, without a sandbox.

Kept apart from the workspaces because it is not one: the service serves a
session's host directory straight off its volume, so a session reaped long ago
is still browsable and listing its files costs no container start. An
application uses it to show a user the files an agent wrote.
"""

from __future__ import annotations

import base64
import contextlib
from typing import TYPE_CHECKING, Any

from pydantic_ai_backends._optional import load
from pydantic_ai_backends.remote import wire
from pydantic_ai_backends.types import FileInfo

if TYPE_CHECKING:
    import httpx

DEFAULT_TIMEOUT_SECONDS = 60.0
"""Request timeout for one archive read."""


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

    This **raises** rather than degrading. Nothing here is in an agent's tool
    path — the caller is an application answering a user who asked to see some
    files, and it needs to tell "there are none" apart from "the service is
    misconfigured".

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
        Path("chart.png").write_bytes(archive.read_bytes(session_id, "chart.png"))
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
        return [
            FileInfo(
                name=e.name, path=e.path, is_dir=e.is_dir, size=e.size, modified_at=e.modified_at
            )
            for e in entries
        ]

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

    def read_bytes(self, session_id: str, path: str) -> bytes:
        """Read a whole stored workspace file as bytes.

        The sibling of :meth:`read`, and the one to use for anything an agent
        produced that is not text - a chart, a rendered PDF, an image it fetched.
        Those are the commonest contents of a real workspace, and `read` decodes
        them: the result re-encodes to a corrupt file that nonetheless downloads
        successfully, which is worse than an error, so a caller serving downloads
        had to refuse them instead.

        Whole rather than sliced, because a byte range means nothing for the
        formats this exists for. `ls` carries `size`, so a caller that wants to
        bound a read can look first.

        Raises:
            WorkspaceArchiveError: If the file is absent, over the service's read
                ceiling, outside the workspace, or the service is unreachable.
        """
        payload = wire.ReadBytesRequest(path=path).model_dump(mode="json")
        response = self._post(f"/workspaces/{session_id}/read_bytes", payload)
        encoded = wire.ReadBytesResponse.model_validate(response.json()).content_b64
        return base64.b64decode(encoded)

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
