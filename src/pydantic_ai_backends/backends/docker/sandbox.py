"""A Docker container the workspaces and `sandboxd` run commands in."""

from __future__ import annotations

import contextlib
import time
import uuid
import warnings
from typing import TYPE_CHECKING, Any

from pydantic_ai_backends._limits import MAX_RUN_OUTPUT_BYTES
from pydantic_ai_backends.backends.docker._client import docker_client
from pydantic_ai_backends.backends.docker._image import resolve_image
from pydantic_ai_backends.backends.docker._stats import parse_usage
from pydantic_ai_backends.types import RuntimeConfig, SandboxUsage

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from docker import DockerClient
    from docker.models.containers import Container

    from pydantic_ai_backends.types import CommandOutcome

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

SANDBOX_ENV: dict[str, str] = {
    # git reads its whole configuration from these, so this works on an image we
    # did not build — a ready-made `bun` or `go` runtime gets it too. Without
    # `safe.directory` every git command in a bind-mounted workspace fails with
    # "detected dubious ownership", because the directory belongs to whoever the
    # service runs as and the container does not. Measured: `status`, `diff`,
    # `log` and `commit` all refuse.
    "GIT_CONFIG_COUNT": "5",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": "*",
    # An identity, because "Author identity unknown" is what an agent asked to
    # commit its work otherwise gets, and it cannot invent one that is true.
    "GIT_CONFIG_KEY_1": "user.name",
    "GIT_CONFIG_VALUE_1": "Agent",
    "GIT_CONFIG_KEY_2": "user.email",
    "GIT_CONFIG_VALUE_2": "agent@sandbox.local",
    "GIT_CONFIG_KEY_3": "init.defaultBranch",
    "GIT_CONFIG_VALUE_3": "main",
    "GIT_CONFIG_KEY_4": "advice.detachedHead",
    "GIT_CONFIG_VALUE_4": "false",
    "GIT_TERMINAL_PROMPT": "0",
    # Without this a script killed by the command timeout returns *nothing*
    # rather than its output up to the point it hung, because the last writes
    # are still sitting in a pipe buffer.
    "PYTHONUNBUFFERED": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    "PIP_ROOT_USER_ACTION": "ignore",
    "UV_SYSTEM_PYTHON": "1",
    # uv parallelises downloads, and that parallelism is memory. Measured
    # installing pandas: uncapped it is killed by a 128 MB ceiling that pip
    # survives, and capped at two it fits while staying 6.6x faster than pip.
    "UV_CONCURRENT_DOWNLOADS": "2",
    "UV_CONCURRENT_INSTALLS": "2",
    "UV_COMPILE_BYTECODE": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    # Colour is escape sequences a model pays for and cannot read, and a pager
    # waiting for a keypress is a command that occupies a worker until it times
    # out. Neither happens without a TTY, but the tools that force it anyway are
    # pure waste.
    "NO_COLOR": "1",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    # `python:3.12-slim` sets this; `node:20-slim` does not, so a Node runtime
    # otherwise starts in the POSIX locale where non-ASCII output is a coin flip.
    "LANG": "C.UTF-8",
}
"""Environment every sandbox starts with, before a runtime's own `env_vars`.

Applied at the container rather than in an image, which is what lets it reach
the ready-made runtimes as well — those build nothing, so a Dockerfile could
never have carried it. A runtime overrides any of it by naming the same key,
though overriding `GIT_CONFIG_COUNT` without supplying the matching pairs would
leave git reading configuration that is not there.
"""


