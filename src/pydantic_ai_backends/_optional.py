"""Imports of packages that only ship with an optional extra.

Every optional dependency is imported through here so a missing one always
fails with the same actionable message naming the extra that provides it.

The loaders return `Any` rather than `ModuleType`: a type checker cannot see
the attributes of a module resolved at runtime, and annotating them precisely
would mean maintaining a stub per third-party package.
"""

from __future__ import annotations

import importlib
from typing import Any

EXTRAS: dict[str, str] = {
    "chardet": "docker",
    "docker": "docker",
    "pypdf": "docker",
    "httpx": "remote",
    "kubernetes": "kubernetes",
    "PIL": "images",
}
"""Module name -> the extra that installs it."""


def load(module_name: str, *, purpose: str) -> Any:
    """Import an optional module, or raise with the install command.

    Args:
        module_name: Top-level module to import, e.g. `"chardet"`.
        purpose: What the caller needs it for, used in the error message.

    Raises:
        ImportError: If the module is not installed.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        extra = EXTRAS[module_name]
        raise ImportError(
            f"{module_name} is required for {purpose}. "
            f"Install with: pip install pydantic-ai-backend[{extra}]"
        ) from e


def load_optional(module_name: str) -> Any | None:
    """Import an optional module, or return `None` when it is absent.

    For features that degrade instead of failing — image downscaling works
    without Pillow, it just does nothing.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError:  # pragma: no cover - depends on which extras are installed
        return None
