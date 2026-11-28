"""Native desktop operations over existing workspaces or operator-owned daemons.

Install the ``desktop`` extra. Provisioning and container retirement remain
owned by the workspace's sandboxd/provider; no agent or model call is required.
"""

from ._actions import (
    Action,
    Click,
    Drag,
    Key,
    Launch,
    Move,
    NativeAction,
    Screen,
    Scroll,
    TypeText,
    Wait,
    describe,
    points_of,
    to_native,
)
from ._commands import _PRIVATE_TEXT_ENV as TEXT_INPUT_ENV
from ._commands import x11_keys, xdotool
from ._daemon import DaemonDesktop
from ._sandbox import (
    SandboxDesktop,
    desktop_apps_script,
    desktop_session_script,
    desktop_start,
    open_sandbox_desktop,
    read_apps,
)
from ._types import (
    Capture,
    Desktop,
    DesktopCommandResult,
    DesktopError,
    DesktopUnavailableError,
    DesktopWorkspace,
    png_size,
)
from .apps import App

__all__ = [
    "Action",
    "App",
    "Capture",
    "Click",
    "DaemonDesktop",
    "Desktop",
    "DesktopError",
    "DesktopCommandResult",
    "DesktopWorkspace",
    "DesktopUnavailableError",
    "Drag",
    "Key",
    "Launch",
    "Move",
    "NativeAction",
    "SandboxDesktop",
    "Screen",
    "Scroll",
    "TypeText",
    "Wait",
    "describe",
    "desktop_apps_script",
    "desktop_session_script",
    "read_apps",
    "png_size",
    "TEXT_INPUT_ENV",
    "x11_keys",
    "xdotool",
    "desktop_start",
    "open_sandbox_desktop",
    "points_of",
    "to_native",
]
