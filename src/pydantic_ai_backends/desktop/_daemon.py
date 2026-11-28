"""Detach-only adapter for an operator-owned computerd HTTP desktop."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from ._actions import NativeAction
from ._sandbox import read_apps
from ._types import Capture, DesktopError, DesktopUnavailableError, png_size
from .apps import App

MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class DaemonDesktop:
    """Drive a caller-vetted endpoint; closing never shuts down its machine.

    Applications must authorize the host before constructing this adapter. No
    redirect is followed, including when the supplied client enables redirects.
    """

    def __init__(
        self, base_url: str, token: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        parsed = urlsplit(base_url)
        try:
            valid_port = parsed.port
        except ValueError as exc:
            raise ValueError("The desktop endpoint has an invalid port.") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in base_url
            or "#" in base_url
            or valid_port == 0
        ):
            raise ValueError("The desktop endpoint must be HTTP(S), without credentials or query.")
        if not token:
            raise ValueError("The desktop token cannot be empty.")
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5))
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self.label = "Desktop"

    async def _request(
        self, method: str, path: str, json: dict[str, object] | None = None
    ) -> httpx.Response:
        try:
            async with self._client.stream(
                method,
                f"{self._base}{path}",
                headers=self._headers,
                json=json,
                follow_redirects=False,
                timeout=httpx.Timeout(30, connect=5),
            ) as streamed:
                payload = bytearray()
                async for chunk in streamed.aiter_bytes():
                    if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise DesktopError("The desktop response exceeds the size limit.")
                    payload.extend(chunk)
                response = httpx.Response(
                    streamed.status_code,
                    headers=streamed.headers,
                    content=bytes(payload),
                    request=streamed.request,
                )
        except httpx.HTTPError as exc:
            raise DesktopUnavailableError("The desktop could not be reached.") from exc
        if response.status_code >= 500:
            raise DesktopUnavailableError("The desktop service is temporarily unavailable.")
        if response.status_code >= 300:
            # Remote errors can contain URLs, credentials or echoed input.
            raise DesktopError(f"The desktop refused the request (HTTP {response.status_code}).")
        return response

    async def capture(self) -> Capture:
        """Read native input dimensions separately from PNG pixel dimensions."""
        response = await self._request("GET", "/screenshot")
        width, height = png_size(response.content)
        try:
            width = int(response.headers.get("x-screen-width", width))
            height = int(response.headers.get("x-screen-height", height))
        except ValueError as exc:
            raise DesktopError("The desktop returned invalid input dimensions.") from exc
        if not 0 < width <= 32768 or not 0 < height <= 32768:
            raise DesktopError("The desktop returned invalid input dimensions.")
        self.label = response.headers.get("x-platform", "Desktop")[:100]
        return Capture(response.content, width, height)

    async def perform(self, action: NativeAction) -> None:
        """Apply one action; unknown acceptance must not be replayed implicitly."""
        await self._request("POST", "/actions", {"actions": [action.model_dump()]})

    async def apps(self) -> list[App]:
        """Return validated application menu entries."""
        return read_apps((await self._request("GET", "/apps")).content)

    async def aclose(self) -> None:
        """Close only the HTTP client this adapter created."""
        if self._owned:
            await self._client.aclose()

    async def close(self) -> None:
        """Compatibility alias for :meth:`aclose`."""
        await self.aclose()
