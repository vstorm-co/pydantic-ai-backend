"""An X11 desktop running inside the caller's existing Pydantic AI workspace."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import anyio
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from pydantic_ai_backends.protocol import SandboxUnavailableError

from . import _capture, _desktop_session
from . import apps as _desktop_apps
from ._actions import Launch, NativeAction, TypeText, Wait
from ._commands import _PRIVATE_TEXT_ENV, xdotool
from ._types import Capture, DesktopError, DesktopUnavailableError, DesktopWorkspace, png_size
from .apps import App

logger = logging.getLogger(__name__)
DISPLAY = ":0"
NO_DESKTOP = 3
SCREENSHOT_CLEANUP_SECONDS = 5
_DESKTOP_APPS = Path(_desktop_apps.__file__).read_text()
_DESKTOP_SESSION = Path(_desktop_session.__file__).read_text() + f"\nretire({DISPLAY!r})\n"
_APPS = TypeAdapter(list[App])
_CAPTURE_SCRIPT = Path(_capture.__file__).read_text() + "\nmain()\n"


class _InlineScreenshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["inline"]
    size: int = Field(ge=1, le=_capture.INLINE_BYTES)
    image_b64: str = Field(max_length=4 * ((_capture.INLINE_BYTES + 2) // 3))


class _SpilledScreenshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["spill"]
    size: int = Field(gt=_capture.INLINE_BYTES, le=_capture.MAX_IMAGE_BYTES)


_SCREENSHOT = TypeAdapter(
    Annotated[_InlineScreenshot | _SpilledScreenshot, Field(discriminator="mode")]
)


def read_apps(listing: str | bytes) -> list[App]:
    """Validate the native desktop menu response."""
    try:
        return _APPS.validate_json(listing)
    except ValidationError as exc:
        raise DesktopError("The desktop's list of applications could not be read.") from exc


def desktop_start(width: int, height: int, *, reset_session: bool = False) -> str:
    """Start the display idempotently; reset is only for exclusively held desktops."""
    if not 1 <= width <= 3840 or not 1 <= height <= 2160:
        raise ValueError("Desktop dimensions must fit within 3840 by 2160 pixels.")
    display = f"xdpyinfo -display {DISPLAY} >/dev/null 2>&1"
    reset = (
        f"python3 -c {shlex.quote(_DESKTOP_SESSION)} || exit 1\nif {display}; then exit 1; fi\n"
        if reset_session
        else ""
    )
    return reset + (
        f"{display} && pgrep -x xfce4-session >/dev/null && exit 0\n"
        "for tool in Xvfb xdpyinfo startxfce4 xdotool scrot; do "
        f"command -v $tool >/dev/null || exit {NO_DESKTOP}; done\n"
        f"if ! {display}; then\n"
        f"rm -f /tmp/.X{DISPLAY[1:]}-lock /tmp/.X11-unix/X{DISPLAY[1:]}\n"
        f"setsid nohup Xvfb {DISPLAY} -ac -screen 0 {width}x{height}x24 "
        "-nolisten tcp >/dev/null 2>&1 &\n"
        f"for _ in $(seq 100); do {display} && break; sleep 0.1; done\n"
        "fi\n"
        "if ! pgrep -x xfce4-session >/dev/null; then\n"
        f"DISPLAY={DISPLAY} setsid nohup dbus-launch --exit-with-session "
        "startxfce4 >/tmp/xfce.log 2>&1 &\n"
        "fi\n"
        "for _ in $(seq 600); do pgrep -x xfwm4 >/dev/null && "
        "pgrep -x xfdesktop >/dev/null && exit 0; sleep 0.1; done\n"
        "exit 1\n"
    )


class SandboxDesktop:
    """A desktop in an existing workspace, whose container the caller still owns.

    Commands and screenshots never provision a second machine. A close only
    detaches this adapter, leaving workspace retirement to sandboxd/provider.
    """

    label = "Linux"

    def __init__(self, workspace: DesktopWorkspace) -> None:
        self._workspace = workspace
        self._shot = f"/tmp/pydantic-desktop-screen-{uuid4().hex}.png"
        self._capture_lock = asyncio.Lock()

    async def _run(
        self,
        argv: list[str],
        what: str,
        *,
        env: Mapping[str, str] | None = None,
        private_output: bool = False,
    ) -> str:
        try:
            result = await self._workspace.run(
                argv, env={"DISPLAY": DISPLAY, **(env or {})}, timeout=60
            )
        except (SandboxUnavailableError, OSError) as exc:
            raise DesktopUnavailableError(f"The sandbox could not {what}.") from exc
        if result.exit_code != 0:
            # A provider may echo typed text in stderr; input failures expose only status.
            detail = (
                f"exit {result.exit_code}"
                if private_output or (env is not None and _PRIVATE_TEXT_ENV in env)
                else (result.stderr or result.stdout).strip()[:200] or f"exit {result.exit_code}"
            )
            raise DesktopError(f"The desktop refused to {what}: {detail}")
        return result.stdout

    async def capture(self) -> Capture:
        """Capture one PNG; ordinary frames need one command and no temporary file.

        The inline reply uses at most 700 KiB of command output. A large frame is
        transferred through the workspace's existing chunked file reader without
        taking a second screenshot. Providers must refuse output-limit overflow
        explicitly; a lower command budget is never treated as an empty image.
        """
        async with self._capture_lock:
            confirmed_inline = False
            try:
                raw = await self._run(
                    ["python3", "-c", _CAPTURE_SCRIPT, self._shot],
                    "capture its screen",
                    private_output=True,
                )
                try:
                    reply = _SCREENSHOT.validate_json(raw)
                except ValidationError:
                    # A validation error embeds input values, including image bytes.
                    raise DesktopError(
                        "The desktop returned an invalid screenshot response."
                    ) from None
                if isinstance(reply, _InlineScreenshot):
                    confirmed_inline = True
                    try:
                        image = base64.b64decode(reply.image_b64, validate=True)
                    except (ValueError, binascii.Error):
                        raise DesktopError(
                            "The desktop returned invalid screenshot bytes."
                        ) from None
                else:
                    try:
                        image = await self._workspace.read_bytes(self._shot)
                    except (SandboxUnavailableError, OSError) as exc:
                        raise DesktopUnavailableError("The screenshot could not be read.") from exc
                if len(image) != reply.size:
                    raise DesktopError("The screenshot size did not match its response.")
                width, height = png_size(image)
                return Capture(image, width, height)
            finally:
                # An interrupted RPC may have published a large frame before its
                # acknowledgement was lost. Only a confirmed inline reply rules it out.
                if not confirmed_inline:
                    await self._discard_screenshot()

    async def _discard_screenshot(self) -> None:
        with anyio.move_on_after(SCREENSHOT_CLEANUP_SECONDS, shield=True) as cleanup:
            try:
                await self._workspace.remove(self._shot)
            except (SandboxUnavailableError, OSError) as exc:
                logger.warning("desktop_screenshot_cleanup_failed: %s", type(exc).__name__)
        if cleanup.cancel_called:
            logger.warning("desktop_screenshot_cleanup_timed_out")

    async def perform(self, action: NativeAction) -> None:
        """Apply one validated action in the native pointer coordinate space."""
        if isinstance(action, TypeText) and "\x00" in action.text:
            raise DesktopError("Text input cannot contain a null character.")
        if isinstance(action, Launch):
            await self._run(
                ["python3", "-c", _DESKTOP_APPS, "launch", action.app], "open application"
            )
            return
        if isinstance(action, Wait):
            await asyncio.sleep(action.seconds)
            return
        argv = xdotool(action)
        await self._run(
            argv,
            f"run the {action.action} action",
            env={_PRIVATE_TEXT_ENV: action.text} if isinstance(action, TypeText) else None,
        )

    async def apps(self) -> list[App]:
        """Read the applications available to the workspace's desktop account."""
        return read_apps(
            await self._run(["python3", "-c", _DESKTOP_APPS, "list"], "list applications")
        )

    async def aclose(self) -> None:
        """Detach without stopping the caller's workspace or its GUI session."""

    async def close(self) -> None:
        """Compatibility alias for :meth:`aclose`."""
        await self.aclose()


