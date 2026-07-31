"""Tests for DockerSandbox initialization (without running Docker)."""

import io
import sys
import tarfile
import time
import types

import pytest


def _tar_bytes(name: str, payload: bytes) -> bytes:
    """Build a single-member tar archive, as Docker's get_archive returns."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class _FakeArchiveStream:
    """Archive stream that records consumption and closure.

    Mirrors the generator `docker.Container.get_archive` returns so tests can
    assert that an oversized file is abandoned before its body is transferred.
    """

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.consumed = 0
        self.closed = False

    def __iter__(self):
        for chunk in self._chunks:
            self.consumed += len(chunk)
            yield chunk

    def close(self) -> None:
        self.closed = True


class _FakeContainer:
    """Minimal stand-in for a running Docker container."""

    def __init__(self, chunks: list[bytes] | None = None, stat: dict[str, int] | None = None):
        self.stream = _FakeArchiveStream(chunks or [])
        self.stat = stat

    def get_archive(self, path: str):
        return self.stream, self.stat


class _RecordingChardet:
    """chardet stub that records how many bytes each detect() call received."""

    def __init__(self, encoding: str = "utf-8", confidence: float = 0.99):
        self.seen_sizes: list[int] = []
        self._result = {"encoding": encoding, "confidence": confidence}

    def detect(self, data: bytes) -> dict[str, object]:
        self.seen_sizes.append(len(data))
        return self._result


def _sandbox(**kwargs):
    """Build a DockerSandbox without touching the Docker daemon."""
    from pydantic_ai_backends import DockerSandbox

    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.__init__(**kwargs)
    return sandbox


@pytest.fixture(scope="module")
def docker_sandbox():
    """Shared Docker sandbox for TestDockerSandboxEdit class.

    Reduces container creation from 3 times to 1 time.
    """
    pytest.importorskip("docker")
    from pydantic_ai_backends import DockerSandbox

    sandbox = DockerSandbox()
    yield sandbox
    sandbox.stop()


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


class TestDockerTimeoutEscaping:
    """Tests for timeout command escaping (fixes command!r bug).

    These tests verify that commands with quotes, variables, and pipes work
    correctly when timeout is specified. Previously failed due to command!r bug.
    """

    @pytest.mark.docker
    def test_execute_timeout_with_quotes(self, docker_sandbox):
        """Test execute with timeout handles quoted strings correctly.

        Previously failed because command!r added extra quotes:
        command = "echo 'hello world'"
        command!r = "'echo \\'hello world\\''"  # BAD - extra quotes
        """
        # Command with double quotes
        result = docker_sandbox.execute('echo "hello world"', timeout=5)
        assert result.exit_code == 0
        assert "hello world" in result.output

        # Command with single quotes
        result = docker_sandbox.execute("echo 'goodbye world'", timeout=5)
        assert result.exit_code == 0
        assert "goodbye world" in result.output

    @pytest.mark.docker
    def test_execute_timeout_with_variables(self, docker_sandbox):
        """Test execute with timeout handles shell variables correctly.

        Previously failed because command!r escaped $ incorrectly:
        command = "echo $HOME"
        command!r = "'echo $HOME'"  # $ gets escaped/not expanded
        """
        # Shell variable expansion
        result = docker_sandbox.execute("echo $HOME", timeout=5)
        assert result.exit_code == 0
        # HOME should be expanded (not literal "$HOME")
        assert "$HOME" not in result.output or result.output.strip() != "$HOME"

        # Command substitution
        result = docker_sandbox.execute("echo $(pwd)", timeout=5)
        assert result.exit_code == 0
        assert result.output.strip()  # Should output the working directory

    @pytest.mark.docker
    def test_execute_timeout_with_pipes(self, docker_sandbox):
        """Test execute with timeout handles pipes and redirects correctly.

        Previously failed because command!r broke shell piping:
        command = "echo test | grep test"
        command!r = "'echo test | grep test'"  # Pipe becomes literal string
        """
        # Pipe command
        result = docker_sandbox.execute("echo 'test line' | grep test", timeout=5)
        assert result.exit_code == 0
        assert "test line" in result.output

        # Multiple pipes
        result = docker_sandbox.execute("echo 'hello world' | tr a-z A-Z | grep HELLO", timeout=5)
        assert result.exit_code == 0
        assert "HELLO WORLD" in result.output


class TestDockerSandboxEdit:
    """Tests for DockerSandbox.edit() method using Python string operations."""

    @pytest.mark.docker
    def test_edit_basic_single_occurrence(self, docker_sandbox):
        """Test basic edit with single occurrence."""
        # Write a simple file
        docker_sandbox.write("/workspace/test1.txt", "Hello, World!")

        # Edit single occurrence
        result = docker_sandbox.edit("/workspace/test1.txt", "World", "Universe")

        assert result.error is None
        assert result.occurrences == 1

        # Verify the change
        content = docker_sandbox.read("/workspace/test1.txt")
        assert "Universe" in content
        assert "World" not in content

    @pytest.mark.docker
    def test_edit_multiline_string(self, docker_sandbox):
        """Test editing multiline strings (main improvement over sed approach)."""
        # Write file with multiline content
        original = "def foo():\n    return 'old'\n\nprint('test')"
        docker_sandbox.write("/workspace/code.py", original)

        # Edit multiline string (this would fail with sed approach)
        old_function = "def foo():\n    return 'old'"
        new_function = "def foo():\n    return 'new'"

        result = docker_sandbox.edit("/workspace/code.py", old_function, new_function)

        assert result.error is None
        assert result.occurrences == 1

        # Verify the multiline replacement worked
        content = docker_sandbox.read("/workspace/code.py")
        assert "return 'new'" in content
        assert "return 'old'" not in content
        assert "print('test')" in content  # Rest of file unchanged

    @pytest.mark.docker
    def test_edit_multiple_occurrences_replace_all(self, docker_sandbox):
        """Test editing with multiple occurrences using replace_all."""
        # Write file with multiple occurrences
        docker_sandbox.write("/workspace/multi.txt", "foo bar foo baz foo")

        # Should fail without replace_all
        result = docker_sandbox.edit("/workspace/multi.txt", "foo", "qux")
        assert result.error is not None
        assert "3 times" in result.error

        # Should succeed with replace_all=True
        result = docker_sandbox.edit("/workspace/multi.txt", "foo", "qux", replace_all=True)
        assert result.error is None
        assert result.occurrences == 3

        # Verify all occurrences replaced
        content = docker_sandbox.read("/workspace/multi.txt")
        assert "qux" in content
        assert "foo" not in content
        assert content.count("qux") == 3


class TestDockerSandboxGrepRaw:
    """Tests for BaseSandbox.grep_raw default path behaviour."""

    @pytest.mark.docker
    def test_grep_raw_finds_match_without_explicit_path(self, docker_sandbox):
        """grep_raw with no path searches the working directory, not /."""
        docker_sandbox.write("/workspace/grep_target.txt", "hello_unique_sentinel\n")
        result = docker_sandbox.grep_raw("hello_unique_sentinel", ignore_hidden=False)
        assert isinstance(result, list)
        assert any("grep_target.txt" in m["path"] for m in result)

    @pytest.mark.docker
    def test_grep_raw_no_path_is_fast(self, docker_sandbox):
        """grep_raw with no path completes quickly, proving it searches . not /.

        Searching / inside a Docker container takes minutes; searching the
        workspace directory takes milliseconds.
        """
        start = time.monotonic()
        result = docker_sandbox.grep_raw("this_string_will_never_exist_xyzzy_99999")
        elapsed = time.monotonic() - start
        assert result == []
        assert elapsed < 5, f"grep_raw took {elapsed:.1f}s — likely searched / instead of ."


class TestDockerSandboxResolvePath:
    """Tests for _resolve_path helper (no Docker required)."""

    def test_resolve_path_relative(self):
        """Relative paths are resolved against work_dir."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(work_dir="/workspace")

        assert sandbox._resolve_path("file.txt") == "/workspace/file.txt"

    def test_resolve_path_relative_nested(self):
        """Nested relative paths are resolved against work_dir."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(work_dir="/workspace")

        assert sandbox._resolve_path("subdir/file.txt") == "/workspace/subdir/file.txt"

    def test_resolve_path_absolute(self):
        """Absolute paths pass through unchanged."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(work_dir="/workspace")

        assert sandbox._resolve_path("/custom/dir/file.txt") == "/custom/dir/file.txt"

    def test_resolve_path_custom_work_dir(self):
        """Relative paths resolve against a custom work_dir."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(work_dir="/custom/workspace")

        assert sandbox._resolve_path("file.txt") == "/custom/workspace/file.txt"


class TestDockerSandboxFilePathResolution:
    """Tests for file operations with relative and absolute paths (requires Docker)."""

    @pytest.mark.docker
    def test_read_file_absolute_path(self, docker_sandbox):
        """Write to an absolute path and read it back with an absolute path."""
        docker_sandbox.write("/custom/dir/file.txt", "absolute content")
        content = docker_sandbox.read("/custom/dir/file.txt")
        assert "absolute content" in content

    @pytest.mark.docker
    def test_read_file_relative_path(self, docker_sandbox):
        """Write to work_dir and read with a relative path."""
        docker_sandbox.write("/workspace/rel_test.txt", "relative content")
        content = docker_sandbox.read("rel_test.txt")
        assert "relative content" in content

    @pytest.mark.docker
    def test_write_file_relative_path(self, docker_sandbox):
        """Write using a relative path and read back with absolute path."""
        docker_sandbox.write("rel_write.txt", "written relatively")
        content = docker_sandbox.read("/workspace/rel_write.txt")
        assert "written relatively" in content

    @pytest.mark.docker
    def test_edit_file_relative_path(self, docker_sandbox):
        """Edit a file using a relative path."""
        docker_sandbox.write("edit_rel.txt", "old value")
        result = docker_sandbox.edit("edit_rel.txt", "old value", "new value")
        assert result.error is None
        content = docker_sandbox.read("edit_rel.txt")
        assert "new value" in content

    @pytest.mark.docker
    def test_read_file_nonexistent_path(self, docker_sandbox):
        """Reading a non-existent file returns an error string, not a crash."""
        content = docker_sandbox.read("/workspace/does_not_exist.txt")
        assert "Error" in content
        assert "not found" in content

    @pytest.mark.docker
    def test_read_bytes_nonexistent_path(self, docker_sandbox):
        """read_bytes on a non-existent path returns empty bytes."""
        result = docker_sandbox.read_bytes("/workspace/no_such_file.bin")
        assert result == b""

    @pytest.mark.docker
    def test_exists_returns_true_for_existing_file(self, docker_sandbox):
        """exists() returns True for a file written into the container."""
        docker_sandbox.write("/workspace/exists_check.txt", "hi")
        assert docker_sandbox.exists("/workspace/exists_check.txt") is True

    @pytest.mark.docker
    def test_exists_returns_false_for_missing_file(self, docker_sandbox):
        """exists() returns False for an unwritten path inside the container."""
        assert docker_sandbox.exists("/workspace/never_written.txt") is False

    @pytest.mark.docker
    def test_exists_returns_false_for_directory(self, docker_sandbox):
        """exists() returns False for directories — `test -f` fails on dirs."""
        # /workspace is the work_dir, guaranteed to be a directory.
        assert docker_sandbox.exists("/workspace") is False


class TestDockerSandboxNetworkMode:
    """Tests for DockerSandbox network_mode parameter."""

    @pytest.mark.docker
    def test_network_mode_none_disables_networking(self):
        """Test that network_mode='none' prevents network access."""
        pytest.importorskip("docker")
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox(network_mode="none")
        try:
            result = sandbox.execute(
                "python -c \"import urllib.request; urllib.request.urlopen('http://example.com')\"",
                timeout=10,
            )
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


class TestDockerSandboxResourceLimits:
    """Tests that limits and hardening reach `containers.run` (no daemon needed)."""

    @pytest.fixture
    def fake_client(self, monkeypatch):
        """Install fake `docker` modules and a recording client."""
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
        assert sandbox._max_read_bytes == 8 * 1024 * 1024


class TestDockerSandboxReadLimits:
    """Tests for the bounded file-fetch path (no Docker daemon needed)."""

    def test_read_bytes_extracts_archive_member(self):
        """The happy path still returns file content after the buffer rework."""
        sandbox = _sandbox()
        payload = b"hello sandbox"
        sandbox._container = _FakeContainer([_tar_bytes("f.txt", payload)])

        assert sandbox.read_bytes("/workspace/f.txt") == payload

    def test_read_bytes_empty_when_archive_holds_no_regular_file(self):
        """A directory-only archive yields empty bytes, not a crash."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo(name="subdir")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)

        sandbox = _sandbox()
        sandbox._container = _FakeContainer([buffer.getvalue()])

        assert sandbox.read_bytes("/workspace/subdir") == b""

    def test_read_bytes_empty_when_archive_is_corrupt(self):
        sandbox = _sandbox()
        sandbox._container = _FakeContainer([b"not a tar archive"])

        assert sandbox.read_bytes("/workspace/f.txt") == b""

    def test_read_bytes_empty_when_get_archive_fails(self):
        class Failing:
            def get_archive(self, path):
                raise RuntimeError("no such container")

        sandbox = _sandbox()
        sandbox._container = Failing()

        assert sandbox.read_bytes("/workspace/missing.txt") == b""

    def test_oversized_file_is_refused_before_its_body_transfers(self):
        """The size header lets us reject without paying for the download."""
        sandbox = _sandbox(max_read_bytes=1024)
        container = _FakeContainer([b"x" * 4096], stat={"size": 10_000})
        sandbox._container = container

        message = sandbox.read("/workspace/big.log")

        assert "over the 1024-byte read limit" in message
        assert "10000 bytes" in message
        assert container.stream.consumed == 0
        assert container.stream.closed is True

    def test_oversized_file_refused_while_streaming_without_size_header(self):
        """Daemons that omit the header must still not blow up the host."""
        sandbox = _sandbox(max_read_bytes=1024)
        container = _FakeContainer([b"x" * 800] * 4, stat=None)
        sandbox._container = container

        message = sandbox.read("/workspace/big.log")

        assert "exceeds the 1024-byte read limit" in message
        # Stopped as soon as the cap was crossed rather than draining it all.
        assert container.stream.consumed == 1600
        assert container.stream.closed is True

    def test_read_bytes_returns_empty_for_oversized_file(self):
        """`read_bytes` keeps its documented empty-bytes contract."""
        sandbox = _sandbox(max_read_bytes=1024)
        sandbox._container = _FakeContainer([b"x" * 4096], stat={"size": 10_000})

        assert sandbox.read_bytes("/workspace/big.log") == b""

    def test_edit_reports_the_read_limit(self):
        """Edit surfaces the limit instead of claiming the file is missing."""
        sandbox = _sandbox(max_read_bytes=1024)
        sandbox._container = _FakeContainer([b"x" * 4096], stat={"size": 10_000})

        result = sandbox.edit("/workspace/big.log", "a", "b")

        assert result.error is not None
        assert "read limit" in result.error
        assert "not found" not in result.error


