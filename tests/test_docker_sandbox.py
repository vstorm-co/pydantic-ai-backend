"""Tests for DockerSandbox initialization (without running Docker)."""

import sys
import types

import pytest


class _FakeContainer:
    """Minimal stand-in for a container `containers.run` returned."""


def _sandbox(**kwargs):
    """Build a DockerSandbox without touching the Docker daemon."""
    from pydantic_ai_backends import DockerSandbox

    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.__init__(**kwargs)
    return sandbox


class TestDockerSandboxInit:
    """Tests for DockerSandbox initialization parameters."""

    def test_init_default_values(self):
        """Test default initialization values."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        # Call __init__ manually to test parameter defaults
        sandbox.__init__()

        assert sandbox._image == "python:3.12-slim"
        assert sandbox._work_dir == "/workspace"
        assert sandbox._auto_remove is True
        assert sandbox._idle_timeout == 3600
        assert sandbox._volumes == {}
        assert sandbox._network_mode is None
        assert sandbox._runtime is None

    def test_init_with_volumes(self):
        """Test initialization with volumes parameter."""
        from pydantic_ai_backends import DockerSandbox

        volumes = {"/host/path": "/container/path"}
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(volumes=volumes)

        assert sandbox._volumes == volumes

    def test_init_with_empty_volumes(self):
        """Test initialization with empty volumes dict."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(volumes={})

        assert sandbox._volumes == {}

    def test_init_with_none_volumes(self):
        """Test initialization with None volumes (default)."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(volumes=None)

        assert sandbox._volumes == {}

    def test_init_with_multiple_volumes(self):
        """Test initialization with multiple volume mappings."""
        from pydantic_ai_backends import DockerSandbox

        volumes = {
            "/host/workspace": "/workspace",
            "/host/data": "/data",
            "/host/config": "/config",
        }
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(volumes=volumes)

        assert sandbox._volumes == volumes
        assert len(sandbox._volumes) == 3

    def test_init_with_all_parameters(self):
        """Test initialization with all parameters including volumes."""
        from pydantic_ai_backends import DockerSandbox

        volumes = {"/host/path": "/workspace"}
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(
            image="python:3.11",
            sandbox_id="test-sandbox",
            work_dir="/app",
            auto_remove=False,
            idle_timeout=7200,
            volumes=volumes,
            network_mode="none",
        )

        assert sandbox._image == "python:3.11"
        assert sandbox._id == "test-sandbox"
        assert sandbox._work_dir == "/app"
        assert sandbox._auto_remove is False
        assert sandbox._idle_timeout == 7200
        assert sandbox._volumes == volumes
        assert sandbox._network_mode == "none"

    def test_init_default_network_mode(self):
        """Test default network_mode is None."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__()

        assert sandbox._network_mode is None

    def test_init_with_network_mode(self):
        """Test initialization with network_mode parameter."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(network_mode="none")

        assert sandbox._network_mode == "none"

    def test_init_with_session_id_alias(self):
        """Test that session_id works as alias for sandbox_id."""
        from pydantic_ai_backends import DockerSandbox

        volumes = {"/host": "/container"}
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(session_id="my-session", volumes=volumes)

        assert sandbox._id == "my-session"
        assert sandbox._volumes == volumes


class TestDockerSandboxNetworkMode:
    """Tests for DockerSandbox network_mode parameter."""

    @pytest.mark.docker
    async def test_network_mode_none_disables_networking(self):
        """Test that network_mode='none' prevents network access."""
        pytest.importorskip("docker")
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox(network_mode="none")
        try:
            script = "import urllib.request; urllib.request.urlopen('http://example.com')"
            argv = ["python", "-c", script]
            result = await sandbox.run_command(argv, run_id="0" * 32, timeout=10)
            assert result.exit_code != 0
        finally:
            sandbox.stop()


class TestSharedDockerClient:
    """Tests for the process-wide Docker client (no Docker daemon needed)."""

    @pytest.fixture(autouse=True)
    def _reset_client(self, monkeypatch):
        """Clear the cached client so each test builds its own."""
        import pydantic_ai_backends.backends.docker._client as client_mod

        monkeypatch.setattr(client_mod, "_client", None)
        monkeypatch.setattr(client_mod, "_client_pid", None)

    def test_client_is_rebuilt_after_a_fork(self, monkeypatch):
        """Pooled sockets must not be shared across forked workers."""
        import pydantic_ai_backends.backends.docker._client as client_mod

        built: list[str] = []
        fake_docker = types.ModuleType("docker")
        fake_docker.from_env = lambda: built.append("client") or f"client-{len(built)}"
        monkeypatch.setitem(sys.modules, "docker", fake_docker)

        monkeypatch.setattr(client_mod.os, "getpid", lambda: 1000)
        parent = client_mod.docker_client()
        assert client_mod.docker_client() is parent

        # Same module state, new process: the cached client belongs to the parent.
        monkeypatch.setattr(client_mod.os, "getpid", lambda: 2000)
        child = client_mod.docker_client()

        assert child != parent
        assert len(built) == 2

    def test_client_is_built_once_and_reused(self, monkeypatch):
        """from_env() runs a blocking daemon handshake, so it must not repeat."""
        import pydantic_ai_backends.backends.docker._client as client_mod

        sentinel = object()
        calls: list[int] = []
        fake_docker = types.ModuleType("docker")
        fake_docker.from_env = lambda: (calls.append(1), sentinel)[1]
        monkeypatch.setitem(sys.modules, "docker", fake_docker)

        assert client_mod.docker_client() is sentinel
        assert client_mod.docker_client() is sentinel
        assert len(calls) == 1


@pytest.fixture
def fake_client(monkeypatch):
    """Install fake `docker` modules and a client that records `containers.run`."""
    import pydantic_ai_backends.backends.docker.sandbox as sandbox_mod

    fake_errors = types.ModuleType("docker.errors")
    fake_errors.NotFound = type("NotFound", (Exception,), {})
    fake_errors.ImageNotFound = type("ImageNotFound", (Exception,), {})
    fake_docker = types.ModuleType("docker")
    fake_docker.errors = fake_errors
    monkeypatch.setitem(sys.modules, "docker", fake_docker)
    monkeypatch.setitem(sys.modules, "docker.errors", fake_errors)

    class Containers:
        def __init__(self):
            self.image = None
            self.kwargs = None

        def run(self, image, **kwargs):
            self.image = image
            self.kwargs = kwargs
            return _FakeContainer()

    class Client:
        def __init__(self):
            self.containers = Containers()

    client = Client()
    monkeypatch.setattr(sandbox_mod, "docker_client", lambda: client)
    return client


class TestDockerSandboxResourceLimits:
    """Tests that limits and hardening reach `containers.run` (no daemon needed)."""

    def test_defaults_bound_processes_and_block_escalation(self, fake_client):
        """A default sandbox still caps PIDs and denies setuid escalation."""
        _sandbox()._ensure_container()

        kwargs = fake_client.containers.kwargs
        assert kwargs["pids_limit"] == 512
        assert kwargs["security_opt"] == ["no-new-privileges:true"]
        # Memory and CPU stay unlimited unless asked for, so existing
        # workloads are not silently throttled.
        assert "mem_limit" not in kwargs
        assert "nano_cpus" not in kwargs

    def test_memory_limit_pins_swap_to_the_same_value(self, fake_client):
        """An unmatched swap ceiling lets a capped container starve the host."""
        _sandbox(mem_limit="512m")._ensure_container()

        kwargs = fake_client.containers.kwargs
        assert kwargs["mem_limit"] == "512m"
        assert kwargs["memswap_limit"] == "512m"

    def test_every_container_gets_an_init_to_reap_with(self, fake_client):
        """`sleep` as PID 1 never waits, so an orphan is a zombie for good.

        Measured: ten orphaned children left ten permanent zombies, which
        accumulate against `pids_limit` until the session cannot fork.
        """
        _sandbox()._ensure_container()

        assert fake_client.containers.kwargs["init"] is True

    def test_git_is_configured_through_the_environment(self, fake_client):
        """Which is what reaches a ready-made image we never built.

        Without `safe.directory` every git command in a bind-mounted workspace
        fails with "detected dubious ownership", and without an identity a
        commit fails outright.
        """
        _sandbox()._ensure_container()
        env = fake_client.containers.kwargs["environment"]

        pairs = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert pairs["safe.directory"] == "*"
        assert pairs["user.email"]
        assert pairs["user.name"]

    def test_the_environment_keeps_output_readable_and_installs_bounded(self, fake_client):
        _sandbox()._ensure_container()
        env = fake_client.containers.kwargs["environment"]

        # A command killed by the timeout must still return what it printed.
        assert env["PYTHONUNBUFFERED"] == "1"
        # Escape sequences are tokens the model pays for and cannot read.
        assert env["NO_COLOR"] == "1"
        assert env["PAGER"] == "cat"
        # uv's parallelism is memory: uncapped it is OOM-killed at 128 MB where
        # pip survives, and capped at two it fits and stays 6.6x faster.
        assert env["UV_CONCURRENT_DOWNLOADS"] == "2"
        # node:20-slim ships no LANG, so a Node runtime would start in POSIX.
        assert env["LANG"] == "C.UTF-8"

    def test_a_runtime_may_override_any_of_it(self, fake_client):
        from pydantic_ai_backends.types import RuntimeConfig

        runtime = RuntimeConfig(name="loud", image="img", env_vars={"NO_COLOR": "0"})
        _sandbox(runtime=runtime)._ensure_container()
        env = fake_client.containers.kwargs["environment"]

        assert env["NO_COLOR"] == "0"
        # And the rest still arrives.
        assert env["PYTHONUNBUFFERED"] == "1"

    def test_a_wider_swap_ceiling_is_honoured(self, fake_client):
        """A host whose swap is zram can afford one; the default still cannot."""
        _sandbox(mem_limit="512m", memswap_limit="768m")._ensure_container()

        kwargs = fake_client.containers.kwargs
        assert kwargs["mem_limit"] == "512m"
        assert kwargs["memswap_limit"] == "768m"

    def test_a_swap_ceiling_without_a_memory_one_is_ignored(self, fake_client):
        """Docker rejects a swap ceiling with no memory ceiling under it."""
        _sandbox(memswap_limit="768m")._ensure_container()

        assert "memswap_limit" not in fake_client.containers.kwargs

    def test_cpu_limit_converts_cores_to_nano_cpus(self, fake_client):
        _sandbox(cpus=1.5)._ensure_container()

        assert fake_client.containers.kwargs["nano_cpus"] == 1_500_000_000

    def test_pids_limit_can_be_disabled(self, fake_client):
        _sandbox(pids_limit=None)._ensure_container()

        assert "pids_limit" not in fake_client.containers.kwargs

    def test_init_limit_defaults(self):
        sandbox = _sandbox()

        assert sandbox._mem_limit is None
        assert sandbox._cpus is None
        assert sandbox._pids_limit == 512


class TestDockerSandboxLiveness:
    """Tests for the cached liveness check (no Docker daemon needed)."""

    class _Container:
        def __init__(self, status: str = "running"):
            self.status = status
            self.reloads = 0
            self.stopped = 0
            self.removed = 0

        def reload(self) -> None:
            self.reloads += 1

        def stop(self) -> None:
            self.stopped += 1

        def remove(self, force: bool = False) -> None:
            self.removed += 1

    def test_no_container_is_not_alive(self):
        assert _sandbox().is_alive() is False

    def test_repeated_checks_hit_the_daemon_once(self):
        """SessionManager calls this per request; reload() is a round trip."""
        sandbox = _sandbox()
        container = self._Container()
        sandbox._container = container

        assert all(sandbox.is_alive() for _ in range(5))
        assert container.reloads == 1

    def test_cache_expires(self, monkeypatch):
        import pydantic_ai_backends.backends.docker.sandbox as sandbox_mod

        sandbox = _sandbox()
        container = self._Container()
        sandbox._container = container

        clock = [1000.0]
        monkeypatch.setattr(sandbox_mod.time, "monotonic", lambda: clock[0])

        assert sandbox.is_alive() is True
        clock[0] += sandbox_mod.ALIVE_CACHE_SECONDS + 0.1
        assert sandbox.is_alive() is True
        assert container.reloads == 2

    def test_non_running_status_is_not_alive(self):
        sandbox = _sandbox()
        sandbox._container = self._Container(status="exited")

        assert sandbox.is_alive() is False

    def test_reload_failure_is_not_alive_and_is_cached(self):
        class Failing(TestDockerSandboxLiveness._Container):
            def reload(self) -> None:
                self.reloads += 1
                raise RuntimeError("daemon gone")

        sandbox = _sandbox()
        container = Failing()
        sandbox._container = container

        assert sandbox.is_alive() is False
        assert sandbox.is_alive() is False
        assert container.reloads == 1

    def test_stop_clears_the_cached_answer(self):
        sandbox = _sandbox()
        sandbox._container = self._Container()

        assert sandbox.is_alive() is True
        sandbox.stop()

        assert sandbox._alive_checked_at is None
        assert sandbox.is_alive() is False


class TestDockerSandboxStop:
    """Tests for stop() and explicit container removal."""

    def test_stop_does_not_remove_by_default(self):
        """A named container is meant to survive — that is the point of naming it."""
        sandbox = _sandbox(container_name="reusable")
        container = TestDockerSandboxLiveness._Container()
        sandbox._container = container

        sandbox.stop()

        assert container.stopped == 1
        assert container.removed == 0

    def test_stop_with_purge_deletes_the_container(self):
        sandbox = _sandbox(container_name="reusable")
        container = TestDockerSandboxLiveness._Container()
        sandbox._container = container

        sandbox.stop(purge=True)

        assert container.stopped == 1
        assert container.removed == 1
        assert sandbox._container is None

    def test_garbage_collection_leaves_a_named_container_running(self):
        """A workspace drops its sandbox after every run while the container is still in use."""
        sandbox = _sandbox(container_name="pydantic-ai-workspace-0123456789abcdef")
        container = TestDockerSandboxLiveness._Container()
        sandbox._container = container

        sandbox.__del__()

        assert container.stopped == 0
        assert container.removed == 0

    def test_garbage_collection_stops_an_anonymous_container(self):
        sandbox = _sandbox()
        container = TestDockerSandboxLiveness._Container()
        sandbox._container = container

        sandbox.__del__()

        assert container.stopped == 1

    def test_stop_is_idempotent_and_never_raises(self):
        class Hostile(TestDockerSandboxLiveness._Container):
            def stop(self) -> None:
                raise RuntimeError("already gone")

            def remove(self, force: bool = False) -> None:
                raise RuntimeError("already gone")

        sandbox = _sandbox()
        sandbox._container = Hostile()

        sandbox.stop(purge=True)
        sandbox.stop(purge=True)

        assert sandbox._container is None

    def test_purge_is_what_every_other_sandbox_calls_it(self):
        """One signature across the sandboxes, which is the point of the rename.

        A caller holding "a sandbox" could not call `stop` without knowing which
        it had: this one took `remove`, the Kubernetes pod takes `purge`, and
        the old Daytona sandbox took nothing at all. Passing the wrong one raised a
        `TypeError` from inside a teardown that was already wrapped in a broad
        `except`, so the call that should have released the container was the one
        that failed - silently.
        """
        removed: list[bool] = []

        class Recording(TestDockerSandboxLiveness._Container):
            def remove(self, force: bool = False) -> None:
                removed.append(force)

        sandbox = _sandbox()
        sandbox._container = Recording()

        sandbox.stop(purge=True)

        assert removed == [True]

    def test_the_old_name_still_works_and_says_it_is_going(self):
        """`remove=` is honoured rather than broken: this is a patch release, and
        somebody's teardown is calling it."""
        removed: list[bool] = []

        class Recording(TestDockerSandboxLiveness._Container):
            def remove(self, force: bool = False) -> None:
                removed.append(force)

        sandbox = _sandbox()
        sandbox._container = Recording()

        with pytest.warns(DeprecationWarning, match="pass purge="):
            sandbox.stop(remove=True)

        assert removed == [True]

    def test_the_old_name_can_also_ask_for_the_container_to_be_kept(self):
        """`remove=False` is an explicit choice, not an absent one - so it must not
        read as "no opinion" and fall through to `purge`'s default."""
        removed: list[bool] = []

        class Recording(TestDockerSandboxLiveness._Container):
            def remove(self, force: bool = False) -> None:
                removed.append(force)

        sandbox = _sandbox()
        sandbox._container = Recording()

        with pytest.warns(DeprecationWarning):
            sandbox.stop(purge=True, remove=False)

        assert removed == []


