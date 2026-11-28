"""A managed sandboxd Browser client; reads and attachment never wake a sandbox."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from pydantic_ai_backends.remote.wire import SESSION_ID_PATTERN, TOKEN_HEADER

from ._session import BrowserSession
from ._transport import BrowserError, WebSocketCDP

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


class BrowserUnavailableError(BrowserError):
    """The exact managed browser is absent, expired or temporarily unreachable."""


class BrowserGoneError(BrowserUnavailableError):
    """The server authoritatively reports that the exact browser no longer exists."""


class BrowserConflictError(BrowserError):
    """The requested browser policy or physical lease conflicts with its current state."""


class BrowserAuthorizationError(BrowserError):
    """sandboxd refused this session's credential."""


class ManagedBrowserState(BaseModel):
    """sandboxd's monotonic-clock snapshot, not UTC timestamps or a resume promise."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    environment_id: str
    kind: Literal["browser"]
    state: Literal["ready", "unavailable", "expired"]
    created_at: float = Field(ge=0, allow_inf_nan=False)
    last_activity_at: float = Field(ge=0, allow_inf_nan=False)
    expires_at: float = Field(ge=0, allow_inf_nan=False)
    lease_owner: str | None
    persistent: bool = False
    profile_restored: bool = False
    profile_saved_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    generation: str | None = Field(default=None, min_length=1, max_length=256)


class _Start(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    idle_seconds: int = Field(ge=1, le=86400)
    lifetime_seconds: int = Field(ge=1, le=86400)
    width: int = Field(ge=640, le=1920)
    height: int = Field(ge=480, le=1200)
    profile_key: SecretStr | None = Field(default=None, repr=False)


@dataclass
class ManagedBrowserConnection:
    """An attached viewer. Closing it preserves the supervised context and tabs.

    Call SandboxdBrowser.end to retire the managed environment. Keep context_id
    in application-owned session state to reattach the same live context later.
    """

    browser: BrowserSession
    transport: WebSocketCDP = field(repr=False)

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def __aenter__(self) -> ManagedBrowserConnection:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


@dataclass(frozen=True)
class SandboxdBrowser:
    """Control a browser inside an existing dedicated sandboxd browser session.

    This client never creates a workspace/session. Start is the only method that
    may start the already-authorized session's browser process. Inspect/connect
    refuse missing or expired state without healing it. Endpoint/token are trusted
    application configuration; never send them to a browser UI. Mutations are
    never automatically retried after an uncertain provider response.
    """

    base_url: str
    session_id: str
    token: str = field(repr=False)
    timeout: float = 120.0
    client: httpx.AsyncClient | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "sandboxd needs an HTTP(S) base URL without credentials, query or fragment"
            )
        if re.fullmatch(SESSION_ID_PATTERN, self.session_id) is None:
            raise ValueError("Invalid sandboxd session identity")
        if not self.token or "\r" in self.token or "\n" in self.token:
            raise ValueError("A sandboxd session credential is required")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("The sandboxd request timeout must be positive and finite")

    @property
    def _path(self) -> str:
        return f"{self.base_url.rstrip('/')}/sessions/{self.session_id}/environments/browser"

    @property
    def websocket_url(self) -> str:
        """Private authenticated CDP endpoint, for trusted backend adapters only."""
        parsed = urlsplit(self.base_url)
        return urlunsplit(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                parsed.path.rstrip("/") + f"/sessions/{self.session_id}/browser/cdp",
                "",
                "",
            )
        )

    @property
    def connection_headers(self) -> dict[str, str]:
        """Session-scoped credential headers; never include in logs or UI responses."""
        return {TOKEN_HEADER: self.token}

    async def _request(
        self, method: str, body: _Start | None = None, *, suffix: str = ""
    ) -> bytes | None:
        import httpx

        async def send(client: httpx.AsyncClient) -> httpx.Response:
            return await client.request(
                method,
                self._path + suffix,
                headers=self.connection_headers,
                json=None if body is None else self._start_payload(body),
                timeout=self.timeout,
                follow_redirects=False,
            )

        try:
            if self.client is None:
                async with httpx.AsyncClient() as client:
                    response = await send(client)
            else:
                response = await send(self.client)
        except httpx.HTTPError as error:
            raise BrowserUnavailableError(
                "The sandboxd browser request did not complete"
            ) from error
        if response.status_code in (401, 403):
            raise BrowserAuthorizationError("sandboxd refused this browser credential")
        if response.status_code in (404, 410):
            if method == "DELETE":
                return None
            raise BrowserGoneError("The exact managed browser no longer exists")
        if response.status_code in (409, 429):
            raise BrowserConflictError("The managed browser conflicts with the requested operation")
        if response.status_code >= 500:
            raise BrowserUnavailableError("The managed browser service is temporarily unavailable")
        if not response.is_success:
            raise BrowserError(
                f"sandboxd refused the browser request (HTTP {response.status_code})"
            )
        return response.content

    @staticmethod
    def _start_payload(body: _Start) -> dict[str, object]:
        result = body.model_dump(exclude={"profile_key"})
        if body.profile_key is not None:
            result["profile_key"] = body.profile_key.get_secret_value()
        return result

    def _state(self, content: bytes | None) -> ManagedBrowserState:
        try:
            state = ManagedBrowserState.model_validate_json(content or b"")
        except ValidationError as error:
            raise BrowserError("sandboxd returned an invalid managed browser state") from error
        if state.environment_id != f"{self.session_id}:browser":
            raise BrowserError("sandboxd returned another browser environment identity")
        return state

    async def start(
        self,
        *,
        idle_seconds: int = 900,
        lifetime_seconds: int = 3600,
        width: int = 1280,
        height: int = 800,
        profile_key: str | None = None,
    ) -> ManagedBrowserState:
        """Explicitly start the authorized session's browser; never creates a workspace."""
        body = _Start(
            idle_seconds=idle_seconds,
            lifetime_seconds=lifetime_seconds,
            width=width,
            height=height,
            profile_key=None if profile_key is None else SecretStr(profile_key),
        )
        return self._state(await self._request("POST", body))

    async def inspect(self) -> ManagedBrowserState:
        """Inspect without starting processes, renewing idle or making screenshots."""
        return self._state(await self._request("GET"))

    async def end(self) -> None:
        """End the supervised browser and its dedicated runtime; missing is idempotent."""
        await self._request("DELETE")

    async def suspend(self) -> None:
        """Checkpoint a durable profile and sleep its runtime; keep the named session."""
        await self._request("POST", suffix="/suspend")

    async def purge(self) -> None:
        """Erase a profile and its session files; never use this for ordinary idle."""
        await self._request("DELETE", suffix="/profile")

    async def connect(self, *, context_id: str | None = None) -> ManagedBrowserConnection:
        """Attach a transport, preserving existing DOM after consumer disconnect.

        An omitted context_id creates a new private context inside an already-live
        environment. An explicit id attaches exactly that context, with no fallback.
        Neither path starts or wakes the sandbox.
        """
        if (await self.inspect()).state != "ready":
            raise BrowserUnavailableError("The managed browser is not ready")
        transport = await WebSocketCDP.connect(
            self.websocket_url, timeout=self.timeout, additional_headers=self.connection_headers
        )
        try:
            if context_id is None:
                browser = await BrowserSession.create(transport, owns_context=False)
            else:
                browser = await BrowserSession.attach(transport, context_id, owns_context=False)
        except BaseException:
            try:
                await transport.aclose()
            except Exception as error:
                logger.warning("managed_browser_detach_failed: %s", type(error).__name__)
            raise
        return ManagedBrowserConnection(browser, transport)