class TestDockerSandboxExecuteOutputCap:
    """Tests for the byte-bounded execute() output."""

    def _sandbox_with_output(self, output: bytes):
        class Container:
            def exec_run(self, cmd, workdir=None):
                return 0, output

        sandbox = _sandbox()
        sandbox._container = Container()
        return sandbox

    def test_output_under_cap_is_returned_whole(self):
        result = self._sandbox_with_output(b"hello").execute("echo hello")

        assert result.output == "hello"
        assert result.truncated is False

    def test_output_over_cap_is_truncated_to_the_byte_limit(self):
        from pydantic_ai_backends import _limits

        cap = _limits.MAX_EXECUTE_OUTPUT_BYTES
        result = self._sandbox_with_output(b"y" * (cap + 500)).execute("cat big.log")

        assert result.truncated is True
        assert len(result.output) == cap


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

    def test_stop_with_remove_deletes_the_container(self):
        sandbox = _sandbox(container_name="reusable")
        container = TestDockerSandboxLiveness._Container()
        sandbox._container = container

        sandbox.stop(remove=True)

        assert container.stopped == 1
        assert container.removed == 1
        assert sandbox._container is None

    def test_stop_is_idempotent_and_never_raises(self):
        class Hostile(TestDockerSandboxLiveness._Container):
            def stop(self) -> None:
                raise RuntimeError("already gone")

            def remove(self, force: bool = False) -> None:
                raise RuntimeError("already gone")

        sandbox = _sandbox()
        sandbox._container = Hostile()

        sandbox.stop(remove=True)
        sandbox.stop(remove=True)

        assert sandbox._container is None


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


