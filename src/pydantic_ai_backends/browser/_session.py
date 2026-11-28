"""Private browser contexts and exact tabs over a CDP transport."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from ._transport import BrowserError, CDPTransport

logger = logging.getLogger(__name__)


_KEY_CODES = {
    "Enter": 13,
    "Tab": 9,
    "Backspace": 8,
    "Delete": 46,
    "Escape": 27,
    "ArrowUp": 38,
    "ArrowDown": 40,
    "ArrowLeft": 37,
    "ArrowRight": 39,
    "Home": 36,
    "End": 35,
    "PageUp": 33,
    "PageDown": 34,
    "Shift": 16,
    "Control": 17,
    "Alt": 18,
    "Meta": 91,
}


class _Context(BaseModel):
    context_id: str = Field(alias="browserContextId", min_length=1)


class _Contexts(BaseModel):
    ids: list[str] = Field(alias="browserContextIds")


class _CreatedTab(BaseModel):
    target_id: str = Field(alias="targetId", min_length=1)


class _AttachedTab(BaseModel):
    session_id: str = Field(alias="sessionId", min_length=1)


class _Target(BaseModel):
    target_id: str = Field(alias="targetId")
    context_id: str | None = Field(default=None, alias="browserContextId")
    type: str
    title: str = ""
    url: str = ""


class _Targets(BaseModel):
    targets: list[_Target] = Field(alias="targetInfos")


class _TargetReply(BaseModel):
    target: _Target = Field(alias="targetInfo")


@dataclass(frozen=True)
class BrowserTabInfo:
    """Metadata for a tab in this session's private context."""

    id: str
    title: str
    url: str


class BrowserSession:
    """An isolated context. Its owner keeps this object/transport alive until End.

    A browser supervisor should own this lifetime independently of HTTP requests.
    ``disposeOnDetach`` prevents orphaned contexts when that supervisor dies.
    Attaching never creates a replacement for a lost context or tab.
    """

    def __init__(
        self, transport: CDPTransport, context_id: str, *, owns_context: bool = True
    ) -> None:
        if not context_id:
            raise ValueError("A browser session requires a private context identity")
        self.transport = transport
        self.context_id = context_id
        self._owns_context = owns_context
        self._closed = False
        self._close_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls, transport: CDPTransport, *, proxy_server: str | None = None, owns_context: bool = True
    ) -> BrowserSession:
        params: dict[str, object] = {"disposeOnDetach": True}
        if proxy_server is not None:
            params["proxyServer"] = proxy_server
        answer = _Context.model_validate(
            await transport.call("Target.createBrowserContext", params)
        )
        return cls(transport, answer.context_id, owns_context=owns_context)

    @classmethod
    async def attach(
        cls, transport: CDPTransport, context_id: str, *, owns_context: bool = False
    ) -> BrowserSession:
        """Attach only an existing exact context, without creating a replacement."""
        contexts = _Contexts.model_validate(await transport.call("Target.getBrowserContexts", {}))
        if context_id not in contexts.ids:
            raise BrowserError("The exact browser context no longer exists")
        return cls(transport, context_id, owns_context=owns_context)

    def require_open(self) -> None:
        if self._closed:
            raise BrowserError("This browser session has ended")

    async def tabs(self) -> list[BrowserTabInfo]:
        self.require_open()
        answer = _Targets.model_validate(await self.transport.call("Target.getTargets", {}))
        return [
            BrowserTabInfo(target.target_id, target.title[:512], target.url[:8192])
            for target in answer.targets
            if target.context_id == self.context_id and target.type == "page"
        ]

    async def new_tab(self) -> BrowserTab:
        self.require_open()
        answer = _CreatedTab.model_validate(
            await self.transport.call(
                "Target.createTarget", {"url": "about:blank", "browserContextId": self.context_id}
            )
        )
        try:
            return await self.tab(answer.target_id)
        except BaseException:
            try:
                await asyncio.wait_for(
                    self.transport.call("Target.closeTarget", {"targetId": answer.target_id}), 5
                )
            except Exception as error:
                # The private context remains owned by this session and its
                # durable supervisor; cleanup can still dispose it as a whole.
                logger.warning("browser_tab_creation_cleanup_failed: %s", type(error).__name__)
            raise

    async def tab(self, target_id: str) -> BrowserTab:
        self.require_open()
        answer = _TargetReply.model_validate(
            await self.transport.call("Target.getTargetInfo", {"targetId": target_id})
        )
        if (
            answer.target.context_id != self.context_id
            or answer.target.type != "page"
            or answer.target.target_id != target_id
        ):
            raise BrowserError("The tab does not belong to this browser session")
        attached = _AttachedTab.model_validate(
            await self.transport.call(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )
        )
        return BrowserTab(self, target_id, attached.session_id)

    async def close_tab(self, target_id: str) -> None:
        """Close a page only after verifying its exact context ownership."""
        self.require_open()
        current = await self.tabs()
        if target_id not in {tab.id for tab in current}:
            raise BrowserError("The tab does not belong to this browser session")
        await self.transport.call("Target.closeTarget", {"targetId": target_id})

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            if self._owns_context:
                # Keep the disposal obligation after provider failure, and accept
                # an already absent context after a lost acknowledgement.
                contexts = _Contexts.model_validate(
                    await self.transport.call("Target.getBrowserContexts", {})
                )
                if self.context_id in contexts.ids:
                    await self.transport.call(
                        "Target.disposeBrowserContext", {"browserContextId": self.context_id}
                    )
            self._closed = True


