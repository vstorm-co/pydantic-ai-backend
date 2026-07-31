"""Sandbox that runs commands and holds files inside a Docker container."""

from __future__ import annotations

import contextlib
import io
import shlex
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from pydantic_ai_backends._editing import Replacement, replace_in_content
from pydantic_ai_backends._limits import (
    DEFAULT_MAX_READ_BYTES,
    MAX_EXECUTE_OUTPUT_BYTES,
    READ_LIMIT_HINT,
)
from pydantic_ai_backends._text import bytes_to_text
from pydantic_ai_backends.backends.base import BaseSandbox
from pydantic_ai_backends.backends.docker._client import docker_client
from pydantic_ai_backends.backends.docker._image import resolve_image
from pydantic_ai_backends.backends.docker._stats import parse_usage
from pydantic_ai_backends.types import (
    EditResult,
    ExecuteResponse,
    RuntimeConfig,
    SandboxUsage,
    WriteResult,
)

if TYPE_CHECKING:
    from docker import DockerClient
    from docker.models.containers import Container

ALIVE_CACHE_SECONDS = 5.0
"""How long a liveness answer is trusted before the daemon is asked again.

`SessionManager.get_or_create` calls `is_alive()` on every request, so an
uncached check bills a daemon round trip to each agent turn.
"""

DEFAULT_PIDS_LIMIT = 512
"""Process ceiling per container. No ordinary workload approaches it, but it
bounds a runaway `fork` loop that would otherwise exhaust host PIDs."""

REATTACHABLE_STATUSES = ("created", "exited", "paused")
"""Statuses a named container can be started from instead of recreated."""

TMPFS_OPTIONS = "exec"
"""Docker mounts a tmpfs `noexec` by default, which breaks any `pip install` of a
source distribution — pip unpacks into `/tmp` and runs the build from there."""


class ReadLimitExceeded(Exception):
    """Raised when a file is too large to pull out of the container.

    Distinct from the empty-bytes result used for a missing file: callers turn
    this into a message explaining what to do instead (read a slice), which an
    empty `bytes` return could not convey.
    """