class TestDockerFileTransfer:
    """Tests for the tar helpers behind write()/read_bytes() (no daemon needed)."""

    def test_archive_round_trips_content(self):
        from pydantic_ai_backends.backends.docker.sandbox import (
            _extract_single_file,
            _single_file_archive,
        )

        archive = _single_file_archive("app.py", b"print('hi')")

        assert _extract_single_file(archive) == b"print('hi')"

    def test_archive_entry_is_named_and_mode_0644(self):
        from pydantic_ai_backends.backends.docker.sandbox import _single_file_archive

        archive = _single_file_archive("app.py", b"body")
        with tarfile.open(fileobj=archive, mode="r") as tar:
            (entry,) = tar.getmembers()

        assert entry.name == "app.py"
        assert entry.mode == 0o644
        assert entry.size == 4

    def test_extract_ignores_an_archive_without_regular_files(self):
        from pydantic_ai_backends.backends.docker.sandbox import _extract_single_file

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            tar.addfile(tarfile.TarInfo(name="adir"))
        buffer.seek(0)

        assert _extract_single_file(buffer) == b""

    def test_extract_tolerates_a_corrupt_stream(self):
        from pydantic_ai_backends.backends.docker.sandbox import _extract_single_file

        assert _extract_single_file(io.BytesIO(b"not a tar")) == b""


