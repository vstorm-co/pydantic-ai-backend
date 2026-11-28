"""Managed browser contexts and physical page operations, independent of agent tools.

Install ``pydantic-ai-backend[browser]`` for the optional WebSocket transport.
A session supervisor should own BrowserSession and WebSocketCDP lifetimes;
closing a viewer or model run must only detach that viewer, not its session.
"""

from ._sandboxd import (
    BrowserAuthorizationError,
    BrowserConflictError,
    BrowserGoneError,
    BrowserUnavailableError,
    ManagedBrowserConnection,
    ManagedBrowserState,
    SandboxdBrowser,
)
from ._session import BrowserSession, BrowserTab, BrowserTabInfo
from ._transport import BrowserError, BrowserSocket, CDPTransport, WebSocketCDP

__all__ = [
    "BrowserError",
    "BrowserAuthorizationError",
    "BrowserConflictError",
    "BrowserGoneError",
    "BrowserUnavailableError",
    "ManagedBrowserConnection",
    "ManagedBrowserState",
    "SandboxdBrowser",
    "BrowserSession",
    "BrowserSocket",
    "BrowserTab",
    "BrowserTabInfo",
    "CDPTransport",
    "WebSocketCDP",
]
