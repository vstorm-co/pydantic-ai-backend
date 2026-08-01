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


@dataclass(frozen=True)
class SandboxUsage:
    """Point-in-time resource usage of a sandbox.

    Every field is optional because the numbers come from whatever the
    underlying runtime reports, and a container that has just started (or has
    already exited) exposes only some of them.

    Attributes:
        memory_bytes: Resident memory currently used.
        memory_limit_bytes: Ceiling the runtime is enforcing, if any.
        cpu_percent: CPU use across the sample, as a percentage of one core
            multiplied by the number of cores available.
        pids: Number of processes running inside the sandbox.
    """

    memory_bytes: int | None = None
    memory_limit_bytes: int | None = None
    cpu_percent: float | None = None
    pids: int | None = None


class GrepMatch(TypedDict):
    """A single grep match result."""

    path: str
    line_number: int
    line: str


class RuntimeConfig(BaseModel):
    """Configuration for a Docker runtime environment.

    Describes a pre-configured execution environment so a `DockerSandbox` needs
    no manual package installation. Give it either a ready-made `image`, or a
    `base_image` plus `packages` to build from.

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

    image: str | None = None
    """Ready-to-use Docker image (e.g., "myregistry/python-ds:v1")."""

    base_image: str | None = None
    """Base image to build upon (e.g., "python:3.12-slim")."""

    packages: list[str] = []
    """Packages to install (e.g., ["pandas", "numpy", "matplotlib"])."""

    package_manager: Literal["pip", "npm", "apt", "cargo"] = "pip"
    """Package manager to use for installation."""

    setup_commands: list[str] = []
    """Additional setup commands to run (e.g., ["apt-get update"])."""

    env_vars: dict[str, str] = {}
    """Environment variables to set in the container."""

    work_dir: str = "/workspace"
    """Working directory inside the container."""

    cache_image: bool = True
    """Whether to cache the built image locally."""

    run_as_uid: int | None = None
    """Build the image around an unprivileged user, and run containers as them.

    `None` — the default — runs as root, which is what a container does unless
    told otherwise. Naming a uid instead adds three things to the generated
    image: a real user with that id (so `whoami` and anything else calling
    `getpwuid` works), a home directory it owns, and a virtualenv at
    :data:`VENV_PATH` it owns, put first on `PATH`.

    The virtualenv is the part that makes this workable rather than merely
    safer. A non-root user cannot write to the interpreter's own
    `site-packages`, so without one an agent's first `pip install` fails — and
    `uv`, unlike pip, has no `--user` mode to fall back on, so it fails with no
    way forward at all. Owning a virtualenv means both simply work, and console
    scripts land somewhere already on `PATH`.

    Only meaningful with `base_image`: a ready-made `image` was not built with
    this user, so running it unprivileged would leave an agent unable to install
    anything system-wide.

    The uid has to match whoever owns the workspace mounted into the container,
    which is why it is a number rather than a name.
    """
