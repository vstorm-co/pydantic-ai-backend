"""The service supervisor owns lifetime; client views and failures never recreate it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from pydantic_ai_backends.remote._environments import (
    EnvironmentConflictError,
    EnvironmentUnavailableError,
    ManagedEnvironmentRegistry,
)


@dataclass
class Resource:
    closed: int = 0
    fail: bool = False

    async def aclose(self) -> None:
        self.closed += 1
        if self.fail:
            raise OSError("Provider unavailable")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def registry(clock: Clock) -> ManagedEnvironmentRegistry[Resource]:
    return ManagedEnvironmentRegistry(now=clock, lease_seconds=5, recovery_seconds=3)


async def opened(registry: ManagedEnvironmentRegistry[Resource], **kwargs: float) -> Resource:
    resource = Resource()

    async def provision() -> Resource:
        return resource

    await registry.ensure("one", kind="browser", open_resource=provision, **kwargs)
    return resource


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_environments": 0},
        {"lease_seconds": 0},
        {"recovery_seconds": -1},
        {"lease_seconds": float("inf")},
        {"recovery_seconds": float("nan")},
    ],
)
def test_constructor_requires_bounded_positive_policy(kwargs):
    with pytest.raises(ValueError, match="positive"):
        ManagedEnvironmentRegistry(**kwargs)


@pytest.mark.parametrize(
    "environment_id,kind,idle,lifetime",
    [
        ("", "browser", 1, 1),
        ("one", "other", 1, 1),
        ("one", "browser", 0, 1),
        ("one", "browser", float("nan"), 1),
        ("one", "browser", 1, -1),
        ("one", "browser", 1, float("inf")),
    ],
)
async def test_open_rejects_invalid_policy(registry, environment_id, kind, idle, lifetime):
    async def provision() -> Resource:
        pytest.fail("Invalid policies must not provision")

    with pytest.raises(ValueError):
        await registry.ensure(
            environment_id,
            kind=kind,
            idle_seconds=idle,
            lifetime_seconds=lifetime,
            open_resource=provision,
        )


async def test_idempotent_start_keeps_exact_resource_and_does_not_touch_idle(registry, clock):
    resource = await opened(registry)
    clock.now = 2
    snapshot = await registry.ensure("one", kind="browser", open_resource=lambda: opened(registry))
    assert snapshot.last_activity_at == 0
    async with registry.use("one") as reused:
        assert reused is resource
    assert registry.inspect("one").last_activity_at == 0
    with pytest.raises(EnvironmentConflictError, match="changing"):
        await registry.ensure("one", kind="desktop", open_resource=lambda: opened(registry))
    assert resource.closed == 0


async def test_capacity_counts_resources_waiting_for_cleanup(clock):
    registry = ManagedEnvironmentRegistry[Resource](max_environments=1, now=clock)
    resource = await opened(registry)
    resource.fail = True
    registry.mark_gone("one")
    with pytest.raises(OSError):
        await registry.close("one")
    with pytest.raises(EnvironmentConflictError, match="capacity"):
        await registry.ensure("two", kind="browser", open_resource=lambda: opened(registry))


async def test_failed_opener_does_not_publish_a_resource(registry):
    async def failing() -> Resource:
        raise OSError("Not created")

    with pytest.raises(OSError):
        await registry.ensure("one", kind="browser", open_resource=failing)
    with pytest.raises(EnvironmentUnavailableError, match="no longer exists"):
        registry.inspect("one")


async def test_concurrent_open_provisions_only_once(registry):
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def provision() -> Resource:
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        return Resource()

    first = asyncio.create_task(registry.ensure("one", kind="browser", open_resource=provision))
    await entered.wait()
    second = asyncio.create_task(registry.ensure("one", kind="browser", open_resource=provision))
    finish.set()
    assert await first == await second
    assert calls == 1


async def test_control_is_exclusive_and_stale_or_unicode_tokens_cannot_drive(registry, clock):
    await opened(registry)
    with pytest.raises(ValueError, match="owner"):
        registry.acquire("one", "")
    lease = registry.acquire("one", "alice")
    assert "token" not in repr(lease)
    assert registry.inspect("one").lease_owner == "alice"
    with pytest.raises(EnvironmentConflictError, match="Another"):
        registry.acquire("one", "alice")
    for token in (None, "bad", "☃"):
        with pytest.raises(EnvironmentConflictError, match="no longer valid"):
            async with registry.use("one", token=token, activity=True):
                pytest.fail("Stale input must be refused")
    clock.now = 6
    assert registry.inspect("one").lease_owner is None
    with pytest.raises(EnvironmentConflictError):
        registry.renew("one", lease.token)
    replacement = registry.acquire("one", "bob")
    with pytest.raises(EnvironmentConflictError):
        registry.release("one", lease.token)
    registry.release("one", replacement.token)
    assert registry.inspect("one").lease_owner is None


async def test_heartbeat_does_not_renew_idle_but_real_input_does(registry, clock):
    await opened(registry, idle_seconds=10)
    lease = registry.acquire("one", "alice")
    clock.now = 4
    renewed = registry.renew("one", lease.token)
    assert renewed.expires_at == 9
    assert registry.inspect("one").last_activity_at == 0
    async with registry.use("one", token=lease.token, activity=True):
        pass
    assert registry.inspect("one").last_activity_at == 4
    registry.release("one", lease.token)
    clock.now = 14
    assert registry.inspect("one").state == "expired"
    with pytest.raises(EnvironmentUnavailableError, match="expired"):
        registry.acquire("one", "alice")


async def test_active_input_cannot_extend_absolute_lifetime(registry, clock):
    await opened(registry, idle_seconds=10, lifetime_seconds=12)
    clock.now = 8
    lease = registry.acquire("one", "alice")
    async with registry.use("one", token=lease.token, activity=True):
        pass
    assert registry.inspect("one").expires_at == 12
    clock.now = 12
    assert registry.inspect("one").state == "expired"


async def test_transient_failure_retains_exact_resource_and_bounded_recovery(registry, clock):
    resource = await opened(registry)
    lease = registry.acquire("one", "alice")
    registry.mark_unavailable("one")
    assert registry.inspect("one").state == "unavailable"
    assert registry.inspect("one").lease_owner is None
    with pytest.raises(EnvironmentUnavailableError, match="unavailable"):
        async with registry.use("one", token=lease.token, activity=True):
            pytest.fail("Input while unavailable must be refused")
    assert await registry.sweep() == []
    assert resource.closed == 0
    clock.now = 1
    registry.mark_unavailable("one")
    assert registry.inspect("one").expires_at == 3
    registry.recovered("one")
    async with registry.use("one") as recovered:
        assert recovered is resource
    registry.mark_unavailable("one")
    clock.now = 4
    with pytest.raises(EnvironmentUnavailableError, match="deadline"):
        registry.recovered("one")
    registry.mark_unavailable("one")
    assert registry.inspect("one").state == "expired"
    assert await registry.sweep() == []
    assert resource.closed == 1


async def test_sweep_keeps_failed_cleanup_and_does_not_starve_other_resource(registry, clock):
    resource = await opened(registry, idle_seconds=1)
    resource.fail = True
    other = Resource()

    async def second() -> Resource:
        return other

    await registry.ensure("two", kind="desktop", open_resource=second, idle_seconds=1)
    clock.now = 1
    failures = await registry.sweep()
    assert [failure.environment_id for failure in failures] == ["one"]
    assert isinstance(failures[0].error, OSError)
    assert "Provider unavailable" not in repr(failures[0])
    assert other.closed == 1
    resource.fail = False
    assert await registry.sweep() == []
    assert resource.closed == 2
    await registry.close("one")
    with pytest.raises(EnvironmentUnavailableError):
        registry.inspect("one")


async def test_waiting_input_is_rechecked_after_lease_changes(registry):
    await opened(registry)
    first = registry.acquire("one", "alice")
    entered = asyncio.Event()
    done = asyncio.Event()

    async def viewer() -> None:
        async with registry.use("one"):
            entered.set()
            await done.wait()

    task = asyncio.create_task(viewer())
    await entered.wait()

    async def input() -> None:
        async with registry.use("one", token=first.token, activity=True):
            pytest.fail("A queued stale operation must not run")

    waiting = asyncio.create_task(input())
    await asyncio.sleep(0)
    registry.release("one", first.token)
    done.set()
    await task
    with pytest.raises(EnvironmentConflictError):
        await waiting


async def test_inflight_operation_is_not_reaped_and_cancellation_releases_it(registry, clock):
    resource = await opened(registry, idle_seconds=1)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def operation() -> None:
        async with registry.use("one"):
            entered.set()
            await blocked.wait()

    task = asyncio.create_task(operation())
    await entered.wait()
    clock.now = 10
    assert await registry.sweep() == []
    assert resource.closed == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await registry.sweep()
    assert resource.closed == 1


async def test_duplicate_close_is_idempotent_even_when_queued(registry):
    resource = await opened(registry)
    await asyncio.gather(registry.close("one"), registry.close("one"))
    assert resource.closed == 1


async def test_shutdown_drains_operations_and_rejects_further_admission(registry):
    resource = await opened(registry)
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def operation() -> None:
        async with registry.use("one"):
            entered.set()
            await finish.wait()

    task = asyncio.create_task(operation())
    await entered.wait()
    shutdown = asyncio.create_task(registry.shutdown())
    await asyncio.sleep(0)
    finish.set()
    await task
    assert await shutdown == []
    assert resource.closed == 1
    with pytest.raises(EnvironmentUnavailableError, match="closed"):
        await registry.ensure("new", kind="browser", open_resource=lambda: opened(registry))


async def test_shutdown_reports_disposal_failure_and_can_retry(registry):
    resource = await opened(registry)
    resource.fail = True
    assert len(await registry.shutdown()) == 1
    resource.fail = False
    assert await registry.shutdown() == []


async def test_concurrent_close_checks_exact_entry_after_waiting(registry):
    entered = asyncio.Event()
    finish = asyncio.Event()

    class SlowResource(Resource):
        async def aclose(self) -> None:
            entered.set()
            await finish.wait()
            await super().aclose()

    resource = SlowResource()

    async def provision() -> Resource:
        return resource

    await registry.ensure("one", kind="browser", open_resource=provision)
    first = asyncio.create_task(registry.close("one"))
    await entered.wait()
    second = asyncio.create_task(registry.close("one"))
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(first, second)
    assert resource.closed == 1


async def test_sweep_tolerates_another_caller_closing_a_later_entry(registry, clock):
    entered = asyncio.Event()
    finish = asyncio.Event()

    class SlowResource(Resource):
        async def aclose(self) -> None:
            entered.set()
            await finish.wait()
            await super().aclose()

    first = SlowResource()
    second = Resource()

    async def provision_first() -> Resource:
        return first

    async def provision_second() -> Resource:
        return second

    await registry.ensure("one", kind="browser", open_resource=provision_first, idle_seconds=1)
    await registry.ensure("two", kind="browser", open_resource=provision_second, idle_seconds=1)
    clock.now = 2
    sweeping = asyncio.create_task(registry.sweep())
    await entered.wait()
    await registry.close("two")
    finish.set()
    assert await sweeping == []
    assert first.closed == second.closed == 1


async def test_expired_lease_cannot_be_reassigned_during_a_physical_operation(registry, clock):
    await opened(registry)
    lease = registry.acquire("one", "alice")
    async with registry.use("one", token=lease.token, activity=True):
        clock.now = 6
        with pytest.raises(EnvironmentConflictError, match="still using"):
            registry.acquire("one", "bob")
    assert registry.acquire("one", "bob").owner == "bob"


async def test_resource_identity_lookup_never_waits_on_active_operation_or_renews_idle(registry):
    resource = await opened(registry)

    initial = registry.inspect("one").expires_at
    lease = registry.acquire("one", "driver")
    async with registry.use("one", token=lease.token, activity=True):
        active_expiry = registry.inspect("one").expires_at
        assert registry.resource("one") is resource
        assert registry.inspect("one").expires_at == active_expiry
    assert active_expiry >= initial
    registry.mark_unavailable("one")
    with pytest.raises(EnvironmentUnavailableError):
        registry.resource("one")
    await registry.shutdown()


async def test_absolute_deadline_cancels_admitted_operation_and_refuses_new_input(registry, clock):
    resource = await opened(registry, idle_seconds=20, lifetime_seconds=5)
    entered = asyncio.Event()
    never = asyncio.Event()
    lease = registry.acquire("one", "driver")

    async def blocked_input() -> None:
        async with registry.use("one", token=lease.token, activity=True):
            entered.set()
            await never.wait()

    task = asyncio.create_task(blocked_input())
    await entered.wait()
    clock.now = 5
    try:
        assert registry.inspect("one").state == "expired"
        with pytest.raises(EnvironmentUnavailableError):
            registry.acquire("one", "new-driver")
        with pytest.raises(EnvironmentUnavailableError, match="absolute lifetime"):
            await asyncio.wait_for(task, 0.2)
        assert resource.closed == 0
        assert await registry.sweep() == []
        assert resource.closed == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_absolute_deadline_autonomously_bounds_a_stalled_operation_without_inspection():
    registry = ManagedEnvironmentRegistry[Resource]()
    await opened(registry, idle_seconds=10, lifetime_seconds=0.01)

    async def operation() -> None:
        async with registry.use("one"):
            await asyncio.Event().wait()

    with pytest.raises(EnvironmentUnavailableError, match="absolute lifetime"):
        await asyncio.wait_for(operation(), 0.2)
    assert registry.inspect("one").state == "expired"
    await registry.sweep()


async def test_provider_timeout_is_not_mislabeled_as_environment_absolute_expiry(registry):
    await opened(registry)
    with pytest.raises(TimeoutError, match="Provider deadline"):
        async with registry.use("one"):
            raise TimeoutError("Provider deadline")
    assert registry.inspect("one").state == "ready"


async def test_operation_without_checkpoint_still_reports_crossed_absolute_deadline(
    registry, clock
):
    await opened(registry, idle_seconds=10, lifetime_seconds=5)
    with pytest.raises(EnvironmentUnavailableError, match="absolute lifetime"):
        async with registry.use("one"):
            clock.now = 5
    assert registry.inspect("one").state == "expired"