class BrowserTab:
    """One exact tab. These methods dispatch physical input, not model decisions."""

    def __init__(self, owner: BrowserSession, target_id: str, session_id: str) -> None:
        self._owner = owner
        self.id = target_id
        self._session_id = session_id
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def _call(self, method: str, params: Mapping[str, object]) -> dict[str, object]:
        self._owner.require_open()
        if self._closed:
            raise BrowserError("This browser tab has ended")
        return await self._owner.transport.call(method, params, session_id=self._session_id)

    async def navigate(self, url: str) -> None:
        """Navigate to HTTP(S); applications must enforce their destination/egress policy."""
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Browser navigation requires an HTTP or HTTPS URL")
        result = await self._call("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise BrowserError("The browser could not navigate to the requested page")

    async def screenshot(self, *, quality: int = 80) -> bytes:
        if not 1 <= quality <= 100:
            raise ValueError("Screenshot quality must be between 1 and 100")
        result = await self._call("Page.captureScreenshot", {"format": "jpeg", "quality": quality})
        data = result.get("data")
        if not isinstance(data, str):
            raise BrowserError("The browser did not return an image")
        try:
            return base64.b64decode(data, validate=True)
        except binascii.Error as exc:
            raise BrowserError("The browser returned an invalid image") from exc

    async def resize(self, width: int, height: int, *, device_scale_factor: float = 1.0) -> None:
        if not 1 <= width <= 8192 or not 1 <= height <= 8192:
            raise ValueError("Viewport dimensions must be between 1 and 8192")
        if not math.isfinite(device_scale_factor) or not 0.1 <= device_scale_factor <= 4:
            raise ValueError("Device scale factor must be between 0.1 and 4")
        await self._call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": device_scale_factor,
                "mobile": False,
            },
        )

    async def click(
        self, x: float, y: float, *, button: Literal["left", "middle", "right"] = "left"
    ) -> None:
        self._coordinates(x, y)
        if button not in ("left", "middle", "right"):
            raise ValueError("Unsupported pointer button")
        for event in ("mousePressed", "mouseReleased"):
            await self._call(
                "Input.dispatchMouseEvent",
                {"type": event, "x": x, "y": y, "button": button, "clickCount": 1},
            )

    async def type_text(self, text: str) -> None:
        if len(text) > 65536 or "\x00" in text:
            raise ValueError("Browser text must contain no NUL and at most 65536 characters")
        await self._call("Input.insertText", {"text": text})

    async def key(
        self,
        key: str,
        *,
        code: str | None = None,
        modifiers: int = 0,
        commands: Sequence[Literal["selectAll", "copy", "cut", "paste", "undo", "redo"]] = (),
    ) -> None:
        """Dispatch a named key or ASCII letter/digit with CDP modifier bits.

        Alt=1, Control=2, Meta=4, Shift=8. Use type_text for arbitrary Unicode.
        Virtual key codes and a character event are required for native form and
        selection behavior; sending only the DOM name does not trigger it.
        Chromium on macOS also needs explicit editing commands for shortcuts.
        """
        if not 0 <= modifiers <= 15:
            raise ValueError("Invalid browser key or modifier mask")
        virtual = _KEY_CODES.get(key)
        if virtual is None:
            if len(key) != 1 or not key.isascii() or not key.isalnum():
                raise ValueError("Unsupported browser key; use type_text for text")
            virtual = ord(key.upper())
            inferred_code = ("Key" if key.isalpha() else "Digit") + key.upper()
        else:
            inferred_code = key
        params: dict[str, object] = {
            "key": key,
            "code": code or inferred_code,
            "modifiers": modifiers,
            "windowsVirtualKeyCode": virtual,
            "nativeVirtualKeyCode": virtual,
        }
        if any(
            command not in ("selectAll", "copy", "cut", "paste", "undo", "redo")
            for command in commands
        ):
            raise ValueError("Unsupported browser editing command")
        await self._call(
            "Input.dispatchKeyEvent", {**params, "type": "rawKeyDown", "commands": list(commands)}
        )
        if key == "Enter" or (len(key) == 1 and modifiers in (0, 8)):
            await self._call(
                "Input.dispatchKeyEvent",
                {
                    "type": "char",
                    "key": key,
                    "text": "\r" if key == "Enter" else key,
                    "modifiers": modifiers,
                },
            )
        await self._call("Input.dispatchKeyEvent", {**params, "type": "keyUp"})

    async def scroll(self, x: float, y: float, *, delta_x: float = 0, delta_y: float = 0) -> None:
        self._coordinates(x, y)
        if not math.isfinite(delta_x) or not math.isfinite(delta_y):
            raise ValueError("Scroll deltas must be finite")
        await self._call(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": delta_x, "deltaY": delta_y},
        )

    @staticmethod
    def _coordinates(x: float, y: float) -> None:
        if not math.isfinite(x) or not math.isfinite(y) or x < 0 or y < 0:
            raise ValueError("Pointer coordinates must be finite and nonnegative")

    async def reload(self) -> None:
        await self._call("Page.reload", {})

    async def evaluate(self, expression: str) -> dict[str, object]:
        """Return raw CDP Runtime.evaluate data to an application-owned parser."""
        if len(expression) > 65536:
            raise ValueError("Browser expressions must contain at most 65536 characters")
        return await self._call(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}
        )

    async def aclose(self) -> None:
        """Detach this observer; the page remains owned by BrowserSession."""
        async with self._close_lock:
            if not self._closed:
                await self._owner.transport.call(
                    "Target.detachFromTarget", {"sessionId": self._session_id}
                )
                self._closed = True
