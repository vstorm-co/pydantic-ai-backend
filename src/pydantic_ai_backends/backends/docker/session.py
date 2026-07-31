"""Per-session sandboxes for multi-user applications.

Each session id gets its own isolated sandbox — Docker by default, or whatever a
`sandbox_factory` returns — created on first use and reaped once idle.

Example with the default Docker backend:
    ```python
    from pydantic_ai_backends import SessionManager

    manager = SessionManager(default_runtime="python-datascience")
    sandbox = await manager.get_or_create("user-123")
    result = sandbox.execute("python script.py")
    await manager.release("user-123")
    ```

Example with a custom factory:
    ```python
    from pydantic_ai_backends import DaytonaSandbox, SessionManager

    manager = SessionManager(sandbox_factory=lambda sid: DaytonaSandbox(sandbox_id=sid))
    sandbox = await manager.get_or_create("user-123")
    ```
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from concurrent.futures import Executor

    from pydantic_ai_backends.types import RuntimeConfig

_logger = logging.getLogger(__name__)

SandboxFactory = Callable[[str], Any]
"""Receives a `session_id` and returns a sandbox instance."""

DEFAULT_CLEANUP_INTERVAL = 300

LEGACY_ACTIVITY_ATTR = "_last_activity"
LEGACY_IDLE_TIMEOUT_ATTR = "_idle_timeout"
"""Attributes read as a fallback: custom factory sandboxes were documented
against these before `BaseSandbox` exposed `last_activity` and `touch`."""


class SessionLimitExceeded(RuntimeError):
    """Raised when a new session would exceed `max_sessions`.

    Signals backpressure rather than a bug: the caller should retry later or
    reject the request, having been told how many sessions are already open.

    Attributes:
        limit: The configured ceiling that was reached.
    """

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(
            f"Session limit of {limit} reached. Release an idle session before opening another."
        )


class SessionManager:
    """Creates, reuses and reaps one sandbox per session id.

    Example:
        ```python
        from pydantic_ai_backends import SessionManager

        manager = SessionManager(default_runtime="python-datascience")
        sandbox = await manager.get_or_create("user-123")
        cleaned = await manager.cleanup_idle(max_idle=1800)
        ```
    """

    def __init__(
        self,
        sandbox_factory: SandboxFactory | None = None,
        default_runtime: RuntimeConfig | str | None = None,
        default_idle_timeout: int = 3600,
        workspace_root: str | Path | None = None,
        max_sessions: int | None = None,
        on_release: Callable[[str], None] | None = None,
        executor: Executor | None = None,
    ):
        """Initialize the manager.

        Args:
            sandbox_factory: Builds a sandbox for a session id. It must offer
                `start()`, `stop()` and `is_alive()`; `last_activity` (or a
                legacy `_last_activity`) enables idle cleanup, and sandboxes
                without one are never reaped. Defaults to `DockerSandbox`.
            default_runtime: Runtime for new Docker sandboxes. Only used on the
                default path.
            default_idle_timeout: Idle seconds before a session may be reaped,
                for sandboxes that do not carry their own timeout.
            workspace_root: Root for persistent session storage. Only used on
                the default path, where `{workspace_root}/{session_id}/workspace`
                is created and mounted into the container.
            max_sessions: Ceiling on simultaneously open sessions. Once reached,
                :meth:`get_or_create` raises :class:`SessionLimitExceeded` for
                new session ids rather than starting unbounded containers.
            on_release: Called with a session id just after its sandbox is
                stopped, by :meth:`release` and therefore by idle cleanup too.
                For a caller keeping its own per-session state, this is the only
                notice that a reaping happened — polling `sessions` would mean
                discovering it late, or never. It runs inside a cleanup pass, so
                anything it raises aborts the rest of that pass.
            executor: Thread pool the blocking `start()` and `stop()` calls run
                on. `None` uses asyncio's default pool, which is shared with
                every other `to_thread` caller in the process — a service
                handing out sandboxes wants its own. Assignable afterwards, for
                a caller whose pool outlives fewer things than its manager does.
        """
        self._sessions: dict[str, Any] = {}
        self._sandbox_factory = sandbox_factory
        self._default_runtime = default_runtime
        self._default_idle_timeout = default_idle_timeout
        self._cleanup_task: asyncio.Task[None] | None = None
        self._workspace_root = Path(workspace_root) if workspace_root else None
        self._max_sessions = max_sessions
        self._on_release = on_release
        self.executor = executor
        # Per-session locks serialize concurrent get_or_create calls for the
        # same id, so two awaits cannot each create and start a sandbox — one of
        # which would be overwritten in the dict and leaked.
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def sessions(self) -> dict[str, Any]:
        """Copy of the active sessions."""
        return dict(self._sessions)

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)

    async def get_or_create(
        self,
        session_id: str,
        runtime: RuntimeConfig | str | None = None,
    ) -> Any:
        """Return the session's live sandbox, creating one when needed.

        Args:
            session_id: Unique identifier for the session.
            runtime: Runtime to use. Only applies on the default Docker path.

        Raises:
            SessionLimitExceeded: If `max_sessions` is reached and `session_id`
                is not an existing live session.
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                if existing.is_alive():
                    _record_activity(existing)
                    return existing
                del self._sessions[session_id]

            if self._max_sessions is not None and len(self._sessions) >= self._max_sessions:
                raise SessionLimitExceeded(self._max_sessions)

            if self._sandbox_factory is not None:
                sandbox = self._sandbox_factory(session_id)
            else:
                sandbox = self._create_docker_sandbox(session_id, runtime)

            try:
                await self._offload(sandbox.start)
            except Exception:
                # An unregistered sandbox is one nothing else will ever stop, so
                # a partial start would leak whatever it did manage to create.
                with contextlib.suppress(Exception):
                    await self._offload(sandbox.stop)
                raise

            self._sessions[session_id] = sandbox
            return sandbox

    async def _offload(self, call: Callable[[], Any]) -> None:
        """Run a blocking sandbox lifecycle call off the event loop.

        Starting a sandbox pulls or builds an image and stopping one waits for
        the process inside to die — both are seconds, not milliseconds. Run on
        the loop they stall every other session's work for the duration, which
        for a service handing sandboxes to several tenants is the difference
        between concurrent and merely asynchronous.
        """
        if self.executor is None:
            await asyncio.to_thread(call)
            return
        await asyncio.get_running_loop().run_in_executor(self.executor, call)

    def _create_docker_sandbox(
        self,
        session_id: str,
        runtime: RuntimeConfig | str | None = None,
    ) -> Any:
        """Build a `DockerSandbox`, the default when no factory is given."""
        from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox

        volumes: dict[str, str] | None = None
        if self._workspace_root:
            workspace = self._workspace_root / session_id / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            volumes = {str(workspace.resolve()): "/workspace"}

        return DockerSandbox(
            runtime=runtime or self._default_runtime,
            session_id=session_id,
            idle_timeout=self._default_idle_timeout,
            volumes=volumes,
        )

    async def release(self, session_id: str) -> bool:
        """Stop a session's sandbox. Returns whether the session existed."""
        if session_id not in self._sessions:
            return False

        sandbox = self._sessions.pop(session_id)
        self._locks.pop(session_id, None)
        await self._offload(sandbox.stop)
        if self._on_release is not None:
            self._on_release(session_id)
        return True

    async def cleanup_idle(self, max_idle: int | None = None) -> int:
        """Stop the sandboxes that have been idle too long.

        Args:
            max_idle: Idle ceiling in seconds applied to every session,
                overriding each sandbox's own. When omitted, a sandbox's
                `idle_timeout` wins, falling back to `default_idle_timeout`.

        Returns:
            Number of sessions cleaned up.
        """
        now = time.time()
        # A custom factory may return a sandbox that never records activity.
        # Treating that as "just used" keeps it alive instead of raising and
        # taking the whole cleanup loop down with it.
        idle = [
            session_id
            for session_id, sandbox in self._sessions.items()
            if now - last_activity_of(sandbox, now)
            > (max_idle if max_idle is not None else self._idle_limit_for(sandbox))
        ]

        for session_id in idle:
            await self.release(session_id)

        self._prune_locks()
        return len(idle)

    def _idle_limit_for(self, sandbox: Any) -> int:
        """Idle ceiling for one sandbox, preferring its own configured timeout.

        `DockerSandbox` accepts an `idle_timeout` and documents it as the idle
        cleanup window, so a per-sandbox value has to win over the manager-wide
        default for the parameter to mean anything.
        """
        for attr in ("idle_timeout", LEGACY_IDLE_TIMEOUT_ATTR):
            configured = getattr(sandbox, attr, None)
            if isinstance(configured, int):
                return configured
        return self._default_idle_timeout

    def _prune_locks(self) -> None:
        """Drop locks interned for session ids that no longer have a sandbox.

        `get_or_create` interns a lock before it knows whether the sandbox can
        be created, so every rejected or failed creation would otherwise leave
        an entry behind for good. Held locks are left alone — a waiter is
        relying on that exact object for mutual exclusion.
        """
        stale = [
            session_id
            for session_id, lock in self._locks.items()
            if session_id not in self._sessions and not lock.locked()
        ]
        for session_id in stale:
            del self._locks[session_id]

    def start_cleanup_loop(self, interval: int = DEFAULT_CLEANUP_INTERVAL) -> None:
        """Reap idle sessions periodically in the background.

        The loop survives a failing pass: an unreachable daemon or one
        uncooperative sandbox is logged and retried on the next tick, because a
        loop that exits leaves every future container to accumulate unnoticed.

        Args:
            interval: Seconds between passes.
        """
        if self._cleanup_task is not None:
            return

        async def loop() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.cleanup_idle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("Idle sandbox cleanup failed; retrying next interval")

        self._cleanup_task = asyncio.create_task(loop())

    def stop_cleanup_loop(self) -> None:
        """Stop the background cleanup loop."""
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            self._cleanup_task = None

    async def shutdown(self) -> int:
        """Stop every session and the cleanup loop.

        Sessions are stopped concurrently, because stopping one is seconds of
        waiting for the process inside to die and they do not wait on each
        other: sequentially, a full pool turned a shutdown into minutes, and
        an orchestrator that loses patience kills the process mid-teardown.

        Returns:
            Number of sessions that were stopped.
        """
        self.stop_cleanup_loop()

        session_ids = list(self._sessions)
        # `return_exceptions`, so one uncooperative sandbox cannot leave the
        # rest of the pool running — a shutdown has no later attempt.
        outcomes = await asyncio.gather(
            *(self.release(session_id) for session_id in session_ids),
            return_exceptions=True,
        )
        for session_id, outcome in zip(session_ids, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                _logger.warning(
                    "Session %s did not stop cleanly during shutdown: %s", session_id, outcome
                )
        return len(session_ids)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)


def last_activity_of(sandbox: Any, default: float) -> float:
    """When the sandbox was last used, or `default` when it does not track it."""
    for attr in ("last_activity", LEGACY_ACTIVITY_ATTR):
        value = getattr(sandbox, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
    return default


def _record_activity(sandbox: Any) -> None:
    """Mark the sandbox as just used, so idle cleanup skips it."""
    touch = getattr(sandbox, "touch", None)
    if callable(touch):
        touch()
    elif hasattr(sandbox, LEGACY_ACTIVITY_ATTR):
        setattr(sandbox, LEGACY_ACTIVITY_ATTR, time.time())
