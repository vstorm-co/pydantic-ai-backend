"""Typed native desktop boundaries, independent of agent tools and providers."""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ._actions import NativeAction
from .apps import App


class DesktopError(RuntimeError):
    """A desktop operation failed or was refused, without exposing credentials."""


class DesktopUnavailableError(DesktopError):
    """The desktop transport or resource is currently unavailable."""


@dataclass(frozen=True)
class Capture:
    """PNG bytes and the desktop's native pointer coordinate dimensions.

    Native dimensions may differ from PNG pixels on a scaled display.
    """

    image: bytes
    width: int
    height: int


class Desktop(Protocol):
    """A controllable screen. Closing detaches; it does not destroy its host."""

    label: str

    async def capture(self) -> Capture: ...

    async def perform(self, action: NativeAction) -> None: ...

    async def apps(self) -> list[App]: ...

    async def aclose(self) -> None: ...

    async def close(self) -> None: ...


def png_size(data: bytes) -> tuple[int, int]:
    """Read bounded, positive PNG header dimensions without optional decoders."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise DesktopError("The desktop did not return a PNG screenshot.")
    width, height = struct.unpack(">II", data[16:24])
    if not 0 < width <= 32768 or not 0 < height <= 32768:
        raise DesktopError("The desktop returned invalid screenshot dimensions.")
    return width, height


class DesktopCommandResult(Protocol):
    """The command result fields used by native desktop operations."""

    @property
    def exit_code(self) -> int | None: ...

    @property
    def stdout(self) -> str: ...

    @property
    def stderr(self) -> str: ...


@runtime_checkable
class DesktopWorkspace(Protocol):
    """Consumer seam for screen commands and their temporary screenshot file.

    Pydantic AI's Workspace already supplies these operations. sandboxd can
    adapt its CommandRunner without depending on the Pydantic AI agent runtime.
    Provider adapters translate transport failures to SandboxUnavailableError.
    """

    async def run(
        self,
        command: Sequence[str] | str,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> DesktopCommandResult: ...

    async def read_bytes(self, path: str) -> bytes: ...

    async def remove(self, path: str) -> None: ...