class TestDockerSandboxResourceUsage:
    """Tests for resource_usage() and the stats parsing helpers."""

    def _stats(self, **overrides):
        stats = {
            "memory_stats": {"usage": 2048, "limit": 8192},
            "pids_stats": {"current": 11},
            "cpu_stats": {
                "cpu_usage": {"total_usage": 2_000},
                "system_cpu_usage": 12_000,
                "online_cpus": 4,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 1_000},
                "system_cpu_usage": 10_000,
            },
        }
        stats.update(overrides)
        return stats

    def _sandbox_with_stats(self, stats):
        class Container:
            def stats(self, stream=False):
                if isinstance(stats, Exception):
                    raise stats
                return stats

        sandbox = _sandbox()
        sandbox._container = Container()
        return sandbox

    def test_no_container_reports_no_usage(self):
        assert _sandbox().resource_usage() is None

    def test_usage_is_parsed_from_stats(self):
        usage = self._sandbox_with_stats(self._stats()).resource_usage()

        assert usage is not None
        assert usage.memory_bytes == 2048
        assert usage.memory_limit_bytes == 8192
        assert usage.pids == 11
        # 1000 / 2000 * 4 cores * 100
        assert usage.cpu_percent == pytest.approx(200.0)

    def test_cpu_defaults_to_one_core_when_not_reported(self):
        stats = self._stats()
        del stats["cpu_stats"]["online_cpus"]

        usage = self._sandbox_with_stats(stats).resource_usage()

        assert usage is not None
        assert usage.cpu_percent == pytest.approx(50.0)

    def test_cpu_is_none_without_a_previous_sample(self):
        """Docker reports totals, so the first sample has no rate to compute."""
        usage = self._sandbox_with_stats(self._stats(precpu_stats={})).resource_usage()

        assert usage is not None
        assert usage.cpu_percent is None
        assert usage.memory_bytes == 2048

    def test_cpu_is_none_when_the_system_counter_does_not_advance(self):
        stats = self._stats()
        stats["precpu_stats"]["system_cpu_usage"] = stats["cpu_stats"]["system_cpu_usage"]

        usage = self._sandbox_with_stats(stats).resource_usage()

        assert usage is not None
        assert usage.cpu_percent is None

    def test_missing_sections_yield_empty_usage(self):
        usage = self._sandbox_with_stats({}).resource_usage()

        assert usage is not None
        assert usage.memory_bytes is None
        assert usage.memory_limit_bytes is None
        assert usage.cpu_percent is None
        assert usage.pids is None

    def test_non_numeric_fields_are_ignored(self):
        usage = self._sandbox_with_stats(
            {"memory_stats": {"usage": "lots"}, "pids_stats": {"current": None}}
        ).resource_usage()

        assert usage is not None
        assert usage.memory_bytes is None
        assert usage.pids is None

    def test_stats_failure_reports_no_usage(self):
        assert self._sandbox_with_stats(RuntimeError("daemon gone")).resource_usage() is None

    def test_non_dict_stats_reports_no_usage(self):
        assert self._sandbox_with_stats(["unexpected"]).resource_usage() is None