class DockerSandbox(BaseSandbox):  # pragma: no cover
    """Docker-based sandbox for isolated command execution.

    The container starts lazily on the first operation. File transfers use
    Docker's archive API rather than shell heredocs, so content with quotes,
    newlines or arbitrary bytes survives a round trip intact.

    Example:
        ```python
        from pydantic_ai_backends import DockerSandbox, RuntimeConfig

        sandbox = DockerSandbox(image="python:3.12-slim")

        ml_runtime = RuntimeConfig(
            name="ml-env",
            base_image="python:3.12-slim",
            packages=["torch", "transformers"],
        )
        sandbox = DockerSandbox(runtime=ml_runtime)
        ```
    """

    def __init__(
        self,
        image: str = "python:3.12-slim",
        sandbox_id: str | None = None,
        work_dir: str = "/workspace",
        auto_remove: bool = True,
        runtime: RuntimeConfig | str | None = None,
        session_id: str | None = None,
        idle_timeout: int = 3600,
        volumes: dict[str, str] | None = None,
        network_mode: str | None = None,
        container_name: str | None = None,
        mem_limit: str | None = None,
        cpus: float | None = None,
        cpu_shares: int | None = None,
        pids_limit: int | None = DEFAULT_PIDS_LIMIT,
        tmpfs: dict[str, str] | None = None,
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
    ):
        """Initialize the sandbox without starting its container.

        Args:
            image: Docker image to use. Ignored when `runtime` is given.
            sandbox_id: Unique identifier for this sandbox.
            work_dir: Working directory inside the container. Ignored when
                `runtime` is given.
            auto_remove: Remove the container when it stops. Forced to `False`
                when `container_name` is set, since a named container exists to
                be reused.
            runtime: `RuntimeConfig`, or the name of a built-in runtime.
            session_id: Alias for `sandbox_id`, for session management.
            idle_timeout: Idle seconds after which `SessionManager` may reap it.
            volumes: Host-to-container mounts, as `{"/host": "/container"}`.
            network_mode: Docker network mode (`"bridge"`, `"none"`, `"host"`,
                `"container:<name|id>"`). Pass `"none"` for sandboxes that must
                not reach the network; it also skips per-container veth and
                firewall setup, so containers start measurably faster.
            container_name: Stable name to reattach to across restarts, which
                preserves installed packages and other filesystem state.
                Implies `auto_remove=False`.
            mem_limit: Memory ceiling in Docker syntax (`"512m"`, `"2g"`). Swap
                is pinned to the same value, so a container over its ceiling is
                stopped rather than left swapping against the host.
            cpus: Hard CPU ceiling in cores, e.g. `1.5`. A container never
                exceeds it, which also means it cannot use cores that are sitting
                idle — on a small host that is often the wrong trade.
            cpu_shares: Relative CPU weight (Docker's default is 1024). Unlike
                `cpus` this only applies under contention, so one active sandbox
                may use the whole machine and several are still divided fairly.
                Composes with `cpus` when both are set.
            pids_limit: Maximum number of processes. `None` disables the limit.
            tmpfs: In-memory mounts, as `{"/tmp": "size=64m"}`. Writes to a
                tmpfs never reach the container's write layer, so scratch files
                are both faster and free of disk growth. `exec` is added to the
                options because Docker mounts a tmpfs `noexec`, which breaks
                installing any package that builds from source.

                Its pages count against `mem_limit`, not on top of it: a sandbox
                that fills a 64m `/tmp` has that much less left for its own
                processes, and one that tries to exceed the limit through `/tmp`
                is killed by its own cgroup rather than troubling the host.
            max_read_bytes: Largest file `read`/`read_bytes`/`edit` will pull
                out of the container. Oversized files are refused instead of
                being buffered into the host's memory.
        """
        super().__init__(session_id or sandbox_id)

        self._container_name = container_name
        self._auto_remove = False if container_name else auto_remove
        self._container: Container | None = None
        self._idle_timeout = idle_timeout
        self._last_activity = time.time()
        self._volumes = volumes or {}
        self._network_mode = network_mode
        self._mem_limit = mem_limit
        self._cpus = cpus
        self._cpu_shares = cpu_shares
        self._pids_limit = pids_limit
        self._tmpfs = tmpfs or {}
        self._max_read_bytes = max_read_bytes
        self._alive = False
        self._alive_checked_at: float | None = None

        if isinstance(runtime, str):
            from pydantic_ai_backends.backends.docker.runtimes import get_runtime

            runtime = get_runtime(runtime)
        self._runtime = runtime
        self._image = image
        self._work_dir = runtime.work_dir if runtime is not None else work_dir

    @property
    def runtime(self) -> RuntimeConfig | None:
        """The runtime configuration for this sandbox."""
        return self._runtime

    @property
    def session_id(self) -> str:
        """Alias for the sandbox id, used for session management."""
        return self._id

    @property
    def idle_timeout(self) -> int:
        """Idle seconds after which `SessionManager` may reap this sandbox."""
        return self._idle_timeout

    def _resolve_path(self, path: str) -> str:
        """Resolve a relative path against the container's working directory."""
        if not PurePosixPath(path).is_absolute():
            return str(PurePosixPath(self._work_dir) / path)
        return path

    # ── Container lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        """Start the container now instead of on the first operation."""
        self._ensure_container()

    def _ensure_container(self) -> None:
        """Attach to or create the container backing this sandbox."""
        if self._container is not None:
            return

        # Everything below attaches or creates a container, so any cached
        # liveness answer belongs to a container that is no longer ours.
        self._alive_checked_at = None

        # Resolved before the submodule import so a missing optional dependency
        # surfaces the install hint instead of a bare ImportError.
        client = docker_client()

        existing = self._reattach(client)
        if existing is not None:
            self._container = existing
            return

        image = resolve_image(client, self._runtime, self._image)
        self._container = client.containers.run(image, **self._run_kwargs())

    def _reattach(self, client: DockerClient) -> Container | None:
        """Return the running named container for this sandbox, if there is one.

        A stopped container is started rather than replaced, so installed
        packages, caches and other filesystem state survive a restart.
        """
        import docker.errors

        if not self._container_name:
            return None

        try:
            existing = client.containers.get(self._container_name)
        except docker.errors.NotFound:
            return None

        if existing.status == "running":
            return existing
        if existing.status in REATTACHABLE_STATUSES:
            existing.start()
            return existing
        # Dead or being removed: a fresh container is the only way forward.
        return None

    def _run_kwargs(self) -> dict[str, Any]:
        """Arguments for `containers.run`, including limits and hardening."""
        kwargs: dict[str, Any] = {
            "command": "sleep infinity",
            "detach": True,
            "working_dir": self._work_dir,
            "auto_remove": self._auto_remove,
            "environment": self._runtime.env_vars if self._runtime else {},
            "volumes": {
                host: {"bind": container, "mode": "rw"} for host, container in self._volumes.items()
            }
            or None,
            # Sandboxed code is untrusted by definition, so deny it the one
            # cheap escalation route a container still leaves open: gaining
            # privileges by exec'ing a setuid binary.
            "security_opt": ["no-new-privileges:true"],
        }
        if self._container_name is not None:
            kwargs["name"] = self._container_name
        if self._network_mode is not None:
            kwargs["network_mode"] = self._network_mode
        if self._pids_limit is not None:
            kwargs["pids_limit"] = self._pids_limit
        if self._mem_limit is not None:
            # Without a matching swap ceiling the kernel lets a container over
            # its memory limit swap instead, which starves the whole host.
            kwargs["mem_limit"] = self._mem_limit
            kwargs["memswap_limit"] = self._mem_limit
        if self._cpus is not None:
            kwargs["nano_cpus"] = int(self._cpus * 1_000_000_000)
        if self._cpu_shares is not None:
            kwargs["cpu_shares"] = self._cpu_shares
        if self._tmpfs:
            kwargs["tmpfs"] = {path: _with_exec(options) for path, options in self._tmpfs.items()}
        return kwargs

    def is_alive(self) -> bool:
        """Whether the container is running.

        The answer is cached for `ALIVE_CACHE_SECONDS`, since `reload()` is a
        daemon round trip and session managers call this on every request.
        """
        if self._container is None:
            return False

        now = time.monotonic()
        checked_at = self._alive_checked_at
        if checked_at is not None and now - checked_at < ALIVE_CACHE_SECONDS:
            return self._alive

        try:
            self._container.reload()
            status: str = self._container.status
        except Exception:
            self._alive = False
        else:
            self._alive = status == "running"

        self._alive_checked_at = now
        return self._alive

    def resource_usage(self) -> SandboxUsage | None:
        """Sample the container's current resource usage.

        One non-streaming `stats()` call, which costs a daemon round trip and
        should be polled sparingly rather than per request.
        """
        if self._container is None:
            return None
        try:
            return parse_usage(self._container.stats(stream=False))
        except Exception:
            return None

    def stop(self, remove: bool = False) -> None:
        """Stop the container.

        A container created without `container_name` runs with
        `auto_remove=True` and is discarded by the daemon on exit. A *named*
        container deliberately survives, since reuse across restarts is the
        whole point of naming it.

        Args:
            remove: Also remove the container, discarding its filesystem state.
        """
        container = getattr(self, "_container", None)
        if container is None:
            return

        with contextlib.suppress(Exception):
            container.stop()
        if remove:
            with contextlib.suppress(Exception):
                container.remove(force=True)
        self._container = None
        self._alive_checked_at = None

    def __del__(self) -> None:
        """Best-effort cleanup on garbage collection.

        `__del__` is unreliable for this — it may run during interpreter
        shutdown when modules are already torn down, or never run at all. Prefer
        the explicit :meth:`stop` lifecycle.
        """
        with contextlib.suppress(Exception):
            if getattr(self, "_container", None) is not None:
                self.stop()

    # ── Commands ───────────────────────────────────────────────────────

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Run a command in the container.

        Output beyond `MAX_EXECUTE_OUTPUT_BYTES` is discarded before decoding,
        so the cap is measured in bytes rather than characters.
        """
        self._ensure_container()
        self._last_activity = time.time()
        assert self._container is not None

        # The Docker SDK's exec_run takes no timeout, so the command is wrapped
        # in the `timeout` utility instead.
        argv = ["sh", "-c", command]
        if timeout is not None:
            argv = ["timeout", str(timeout), *argv]

        try:
            exit_code, output = self._container.exec_run(argv, workdir=self._work_dir)
            if not isinstance(output, bytes):
                output = b"".join(output)
        except Exception as e:
            return ExecuteResponse(output=f"Error: {e}", exit_code=1, truncated=False)

        # Sliced before decoding: decoding the whole payload only to throw most
        # of it away doubled peak memory on commands like `cat big.log`.
        return ExecuteResponse(
            output=output[:MAX_EXECUTE_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            exit_code=exit_code,
            truncated=len(output) > MAX_EXECUTE_OUTPUT_BYTES,
        )

    # ── Files ──────────────────────────────────────────────────────────

    def read_bytes(self, path: str) -> bytes:
        """Read a whole file as bytes.

        Returns:
            The content, or `b""` when the file is missing, unreadable, or over
            `max_read_bytes`. Use `read` when the reason matters — it reports
            the limit explicitly.
        """
        try:
            return self._fetch_file_bytes(self._resolve_path(path))
        except ReadLimitExceeded:
            return b""

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a slice of a text file, decoding or extracting it as needed."""
        resolved = self._resolve_path(path)
        try:
            data = self._fetch_file_bytes(resolved)
            if not data:
                return f"Error: File '{path}' not found"

            extension = Path(resolved).suffix.lower().lstrip(".")
            try:
                lines = bytes_to_text(extension, data).splitlines()
            except ValueError as e:
                return f"[Error: {e}]"

            if offset >= len(lines):
                return "[End of file]"

            end = offset + limit
            chunk = "\n".join(lines[offset:end])
            if end >= len(lines):
                return chunk
            remaining = len(lines) - end
            return f"{chunk}\n\n[... {remaining} more lines. Use offset={end} to read more.]"

        except ReadLimitExceeded as e:
            return f"[Error: {e}]"
        except Exception as e:
            return f"[Error reading file: {e}]"

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit a file by replacing a string.

        The file is fetched, edited in Python and written back, so multiline
        strings need no shell escaping.
        """
        resolved = self._resolve_path(path)
        try:
            data = self._fetch_file_bytes(resolved)
            if not data:
                return EditResult(error=f"File '{path}' not found")

            extension = Path(resolved).suffix.lower().lstrip(".")
            try:
                content = bytes_to_text(extension, data)
            except ValueError as e:
                return EditResult(error=str(e))

            outcome = replace_in_content(content, old_string, new_string, replace_all)
            if not isinstance(outcome, Replacement):
                return EditResult(error=outcome)

            written = self.write(resolved, outcome.content)
            if written.error:
                return EditResult(error=written.error)
            return EditResult(path=resolved, occurrences=outcome.occurrences)

        except ReadLimitExceeded as e:
            return EditResult(error=str(e))
        except Exception as e:
            return EditResult(error=f"Failed to edit file: {e}")

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write a file, creating parent directories as needed."""
        path = self._resolve_path(path)
        self._ensure_container()
        assert self._container is not None

        try:
            parent = str(PurePosixPath(path).parent)
            mkdir = self.execute(f"mkdir -p {shlex.quote(parent)}")
            if mkdir.exit_code != 0:
                return WriteResult(error=f"Failed to create directory: {mkdir.output}")

            raw = content if isinstance(content, bytes) else content.encode()
            archive = _single_file_archive(PurePosixPath(path).name, raw)

            # put_archive returns False when the target is not a directory or
            # the upload otherwise fails.
            if not self._container.put_archive(parent, archive):
                return WriteResult(error=f"Failed to write file: put_archive to {parent}")
            return WriteResult(path=path)
        except Exception as e:
            return WriteResult(error=f"Failed to write file: {e}")

    def _fetch_file_bytes(self, path: str) -> bytes:
        """Fetch a file's raw bytes out of the container.

        Args:
            path: Absolute path inside the container.

        Returns:
            The content, or `b""` when the path is missing or holds no regular
            file.

        Raises:
            ReadLimitExceeded: If the file is over `max_read_bytes`.
        """
        self._ensure_container()
        assert self._container is not None

        try:
            stream, stat = self._container.get_archive(path)
        except Exception:
            return b""

        # get_archive reports the size in a response header, so an oversized
        # file is refused before any of its content crosses the socket.
        reported_size = stat.get("size") if stat else None
        if reported_size is not None and reported_size > self._max_read_bytes:
            stream.close()
            raise ReadLimitExceeded(
                f"File is {reported_size} bytes, over the "
                f"{self._max_read_bytes}-byte read limit. {READ_LIMIT_HINT}"
            )

        # Accumulated straight into the buffer tarfile reads from; a
        # `b"".join(stream)` -> `BytesIO(...)` chain held several copies at once.
        buffer = io.BytesIO()
        try:
            for chunk in stream:
                buffer.write(chunk)
                # Re-checked while streaming to stay bounded even when the
                # daemon omits the size header.
                if buffer.tell() > self._max_read_bytes:
                    stream.close()
                    raise ReadLimitExceeded(
                        f"File exceeds the {self._max_read_bytes}-byte read limit. "
                        f"{READ_LIMIT_HINT}"
                    )
        except ReadLimitExceeded:
            raise
        except Exception:
            return b""

        return _extract_single_file(buffer)


def _with_exec(options: str) -> str:
    """Add `exec` to a tmpfs option string unless it is already spelled out."""
    if "exec" in {option.strip() for option in options.split(",")}:
        return options
    return f"{options},{TMPFS_OPTIONS}" if options else TMPFS_OPTIONS


def _single_file_archive(name: str, content: bytes) -> io.BytesIO:
    """Wrap one file's content in the tar stream `put_archive` expects."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        entry = tarfile.TarInfo(name=name)
        entry.size = len(content)
        entry.mtime = int(time.time())
        entry.mode = 0o644
        tar.addfile(entry, io.BytesIO(content))
    buffer.seek(0)
    return buffer


def _extract_single_file(buffer: io.BytesIO) -> bytes:
    """Read the first regular file out of a tar stream, or `b""`."""
    try:
        buffer.seek(0)
        with buffer, tarfile.open(fileobj=buffer, mode="r") as tar:
            member = next((m for m in tar.getmembers() if m.isfile()), None)
            if member is None:
                return b""
            extracted = tar.extractfile(member)
            return extracted.read() if extracted is not None else b""
    except Exception:
        return b""
