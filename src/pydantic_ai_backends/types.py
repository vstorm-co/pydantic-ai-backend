"""Type definitions for pydantic-ai-backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import BaseModel


class FileData(TypedDict):
    """Data structure for storing file content in StateBackend."""

    content: list[str]  # Lines of the file
    created_at: str  # ISO 8601 timestamp
    modified_at: str  # ISO 8601 timestamp


class FileInfo(TypedDict):
    """Information about a file or directory."""

    name: str
    path: str
    is_dir: bool
    size: int | None


@dataclass
class WriteResult:
    """Result of a write operation."""

    path: str | None = None
    error: str | None = None


@dataclass
class EditResult:
    """Result of an edit operation."""

    path: str | None = None
    error: str | None = None
    occurrences: int | None = None


@dataclass
class ExecuteResponse:
    """Response from command execution in a sandbox."""

    output: str
    exit_code: int | None = None
    truncated: bool = False


@dataclass
class BackgroundHandle:
    """Handle to a started background (long-lived) process."""

    shell_id: str
    pid: int
    command: str


@dataclass
class BackgroundOutput:
    """Incremental output + status of a background process.

    `stdout`/`stderr` hold only what is new since the previous `read_background`
    call for this shell (a growing log is drained in chunks, not re-sent whole).
    """

    shell_id: str
    stdout: str
    stderr: str
    running: bool
    exit_code: int | None = None


@dataclass
class BackgroundProcessInfo:
    """Status of one background process, for listing active shells."""

    shell_id: str
    command: str
    pid: int
    running: bool
    exit_code: int | None = None


class GrepMatch(TypedDict):
    """A single grep match result."""

    path: str
    line_number: int
    line: str


class RuntimeConfig(BaseModel):
    """Configuration for a Docker runtime environment.

    A runtime defines a pre-configured execution environment with specific
    packages and settings. Can be used with DockerSandbox to provide
    ready-to-use environments without manual package installation.

    Example:
        ```python
        from pydantic_ai_backends import RuntimeConfig, DockerSandbox

        # Custom runtime with ML packages
        ml_runtime = RuntimeConfig(
            name="ml-env",
            description="Machine learning environment",
            base_image="python:3.12-slim",
            packages=["torch", "transformers", "datasets"],
        )

        sandbox = DockerSandbox(runtime=ml_runtime)
        ```
    """

    name: str
    """Unique name for the runtime (e.g., "python-datascience")."""

    description: str = ""
    """Human-readable description of the runtime."""

    # Image source (one of these)
    image: str | None = None
    """Ready-to-use Docker image (e.g., "myregistry/python-ds:v1")."""

    base_image: str | None = None
    """Base image to build upon (e.g., "python:3.12-slim")."""

    # Packages to install (only if base_image)
    packages: list[str] = []
    """Packages to install (e.g., ["pandas", "numpy", "matplotlib"])."""

    package_manager: Literal["pip", "npm", "apt", "cargo"] = "pip"
    """Package manager to use for installation."""

    # Additional configuration
    setup_commands: list[str] = []
    """Additional setup commands to run (e.g., ["apt-get update"])."""

    env_vars: dict[str, str] = {}
    """Environment variables to set in the container."""

    work_dir: str = "/workspace"
    """Working directory inside the container."""

    # Cache settings
    cache_image: bool = True
    """Whether to cache the built image locally."""
