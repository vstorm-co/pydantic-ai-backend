"""Exact-container archive capture is bounded and always unpauses writers."""

from types import SimpleNamespace

import pytest

from pydantic_ai_backends.backends.docker import _browser_profile_io as transfer
from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox
from pydantic_ai_backends.remote._browser_profiles import BrowserProfileError
from tests.test_browser_profiles import archive


class Container:
    def __init__(self):
        self.code = 0
        self.chunks = [archive()]
        self.paused = False
        self.put = None
        self.accept = True
        self.fail = False
        self.closed = False
        self.control_fail = None

    def exec_run(self, cmd):
        assert cmd[:2] == ["python3", "-c"]
        if len(cmd) == 4:
            self.paused = cmd[3] == "stop"
            if self.control_fail == cmd[3]:
                raise RuntimeError("writer control unavailable")
        return SimpleNamespace(exit_code=self.code)

    def get_archive(self, path):
        assert path == "/tmp/sandboxd-browser-profile"

        def stream():
            try:
                if self.fail:
                    raise OSError("lost Docker")
                yield from self.chunks
            finally:
                self.closed = True

        return stream(), {}

    def put_archive(self, path, data):
        self.put = (path, data)
        return self.accept


@pytest.mark.parametrize("graceful", [True, False])
def test_capture_preserves_database_and_releases_container(graceful):
    container = Container()
    result = transfer.export_profile(container, graceful=graceful)
    assert result
    assert not container.paused
    assert container.closed
    transfer.restore_profile(container, result)
    assert container.put == ("/tmp", result)


def test_capture_and_restore_refuse_provider_failures_and_limits(monkeypatch):
    container = Container()
    container.code = 1
    with pytest.raises(BrowserProfileError, match="flush"):
        transfer.export_profile(container, graceful=True)
    with pytest.raises(BrowserProfileError, match="restored"):
        transfer.restore_profile(container, archive())
    container.code = 0
    container.accept = False
    with pytest.raises(BrowserProfileError, match="refused"):
        transfer.restore_profile(container, archive())
    container.fail = True
    with pytest.raises(OSError):
        transfer.export_profile(container, graceful=False)
    assert not container.paused
    container.fail = False
    monkeypatch.setattr(transfer, "MAX_PROFILE_BYTES", 1)
    with pytest.raises(BrowserProfileError, match="bound"):
        transfer.export_profile(container, graceful=False)
    assert not container.paused


async def test_docker_profile_adapters_never_create_or_heal_a_missing_container(monkeypatch):
    from pydantic_ai_backends.backends.docker import _profile_exec

    def export(container):
        if container.fail:
            raise OSError("lost Docker")
        return archive()

    monkeypatch.setattr(_profile_exec, "export_archive", export)
    monkeypatch.setattr(
        _profile_exec, "import_archive", lambda container, data: container.put_archive("/tmp", data)
    )
    sandbox = DockerSandbox(image="browser:test")
    with pytest.raises(BrowserProfileError, match="unavailable"):
        await sandbox.export_browser_profile(graceful=True)
    with pytest.raises(BrowserProfileError, match="unavailable"):
        await sandbox.restore_browser_profile(archive())
    container = Container()
    sandbox._container = container
    try:
        captured = await sandbox.export_browser_profile(graceful=False)
        await sandbox.restore_browser_profile(captured)
        assert container.put
        container.fail = True
        with pytest.raises(BrowserProfileError, match="captured"):
            await sandbox.export_browser_profile(graceful=False)

        def bad_put(_path, _payload):
            raise OSError("lost Docker")

        container.put_archive = bad_put
        with pytest.raises(BrowserProfileError, match="restored"):
            await sandbox.restore_browser_profile(captured)
    finally:
        sandbox._container = None


def test_archive_iterator_need_not_offer_close():
    container = Container()
    container.get_archive = lambda _path: ([archive()], {})
    assert transfer.export_profile(container, graceful=False)
    assert not container.paused


def test_profile_control_results_are_validated():
    container = Container()
    container.exec_run = lambda _cmd: object()
    with pytest.raises(BrowserProfileError, match="flush"):
        transfer.export_profile(container, graceful=True)
    with pytest.raises(BrowserProfileError, match="restored"):
        transfer.restore_profile(container, archive())


def test_dynamic_docker_archive_paths_are_not_caller_configurable():
    adapter = transfer.DockerProfileContainer(Container())
    with pytest.raises(BrowserProfileError, match="managed"):
        adapter.get_archive("/etc")
    with pytest.raises(BrowserProfileError, match="managed"):
        adapter.put_archive("/workspace", archive())


@pytest.mark.parametrize("failed_capture", [False, True])
def test_writer_resume_failure_is_visible_without_masking_capture_error(failed_capture, caplog):
    container = Container()
    container.fail = failed_capture

    container.control_fail = "resume"
    if failed_capture:
        with pytest.raises(OSError, match="lost Docker"):
            transfer.export_profile(container, graceful=False)
        assert "writer resume failed" in caplog.text
    else:
        with pytest.raises(RuntimeError, match="writer control unavailable"):
            transfer.export_profile(container, graceful=False)


def test_unknown_stop_acceptance_still_resumes_exact_browser_group():
    container = Container()

    container.control_fail = "stop"
    with pytest.raises(RuntimeError, match="writer control unavailable"):
        transfer.export_profile(container, graceful=False)
    assert not container.paused


