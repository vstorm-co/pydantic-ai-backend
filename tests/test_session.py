"""Tests for SessionManager."""

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pydantic_ai_backends import RuntimeConfig, SessionManager
from pydantic_ai_backends.backends.docker.session import SessionLimitExceeded


class MockDockerSandbox:
    """Mock DockerSandbox for testing SessionManager."""

    def __init__(
        self,
        runtime: RuntimeConfig | str | None = None,
        session_id: str | None = None,
        idle_timeout: int = 3600,
        volumes: dict[str, str] | None = None,
        **kwargs: object,
    ) -> None:
        self._id = session_id or "test-id"
        self._runtime = runtime
        self._idle_timeout = idle_timeout
        self._last_activity = time.time()
        self._alive = True
        self._volumes = volumes or {}

    @property
    def session_id(self) -> str:
        return self._id

    def is_alive(self) -> bool:
        return self._alive

    def start(self) -> None:
        self._alive = True

    def stop(self) -> None:
        self._alive = False


class MockCustomSandbox:
    """Mock sandbox for testing custom factory support."""

    def __init__(self, session_id: str) -> None:
        self._id = session_id
        self._last_activity = time.time()
        self._alive = True
        self._started = False

    @property
    def session_id(self) -> str:
        return self._id

    def is_alive(self) -> bool:
        return self._alive

    def start(self) -> None:
        self._started = True
        self._alive = True

    def stop(self) -> None:
        self._alive = False


