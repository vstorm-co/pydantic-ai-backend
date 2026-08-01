"""Tests for `BaseSandbox` and `AsyncBaseSandbox`.

The two derive every file operation from the same shell commands, so the point of
most of these is *parity*: given the same fake shell, both must answer
identically. A drift between them is the bug this pairing exists to prevent.
"""

from __future__ import annotations

import base64

import pytest

from pydantic_ai_backends import AsyncBaseSandbox, BaseSandbox
from pydantic_ai_backends.backends.base import FILE_OP_TIMEOUT, SEARCH_TIMEOUT
from pydantic_ai_backends.types import EditResult, ExecuteResponse

LISTING = (
    "total 8\n"
    "drwxr-xr-x 2 root root 4096 Jan  1 00:00 .\n"
    "drwxr-xr-x 2 root root 4096 Jan  1 00:00 src\n"
    "-rw-r--r-- 1 root root   12 Jan  1 00:00 notes.md\n"
)


class FakeShell(BaseSandbox):
    """Synchronous sandbox whose shell is a lookup table."""

    def __init__(
        self,
        responses: dict[str, ExecuteResponse] | None = None,
        sandbox_id: str | None = "fake",
    ) -> None:
        super().__init__(sandbox_id)
        self.responses = responses or {}
        self.commands: list[str] = []
        self.default = ExecuteResponse(output="", exit_code=0)

    def _answer(self, command: str) -> ExecuteResponse:
        self.commands.append(command)
        for fragment, response in self.responses.items():
            if fragment in command:
                return response
        return self.default

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        return self._answer(command)

    def edit(self, path, old_string, new_string, replace_all=False) -> EditResult:
        return EditResult(path=path, occurrences=1)


class AsyncFakeShell(AsyncBaseSandbox):
    """The same lookup table, reached asynchronously."""

    def __init__(
        self,
        responses: dict[str, ExecuteResponse] | None = None,
        sandbox_id: str | None = "fake",
    ) -> None:
        super().__init__(sandbox_id)
        self.responses = responses or {}
        self.commands: list[str] = []
        self.default = ExecuteResponse(output="", exit_code=0)

    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        self.commands.append(command)
        for fragment, response in self.responses.items():
            if fragment in command:
                return response
        return self.default

    async def edit(self, path, old_string, new_string, replace_all=False) -> EditResult:
        return EditResult(path=path, occurrences=1)


class TestIdentity:
    """Both bases share the identity and idle bookkeeping."""

    @pytest.mark.parametrize("cls", [FakeShell, AsyncFakeShell])
    def test_an_explicit_id_is_kept(self, cls):
        assert cls().id == "fake"

    @pytest.mark.parametrize("cls", [FakeShell, AsyncFakeShell])
    def test_an_id_is_generated_when_omitted(self, cls):
        first, second = cls(sandbox_id=None), cls(sandbox_id=None)

        assert first.id and second.id
        assert first.id != second.id

    @pytest.mark.parametrize("cls", [FakeShell, AsyncFakeShell])
    def test_touch_moves_last_activity_forward(self, cls):
        sandbox = cls()
        before = sandbox.last_activity

        sandbox.touch()

        assert sandbox.last_activity >= before


class TestLifecycleDefaults:
    """Sandboxes start on first use, so the eager hooks are no-ops."""

    def test_sync_defaults(self):
        sandbox = FakeShell()

        assert sandbox.start() is None
        assert sandbox.is_alive() is False
        assert sandbox.stop() is None

    async def test_async_defaults_are_awaitable(self):
        sandbox = AsyncFakeShell()

        assert await sandbox.start() is None
        assert await sandbox.is_alive() is False
        assert await sandbox.stop() is None


