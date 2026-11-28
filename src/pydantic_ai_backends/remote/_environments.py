"""Service-owned interactive resources, independent of their API viewers.

The sandboxd process owns these resources. A client disconnect never closes one;
explicit end, idle expiry, the absolute lifetime and shutdown do. Providers own
resource creation until their opener returns, including cleanup on cancellation.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Generic, Literal, Protocol, TypeVar

import anyio

EnvironmentKind = Literal["browser", "desktop"]
EnvironmentState = Literal["ready", "unavailable", "expired"]


class ManagedResource(Protocol):
    """An exact live resource whose disposal is idempotent."""

    async def aclose(self) -> None: ...


ResourceT = TypeVar("ResourceT", bound=ManagedResource)


class EnvironmentUnavailableError(RuntimeError):
    """The requested resource is absent, expired or temporarily unavailable."""


class EnvironmentConflictError(RuntimeError):
    """A resource policy or physical-control lease conflicts with this request."""


@dataclass(frozen=True)
class EnvironmentLease:
    token: str = field(repr=False)
    owner: str
    expires_at: float


@dataclass(frozen=True)
class EnvironmentSnapshot:
    environment_id: str
    kind: EnvironmentKind
    state: EnvironmentState
    created_at: float
    last_activity_at: float
    expires_at: float
    lease_owner: str | None


@dataclass(frozen=True)
class CleanupFailure:
    environment_id: str
    error: Exception = field(repr=False)


@dataclass
class _Entry(Generic[ResourceT]):
    environment_id: str
    kind: EnvironmentKind
    resource: ResourceT
    idle_seconds: float
    lifetime_seconds: float
    created_at: float
    last_activity_at: float
    state: EnvironmentState = "ready"
    unavailable_until: float | None = None
    lease: EnvironmentLease | None = None
    inflight: int = 0
    operation_scope: anyio.CancelScope | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def expires_at(self) -> float:
        expiry = min(
            self.last_activity_at + self.idle_seconds,
            self.created_at + self.lifetime_seconds,
        )
        return expiry if self.unavailable_until is None else min(expiry, self.unavailable_until)


class ManagedEnvironmentRegistry(Generic[ResourceT]):
    """Keep exact resources alive across client restarts, with bounded lifetime.

    Clock values are monotonic seconds internal to this service, never persisted
    timestamps. ``inspect`` and passive ``use`` do not renew idle. The service's
    scheduled sweep must run regardless of whether any clients are connected.
    Admission authorization belongs to sandboxd's existing session-token boundary.
    """

    def __init__(
        self,
        *,
        max_environments: int = 100,
        lease_seconds: float = 30,
        recovery_seconds: float = 30,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            max_environments < 1
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
            or not math.isfinite(recovery_seconds)
            or recovery_seconds <= 0
        ):
            raise ValueError("Environment capacity and lease/recovery periods must be positive")
        self._max_environments = max_environments
        self._lease_seconds = lease_seconds
        self._recovery_seconds = recovery_seconds
        self._now = now
        self._entries: dict[str, _Entry[ResourceT]] = {}
        self._admission = asyncio.Lock()
        self._closed = False

    async def ensure(
        self,
        environment_id: str,
        *,
        kind: EnvironmentKind,
        open_resource: Callable[[], Awaitable[ResourceT]],
        idle_seconds: float = 900,
        lifetime_seconds: float = 3600,
    ) -> EnvironmentSnapshot:
        """Open once, or return the same exact resource without renewing idle.

        Provisioning is serialized: capacity is reserved before the opener runs.
        A cancelled/failed opener must close anything it has not returned.
        """
        if (
            not environment_id
            or kind not in ("browser", "desktop")
            or not math.isfinite(idle_seconds)
            or idle_seconds <= 0
            or not math.isfinite(lifetime_seconds)
            or lifetime_seconds <= 0
        ):
            raise ValueError("Environment id and positive idle/lifetime periods are required")
        async with self._admission:
            if self._closed:
                raise EnvironmentUnavailableError("Environment supervisor is closed")
            existing = self._entries.get(environment_id)
            if existing is not None:
                if (existing.kind, existing.idle_seconds, existing.lifetime_seconds) != (
                    kind,
                    idle_seconds,
                    lifetime_seconds,
                ):
                    raise EnvironmentConflictError("End the environment before changing its policy")
                return self.inspect(environment_id)
            if len(self._entries) >= self._max_environments:
                raise EnvironmentConflictError("Interactive environment capacity reached")
            resource = await open_resource()
            now = self._now()
            entry = _Entry(environment_id, kind, resource, idle_seconds, lifetime_seconds, now, now)
            self._entries[environment_id] = entry
            return self._snapshot(entry)

    def _entry(self, environment_id: str) -> _Entry[ResourceT]:
        entry = self._entries.get(environment_id)
        if entry is None:
            raise EnvironmentUnavailableError("The exact live environment no longer exists")
        now = self._now()
        absolute_expired = entry.created_at + entry.lifetime_seconds <= now
        if absolute_expired or (entry.expires_at <= now and not entry.inflight):
            entry.state = "expired"
            entry.lease = None
            if absolute_expired and entry.operation_scope is not None:
                entry.operation_scope.cancel()
        return entry

    def _ready(self, environment_id: str) -> _Entry[ResourceT]:
        entry = self._entry(environment_id)
        if entry.state != "ready":
            raise EnvironmentUnavailableError(f"The environment is {entry.state}")
        return entry

    def resource(self, environment_id: str) -> ResourceT:
        """Read the exact ready resource without locking, touching or recreating it.

        This is the supervisor's non-mutating identity/liveness lookup. Physical
        operations must still enter ``use`` so their lease and lifetime are fenced.
        """
        return self._ready(environment_id).resource

    def _snapshot(self, entry: _Entry[ResourceT]) -> EnvironmentSnapshot:
        lease = entry.lease
        return EnvironmentSnapshot(
            entry.environment_id,
            entry.kind,
            entry.state,
            entry.created_at,
            entry.last_activity_at,
            entry.expires_at,
            lease.owner if lease is not None and lease.expires_at > self._now() else None,
        )

    def inspect(self, environment_id: str) -> EnvironmentSnapshot:
        """Read status without opening, waking or renewing the environment."""
        return self._snapshot(self._entry(environment_id))

    def acquire(self, environment_id: str, owner: str) -> EnvironmentLease:
        """Acquire exclusive physical input; a passive view needs no lease."""
        if not owner:
            raise ValueError("A control lease needs an owner")
        entry = self._ready(environment_id)
        now = self._now()
        if entry.lease is not None and entry.lease.expires_at > now:
            raise EnvironmentConflictError("Another driver controls this environment")
        if entry.inflight:
            raise EnvironmentConflictError("An operation is still using this environment")
        lease = EnvironmentLease(secrets.token_urlsafe(32), owner, now + self._lease_seconds)
        entry.lease = lease
        entry.last_activity_at = now
        return lease

    def _require_lease(self, entry: _Entry[ResourceT], token: str | None) -> EnvironmentLease:
        lease = entry.lease
        if (
            lease is None
            or token is None
            or not secrets.compare_digest(lease.token.encode(), token.encode())
            or lease.expires_at <= self._now()
        ):
            raise EnvironmentConflictError("The physical-control lease is no longer valid")
        return lease

    def renew(self, environment_id: str, token: str) -> EnvironmentLease:
        """Renew control without counting a heartbeat as user activity."""
        entry = self._ready(environment_id)
        previous = self._require_lease(entry, token)
        lease = EnvironmentLease(previous.token, previous.owner, self._now() + self._lease_seconds)
        entry.lease = lease
        return lease

    def release(self, environment_id: str, token: str) -> None:
        """Release only the supplied current lease, without touching activity."""
        entry = self._entry(environment_id)
        self._require_lease(entry, token)
        entry.lease = None

    @asynccontextmanager
    async def use(
        self, environment_id: str, *, token: str | None = None, activity: bool = False
    ) -> AsyncIterator[ResourceT]:
        """Use the exact resource, serializing commands and fencing input.

        ``activity=True`` requires a current physical lease and renews idle only
        when the operation is admitted. A passive capture never renews idle.
        Active work postpones idle retirement, but never the absolute deadline.
        A cooperative cancellation at that deadline has an unknown remote outcome;
        it is not permission to replay an input. Cleanup follows through the sweep.
        """
        entry = self._ready(environment_id)
        async with entry.lock:
            entry = self._ready(environment_id)
            if activity:
                self._require_lease(entry, token)
                entry.last_activity_at = self._now()
            entry.inflight += 1
            remaining = max(0.0, entry.created_at + entry.lifetime_seconds - self._now())
            try:
                try:
                    with anyio.fail_after(remaining) as deadline:
                        entry.operation_scope = deadline
                        yield entry.resource
                except TimeoutError as error:
                    if not deadline.cancel_called:
                        raise
                    self.mark_gone(environment_id)
                    raise EnvironmentUnavailableError(
                        "The environment's absolute lifetime expired; operation outcome unknown"
                    ) from error
                if (
                    deadline.cancel_called
                    or self._now() >= entry.created_at + entry.lifetime_seconds
                ):
                    self.mark_gone(environment_id)
                    raise EnvironmentUnavailableError(
                        "The environment's absolute lifetime expired; operation outcome unknown"
                    )
            finally:
                entry.operation_scope = None
                entry.inflight -= 1

    def mark_unavailable(self, environment_id: str) -> None:
        """Retain a temporarily unreachable resource for one bounded recovery period."""
        entry = self._entry(environment_id)
        if entry.state == "expired":
            return
        entry.state = "unavailable"
        entry.lease = None
        if entry.unavailable_until is None:
            entry.unavailable_until = self._now() + self._recovery_seconds

    def recovered(self, environment_id: str) -> None:
        """Recover the same resource after a successful non-mutating probe."""
        entry = self._entry(environment_id)
        if entry.state == "expired":
            raise EnvironmentUnavailableError("The environment recovery deadline expired")
        entry.state = "ready"
        entry.unavailable_until = None

    def mark_gone(self, environment_id: str) -> None:
        """Revoke access now; retain the disposal obligation until close succeeds."""
        entry = self._entry(environment_id)
        entry.state = "expired"
        entry.lease = None

    async def close(self, environment_id: str) -> None:
        """Dispose an owned resource, retaining it for retry on provider failure."""
        entry = self._entries.get(environment_id)
        if entry is None:
            return
        entry.state = "expired"
        entry.lease = None
        async with entry.lock:
            if self._entries.get(environment_id) is not entry:
                return
            await entry.resource.aclose()
            del self._entries[environment_id]

    async def sweep(self) -> list[CleanupFailure]:
        """Retry due disposal independently; one failed provider cannot starve others."""
        failed: list[CleanupFailure] = []
        for environment_id, entry in list(self._entries.items()):
            if self._entries.get(environment_id) is not entry:
                continue
            entry = self._entry(environment_id)
            if entry.state != "expired" or entry.inflight:
                continue
            try:
                await self.close(environment_id)
            except Exception as error:
                failed.append(CleanupFailure(environment_id, error))
        return failed

    async def shutdown(self) -> list[CleanupFailure]:
        """Stop admissions, then dispose every remaining owned environment."""
        async with self._admission:
            self._closed = True
            for environment_id in self._entries:
                self.mark_gone(environment_id)
        failed: list[CleanupFailure] = []
        for environment_id in list(self._entries):
            try:
                await self.close(environment_id)
            except Exception as error:
                failed.append(CleanupFailure(environment_id, error))
        return failed
