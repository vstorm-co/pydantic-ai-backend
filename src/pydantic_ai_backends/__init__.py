"""File storage and sandbox backends for AI agents.

A unified interface for file storage and command execution across in-memory,
local and containerised backends.

Basic usage:
    ```python
    from pydantic_ai_backends import LocalBackend, StateBackend

    backend = StateBackend()
    backend.write("/app.py", "print('hello')")
    content = backend.read("/app.py")

    backend = LocalBackend("/workspace")
    result = backend.execute("python app.py")
    ```

Console toolset for AI agents:
    ```python
    from dataclasses import dataclass

    from pydantic_ai_backends import LocalBackend, create_console_toolset

    @dataclass
    class MyDeps:
        backend: LocalBackend

    # Provides: ls, read_file, write_file, edit_file, glob, grep, execute
    toolset = create_console_toolset()
    ```

Docker sandbox (needs `pip install pydantic-ai-backend[docker]`):
    ```python
    from pydantic_ai_backends import DockerSandbox

    sandbox = DockerSandbox(image="python:3.12-slim")
    print(sandbox.execute("python -c 'print(1+1)'").output)  # "2"
    ```
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai_backends.adapter import (
    AsyncBackendAdapter,
    AsyncBackgroundSandboxAdapter,
    AsyncSandboxAdapter,
    ensure_async,
)
from pydantic_ai_backends.backends.composite import AsyncCompositeBackend, CompositeBackend
from pydantic_ai_backends.backends.local import LocalBackend
from pydantic_ai_backends.backends.state import StateBackend
from pydantic_ai_backends.protocol import (
    AsyncBackendProtocol,
    AsyncBackgroundSandboxProtocol,
    AsyncSandboxProtocol,
    BackendProtocol,
    BackgroundSandboxProtocol,
    SandboxProtocol,
)
from pydantic_ai_backends.types import (
    BackgroundHandle,
    BackgroundOutput,
    BackgroundProcessInfo,
    EditResult,
    ExecuteResponse,
    FileData,
    FileInfo,
    GrepMatch,
    RuntimeConfig,
    SandboxUsage,
    WriteResult,
)

if TYPE_CHECKING:
    from pydantic_ai_backends.backends.base import AsyncBaseSandbox
    from pydantic_ai_backends.backends.daytona import DaytonaSandbox
    from pydantic_ai_backends.backends.docker import (
        BUILTIN_RUNTIMES,
        BaseSandbox,
        DockerSandbox,
        SessionManager,
    )
    from pydantic_ai_backends.backends.docker.runtimes import get_runtime
    from pydantic_ai_backends.backends.docker.session import SandboxFactory
    from pydantic_ai_backends.backends.kubernetes import KubernetesPodSandbox
    from pydantic_ai_backends.capability import ConsoleCapability
    from pydantic_ai_backends.hashline import (
        apply_hashline_edit,
        apply_hashline_edit_with_summary,
        format_hashline_output,
        line_hash,
    )
    from pydantic_ai_backends.permissions import (
        DEFAULT_RULESET,
        PERMISSIVE_RULESET,
        READONLY_RULESET,
        SECRETS_PATTERNS,
        STRICT_RULESET,
        SYSTEM_PATTERNS,
        AskCallback,
        AskFallback,
        OperationPermissions,
        PermissionAction,
        PermissionAskError,
        PermissionChecker,
        PermissionDeniedError,
        PermissionError,
        PermissionOperation,
        PermissionRule,
        PermissionRuleset,
        create_ruleset,
    )
    from pydantic_ai_backends.remote import RemoteSandbox
    from pydantic_ai_backends.toolsets.console import (
        DEFAULT_MAX_DOCUMENT_BYTES,
        DEFAULT_MAX_IMAGE_BYTES,
        DOCUMENT_EXTENSIONS,
        DOCUMENT_MEDIA_TYPES,
        EDIT_FILE_DESCRIPTION,
        EXECUTE_DESCRIPTION,
        GLOB_DESCRIPTION,
        GREP_DESCRIPTION,
        HASHLINE_CONSOLE_PROMPT,
        HASHLINE_EDIT_DESCRIPTION,
        HASHLINE_READ_FILE_DESCRIPTION,
        IMAGE_EXTENSIONS,
        IMAGE_MEDIA_TYPES,
        LS_DESCRIPTION,
        READ_FILE_DESCRIPTION,
        WRITE_FILE_DESCRIPTION,
        ConsoleDeps,
        ConsoleToolset,
        EditFormat,
        create_console_toolset,
        get_console_system_prompt,
    )

_LAZY_MODULES: dict[str, tuple[str, ...]] = {
    "pydantic_ai_backends.hashline": (
        "apply_hashline_edit",
        "apply_hashline_edit_with_summary",
        "format_hashline_output",
        "line_hash",
    ),
    "pydantic_ai_backends.toolsets.console": (
        "ConsoleDeps",
        "ConsoleToolset",
        "DEFAULT_MAX_DOCUMENT_BYTES",
        "DEFAULT_MAX_IMAGE_BYTES",
        "DOCUMENT_EXTENSIONS",
        "DOCUMENT_MEDIA_TYPES",
        "EDIT_FILE_DESCRIPTION",
        "EXECUTE_DESCRIPTION",
        "EditFormat",
        "GLOB_DESCRIPTION",
        "GREP_DESCRIPTION",
        "HASHLINE_CONSOLE_PROMPT",
        "HASHLINE_EDIT_DESCRIPTION",
        "HASHLINE_READ_FILE_DESCRIPTION",
        "IMAGE_EXTENSIONS",
        "IMAGE_MEDIA_TYPES",
        "LS_DESCRIPTION",
        "READ_FILE_DESCRIPTION",
        "WRITE_FILE_DESCRIPTION",
        "create_console_toolset",
        "get_console_system_prompt",
    ),
    "pydantic_ai_backends.capability": ("ConsoleCapability",),
    "pydantic_ai_backends.backends.base": ("AsyncBaseSandbox", "BaseSandbox"),
    "pydantic_ai_backends.backends.daytona": ("DaytonaSandbox",),
    "pydantic_ai_backends.backends.docker.sandbox": ("DockerSandbox",),
    "pydantic_ai_backends.backends.docker.session": ("SandboxFactory", "SessionManager"),
    "pydantic_ai_backends.backends.docker.runtimes": ("BUILTIN_RUNTIMES", "get_runtime"),
    "pydantic_ai_backends.backends.kubernetes": ("KubernetesPodSandbox",),
    "pydantic_ai_backends.remote.client": ("RemoteSandbox",),
    "pydantic_ai_backends.permissions": (
        "AskCallback",
        "AskFallback",
        "DEFAULT_RULESET",
        "OperationPermissions",
        "PERMISSIVE_RULESET",
        "PermissionAction",
        "PermissionAskError",
        "PermissionChecker",
        "PermissionDeniedError",
        "PermissionError",
        "PermissionOperation",
        "PermissionRule",
        "PermissionRuleset",
        "READONLY_RULESET",
        "SECRETS_PATTERNS",
        "STRICT_RULESET",
        "SYSTEM_PATTERNS",
        "create_ruleset",
    ),
}
"""Exports loaded on first use, grouped by the module that defines them.