class TestParity:
    """Same fake shell, same answers — or the two have drifted."""

    @staticmethod
    def _pair(responses):
        return FakeShell(dict(responses)), AsyncFakeShell(dict(responses))

    async def test_exists(self):
        sync, asyncish = self._pair({"test -f": ExecuteResponse(output="", exit_code=0)})

        assert sync.exists("/f") is True
        assert await asyncish.exists("/f") is True
        assert sync.commands == asyncish.commands

    async def test_exists_is_false_on_a_nonzero_exit(self):
        sync, asyncish = self._pair({"test -f": ExecuteResponse(output="", exit_code=1)})

        assert sync.exists("/f") is False
        assert await asyncish.exists("/f") is False

    async def test_ls_info(self):
        sync, asyncish = self._pair({"ls -la": ExecuteResponse(output=LISTING, exit_code=0)})

        rows = sync.ls_info("/work")

        assert [row["name"] for row in rows] == ["src", "notes.md"]
        assert await asyncish.ls_info("/work") == rows

    async def test_read(self):
        numbered = ExecuteResponse(output="     1\thello", exit_code=0)
        sync, asyncish = self._pair({"awk": numbered})

        assert sync.read("/f", offset=0, limit=10) == "     1\thello"
        assert await asyncish.read("/f", offset=0, limit=10) == "     1\thello"
        assert sync.commands == asyncish.commands

    async def test_read_bytes(self):
        sync, asyncish = self._pair({"cat ": ExecuteResponse(output="payload", exit_code=0)})

        assert sync.read_bytes("/f") == b"payload"
        assert await asyncish.read_bytes("/f") == b"payload"

    async def test_read_bytes_degrades_to_empty(self):
        sync, asyncish = self._pair({"cat ": ExecuteResponse(output="nope", exit_code=1)})

        assert sync.read_bytes("/f") == b""
        assert await asyncish.read_bytes("/f") == b""

    async def test_write(self):
        sync, asyncish = self._pair({})

        assert sync.write("/f", "body").path == "/f"
        assert (await asyncish.write("/f", "body")).path == "/f"
        assert len(sync.commands) == len(asyncish.commands) == 1
        assert "base64 -d > /f" in sync.commands[0]
        assert "base64 -d > /f" in asyncish.commands[0]

    async def test_write_accepts_bytes(self):
        """The protocol types `content` as `str | bytes`; both bases narrowed it."""
        sync, asyncish = self._pair({})

        assert sync.write("/img.png", b"\x89PNG").path == "/img.png"
        assert (await asyncish.write("/img.png", b"\x89PNG")).path == "/img.png"
        payload = sync.commands[0].split("printf %s ", 1)[1].split(" ", 1)[0]
        assert base64.b64decode(payload) == b"\x89PNG"

    async def test_write_reports_a_failure(self):
        failed = ExecuteResponse(output="Permission denied", exit_code=1)
        sync, asyncish = self._pair({"base64 -d": failed})

        assert sync.write("/f", "x").error == "Permission denied"
        assert (await asyncish.write("/f", "x")).error == "Permission denied"

    async def test_glob_info(self):
        found = ExecuteResponse(output="/w/b.py\n/w/a.py\n", exit_code=0)
        sync, asyncish = self._pair({"find": found})

        rows = sync.glob_info("*.py", "/w")

        assert [row["path"] for row in rows] == ["/w/a.py", "/w/b.py"]
        assert await asyncish.glob_info("*.py", "/w") == rows

    async def test_grep_raw(self):
        hits = ExecuteResponse(output="a.py:2:todo\n", exit_code=0)
        sync, asyncish = self._pair({"grep": hits})

        matches = sync.grep_raw("todo")

        assert matches == [{"path": "a.py", "line_number": 2, "line": "todo"}]
        assert await asyncish.grep_raw("todo") == matches

    async def test_grep_raw_reports_an_error_as_a_string(self):
        broken = ExecuteResponse(output="bad regex", exit_code=2)
        sync, asyncish = self._pair({"grep": broken})

        assert sync.grep_raw("(") == "Error: bad regex"
        assert await asyncish.grep_raw("(") == "Error: bad regex"

    async def test_grep_options_are_forwarded_identically(self):
        sync, asyncish = self._pair({"grep": ExecuteResponse(output="", exit_code=1)})

        sync.grep_raw("todo", path="/w", glob="*.py", ignore_hidden=False)
        await asyncish.grep_raw("todo", path="/w", glob="*.py", ignore_hidden=False)

        assert sync.commands == asyncish.commands
        assert "--include='*.py'" in sync.commands[0]
        assert "--exclude" not in sync.commands[0]