def test_periodic_capture_refuses_invalid_control_ack_and_enforces_wall_deadline(monkeypatch):
    container = Container()
    container.exec_run = lambda _cmd: object()
    with pytest.raises(BrowserProfileError, match="controlled"):
        transfer.export_profile(container, graceful=False)
    container = Container()
    times = iter([0, 31])
    monkeypatch.setattr(transfer.time, "monotonic", lambda: next(times))
    with pytest.raises(BrowserProfileError, match="deadline"):
        transfer.export_profile(container, graceful=False)
    assert not container.paused


@pytest.mark.parametrize("mode", ["stop", "resume", "flush"])
def test_profile_control_only_signals_the_recorded_chromium_group(monkeypatch, mode):
    import json
    import os
    import signal
    import sys
    from pathlib import Path

    pid, start = 900007, 123
    sent = []
    gone = False

    def read(path):
        if path.name == "sandboxd-browser-watchdog.json":
            return json.dumps({"browser": {"pid": pid, "start": start}})
        if gone:
            raise FileNotFoundError
        return f"{pid} (chromium) " + " ".join(["S"] + ["0"] * 18 + [str(start)])

    def terminate(target, sig):
        nonlocal gone
        sent.append((target, sig))
        gone = True

    import io
    import urllib.request

    import websocket

    class BrowserSocket:
        def send(self, message):
            assert json.loads(message)["method"] == "Browser.close"

        def recv(self):
            nonlocal gone
            gone = True
            return '{"id":1,"result":{}}'

        def settimeout(self, _seconds):
            pass

        def close(self):
            pass

    with monkeypatch.context() as patch:
        patch.setattr(
            urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: io.BytesIO(
                b'{"webSocketDebuggerUrl":"ws://untrusted:9999/devtools/browser/id"}'
            ),
        )
        patch.setattr(
            websocket,
            "create_connection",
            lambda endpoint, **_kwargs: (
                BrowserSocket()
                if endpoint == "ws://127.0.0.1:9222/devtools/browser/id"
                else pytest.fail("unvetted debugger authority")
            ),
        )
        patch.setattr(Path, "iterdir", lambda _path: [Path("/proc/900007")])
        patch.setattr(Path, "read_text", read)
        patch.setattr(Path, "resolve", lambda _path, *, strict: Path("/bin/chromium"))
        patch.setattr(Path, "stat", lambda _path: SimpleNamespace(st_uid=os.geteuid()))
        patch.setattr(os, "getpgid", lambda _pid: pid)
        patch.setattr(os, "getpgrp", lambda: 77)
        patch.setattr(os, "killpg", lambda group, sig: sent.append((group, sig)))
        patch.setattr(os, "kill", terminate)
        patch.setattr(sys, "argv", ["profile-control", mode])
        exec(transfer._FLUSH if mode == "flush" else transfer._CONTROL, {})
    if mode == "flush":
        assert sent == [(pid, signal.SIGCONT)]
    else:
        assert sent == [(pid, signal.SIGSTOP if mode == "stop" else signal.SIGCONT)]


@pytest.mark.parametrize(
    "failure",
    ["wrong-group", "helper-group", "stale", "wrong-user", "gone", "invalid-mode", "flush-timeout"],
)
def test_profile_control_refuses_stale_or_unowned_processes(monkeypatch, failure):
    import json
    import os
    import sys
    from pathlib import Path

    pid = 900007
    sent = []

    def read(path):
        if path.name == "sandboxd-browser-watchdog.json":
            return json.dumps({"browser": {"pid": pid, "start": 123}})
        if failure == "gone":
            raise FileNotFoundError
        return f"{pid} (chromium) " + " ".join(
            ["S"] + ["0"] * 18 + ["999" if failure == "stale" else "123"]
        )

    import io
    import urllib.request

    import websocket

    class BrowserSocket:
        def send(self, _message):
            pass

        def recv(self):
            return '{"id":1,"result":{}}'

        def settimeout(self, _seconds):
            pass

        def close(self):
            pass

    with monkeypatch.context() as patch:
        patch.setattr(
            urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: io.BytesIO(
                b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/id"}'
            ),
        )
        patch.setattr(websocket, "create_connection", lambda *_args, **_kwargs: BrowserSocket())
        patch.setattr(Path, "iterdir", lambda _path: [Path("/proc/900007")])
        patch.setattr(Path, "read_text", read)
        patch.setattr(Path, "resolve", lambda _path, *, strict: Path("/bin/chromium"))
        patch.setattr(
            Path,
            "stat",
            lambda _path: SimpleNamespace(
                st_uid=os.geteuid() + (1 if failure == "wrong-user" else 0)
            ),
        )
        patch.setattr(os, "getpgid", lambda _pid: 1 if failure == "wrong-group" else pid)
        patch.setattr(os, "getpgrp", lambda: pid if failure == "helper-group" else 77)
        patch.setattr(os, "killpg", lambda group, sig: sent.append((group, sig)))
        patch.setattr(os, "kill", lambda target, sig: sent.append((target, sig)))
        patch.setattr(
            sys, "argv", ["profile-control", "invalid" if failure == "invalid-mode" else "stop"]
        )
        if failure == "flush-timeout":
            times = iter([0, 16])
            patch.setattr(transfer.time, "monotonic", lambda: next(times))
            with pytest.raises(RuntimeError, match="deadline"):
                exec(transfer._FLUSH, {})
        elif failure in {"wrong-group", "helper-group", "invalid-mode"}:
            with pytest.raises((RuntimeError, ValueError)):
                exec(transfer._CONTROL, {})
        else:
            exec(transfer._CONTROL, {})
    if failure != "flush-timeout":
        assert sent == []