# ---------------------------------------------------------------------------
# A fake daemon, so the file/command paths are exercised without real Docker.
# Previously these were reachable only through `@pytest.mark.docker` tests,
# which CI deselects — the class carried `# pragma: no cover` and the most
# security-sensitive code in the package went unmeasured.
# ---------------------------------------------------------------------------


class _StubContainer:
    """Container backed by an in-memory filesystem."""

    def __init__(self, status: str = "running", files: dict[str, bytes] | None = None):
        self.status = status
        self.files: dict[str, bytes] = dict(files or {})
        self.name = "stub"
        self.attrs: dict = {"State": {"Running": status == "running"}}
        self.started = 0
        self.stopped = 0
        self.removed = False
        self.commands: list[list[str]] = []
        self.exec_result: tuple[int, object] | None = None
        self.exec_error: Exception | None = None
        self.put_archive_ok = True
        self.puts: list[tuple[str, bytes]] = []

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

    # commands
    def exec_run(self, argv: list[str], workdir: str | None = None):
        self.commands.append(argv)
        if self.exec_error is not None:
            raise self.exec_error
        if self.exec_result is not None:
            return self.exec_result
        return 0, b""

    # files
    def get_archive(self, path: str):
        if path not in self.files:
            raise KeyError(path)
        payload = self.files[path]
        name = path.rsplit("/", 1)[-1]
        blob = _tar_bytes(name, payload)

        def stream():
            # docker-py streams the archive from a generator, which is what
            # makes closing it early on an oversized file possible at all.
            yield blob

        return stream(), {"size": len(payload)}

    def put_archive(self, parent: str, archive: io.BytesIO) -> bool:
        # The real SDK is handed a file object, not bytes.
        blob = archive.getvalue()
        self.puts.append((parent, blob))
        if not self.put_archive_ok:
            return False
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            for member in tar.getmembers():
                handle = tar.extractfile(member)
                assert handle is not None
                self.files[f"{parent.rstrip('/')}/{member.name}"] = handle.read()
        return True


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


