"""Tests for DaytonaSandbox with fully mocked Daytona SDK."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fake Daytona SDK objects
# ---------------------------------------------------------------------------


@dataclass
class FakeExecResult:
    result: str = ""
    exit_code: int = 0


@dataclass
class FakeDownloadResponse:
    source: str = ""
    result: str | bytes = b""


@dataclass
class FakeProcess:
    """Stub for sandbox.process."""

    _exec_results: list[FakeExecResult] = field(default_factory=list)
    _call_index: int = 0

    def exec(self, command: str, **kwargs: object) -> FakeExecResult:
        if self._exec_results:
            idx = min(self._call_index, len(self._exec_results) - 1)
            self._call_index += 1
            return self._exec_results[idx]
        return FakeExecResult(result="", exit_code=0)


@dataclass
class FakeFileInfo:
    """Stub mirroring daytona-sdk's FileInfo."""

    name: str = ""
    is_dir: bool = False
    size: int = 0


@dataclass
class FakeFs:
    """Stub for sandbox.fs."""

    _download_responses: list[FakeDownloadResponse] = field(default_factory=list)
    _uploaded: list[object] = field(default_factory=list)
    _file_infos: dict[str, FakeFileInfo] = field(default_factory=dict)

    def download_files(self, requests: list[object]) -> list[FakeDownloadResponse]:
        return self._download_responses

    def upload_files(self, uploads: list[object]) -> None:
        self._uploaded.extend(uploads)

    def get_file_info(self, path: str) -> FakeFileInfo:
        if path not in self._file_infos:
            raise FileNotFoundError(path)
        return self._file_infos[path]


@dataclass
class FakeSandbox:
    id: str = "sandbox-123"
    process: FakeProcess = field(default_factory=FakeProcess)
    fs: FakeFs = field(default_factory=FakeFs)


class FakeDaytonaConfig:
    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key


class FakeDaytona:
    def __init__(self, config: object | None = None) -> None:
        self.config = config
        self._sandbox = FakeSandbox()

    def create(self) -> FakeSandbox:
        return self._sandbox

    def delete(self, sandbox: object) -> None:
        pass


class FakeFileDownloadRequest:
    def __init__(self, source: str = "") -> None:
        self.source = source


class FakeFileUpload:
    def __init__(self, source: bytes = b"", destination: str = "") -> None:
        self.source = source
        self.destination = destination


# ---------------------------------------------------------------------------
# Fixture — keeps mock daytona module alive for entire test
# ---------------------------------------------------------------------------

_FAKE_DAYTONA_MODULE = MagicMock(
    Daytona=FakeDaytona,
    DaytonaConfig=FakeDaytonaConfig,
    FileDownloadRequest=FakeFileDownloadRequest,
    FileUpload=FakeFileUpload,
)