class TestAbstractMethods:
    """Only `execute` and `edit` are the subclass's job."""

    @pytest.mark.parametrize("base", [BaseSandbox, AsyncBaseSandbox])
    def test_the_base_cannot_be_instantiated(self, base):
        with pytest.raises(TypeError, match="abstract"):
            base()  # type: ignore[abstract]

    @pytest.mark.parametrize("base", [BaseSandbox, AsyncBaseSandbox])
    def test_execute_and_edit_are_the_only_abstract_methods(self, base):
        assert base.__abstractmethods__ == frozenset({"execute", "edit"})


class TestEveryOperationIsBounded:
    """A shell command with no timeout is one nothing can reclaim.

    `DockerSandbox.execute` only wraps the `timeout` utility around a command it
    was given a limit for, and `exec_run` has no deadline of its own — so an
    uncapped operation pins a worker thread for good. `exists` passed a timeout;
    the other seven did not, which is how a handful of `grep` requests wedged a
    whole sandbox service.
    """

    @staticmethod
    def _calls(sandbox) -> list[int | None]:
        return [timeout for _command, timeout in sandbox.timed_commands]

    @pytest.mark.parametrize(
        "operation",
        [
            lambda s: s.exists("/f"),
            lambda s: s.ls_info("/d"),
            lambda s: s.read_bytes("/f"),
            lambda s: s.read("/f"),
            lambda s: s.write("/f", "x"),
            lambda s: s.glob_info("*.py", "/d"),
            lambda s: s.grep_raw("x", "/d"),
        ],
    )
    def test_sync(self, operation):
        sandbox = TimedShell()

        operation(sandbox)

        assert self._calls(sandbox) and all(t is not None for t in self._calls(sandbox))

    @pytest.mark.parametrize(
        "operation",
        [
            lambda s: s.exists("/f"),
            lambda s: s.ls_info("/d"),
            lambda s: s.read_bytes("/f"),
            lambda s: s.read("/f"),
            lambda s: s.write("/f", "x"),
            lambda s: s.glob_info("*.py", "/d"),
            lambda s: s.grep_raw("x", "/d"),
        ],
    )
    async def test_async(self, operation):
        sandbox = TimedAsyncShell()

        await operation(sandbox)

        assert self._calls(sandbox) and all(t is not None for t in self._calls(sandbox))

    def test_a_search_gets_longer_than_a_listing(self):
        """A grep over a large repository is slow in a way a `ls` never is."""
        sandbox = TimedShell()

        sandbox.ls_info("/d")
        sandbox.grep_raw("x", "/d")

        listing, search = self._calls(sandbox)
        assert listing == FILE_OP_TIMEOUT < search == SEARCH_TIMEOUT


class TimedShell(BaseSandbox):
    """Records the timeout each derived operation asks for."""

    def __init__(self) -> None:
        super().__init__("timed")
        self.timed_commands: list[tuple[str, int | None]] = []

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        self.timed_commands.append((command, timeout))
        return ExecuteResponse(output="", exit_code=0)

    def edit(self, path, old_string, new_string, replace_all=False) -> EditResult:
        return EditResult(path=path)


class TimedAsyncShell(AsyncBaseSandbox):
    """Async counterpart to `TimedShell`."""

    def __init__(self) -> None:
        super().__init__("timed")
        self.timed_commands: list[tuple[str, int | None]] = []

    async def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        self.timed_commands.append((command, timeout))
        return ExecuteResponse(output="", exit_code=0)

    async def edit(self, path, old_string, new_string, replace_all=False) -> EditResult:
        return EditResult(path=path)