class TestExecuteAgainstAStub:
    """The command path, including how a failing exec is reported."""

    def test_output_and_exit_code_are_returned(self, stub_docker):
        sandbox = _sandbox()
        sandbox.start()
        sandbox._container.exec_result = (0, b"hello\n")

        result = sandbox.execute("echo hello")

        assert result.output == "hello\n"
        assert result.exit_code == 0
        assert sandbox._container.commands[-1] == ["sh", "-c", "echo hello"]

    def test_a_timeout_wraps_the_command(self, stub_docker):
        sandbox = _sandbox()
        sandbox.start()

        sandbox.execute("sleep 99", timeout=5)

        assert sandbox._container.commands[-1][:3] == ["timeout", "5", "sh"]

    def test_a_chunked_output_stream_is_joined(self, stub_docker):
        """The SDK returns an iterator when the exec is not demuxed."""
        sandbox = _sandbox()
        sandbox.start()
        sandbox._container.exec_result = (0, iter([b"one ", b"two"]))

        assert sandbox.execute("x").output == "one two"

    def test_a_daemon_failure_is_reported_not_raised(self, stub_docker):
        sandbox = _sandbox()
        sandbox.start()
        sandbox._container.exec_error = RuntimeError("daemon gone")

        result = sandbox.execute("x")

        assert result.exit_code == 1
        assert "daemon gone" in result.output