class _StubContainer:
    """Container with a lifecycle and nothing in it."""

    def __init__(self, status: str = "running"):
        self.status = status
        self.name = "stub"
        self.attrs: dict = {"State": {"Running": status == "running"}}
        self.started = 0
        self.stopped = 0
        self.removed = False

    # lifecycle
    def start(self) -> None:
        self.started += 1
        self.status = "running"

    def stop(self) -> None:
        self.stopped += 1

    def remove(self, force: bool = False) -> None:
        self.removed = True

    def reload(self) -> None:
        pass


class _StubContainers:
    def __init__(self, existing: dict[str, _StubContainer] | None = None):
        self._existing = existing or {}
        self.runs: list[tuple[str, dict]] = []
        self.created = _StubContainer()

    def get(self, name: str):
        import docker.errors

        if name not in self._existing:
            raise docker.errors.NotFound(name)
        return self._existing[name]

    def run(self, image: str, **kwargs):
        self.runs.append((image, kwargs))
        return self.created


class _StubClient:
    def __init__(self, existing: dict[str, _StubContainer] | None = None):
        self.containers = _StubContainers(existing)


@pytest.fixture
def stub_docker(monkeypatch):
    """Patch the daemon and the image resolver out of the sandbox module."""
    from pydantic_ai_backends.backends.docker import sandbox as sandbox_mod

    client = _StubClient()
    monkeypatch.setattr(sandbox_mod, "docker_client", lambda: client)
    monkeypatch.setattr(sandbox_mod, "resolve_image", lambda *args: "resolved:image")
    return client