class TestSessionManager:
    """Tests for SessionManager class."""

    def test_init_defaults(self):
        """Test default initialization."""
        manager = SessionManager()
        assert manager._default_runtime is None
        assert manager._default_idle_timeout == 3600
        assert manager.session_count == 0
        assert len(manager) == 0

    def test_init_with_runtime(self):
        """Test initialization with default runtime."""
        runtime = RuntimeConfig(name="test")
        manager = SessionManager(default_runtime=runtime)
        assert manager._default_runtime is runtime

    def test_init_with_string_runtime(self):
        """Test initialization with runtime name."""
        manager = SessionManager(default_runtime="python-datascience")
        assert manager._default_runtime == "python-datascience"

    def test_init_with_timeout(self):
        """Test initialization with custom timeout."""
        manager = SessionManager(default_idle_timeout=1800)
        assert manager._default_idle_timeout == 1800

    def test_init_with_factory(self):
        """Test initialization with custom sandbox factory."""

        def factory(sid: str) -> MockCustomSandbox:
            return MockCustomSandbox(sid)

        manager = SessionManager(sandbox_factory=factory)
        assert manager._sandbox_factory is factory

    @pytest.mark.asyncio
    async def test_get_or_create_new_session(self):
        """Test creating a new session."""
        manager = SessionManager()

        with patch("pydantic_ai_backends.backends.docker.sandbox.DockerSandbox", MockDockerSandbox):
            sandbox = await manager.get_or_create("user-123")
            assert sandbox.session_id == "user-123"
            assert "user-123" in manager
            assert manager.session_count == 1

    @pytest.mark.asyncio
    async def test_get_or_create_existing_session(self):
        """Test retrieving existing session."""
        manager = SessionManager()

        with patch("pydantic_ai_backends.backends.docker.sandbox.DockerSandbox", MockDockerSandbox):
            sandbox1 = await manager.get_or_create("user-123")
            sandbox2 = await manager.get_or_create("user-123")
            assert sandbox1 is sandbox2
            assert manager.session_count == 1

    @pytest.mark.asyncio
    async def test_get_or_create_dead_session_recreates(self):
        """Test that dead sessions are recreated."""
        manager = SessionManager()

        with patch("pydantic_ai_backends.backends.docker.sandbox.DockerSandbox", MockDockerSandbox):
            sandbox1 = await manager.get_or_create("user-123")
            sandbox1._alive = False  # type: ignore[attr-defined]  # Mock attribute

            sandbox2 = await manager.get_or_create("user-123")
            assert sandbox1 is not sandbox2
            assert manager.session_count == 1

    @pytest.mark.asyncio
    async def test_get_or_create_concurrent_same_id_no_duplicate(self):
        """Concurrent calls for the same id share one sandbox (no race leak)."""

        created: list[MockCustomSandbox] = []

        def slow_factory(session_id: str) -> MockCustomSandbox:
            sandbox = MockCustomSandbox(session_id)
            created.append(sandbox)
            return sandbox

        manager = SessionManager(sandbox_factory=slow_factory)

        # Fire two concurrent requests for the same session id.
        results = await asyncio.gather(
            manager.get_or_create("user-x"),
            manager.get_or_create("user-x"),
        )

        # Exactly one sandbox is created and both callers get the same one.
        assert results[0] is results[1]
        assert len(created) == 1
        assert manager.session_count == 1

    @pytest.mark.asyncio
    async def test_release_existing_session(self):
        """Test releasing an existing session."""
        manager = SessionManager()

        with patch("pydantic_ai_backends.backends.docker.sandbox.DockerSandbox", MockDockerSandbox):
            await manager.get_or_create("user-123")
            assert manager.session_count == 1

            result = await manager.release("user-123")
            assert result is True
            assert manager.session_count == 0
            assert "user-123" not in manager

    @pytest.mark.asyncio
    async def test_release_nonexistent_session(self):
        """Test releasing a non-existent session."""
        manager = SessionManager()
        result = await manager.release("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_cleanup_idle_sessions(self):
        """Test cleaning up idle sessions."""
        manager = SessionManager(default_idle_timeout=10)

        with patch("pydantic_ai_backends.backends.docker.sandbox.DockerSandbox", MockDockerSandbox):
            sandbox1 = await manager.get_or_create("user-1")
            sandbox2 = await manager.get_or_create("user-2")

            # Make one session idle
            sandbox1._last_activity = time.time() - 20  # 20 seconds ago
            sandbox2._last_activity = time.time()  # Just now

            cleaned = await manager.cleanup_idle(max_idle=10)
            assert cleaned == 1
            assert manager.session_count == 1
            assert "user-1" not in manager
            assert "user-2" in manager

    @pytest.mark.asyncio
    async def test_cleanup_idle_uses_default_timeout(self):
        """Test cleanup uses default timeout when not specified."""
        manager = SessionManager(default_idle_timeout=5)

        with patch("pydantic_ai_backends.backends.docker.sandbox.DockerSandbox", MockDockerSandbox):
            sandbox = await manager.get_or_create("user-1")
            sandbox._last_activity = time.time() - 10  # 10 seconds ago

            cleaned = await manager.cleanup_idle()
            assert cleaned == 1

    @pytest.mark.asyncio
    async def test_shutdown(self):
        """Test shutting down all sessions."""
        manager = SessionManager()

        with patch("pydantic_ai_backends.backends.docker.sandbox.DockerSandbox", MockDockerSandbox):
            await manager.get_or_create("user-1")
            await manager.get_or_create("user-2")
            await manager.get_or_create("user-3")
            assert manager.session_count == 3

            count = await manager.shutdown()
            assert count == 3
            assert manager.session_count == 0

    def test_sessions_property(self):
        """Test sessions property returns copy."""
        manager = SessionManager()
        manager._sessions["test"] = MagicMock()

        sessions = manager.sessions
        assert "test" in sessions
        # Verify it's a copy
        sessions["new"] = MagicMock()
        assert "new" not in manager._sessions

    def test_contains(self):
        """Test __contains__ method."""
        manager = SessionManager()
        manager._sessions["test"] = MagicMock()

        assert "test" in manager
        assert "other" not in manager

    def test_len(self):
        """Test __len__ method."""
        manager = SessionManager()
        assert len(manager) == 0

        manager._sessions["a"] = MagicMock()
        manager._sessions["b"] = MagicMock()
        assert len(manager) == 2

    def test_start_cleanup_loop(self):
        """Test starting cleanup loop."""
        manager = SessionManager()
        assert manager._cleanup_task is None

        # We can't actually test the async loop without running it,
        # but we can verify it's created
        with patch("asyncio.create_task") as mock_create_task:
            manager.start_cleanup_loop(interval=60)
            mock_create_task.assert_called_once()

        # Calling again should do nothing
        with patch("asyncio.create_task") as mock_create_task:
            manager._cleanup_task = MagicMock()
            manager.start_cleanup_loop()
            mock_create_task.assert_not_called()

    def test_stop_cleanup_loop(self):
        """Test stopping cleanup loop."""
        manager = SessionManager()
        mock_task = MagicMock()
        manager._cleanup_task = mock_task

        manager.stop_cleanup_loop()
        mock_task.cancel.assert_called_once()
        assert manager._cleanup_task is None

    def test_stop_cleanup_loop_when_not_running(self):
        """Test stopping cleanup loop when not running."""
        manager = SessionManager()
        manager.stop_cleanup_loop()  # Should not raise
        assert manager._cleanup_task is None

    def test_init_with_workspace_root_string(self):
        """Test initialization with workspace_root as string."""
        manager = SessionManager(workspace_root="/tmp/sessions")
        assert manager._workspace_root is not None
        assert str(manager._workspace_root) == "/tmp/sessions"

    def test_init_with_workspace_root_path(self):
        """Test initialization with workspace_root as Path."""

        manager = SessionManager(workspace_root=Path("/tmp/sessions"))
        assert manager._workspace_root is not None
        assert str(manager._workspace_root) == "/tmp/sessions"

    def test_init_without_workspace_root(self):
        """Test initialization without workspace_root."""
        manager = SessionManager()
        assert manager._workspace_root is None

    @pytest.mark.asyncio
    async def test_get_or_create_with_workspace_root(self, tmp_path):
        """Test that workspace_root creates directories and passes volumes."""
        manager = SessionManager(workspace_root=tmp_path)

        with patch("pydantic_ai_backends.backends.docker.sandbox.DockerSandbox", MockDockerSandbox):
            sandbox = await manager.get_or_create("user-123")

            # Check that directory was created
            expected_dir = tmp_path / "user-123" / "workspace"
            assert expected_dir.exists()
            assert expected_dir.is_dir()

            # Check that volumes were passed to sandbox
            assert sandbox._volumes is not None
            assert str(expected_dir.resolve()) in sandbox._volumes
            assert sandbox._volumes[str(expected_dir.resolve())] == "/workspace"

    @pytest.mark.asyncio
    async def test_get_or_create_without_workspace_root_no_volumes(self):
        """Test that without workspace_root, no volumes are set."""
        manager = SessionManager()

        with patch("pydantic_ai_backends.backends.docker.sandbox.DockerSandbox", MockDockerSandbox):
            sandbox = await manager.get_or_create("user-123")
            assert sandbox._volumes == {}

    @pytest.mark.asyncio
    async def test_get_or_create_multiple_sessions_separate_dirs(self, tmp_path):
        """Test that multiple sessions get separate workspace directories."""
        manager = SessionManager(workspace_root=tmp_path)

        with patch("pydantic_ai_backends.backends.docker.sandbox.DockerSandbox", MockDockerSandbox):
            sandbox1 = await manager.get_or_create("user-1")
            sandbox2 = await manager.get_or_create("user-2")

            # Check separate directories
            dir1 = tmp_path / "user-1" / "workspace"
            dir2 = tmp_path / "user-2" / "workspace"

            assert dir1.exists()
            assert dir2.exists()
            assert dir1 != dir2

            # Check separate volumes
            assert str(dir1.resolve()) in sandbox1._volumes
            assert str(dir2.resolve()) in sandbox2._volumes


class TestSessionManagerWithFactory:
    """Tests for SessionManager with custom sandbox_factory."""

    @pytest.mark.asyncio
    async def test_factory_called_with_session_id(self):
        """Test that factory receives the session_id."""
        created_ids: list[str] = []

        def factory(session_id: str) -> MockCustomSandbox:
            created_ids.append(session_id)
            return MockCustomSandbox(session_id)

        manager = SessionManager(sandbox_factory=factory)
        await manager.get_or_create("user-42")

        assert created_ids == ["user-42"]

    @pytest.mark.asyncio
    async def test_factory_sandbox_started(self):
        """Test that factory-created sandboxes get start() called."""

        def factory(session_id: str) -> MockCustomSandbox:
            return MockCustomSandbox(session_id)

        manager = SessionManager(sandbox_factory=factory)
        sandbox = await manager.get_or_create("user-1")
        assert sandbox._started is True

    @pytest.mark.asyncio
    async def test_factory_sandbox_reused_when_alive(self):
        """Test that alive factory sandboxes are reused."""

        def factory(session_id: str) -> MockCustomSandbox:
            return MockCustomSandbox(session_id)

        manager = SessionManager(sandbox_factory=factory)
        s1 = await manager.get_or_create("user-1")
        s2 = await manager.get_or_create("user-1")
        assert s1 is s2

    @pytest.mark.asyncio
    async def test_factory_sandbox_recreated_when_dead(self):
        """Test that dead factory sandboxes are recreated."""

        def factory(session_id: str) -> MockCustomSandbox:
            return MockCustomSandbox(session_id)

        manager = SessionManager(sandbox_factory=factory)
        s1 = await manager.get_or_create("user-1")
        s1._alive = False

        s2 = await manager.get_or_create("user-1")
        assert s1 is not s2
        assert s2._started is True

    @pytest.mark.asyncio
    async def test_factory_release_stops_sandbox(self):
        """Test that releasing a factory sandbox calls stop()."""

        def factory(session_id: str) -> MockCustomSandbox:
            return MockCustomSandbox(session_id)

        manager = SessionManager(sandbox_factory=factory)
        sandbox = await manager.get_or_create("user-1")
        assert sandbox._alive is True

        await manager.release("user-1")
        assert sandbox._alive is False
        assert "user-1" not in manager

    @pytest.mark.asyncio
    async def test_factory_cleanup_idle(self):
        """Test idle cleanup with factory sandboxes."""

        def factory(session_id: str) -> MockCustomSandbox:
            return MockCustomSandbox(session_id)

        manager = SessionManager(
            sandbox_factory=factory,
            default_idle_timeout=10,
        )
        s1 = await manager.get_or_create("user-1")
        s2 = await manager.get_or_create("user-2")

        s1._last_activity = time.time() - 20  # idle
        s2._last_activity = time.time()  # active

        cleaned = await manager.cleanup_idle()
        assert cleaned == 1
        assert "user-1" not in manager
        assert "user-2" in manager

    @pytest.mark.asyncio
    async def test_factory_shutdown(self):
        """Test shutdown with factory sandboxes."""

        def factory(session_id: str) -> MockCustomSandbox:
            return MockCustomSandbox(session_id)

        manager = SessionManager(sandbox_factory=factory)
        await manager.get_or_create("a")
        await manager.get_or_create("b")

        count = await manager.shutdown()
        assert count == 2
        assert manager.session_count == 0

    @pytest.mark.asyncio
    async def test_factory_ignores_runtime_param(self):
        """Test that runtime param is ignored when using factory."""
        factory_calls: list[str] = []

        def factory(session_id: str) -> MockCustomSandbox:
            factory_calls.append(session_id)
            return MockCustomSandbox(session_id)

        manager = SessionManager(sandbox_factory=factory)
        # Pass runtime — should be ignored (factory doesn't receive it)
        await manager.get_or_create("user-1", runtime="python-datascience")
        assert factory_calls == ["user-1"]

    @pytest.mark.asyncio
    async def test_factory_ignores_workspace_root(self, tmp_path):
        """Test that workspace_root doesn't affect factory-created sandboxes."""

        def factory(session_id: str) -> MockCustomSandbox:
            return MockCustomSandbox(session_id)

        manager = SessionManager(
            sandbox_factory=factory,
            workspace_root=tmp_path,
        )
        await manager.get_or_create("user-1")

        # workspace_root should NOT create directories for factory sandboxes
        assert not (tmp_path / "user-1").exists()

    @pytest.mark.asyncio
    async def test_factory_activity_updated_on_reuse(self):
        """Test that _last_activity is updated when reusing a session."""

        def factory(session_id: str) -> MockCustomSandbox:
            return MockCustomSandbox(session_id)

        manager = SessionManager(sandbox_factory=factory)
        sandbox = await manager.get_or_create("user-1")
        sandbox._last_activity = time.time() - 100  # Simulate old activity

        before = sandbox._last_activity
        await manager.get_or_create("user-1")
        assert sandbox._last_activity > before


class TestSessionLimit:
    """Tests for the `max_sessions` ceiling."""

    def _manager(self, limit: int) -> SessionManager:
        return SessionManager(
            sandbox_factory=lambda session_id: MockCustomSandbox(session_id),
            max_sessions=limit,
        )

    async def test_new_session_beyond_the_cap_is_rejected(self):
        manager = self._manager(2)
        await manager.get_or_create("a")
        await manager.get_or_create("b")

        with pytest.raises(SessionLimitExceeded) as excinfo:
            await manager.get_or_create("c")

        assert excinfo.value.limit == 2
        assert "Session limit of 2" in str(excinfo.value)
        assert manager.session_count == 2

    async def test_existing_session_still_served_at_the_cap(self):
        """The ceiling must not lock out sessions that are already open."""
        manager = self._manager(1)
        first = await manager.get_or_create("a")

        assert await manager.get_or_create("a") is first

    async def test_releasing_frees_a_slot(self):
        manager = self._manager(1)
        await manager.get_or_create("a")
        await manager.release("a")

        assert await manager.get_or_create("b") is not None

    async def test_uncapped_by_default(self):
        manager = SessionManager(sandbox_factory=lambda sid: MockCustomSandbox(sid))
        for index in range(5):
            await manager.get_or_create(f"user-{index}")

        assert manager.session_count == 5


class TestSessionStartFailure:
    """Tests for a sandbox that fails during start()."""

    async def test_failed_start_is_stopped_and_not_registered(self):
        """Nothing else can clean up a sandbox the manager never stored."""
        stopped: list[str] = []

        class Failing(MockCustomSandbox):
            def start(self) -> None:
                raise RuntimeError("daemon refused")

            def stop(self) -> None:
                stopped.append(self._id)

        manager = SessionManager(sandbox_factory=lambda sid: Failing(sid))

        with pytest.raises(RuntimeError, match="daemon refused"):
            await manager.get_or_create("user-1")

        assert stopped == ["user-1"]
        assert manager.session_count == 0
        assert "user-1" not in manager

    async def test_a_failing_stop_does_not_mask_the_start_error(self):
        class Failing(MockCustomSandbox):
            def start(self) -> None:
                raise RuntimeError("daemon refused")

            def stop(self) -> None:
                raise RuntimeError("stop also broken")

        manager = SessionManager(sandbox_factory=lambda sid: Failing(sid))

        with pytest.raises(RuntimeError, match="daemon refused"):
            await manager.get_or_create("user-1")


class TestSessionLockPruning:
    """Tests that interned locks do not accumulate."""

    async def test_lock_for_a_rejected_session_is_pruned(self):
        manager = SessionManager(
            sandbox_factory=lambda sid: MockCustomSandbox(sid),
            max_sessions=1,
        )
        await manager.get_or_create("kept")
        for index in range(3):
            with pytest.raises(SessionLimitExceeded):
                await manager.get_or_create(f"rejected-{index}")

        assert len(manager._locks) == 4

        await manager.cleanup_idle(max_idle=10_000)

        assert set(manager._locks) == {"kept"}

    async def test_a_held_lock_is_never_pruned(self):
        """A waiter depends on that exact object for mutual exclusion."""
        manager = SessionManager(sandbox_factory=lambda sid: MockCustomSandbox(sid))
        manager._locks["in-flight"] = asyncio.Lock()
        await manager._locks["in-flight"].acquire()

        manager._prune_locks()

        assert "in-flight" in manager._locks


class TestSessionIdleLimits:
    """Tests for per-sandbox idle timeouts and missing activity stamps."""

    async def test_sandbox_idle_timeout_wins_over_the_manager_default(self):
        manager = SessionManager(
            sandbox_factory=lambda sid: MockDockerSandbox(session_id=sid, idle_timeout=10),
            default_idle_timeout=100_000,
        )
        sandbox = await manager.get_or_create("user-1")
        sandbox._last_activity = time.time() - 60

        assert await manager.cleanup_idle() == 1

    async def test_explicit_max_idle_overrides_the_sandbox_value(self):
        manager = SessionManager(
            sandbox_factory=lambda sid: MockDockerSandbox(session_id=sid, idle_timeout=10)
        )
        sandbox = await manager.get_or_create("user-1")
        sandbox._last_activity = time.time() - 60

        assert await manager.cleanup_idle(max_idle=10_000) == 0

    async def test_default_used_when_sandbox_has_no_timeout(self):
        class NoTimeout(MockCustomSandbox):
            pass

        manager = SessionManager(
            sandbox_factory=lambda sid: NoTimeout(sid), default_idle_timeout=10
        )
        sandbox = await manager.get_or_create("user-1")
        sandbox._last_activity = time.time() - 60

        assert await manager.cleanup_idle() == 1

    async def test_sandbox_without_activity_stamp_is_kept_not_crashed(self):
        """A third-party sandbox lacking the private stamp must not raise."""

        class NoStamp:
            def __init__(self, session_id: str) -> None:
                self._id = session_id

            def is_alive(self) -> bool:
                return True

            def start(self) -> None: ...

            def stop(self) -> None: ...

        manager = SessionManager(sandbox_factory=lambda sid: NoStamp(sid))
        await manager.get_or_create("user-1")

        assert await manager.cleanup_idle(max_idle=0) == 0
        assert manager.session_count == 1


class TestCleanupLoopResilience:
    """Tests that the background reaper survives a bad pass."""

    async def test_loop_keeps_running_after_a_failure(self, caplog):
        manager = SessionManager()
        calls: list[int] = []
        recovered = asyncio.Event()

        async def flaky(max_idle=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("daemon unreachable")
            recovered.set()
            return 0

        manager.cleanup_idle = flaky
        with caplog.at_level(logging.ERROR):
            manager.start_cleanup_loop(interval=0)
            await asyncio.wait_for(recovered.wait(), timeout=5)
        manager.stop_cleanup_loop()

        assert len(calls) >= 2
        assert "Idle sandbox cleanup failed" in caplog.text

    async def test_cancellation_during_cleanup_is_not_swallowed(self):
        manager = SessionManager()
        entered = asyncio.Event()

        async def slow(max_idle=None):
            entered.set()
            await asyncio.sleep(10)
            return 0

        manager.cleanup_idle = slow
        manager.start_cleanup_loop(interval=0)
        await asyncio.wait_for(entered.wait(), timeout=5)

        task = manager._cleanup_task
        manager.stop_cleanup_loop()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert task.cancelled()


class TestLegacyActivityTracking:
    """Sandboxes predating `touch()` recorded activity in `_last_activity`."""

    class LegacySandbox:
        def __init__(self, session_id: str):
            self.session_id = session_id
            self._last_activity = time.time() - 100
            self.stopped = False

        def start(self) -> None:
            pass

        def stop(self) -> None:
            self.stopped = True

        def is_alive(self) -> bool:
            return True

    async def test_reuse_refreshes_the_legacy_timestamp(self):
        manager = SessionManager(sandbox_factory=self.LegacySandbox)
        sandbox = await manager.get_or_create("user-1")
        before = sandbox._last_activity

        assert await manager.get_or_create("user-1") is sandbox
        assert sandbox._last_activity > before

    async def test_a_sandbox_without_any_timestamp_is_never_reaped(self):
        class NoActivity(TestLegacyActivityTracking.LegacySandbox):
            def __init__(self, session_id: str):
                super().__init__(session_id)
                del self._last_activity

        manager = SessionManager(sandbox_factory=NoActivity, default_idle_timeout=0)
        sandbox = await manager.get_or_create("user-1")

        assert await manager.cleanup_idle() == 0
        assert sandbox.stopped is False

    async def test_reuse_prefers_the_public_touch(self):
        class Modern(TestLegacyActivityTracking.LegacySandbox):
            def __init__(self, session_id: str):
                super().__init__(session_id)
                self.touched = 0

            def touch(self) -> None:
                self.touched += 1

        manager = SessionManager(sandbox_factory=Modern)
        sandbox = await manager.get_or_create("user-1")

        await manager.get_or_create("user-1")

        assert sandbox.touched == 1

    async def test_reuse_of_a_sandbox_that_tracks_nothing_is_a_no_op(self):
        class Untracked(TestLegacyActivityTracking.LegacySandbox):
            def __init__(self, session_id: str):
                super().__init__(session_id)
                del self._last_activity

        manager = SessionManager(sandbox_factory=Untracked)
        sandbox = await manager.get_or_create("user-1")

        assert await manager.get_or_create("user-1") is sandbox
        assert not hasattr(sandbox, "_last_activity")


class TestLifecycleCallsAreOffloaded:
    """Starting a container pulls an image; on the loop that stalls everyone."""

    class Blocking:
        """Records which thread its blocking calls ran on."""

        def __init__(self, session_id: str) -> None:
            self._id = session_id
            self.last_activity = time.time()
            self.start_thread: str | None = None
            self.stop_thread: str | None = None

        def start(self) -> None:
            self.start_thread = threading.current_thread().name

        def stop(self) -> None:
            self.stop_thread = threading.current_thread().name

        def is_alive(self) -> bool:
            return True

    async def test_start_and_stop_run_on_the_supplied_pool(self):
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="sandboxd") as pool:
            manager = SessionManager(
                sandbox_factory=self.Blocking, executor=pool, on_release=lambda _: None
            )
            sandbox = await manager.get_or_create("user-1")
            await manager.release("user-1")

        assert sandbox.start_thread is not None
        assert sandbox.start_thread.startswith("sandboxd")
        assert sandbox.stop_thread is not None
        assert sandbox.stop_thread.startswith("sandboxd")

    async def test_without_a_pool_the_default_one_is_used(self):
        """Still off the loop — just sharing asyncio's pool with everything else."""
        manager = SessionManager(sandbox_factory=self.Blocking)
        sandbox = await manager.get_or_create("user-1")

        assert sandbox.start_thread != threading.current_thread().name

    async def test_a_pool_assigned_after_construction_is_honoured(self):
        """A service builds its pool in lifespan startup, after its manager."""
        manager = SessionManager(sandbox_factory=self.Blocking)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="late") as pool:
            manager.executor = pool
            sandbox = await manager.get_or_create("user-1")

        assert sandbox.start_thread is not None
        assert sandbox.start_thread.startswith("late")

    async def test_a_failed_start_is_stopped_off_the_loop_too(self):
        class Broken(TestLifecycleCallsAreOffloaded.Blocking):
            def start(self) -> None:
                super().start()
                raise RuntimeError("no daemon")

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="sandboxd") as pool:
            manager = SessionManager(sandbox_factory=Broken, executor=pool)
            with pytest.raises(RuntimeError, match="no daemon"):
                await manager.get_or_create("user-1")

        assert manager.session_count == 0