class TestReadWriteEditAgainstAStub:
    """The file operations, which never touched the fake daemon before."""

    def test_write_then_read_round_trips(self, stub_docker):
        sandbox = _sandbox()

        written = sandbox.write("notes.md", "line one\nline two\n")

        assert written.error is None
        assert written.path == "/workspace/notes.md"
        assert sandbox.read("notes.md") == "line one\nline two"

    def test_write_creates_the_parent_directory(self, stub_docker):
        sandbox = _sandbox()

        sandbox.write("deep/nested/file.txt", "x")

        assert any("mkdir -p" in " ".join(argv) for argv in sandbox._container.commands)

    def test_a_failed_mkdir_is_reported(self, stub_docker):
        sandbox = _sandbox()
        sandbox.start()
        sandbox._container.exec_result = (1, b"permission denied")

        result = sandbox.write("x.txt", "y")

        assert result.error is not None
        assert "Failed to create directory" in result.error

    def test_a_refused_upload_is_reported(self, stub_docker):
        sandbox = _sandbox()
        sandbox.start()
        sandbox._container.put_archive_ok = False

        result = sandbox.write("x.txt", "y")

        assert result.error is not None
        assert "put_archive" in result.error

    def test_bytes_content_is_written_verbatim(self, stub_docker):
        sandbox = _sandbox()

        sandbox.write("blob.bin", b"\x00\x01\x02")

        assert sandbox._container.files["/workspace/blob.bin"] == b"\x00\x01\x02"

    def test_reading_a_missing_file_says_so(self, stub_docker):
        sandbox = _sandbox()

        assert "not found" in sandbox.read("nope.txt")

    def test_reading_past_the_end_says_so(self, stub_docker):
        sandbox = _sandbox()
        sandbox.write("short.txt", "one\n")

        assert sandbox.read("short.txt", offset=50) == "[End of file]"

    def test_a_truncated_read_advertises_the_next_offset(self, stub_docker):
        sandbox = _sandbox()
        sandbox.write("many.txt", "\n".join(str(n) for n in range(10)))

        out = sandbox.read("many.txt", offset=0, limit=4)

        assert out.startswith("0\n1\n2\n3")
        assert "offset=4" in out

    def test_undecodable_content_is_reported(self, stub_docker, monkeypatch):
        from pydantic_ai_backends.backends.docker import sandbox as sandbox_mod

        sandbox = _sandbox()
        sandbox.write("weird.bin", b"\xff\xfe")

        def refuse(extension: str, data: bytes) -> str:
            raise ValueError("no readable text")

        monkeypatch.setattr(sandbox_mod, "bytes_to_text", refuse)

        assert "no readable text" in sandbox.read("weird.bin")
        assert "no readable text" in str(sandbox.edit("weird.bin", "a", "b").error)

    def test_edit_replaces_and_writes_back(self, stub_docker):
        sandbox = _sandbox()
        sandbox.write("code.py", "print('a')\n")

        result = sandbox.edit("code.py", "'a'", "'b'")

        assert result.error is None
        assert result.occurrences == 1
        assert sandbox.read("code.py") == "print('b')"

    def test_edit_of_a_missing_file_says_so(self, stub_docker):
        sandbox = _sandbox()

        assert "not found" in str(sandbox.edit("nope.txt", "a", "b").error)

    def test_edit_reports_a_string_that_is_not_there(self, stub_docker):
        sandbox = _sandbox()
        sandbox.write("code.py", "print('a')\n")

        assert sandbox.edit("code.py", "absent", "x").error is not None

    def test_edit_reports_a_failed_write_back(self, stub_docker):
        sandbox = _sandbox()
        sandbox.write("code.py", "print('a')\n")
        sandbox._container.put_archive_ok = False

        assert "put_archive" in str(sandbox.edit("code.py", "'a'", "'b'").error)

    def test_an_oversized_file_is_refused_by_read_and_edit(self, stub_docker):
        sandbox = _sandbox(max_read_bytes=8)
        sandbox.start()
        sandbox._container.files["/workspace/big.txt"] = b"x" * 64

        assert "read limit" in sandbox.read("big.txt")
        assert "read limit" in str(sandbox.edit("big.txt", "x", "y").error)

    def test_an_unexpected_failure_is_wrapped(self, stub_docker, monkeypatch):
        sandbox = _sandbox()
        sandbox.start()

        def explode(path: str) -> bytes:
            raise RuntimeError("socket reset")

        monkeypatch.setattr(sandbox, "_fetch_file_bytes", explode)

        assert "socket reset" in sandbox.read("x.txt")
        assert "socket reset" in str(sandbox.edit("x.txt", "a", "b").error)

    def test_a_write_failure_is_wrapped(self, stub_docker, monkeypatch):
        sandbox = _sandbox()
        sandbox.start()

        def explode(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(sandbox._container, "put_archive", explode)

        assert "disk full" in str(sandbox.write("x.txt", "y").error)

    def test_a_stream_that_fails_mid_transfer_reads_as_absent(self, stub_docker):
        """A socket dropping halfway is a missing file, not an exception."""
        sandbox = _sandbox()
        sandbox.start()

        def broken_get_archive(path: str):
            def stream():
                yield b"partial"
                raise OSError("connection reset")

            return stream(), None

        sandbox._container.get_archive = broken_get_archive

        assert "not found" in sandbox.read("x.txt")
        assert sandbox.read_bytes("x.txt") == b""


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