class TestContainerCreation:
    """`_ensure_container` is what every operation goes through first."""

    def test_a_container_is_created_once_and_reused(self, stub_docker):
        sandbox = _sandbox(image="python:3.12-slim")

        sandbox.start()
        sandbox.start()

        assert len(stub_docker.containers.runs) == 1
        assert stub_docker.containers.runs[0][0] == "resolved:image"

    def test_run_kwargs_carry_the_name_and_network(self, stub_docker):
        sandbox = _sandbox(container_name="pinned", network_mode="none")

        sandbox.start()

        _, kwargs = stub_docker.containers.runs[0]
        assert kwargs["name"] == "pinned"
        assert kwargs["network_mode"] == "none"
        # A named container exists to be reused, so it must not self-destruct.
        assert kwargs["auto_remove"] is False

    def test_a_runtime_named_as_a_string_is_looked_up(self, stub_docker):
        sandbox = _sandbox(runtime="python-datascience")

        assert sandbox._runtime is not None
        assert sandbox._runtime.name == "python-datascience"
        assert sandbox._work_dir == sandbox._runtime.work_dir

    def test_idle_timeout_is_exposed(self):
        assert _sandbox(idle_timeout=42).idle_timeout == 42


class TestReattach:
    """A named container is restarted rather than replaced."""

    def test_a_running_container_is_adopted(self, monkeypatch):
        from pydantic_ai_backends.backends.docker import sandbox as sandbox_mod

        existing = _StubContainer(status="running")
        client = _StubClient({"pinned": existing})
        monkeypatch.setattr(sandbox_mod, "docker_client", lambda: client)

        sandbox = _sandbox(container_name="pinned")
        sandbox.start()

        assert sandbox._container is existing
        assert client.containers.runs == []
        assert existing.started == 0

    @pytest.mark.parametrize("status", ["created", "exited", "paused"])
    def test_a_stopped_container_is_started_not_recreated(self, monkeypatch, status):
        """Recreating it would discard everything the session installed."""
        from pydantic_ai_backends.backends.docker import sandbox as sandbox_mod

        existing = _StubContainer(status=status)
        client = _StubClient({"pinned": existing})
        monkeypatch.setattr(sandbox_mod, "docker_client", lambda: client)

        sandbox = _sandbox(container_name="pinned")
        sandbox.start()

        assert sandbox._container is existing
        assert existing.started == 1
        assert client.containers.runs == []

    def test_a_dead_container_is_replaced(self, monkeypatch):
        from pydantic_ai_backends.backends.docker import sandbox as sandbox_mod

        client = _StubClient({"pinned": _StubContainer(status="dead")})
        monkeypatch.setattr(sandbox_mod, "docker_client", lambda: client)
        monkeypatch.setattr(sandbox_mod, "resolve_image", lambda *args: "img")

        sandbox = _sandbox(container_name="pinned")
        sandbox.start()

        assert len(client.containers.runs) == 1

    def test_an_absent_container_is_created(self, stub_docker):
        sandbox = _sandbox(container_name="missing")

        sandbox.start()

        assert len(stub_docker.containers.runs) == 1

    def test_an_unnamed_sandbox_never_reattaches(self, stub_docker):
        sandbox = _sandbox()

        assert sandbox._reattach(stub_docker) is None