Importing these eagerly would pull in optional dependencies — docker, pypdf,
httpx, pydantic-ai — that most callers do not have installed.
"""

_LAZY_IMPORTS = {name: module for module, names in _LAZY_MODULES.items() for name in names}

# Spelled out rather than derived from the two groups above: type checkers only
# understand a literal `__all__`, and this is the library's public API reference.
__all__ = [
    "AskCallback",
    "AskFallback",
    "AsyncBackendAdapter",
    "AsyncBackendProtocol",
    "AsyncBackgroundSandboxAdapter",
    "AsyncBackgroundSandboxProtocol",
    "AsyncCompositeBackend",
    "AsyncSandboxAdapter",
    "AsyncSandboxProtocol",
    "BUILTIN_RUNTIMES",
    "BackendProtocol",
    "BackgroundHandle",
    "BackgroundOutput",
    "BackgroundProcessInfo",
    "BackgroundSandboxProtocol",
    "AsyncBaseSandbox",
    "BaseSandbox",
    "CompositeBackend",
    "ConsoleCapability",
    "ConsoleDeps",
    "ConsoleToolset",
    "DEFAULT_MAX_DOCUMENT_BYTES",
    "DEFAULT_MAX_IMAGE_BYTES",
    "DEFAULT_RULESET",
    "DOCUMENT_EXTENSIONS",
    "DOCUMENT_MEDIA_TYPES",
    "DaytonaSandbox",
    "DockerSandbox",
    "EDIT_FILE_DESCRIPTION",
    "EXECUTE_DESCRIPTION",
    "EditFormat",
    "EditResult",
    "ExecuteResponse",
    "FileData",
    "FileInfo",
    "GLOB_DESCRIPTION",
    "GREP_DESCRIPTION",
    "GrepMatch",
    "HASHLINE_CONSOLE_PROMPT",
    "HASHLINE_EDIT_DESCRIPTION",
    "HASHLINE_READ_FILE_DESCRIPTION",
    "IMAGE_EXTENSIONS",
    "IMAGE_MEDIA_TYPES",
    "KubernetesPodSandbox",
    "LS_DESCRIPTION",
    "LocalBackend",
    "OperationPermissions",
    "PERMISSIVE_RULESET",
    "PermissionAction",
    "PermissionAskError",
    "PermissionChecker",
    "PermissionDeniedError",
    "PermissionError",
    "PermissionOperation",
    "PermissionRule",
    "PermissionRuleset",
    "READONLY_RULESET",
    "READ_FILE_DESCRIPTION",
    "RemoteSandbox",
    "RuntimeConfig",
    "SECRETS_PATTERNS",
    "STRICT_RULESET",
    "SYSTEM_PATTERNS",
    "SandboxFactory",
    "SandboxProtocol",
    "SandboxUsage",
    "SessionManager",
    "StateBackend",
    "WRITE_FILE_DESCRIPTION",
    "WriteResult",
    "apply_hashline_edit",
    "apply_hashline_edit_with_summary",
    "create_console_toolset",
    "create_ruleset",
    "ensure_async",
    "format_hashline_output",
    "get_console_system_prompt",
    "get_runtime",
    "line_hash",
]


def __getattr__(name: str) -> object:
    """Import an optional export on first access."""
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    return getattr(importlib.import_module(module_name), name)


try:
    from importlib.metadata import version as _get_version

    __version__ = _get_version("pydantic-ai-backend")
except Exception:  # pragma: no cover
    __version__ = "0.0.0"