class DockerSandbox:
    """One Docker container: its lifecycle, its limits, and commands run in it.

    The container starts lazily on the first command or an explicit
    :meth:`start`. Commands go through :meth:`run_command`, which keeps the
    workspace contract; files are reached through those commands by whoever uses
    the container — :class:`~pydantic_ai_backends.workspaces.DockerWorkspace`, or
    `sandboxd`, which also mounts the work directory from the host.

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
        memswap_limit: str | None = None,
        cpus: float | None = None,
        cpu_shares: int | None = None,
        pids_limit: int | None = DEFAULT_PIDS_LIMIT,
        tmpfs: dict[str, str] | None = None,
        oci_runtime: str | None = None,
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
                is pinned to the same value unless `memswap_limit` says
                otherwise, so a container over its ceiling is stopped rather
                than left swapping against the host.
            memswap_limit: Ceiling on memory *and* swap combined, in the same
                syntax. `None` pins it to `mem_limit`, which denies the container
                swap entirely — the right default, because a container swapping
                past its limit against a disk starves every other sandbox on the
                host.

                It is the wrong default on a host backed by `zram`, where swap
                is compressed RAM: the pages never leave memory, idle Python
                heaps compress to roughly a third, and the alternative to a
                little swapping is an OOM kill. Set this above `mem_limit` there
                and nowhere else. Ignored without `mem_limit`, since Docker
                rejects a swap ceiling with no memory ceiling under it.
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
            oci_runtime: Low-level runtime the daemon starts this container
                with — Docker's `--runtime`. `None` takes the daemon's default,
                normally `runc`.

                This is the one knob that changes the *isolation boundary*
                rather than a resource ceiling, which is why it is per sandbox:
                `"runsc"` (gVisor) moves syscall handling into userspace and
                `"kata"` gives the container its own kernel in a microVM, while
                a container under plain `runc` shares the host's. Untrusted
                model-written code is exactly the workload that argues for one
                of them.

                The runtime must already be registered with the daemon in
                `/etc/docker/daemon.json`; naming an unregistered one makes the
                daemon refuse to start the container. See the installation docs
                for the host side, including `crun` as a faster drop-in default.
        """
        self._id = session_id or sandbox_id or str(uuid.uuid4())

        self._container_name = container_name
        self._auto_remove = False if container_name else auto_remove
        self._container: Container | None = None
        self._idle_timeout = idle_timeout
        self._last_activity = time.time()
        self._volumes = volumes or {}
        self._network_mode = network_mode
        self._mem_limit = mem_limit
        self._memswap_limit = memswap_limit
        self._cpus = cpus
        self._cpu_shares = cpu_shares
        self._pids_limit = pids_limit
        self._tmpfs = tmpfs or {}
        self._oci_runtime = oci_runtime
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
    def id(self) -> str:
        """Unique identifier for this sandbox."""
        return self._id

    @property
    def last_activity(self) -> float:
        """Wall clock of the last operation, which idle cleanup reaps against."""
        return self._last_activity

    def touch(self) -> None:
        """Record activity, so idle cleanup does not reap a sandbox in use."""
        self._last_activity = time.time()

    @property
    def session_id(self) -> str:
        """Alias for the sandbox id, used for session management."""
        return self._id

    @property
    def idle_timeout(self) -> int:
        """Idle seconds after which `SessionManager` may reap this sandbox."""
        return self._idle_timeout

    @property
    def work_dir(self) -> str:
        """Directory commands start in and relative paths resolve against."""
        return self._work_dir

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

    def _environment(self) -> dict[str, str]:
        """What the container starts with: the sandbox defaults, then the runtime's.

        `UV_SYSTEM_PYTHON` is dropped for a runtime that runs unprivileged. A
        container's environment overrides its image's, so leaving it set would
        clobber the `0` the image asks for and send uv at the interpreter the
        sandbox user cannot write to — which fails with `Permission denied` and
        no way forward, the virtualenv built for exactly this being ignored.
        """
        env = dict(SANDBOX_ENV)
        if self._runtime is None:
            return env
        if self._runtime.run_as_uid is not None:
            del env["UV_SYSTEM_PYTHON"]
        env.update(self._runtime.env_vars)
        return env

    def _run_kwargs(self) -> dict[str, Any]:
        """Arguments for `containers.run`, including limits and hardening."""
        kwargs: dict[str, Any] = {
            "command": "sleep infinity",
            "detach": True,
            # `sleep` as PID 1 never calls `wait()`, so every process an agent
            # orphans — a backgrounded server, anything the command timeout
            # kills — is reparented to it and stays a zombie for the life of the
            # container. Measured: ten orphans, ten permanent zombies. They
            # accumulate against `pids_limit` until the session cannot fork at
            # all. `init` puts a real reaper in front, for 488 kB.
            "init": True,
            "working_dir": self._work_dir,
            "auto_remove": self._auto_remove,
            "environment": self._environment(),
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
        if self._runtime is not None and self._runtime.run_as_uid is not None:
            # Both halves of the pair, because a process writing into a
            # bind-mounted workspace is checked on its gid as well.
            kwargs["user"] = f"{self._runtime.run_as_uid}:{self._runtime.run_as_uid}"
        if self._network_mode is not None:
            kwargs["network_mode"] = self._network_mode
        if self._pids_limit is not None:
            kwargs["pids_limit"] = self._pids_limit
        if self._mem_limit is not None:
            # Without a matching swap ceiling the kernel lets a container over
            # its memory limit swap instead, which starves the whole host. A
            # host whose swap is `zram` can afford a wider one, and says so.
            kwargs["mem_limit"] = self._mem_limit
            kwargs["memswap_limit"] = self._memswap_limit or self._mem_limit
        if self._cpus is not None:
            kwargs["nano_cpus"] = int(self._cpus * 1_000_000_000)
        if self._cpu_shares is not None:
            kwargs["cpu_shares"] = self._cpu_shares
        if self._tmpfs:
            kwargs["tmpfs"] = {path: _with_exec(options) for path, options in self._tmpfs.items()}
        if self._oci_runtime is not None:
            kwargs["runtime"] = self._oci_runtime
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

    def stop(self, purge: bool = False, *, remove: bool | None = None) -> None:
        """Stop the container.

        A container created without `container_name` runs with
        `auto_remove=True` and is discarded by the daemon on exit. A *named*
        container deliberately survives, since reuse across restarts is the
        whole point of naming it.

        Args:
            purge: Also remove the container, discarding its filesystem state.
                Named `purge` so that one call site can end any sandbox this
                library offers - the Kubernetes pod spells the same idea this
                way, and this one used to spell it `remove`. A caller holding "a sandbox" could
                not call `stop` without knowing which it had.
            remove: The old name for `purge`, still honoured so nothing that
                passes it breaks. Deprecated; pass `purge` instead.
        """
        if remove is not None:
            warnings.warn(
                "DockerSandbox.stop(remove=...) is deprecated; pass purge=... instead, "
                "which is what every other sandbox calls the same argument.",
                DeprecationWarning,
                stacklevel=2,
            )
            purge = remove

        container = getattr(self, "_container", None)
        if container is None:
            return

        with contextlib.suppress(Exception):
            container.stop()
        if purge:
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

    async def run_command(
        self,
        argv: Sequence[str],
        *,
        run_id: str,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        output_limit: int | None = None,
    ) -> CommandOutcome:
        """Run `argv` under the workspace failure contract rather than this protocol's.

        Unlike :meth:`execute` this raises when the container is unreachable,
        keeps stdout and stderr apart, and stops the command's process group when
        the caller times out or is cancelled. It is what a Pydantic AI workspace
        and `sandboxd`'s `/run` are built on; an agent's tool path keeps using
        :meth:`execute`.

        Args:
            argv: The program and its arguments, passed through literally.
            run_id: Names the run so :meth:`stop_command` can stop it.
            env: Variables layered over the container's environment.
            timeout: Seconds before the command is stopped; none when `None`.
            output_limit: Combined output bytes before the command is stopped;
                the module default when `None`.

        Raises:
            SandboxUnavailableError: The container is gone or not running.
        """
        # Imported here: `anyio` arrives with the `workspaces` and `server` extras,
        # the two callers of this method, and not with `docker`.
        import anyio.to_thread

        from pydantic_ai_backends.backends.docker import _exec

        await anyio.to_thread.run_sync(self._ensure_container)
        self._last_activity = time.time()
        assert self._container is not None
        return await _exec.run_in_container(
            self._container,
            argv,
            run_id=run_id,
            env=env,
            workdir=self._work_dir,
            timeout=timeout,
            output_limit=output_limit if output_limit is not None else MAX_RUN_OUTPUT_BYTES,
        )

    async def stop_command(self, run_id: str) -> None:
        """Stop a command :meth:`run_command` started, and everything it started.

        Does nothing when the run has already finished or never started here.
        """
        from pydantic_ai_backends.backends.docker import _exec

        if self._container is not None:
            await _exec.stop_in_container(self._container, run_id)


def _with_exec(options: str) -> str:
    """Add `exec` to a tmpfs option string unless it is already spelled out."""
    if "exec" in {option.strip() for option in options.split(",")}:
        return options
    return f"{options},{TMPFS_OPTIONS}" if options else TMPFS_OPTIONS