class TestOciRuntimePassthrough:
    """Docker takes one low-level runtime per container; we have to pass it."""

    def test_it_reaches_containers_run(self, stub_docker):
        sandbox = _sandbox(oci_runtime="runsc")

        sandbox.start()

        _, kwargs = stub_docker.containers.runs[0]
        assert kwargs["runtime"] == "runsc"

    def test_it_is_absent_by_default(self, stub_docker):
        """Absent, not `None` — the daemon rejects an empty runtime name."""
        sandbox = _sandbox()

        sandbox.start()

        _, kwargs = stub_docker.containers.runs[0]
        assert "runtime" not in kwargs

    def test_it_composes_with_the_resource_ceilings(self, stub_docker):
        sandbox = _sandbox(oci_runtime="kata", mem_limit="1g", network_mode="none")

        sandbox.start()

        _, kwargs = stub_docker.containers.runs[0]
        assert kwargs["runtime"] == "kata"
        assert kwargs["mem_limit"] == "1g"
        assert kwargs["network_mode"] == "none"


class TestUnprivilegedContainers:
    """A runtime that names a uid is run as it, and told so consistently."""

    def _runtime(self, **kwargs):
        from pydantic_ai_backends.types import RuntimeConfig

        return RuntimeConfig(name="nr", image="img", **kwargs)

    def test_a_root_runtime_names_no_user(self, fake_client):
        _sandbox(runtime=self._runtime())._ensure_container()

        assert "user" not in fake_client.containers.kwargs

    def test_the_container_runs_as_the_uid_and_its_group(self, fake_client):
        """The gid matters too: a bind-mounted workspace is checked on both."""
        _sandbox(runtime=self._runtime(run_as_uid=1000))._ensure_container()

        assert fake_client.containers.kwargs["user"] == "1000:1000"

    def test_uv_is_not_aimed_at_the_interpreter_the_user_cannot_write_to(self, fake_client):
        """A container's environment overrides its image's, so this has to go.

        Measured: left set, uv ignores the virtualenv the image built and fails
        with `Permission denied` on the system `site-packages`.
        """
        _sandbox(runtime=self._runtime(run_as_uid=1000))._ensure_container()

        assert "UV_SYSTEM_PYTHON" not in fake_client.containers.kwargs["environment"]

    def test_a_root_runtime_keeps_it(self, fake_client):
        _sandbox(runtime=self._runtime())._ensure_container()

        assert fake_client.containers.kwargs["environment"]["UV_SYSTEM_PYTHON"] == "1"

    def test_a_sandbox_without_a_runtime_still_gets_the_defaults(self, fake_client):
        _sandbox()._ensure_container()
        env = fake_client.containers.kwargs["environment"]

        assert env["UV_SYSTEM_PYTHON"] == "1"
        assert "user" not in fake_client.containers.kwargs


class TestIdentityAndActivity:
    def test_ids_and_touch(self) -> None:
        sandbox = _sandbox(session_id="s1")
        assert sandbox.id == "s1"
        sandbox._last_activity = 0.0
        sandbox.touch()
        assert sandbox.last_activity > 0.0
        assert _sandbox().id
