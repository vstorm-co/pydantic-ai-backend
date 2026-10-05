"""Pydantic AI workspaces from self-hosted sandboxes, and tools that work in them.

A workspace is the environment an agent run works in, which every tool reaches
through `ctx.workspace`. This library supplies workspaces Pydantic AI does not
ship — a Docker container, a `sandboxd` session, a Kubernetes pod, a Daytona
sandbox, a JSON document — and `ConsoleCapability`, file and shell tools that run
in whichever workspace the run has.

Requires `pip install "pydantic-ai-backend[console]"`; each workspace names its
own extra.

```python
from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, DockerWorkspace

agent = Agent(
    "anthropic:claude-opus-5-5",
    capabilities=[DockerWorkspace(image="python:3.12-slim"), ConsoleCapability()],
)
```
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai_backends.backends.state import StateBackend
from pydantic_ai_backends.protocol import CommandRunner, SandboxUnavailableError
from pydantic_ai_backends.types import (
    CommandOutcome,
    FileData,
    FileInfo,
    RuntimeConfig,
    SandboxUsage,
)

if TYPE_CHECKING:
    from pydantic_ai_backends.backends.docker import BUILTIN_RUNTIMES, DockerSandbox, SessionManager
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
    from pydantic_ai_backends.remote import WorkspaceArchive, WorkspaceArchiveError
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
        ConsoleToolset,
        EditFormat,
        create_console_toolset,
        get_console_system_prompt,
    )
    from pydantic_ai_backends.toolsets.descriptions import TOOL_TEXT, Profile, ToolText
    from pydantic_ai_backends.workspaces import (
        DaytonaWorkspace,
        DaytonaWorkspaceBackend,
        DockerWorkspace,
        DockerWorkspaceBackend,
        KubernetesWorkspace,
        KubernetesWorkspaceBackend,
        SandboxdWorkspace,
        SandboxdWorkspaceBackend,
        StateWorkspace,
        StateWorkspaceBackend,
    )

_LAZY_MODULES: dict[str, tuple[str, ...]] = {
    "pydantic_ai_backends.workspaces": (
        "DaytonaWorkspace",
        "DaytonaWorkspaceBackend",
        "DockerWorkspace",
        "DockerWorkspaceBackend",
        "KubernetesWorkspace",
        "KubernetesWorkspaceBackend",
        "SandboxdWorkspace",
        "SandboxdWorkspaceBackend",
        "StateWorkspace",
        "StateWorkspaceBackend",
    ),
    "pydantic_ai_backends.hashline": (
        "apply_hashline_edit",
        "apply_hashline_edit_with_summary",
        "format_hashline_output",
        "line_hash",
    ),
    "pydantic_ai_backends.toolsets.console": (
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
    "pydantic_ai_backends.toolsets.descriptions": ("TOOL_TEXT", "Profile", "ToolText"),
    "pydantic_ai_backends.capability": ("ConsoleCapability",),
    "pydantic_ai_backends.backends.docker.sandbox": ("DockerSandbox",),
    "pydantic_ai_backends.backends.docker.session": ("SandboxFactory", "SessionManager"),
    "pydantic_ai_backends.backends.docker.runtimes": ("BUILTIN_RUNTIMES", "get_runtime"),
    "pydantic_ai_backends.backends.kubernetes": ("KubernetesPodSandbox",),
    "pydantic_ai_backends.remote.archive": ("WorkspaceArchive", "WorkspaceArchiveError"),
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

Importing these eagerly would pull in optional dependencies — docker, kubernetes,
daytona, httpx, pydantic-ai — that most callers do not have installed.
"""

_LAZY_IMPORTS = {name: module for module, names in _LAZY_MODULES.items() for name in names}

# Spelled out rather than derived from the groups above: type checkers only
# understand a literal `__all__`, and this is the library's public API reference.
__all__ = [
    "BUILTIN_RUNTIMES",
    "DEFAULT_MAX_DOCUMENT_BYTES",
    "DEFAULT_MAX_IMAGE_BYTES",
    "DEFAULT_RULESET",
    "DOCUMENT_EXTENSIONS",
    "DOCUMENT_MEDIA_TYPES",
    "EDIT_FILE_DESCRIPTION",
    "EXECUTE_DESCRIPTION",
    "GLOB_DESCRIPTION",
    "GREP_DESCRIPTION",
    "HASHLINE_CONSOLE_PROMPT",
    "HASHLINE_EDIT_DESCRIPTION",
    "HASHLINE_READ_FILE_DESCRIPTION",
    "IMAGE_EXTENSIONS",
    "IMAGE_MEDIA_TYPES",
    "LS_DESCRIPTION",
    "PERMISSIVE_RULESET",
    "READONLY_RULESET",
    "READ_FILE_DESCRIPTION",
    "SECRETS_PATTERNS",
    "STRICT_RULESET",
    "SYSTEM_PATTERNS",
    "TOOL_TEXT",
    "WRITE_FILE_DESCRIPTION",
    "AskCallback",
    "AskFallback",
    "CommandOutcome",
    "CommandRunner",
    "ConsoleCapability",
    "ConsoleToolset",
    "DaytonaWorkspace",
    "DaytonaWorkspaceBackend",
    "DockerSandbox",
    "DockerWorkspace",
    "DockerWorkspaceBackend",
    "EditFormat",
    "FileData",
    "FileInfo",
    "KubernetesPodSandbox",
    "KubernetesWorkspace",
    "KubernetesWorkspaceBackend",
    "OperationPermissions",
    "PermissionAction",
    "PermissionAskError",
    "PermissionChecker",
    "PermissionDeniedError",
    "PermissionError",
    "PermissionOperation",
    "PermissionRule",
    "PermissionRuleset",
    "Profile",
    "RuntimeConfig",
    "SandboxFactory",
    "SandboxUnavailableError",
    "SandboxUsage",
    "SandboxdWorkspace",
    "SandboxdWorkspaceBackend",
    "SessionManager",
    "StateBackend",
    "StateWorkspace",
    "StateWorkspaceBackend",
    "ToolText",
    "WorkspaceArchive",
    "WorkspaceArchiveError",
    "apply_hashline_edit",
    "apply_hashline_edit_with_summary",
    "create_console_toolset",
    "create_ruleset",
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