async def open_sandbox_desktop(
    workspace: DesktopWorkspace, *, width: int, height: int, reset_session: bool = False
) -> SandboxDesktop:
    """Start/reuse a desktop in a command-capable workspace.

    The caller owns physical-driver exclusion when requesting a reset. Reuse
    preserves the running GUI; restart after provider hibernation restores files,
    never promises restoration of unsaved windows or process memory.
    """
    if not isinstance(workspace, DesktopWorkspace):
        raise DesktopError("Desktop access requires a command-capable workspace.")
    script = desktop_start(width, height, reset_session=reset_session)
    try:
        result = await workspace.run(script, shell=True, timeout=90)
    except (SandboxUnavailableError, OSError) as exc:
        raise DesktopUnavailableError("The sandbox could not start its desktop.") from exc
    if result.exit_code == NO_DESKTOP:
        raise DesktopError("The workspace runtime has no desktop programs installed.")
    if result.exit_code != 0:
        raise DesktopError("The desktop in the workspace did not start.")
    return SandboxDesktop(workspace)


def desktop_apps_script() -> str:
    """Standalone stdlib menu/launcher program suitable for python -c."""
    return _DESKTOP_APPS


def desktop_session_script() -> str:
    """Standalone GUI retirement program for the sandbox display."""
    return _DESKTOP_SESSION