class TestShutdownStopsSessionsConcurrently:
    """A full pool must not turn a shutdown into minutes of waiting."""

    class Slow:
        def __init__(self, session_id: str) -> None:
            self._id = session_id
            self.last_activity = time.time()
            self.stopped = False

        def start(self) -> None:
            pass

        def stop(self) -> None:
            time.sleep(0.05)
            self.stopped = True

        def is_alive(self) -> bool:
            return True

    async def test_stops_happen_in_parallel(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            manager = SessionManager(sandbox_factory=self.Slow, executor=pool)
            for index in range(6):
                await manager.get_or_create(f"s-{index}")

            started = time.monotonic()
            assert await manager.shutdown() == 6
            elapsed = time.monotonic() - started

        # Six 50 ms stops: concurrent lands well inside the 300 ms a sequential
        # teardown would need.
        assert elapsed < 0.2
        assert manager.session_count == 0

    async def test_one_uncooperative_sandbox_does_not_strand_the_others(self, caplog):
        """A shutdown has no later attempt, so it cannot stop at the first failure."""

        class Stubborn(TestShutdownStopsSessionsConcurrently.Slow):
            def stop(self) -> None:
                raise RuntimeError("wedged")

        def factory(session_id: str):
            return Stubborn(session_id) if session_id == "bad" else Slow_(session_id)

        Slow_ = TestShutdownStopsSessionsConcurrently.Slow
        manager = SessionManager(sandbox_factory=factory)
        good = await manager.get_or_create("good")
        await manager.get_or_create("bad")

        with caplog.at_level(logging.WARNING):
            assert await manager.shutdown() == 2

        assert good.stopped is True
        assert manager.session_count == 0
        assert "did not stop cleanly" in caplog.text
        assert "bad" in caplog.text


class AsyncSandbox:
    """A natively async sandbox, the shape `AsyncBaseSandbox` gives you."""

    def __init__(self, session_id: str) -> None:
        self._id = session_id
        self.last_activity = time.time()
        self.dead = False
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1

    async def is_alive(self) -> bool:
        return not self.dead


class TestAsyncSandboxLifecycle:
    """A coroutine handed to a thread never runs, and nothing says so."""

    async def test_start_is_awaited_not_thread_wrapped(self):
        manager = SessionManager(sandbox_factory=AsyncSandbox)

        sandbox = await manager.get_or_create("s1")

        assert sandbox.starts == 1

    async def test_liveness_is_awaited_not_taken_as_truthy(self):
        """A coroutine object is truthy, so a dead sandbox looked alive for ever."""
        manager = SessionManager(sandbox_factory=AsyncSandbox)
        first = await manager.get_or_create("s1")
        first.dead = True

        second = await manager.get_or_create("s1")

        assert second is not first

    async def test_a_sandbox_found_dead_is_stopped_before_being_dropped(self):
        """It still holds an SSH connection or an HTTP pool; dropping it leaks."""
        manager = SessionManager(sandbox_factory=AsyncSandbox)
        first = await manager.get_or_create("s1")
        first.dead = True

        await manager.get_or_create("s1")

        assert first.stops == 1

    async def test_a_failing_stop_on_a_dead_sandbox_is_ignored(self):
        """It is already dead, so its stop failing is expected, not fatal."""

        class Stubborn(AsyncSandbox):
            async def stop(self) -> None:
                raise OSError("transport already gone")

        manager = SessionManager(sandbox_factory=Stubborn)
        first = await manager.get_or_create("s1")
        first.dead = True

        replaced = await manager.get_or_create("s1")

        assert replaced is not first

    async def test_release_awaits_stop(self):
        manager = SessionManager(sandbox_factory=AsyncSandbox)
        sandbox = await manager.get_or_create("s1")

        assert await manager.release("s1") is True
        assert sandbox.stops == 1

    async def test_shutdown_awaits_every_stop(self):
        manager = SessionManager(sandbox_factory=AsyncSandbox)
        for index in range(3):
            await manager.get_or_create(f"s-{index}")
        held = [manager.sessions[f"s-{index}"] for index in range(3)]

        assert await manager.shutdown() == 3
        assert [sandbox.stops for sandbox in held] == [1, 1, 1]

    async def test_a_sync_sandbox_still_goes_to_a_thread(self):
        """The blocking path must not regress into running on the loop."""
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pooled") as pool:
            manager = SessionManager(
                sandbox_factory=TestLifecycleCallsAreOffloaded.Blocking, executor=pool
            )
            sandbox = await manager.get_or_create("s1")

        assert sandbox.start_thread is not None
        assert sandbox.start_thread.startswith("pooled")


class TestAliveOf:
    """The helper both the manager and the service resolve liveness with."""

    async def test_a_sync_sandbox_is_read_directly(self):
        from pydantic_ai_backends.backends.docker.session import alive_of

        class Sync:
            def is_alive(self) -> bool:
                return True

        assert await alive_of(Sync()) is True

    async def test_an_async_sandbox_is_awaited(self):
        from pydantic_ai_backends.backends.docker.session import alive_of

        class Async:
            async def is_alive(self) -> bool:
                return False

        assert await alive_of(Async()) is False

    async def test_a_truthy_non_bool_is_normalised(self):
        from pydantic_ai_backends.backends.docker.session import alive_of

        class Odd:
            def is_alive(self):
                return "running"

        assert await alive_of(Odd()) is True