@pytest.fixture(autouse=True)
def _mock_daytona(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake 'daytona' module into sys.modules for every test."""
    monkeypatch.setitem(sys.modules, "daytona", _FAKE_DAYTONA_MODULE)


def _make_sandbox(**kwargs: Any) -> Any:
    """Create a DaytonaSandbox with the fake SDK (module already mocked)."""
    from pydantic_ai_backends.backends.daytona import DaytonaSandbox

    return DaytonaSandbox(api_key="test-key", **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDaytonaSandboxInit:
    def test_creates_sandbox_with_api_key(self) -> None:
        sandbox = _make_sandbox()
        assert sandbox.id == "sandbox-123"

    def test_default_work_dir(self) -> None:
        sandbox = _make_sandbox()
        assert sandbox._work_dir == "/home/daytona"

    def test_custom_work_dir(self) -> None:
        sandbox = _make_sandbox(work_dir="/custom/dir")
        assert sandbox._work_dir == "/custom/dir"

    def test_missing_api_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            from pydantic_ai_backends.backends.daytona import DaytonaSandbox

            with pytest.raises(ValueError, match="API key is required"):
                DaytonaSandbox()

    def test_api_key_from_env(self) -> None:
        with patch.dict("os.environ", {"DAYTONA_API_KEY": "env-key"}):
            from pydantic_ai_backends.backends.daytona import DaytonaSandbox

            sandbox = DaytonaSandbox()
            assert sandbox.id == "sandbox-123"

    def test_startup_timeout(self) -> None:
        """Sandbox that never becomes ready should raise RuntimeError."""
        from pydantic_ai_backends.backends.daytona import DaytonaSandbox

        # Make a Daytona that returns a sandbox whose exec always fails
        class FailingDaytona(FakeDaytona):
            def create(self) -> FakeSandbox:
                return FakeSandbox(
                    process=FakeProcess(_exec_results=[FakeExecResult(exit_code=1)]),
                )

        with (
            patch.object(_FAKE_DAYTONA_MODULE, "Daytona", FailingDaytona),
            pytest.raises(RuntimeError, match="failed to start"),
        ):
            DaytonaSandbox(api_key="key", startup_timeout=1)


class TestDaytonaSandboxExecute:
    def test_basic_command(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.process._exec_results = [FakeExecResult(result="hello world", exit_code=0)]
        sandbox._sandbox.process._call_index = 0

        result = sandbox.execute("echo hello world")
        assert result.output == "hello world"
        assert result.exit_code == 0
        assert result.truncated is False

    def test_command_failure(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.process._exec_results = [FakeExecResult(result="not found", exit_code=127)]
        sandbox._sandbox.process._call_index = 0

        result = sandbox.execute("nonexistent")
        assert result.exit_code == 127

    def test_output_truncation(self) -> None:
        sandbox = _make_sandbox()
        long_output = "x" * 200_000
        sandbox._sandbox.process._exec_results = [FakeExecResult(result=long_output, exit_code=0)]
        sandbox._sandbox.process._call_index = 0

        result = sandbox.execute("cat bigfile")
        assert result.truncated is True
        assert len(result.output) == 100_000

    def test_execute_exception(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.process.exec = MagicMock(side_effect=RuntimeError("boom"))

        result = sandbox.execute("fail")
        assert result.exit_code == 1
        assert "boom" in result.output

    def test_default_timeout(self) -> None:
        sandbox = _make_sandbox()
        mock_exec = MagicMock(return_value=FakeExecResult())
        sandbox._sandbox.process.exec = mock_exec

        sandbox.execute("echo test")
        _, kwargs = mock_exec.call_args
        assert kwargs["timeout"] == 30 * 60

    def test_custom_timeout(self) -> None:
        sandbox = _make_sandbox()
        mock_exec = MagicMock(return_value=FakeExecResult())
        sandbox._sandbox.process.exec = mock_exec

        sandbox.execute("echo test", timeout=42)
        _, kwargs = mock_exec.call_args
        assert kwargs["timeout"] == 42


class TestDaytonaSandboxReadBytes:
    def test_read_bytes_string(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.fs._download_responses = [
            FakeDownloadResponse(source="/test.txt", result="file content")
        ]

        data = sandbox.read_bytes("/test.txt")
        assert data == b"file content"

    def test_read_bytes_binary(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.fs._download_responses = [
            FakeDownloadResponse(source="/img.bin", result=b"\x89PNG")
        ]

        data = sandbox.read_bytes("/img.bin")
        assert data == b"\x89PNG"

    def test_read_bytes_error(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.fs.download_files = MagicMock(side_effect=RuntimeError("download failed"))

        # On failure read_bytes returns empty bytes (not an error sentinel)
        # so callers cannot mistake an error message for file content.
        data = sandbox.read_bytes("/missing.txt")
        assert data == b""


class TestDaytonaSandboxExists:
    def test_exists_returns_true_for_existing_file(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.fs._file_infos = {
            "/foo.txt": FakeFileInfo(name="foo.txt", is_dir=False, size=7)
        }
        assert sandbox.exists("/foo.txt") is True

    def test_exists_returns_false_for_missing_file(self) -> None:
        sandbox = _make_sandbox()
        # Default _file_infos is empty → get_file_info raises FileNotFoundError.
        assert sandbox.exists("/missing.txt") is False

    def test_exists_returns_false_for_invalid_path(self) -> None:
        """API failures (e.g. parent rejecting a bad path) are swallowed → False."""
        sandbox = _make_sandbox()
        sandbox._sandbox.fs.get_file_info = MagicMock(side_effect=ValueError("bad path"))
        assert sandbox.exists("../escape.txt") is False

    def test_exists_returns_false_for_directory(self) -> None:
        """A directory is not a file — the contract returns False."""
        sandbox = _make_sandbox()
        sandbox._sandbox.fs._file_infos = {
            "/home/daytona": FakeFileInfo(name="daytona", is_dir=True, size=0)
        }
        assert sandbox.exists("/home/daytona") is False


class TestDaytonaSandboxWrite:
    def test_write_string(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.process._exec_results = [FakeExecResult()]
        sandbox._sandbox.process._call_index = 0

        result = sandbox.write("/test.txt", "hello")
        assert result.path == "/test.txt"
        assert result.error is None
        assert len(sandbox._sandbox.fs._uploaded) == 1

    def test_write_bytes(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.process._exec_results = [FakeExecResult()]
        sandbox._sandbox.process._call_index = 0

        result = sandbox.write("/test.bin", b"\x00\x01")
        assert result.path == "/test.bin"
        assert result.error is None

    def test_write_error(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.process._exec_results = [FakeExecResult()]
        sandbox._sandbox.process._call_index = 0
        sandbox._sandbox.fs.upload_files = MagicMock(side_effect=RuntimeError("upload failed"))

        result = sandbox.write("/test.txt", "content")
        assert result.error is not None
        assert "upload failed" in result.error


class TestDaytonaSandboxEdit:
    def test_edit_single_occurrence(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.fs._file_infos = {"/f.py": FakeFileInfo(name="f.py", is_dir=False, size=7)}
        sandbox._sandbox.fs._download_responses = [
            FakeDownloadResponse(source="/f.py", result="foo = 1")
        ]
        sandbox._sandbox.process._exec_results = [FakeExecResult()]
        sandbox._sandbox.process._call_index = 0

        result = sandbox.edit("/f.py", "foo", "bar")
        assert result.path == "/f.py"
        assert result.occurrences == 1

    def test_edit_not_found(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.fs._file_infos = {"/f.py": FakeFileInfo(name="f.py", is_dir=False, size=7)}
        sandbox._sandbox.fs._download_responses = [
            FakeDownloadResponse(source="/f.py", result="foo = 1")
        ]

        result = sandbox.edit("/f.py", "baz", "qux")
        assert result.error == "String 'baz' not found in file"

    def test_edit_multiple_without_replace_all(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.fs._file_infos = {
            "/f.py": FakeFileInfo(name="f.py", is_dir=False, size=11)
        }
        sandbox._sandbox.fs._download_responses = [
            FakeDownloadResponse(source="/f.py", result="foo foo foo")
        ]

        result = sandbox.edit("/f.py", "foo", "bar")
        assert result.error is not None
        assert "3 times" in result.error

    def test_edit_replace_all(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.fs._file_infos = {"/f.py": FakeFileInfo(name="f.py", is_dir=False, size=7)}
        sandbox._sandbox.fs._download_responses = [
            FakeDownloadResponse(source="/f.py", result="foo foo")
        ]
        sandbox._sandbox.process._exec_results = [FakeExecResult()]
        sandbox._sandbox.process._call_index = 0

        result = sandbox.edit("/f.py", "foo", "bar", replace_all=True)
        assert result.path == "/f.py"
        assert result.occurrences == 2

    def test_edit_file_not_found(self) -> None:
        """Editing a file that does not exist reports a not-found error."""
        sandbox = _make_sandbox()
        # _file_infos is empty by default, so exists() returns False.
        result = sandbox.edit("/missing.py", "a", "b")
        assert result.error is not None
        assert "not found" in result.error

    def test_edit_read_error(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.fs._file_infos = {"/f.py": FakeFileInfo(name="f.py", is_dir=False, size=1)}
        sandbox._sandbox.fs.download_files = MagicMock(side_effect=RuntimeError("read fail"))

        # read_bytes swallows the error and returns b"", so the edit reports
        # that the search string was not found in the (empty) content.
        result = sandbox.edit("/f.py", "a", "b")
        assert result.error == "String 'a' not found in file"

    def test_edit_write_error(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.fs._file_infos = {"/f.py": FakeFileInfo(name="f.py", is_dir=False, size=8)}
        sandbox._sandbox.fs._download_responses = [
            FakeDownloadResponse(source="/f.py", result="old text")
        ]
        sandbox._sandbox.process._exec_results = [FakeExecResult()]
        sandbox._sandbox.process._call_index = 0
        sandbox._sandbox.fs.upload_files = MagicMock(side_effect=RuntimeError("write fail"))

        result = sandbox.edit("/f.py", "old", "new")
        assert result.error is not None
        assert "write fail" in result.error


class TestDaytonaSandboxLifecycle:
    def test_is_alive_true(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.process._exec_results = [FakeExecResult(result="ok", exit_code=0)]
        sandbox._sandbox.process._call_index = 0

        assert sandbox.is_alive() is True

    def test_is_alive_false(self) -> None:
        sandbox = _make_sandbox()
        sandbox._sandbox.process.exec = MagicMock(side_effect=RuntimeError("dead"))

        assert sandbox.is_alive() is False

    def test_stop(self) -> None:
        sandbox = _make_sandbox()
        mock_delete = MagicMock()
        sandbox._client.delete = mock_delete

        sandbox.stop()
        mock_delete.assert_called_once()
        assert sandbox._sandbox is None

    def test_stop_idempotent(self) -> None:
        sandbox = _make_sandbox()
        sandbox.stop()
        # Second call should not raise
        sandbox.stop()

    def test_del_calls_stop(self) -> None:
        sandbox = _make_sandbox()
        mock_delete = MagicMock()
        sandbox._client.delete = mock_delete

        sandbox.__del__()
        mock_delete.assert_called_once()


class TestDaytonaSandboxLazyImport:
    def test_lazy_import_from_package(self) -> None:
        import pydantic_ai_backends

        cls = pydantic_ai_backends.DaytonaSandbox
        assert cls.__name__ == "DaytonaSandbox"

    def test_in_all(self) -> None:
        import pydantic_ai_backends

        assert "DaytonaSandbox" in pydantic_ai_backends.__all__
