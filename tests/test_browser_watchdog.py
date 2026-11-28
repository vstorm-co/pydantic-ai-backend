"""The runtime watchdog needs neither a live sandboxd nor privileged host access."""

from __future__ import annotations

import fcntl
import json
import os

import pytest

from pydantic_ai_backends.remote import _browser_watchdog as watchdog


@pytest.fixture
def processes(monkeypatch):
    uid = os.geteuid()
    known = {
        1: watchdog.ProcessIdentity(1, 1, "docker-init", uid),
        11: watchdog.ProcessIdentity(11, 11, "chromium", uid),
        12: watchdog.ProcessIdentity(12, 12, "Xvfb", uid),
        77: watchdog.ProcessIdentity(77, 77, "python3", uid),
    }
    monkeypatch.setattr(watchdog, "identity", lambda pid: known[pid])
    return known


@pytest.fixture
def journal(tmp_path, monkeypatch, processes):
    path = tmp_path / "watchdog.json"
    state = {
        "deadline": 20,
        "watchdog": processes[77].wire(),
        "browser": processes[11].wire(),
        "display": processes[12].wire(),
    }
    path.write_text(json.dumps(state))
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 10)
    fd = os.open(path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield path, state
    finally:
        os.close(fd)


def test_process_identity_handles_spaces_and_parentheses_in_process_names(tmp_path):
    process = tmp_path / "17"
    process.mkdir()
    tail = ["S", *["0"] * 18, "1234"]
    (process / "stat").write_text("17 (browser (worker)) " + " ".join(tail))
    binary = tmp_path / "chromium"
    binary.touch()
    (process / "exe").symlink_to(binary)
    result = watchdog.identity(17, tmp_path)
    assert result == watchdog.ProcessIdentity(17, 1234, "chromium", os.geteuid())
    with pytest.raises(ValueError, match="positive"):
        watchdog.identity(0, tmp_path)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"pid": True, "start": 1, "uid": 1, "executable": "x"},
        {"pid": 1, "start": -1, "uid": 1, "executable": "x"},
        {"pid": 1, "start": 1, "uid": -1, "executable": "x"},
        {"pid": 1, "start": 1, "uid": 1, "executable": ""},
    ],
)
def test_process_wire_contract_rejects_invalid_identities(value):
    with pytest.raises(ValueError):
        watchdog._decode_identity(value)


def test_same_process_refuses_pid_reuse_foreign_uid_and_missing_process(processes, monkeypatch):
    assert watchdog._same_process(processes[11])
    old = watchdog.ProcessIdentity(11, 10, "chromium", os.geteuid())
    assert not watchdog._same_process(old)
    foreign = watchdog.ProcessIdentity(11, 11, "chromium", os.geteuid() + 1)
    assert not watchdog._same_process(foreign)

    def gone(_pid):
        raise FileNotFoundError

    monkeypatch.setattr(watchdog, "identity", gone)
    assert not watchdog._same_process(processes[11])


@pytest.mark.parametrize(
    "command,valid",
    [
        (b"/sbin/docker-init\0--\0sleep\0infinity\0", True),
        (b"/sbin/docker-init\0--\0python\0app.py\0", False),
    ],
)
def test_only_proven_dedicated_sleep_init_can_be_stopped(
    tmp_path, processes, monkeypatch, command, valid
):
    root = tmp_path / "1"
    root.mkdir()
    (root / "cmdline").write_bytes(command)
    monkeypatch.setattr(watchdog, "PROC", tmp_path)
    assert watchdog._container_init() == (processes[1] if valid else None)
    (root / "cmdline").unlink()
    assert watchdog._container_init() is None


def test_retire_uses_exact_init_and_owned_isolated_gui_groups(processes, monkeypatch):
    signals = []
    monkeypatch.setattr(watchdog.os, "kill", lambda pid, sig: signals.append(("pid", pid, sig)))
    monkeypatch.setattr(watchdog.os, "killpg", lambda pid, sig: signals.append(("group", pid, sig)))
    monkeypatch.setattr(watchdog.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(watchdog.os, "getpgrp", lambda: 99)
    watchdog.retire(processes[1], processes[11], processes[12])
    assert [entry[:2] for entry in signals] == [("pid", 1), ("group", 11), ("group", 12)]


def test_init_refusal_still_stops_gui_without_killing_other_user_processes(processes, monkeypatch):
    signals = []

    def signal_pid(pid, sig):
        if pid == 1:
            raise PermissionError
        signals.append(pid)

    monkeypatch.setattr(watchdog.os, "kill", signal_pid)
    monkeypatch.setattr(watchdog.os, "getpgid", lambda _pid: 99)
    monkeypatch.setattr(watchdog.os, "getpgrp", lambda: 99)
    watchdog.retire(processes[1], processes[11], processes[12])
    assert signals == [11, 12]
    assert not watchdog._stop_gui(processes[1])
    assert not watchdog._stop_gui(watchdog.ProcessIdentity(11, 0, "chromium", os.geteuid()))


def test_disappeared_gui_is_an_idempotent_stop(processes, monkeypatch):
    def gone(_pid):
        raise ProcessLookupError

    monkeypatch.setattr(watchdog.os, "getpgid", gone)
    assert watchdog._stop_gui(processes[11])


def test_existing_watchdog_keeps_immutable_deadline_and_process_identity(journal):
    path, _state = journal
    assert watchdog.existing(path) == 20
    assert watchdog.arm(100, 11, 12, path) == 20


@pytest.mark.parametrize(
    "change",
    ["expired", "bad_type", "nan", "wrong_process", "invalid_identity", "not_object", "oversized"],
)
def test_reuse_refuses_stale_or_malformed_watchdog_state(journal, change):
    path, state = journal
    if change == "expired":
        state["deadline"] = 9
    elif change == "bad_type":
        state["deadline"] = True
    elif change == "nan":
        state["deadline"] = float("nan")
    elif change == "wrong_process":
        state["browser"]["start"] = 99
    elif change == "invalid_identity":
        state["browser"] = None
    if change == "not_object":
        path.write_text("[]")
    elif change == "oversized":
        path.write_text(" " * (watchdog.MAX_STATE_BYTES + 1))
    else:
        path.write_text(json.dumps(state))
    with pytest.raises(ValueError):
        watchdog.existing(path)


def test_reuse_never_rearms_an_unowned_or_dead_watchdog(tmp_path, journal, monkeypatch):
    path, _state = journal
    unowned = tmp_path / "unowned.json"
    unowned.write_text("{}")
    with pytest.raises(ValueError, match="no longer running"):
        watchdog.existing(unowned)
    monkeypatch.setattr(watchdog.os, "geteuid", lambda: -1)
    with pytest.raises(ValueError, match="owned"):
        watchdog.existing(path)
    with pytest.raises(ValueError, match="owned"):
        watchdog.arm(10, 11, 12, path)


@pytest.mark.parametrize("lifetime", [True, 0, 86401, 1.2])
def test_arm_rejects_unbounded_or_noninteger_lifetimes(tmp_path, lifetime):
    with pytest.raises(ValueError, match="lifetime"):
        watchdog.arm(lifetime, 11, 12, tmp_path / "state")


def test_arm_starts_one_disowned_watchdog_and_records_exact_pids(tmp_path, processes, monkeypatch):
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 10)
    monkeypatch.setattr(watchdog.os, "fork", lambda: 77)
    monkeypatch.setattr(watchdog, "_container_init", lambda: processes[1])
    path = tmp_path / "state"
    assert watchdog.arm(20, 11, 12, path) == 30
    state = json.loads(path.read_text())
    assert state["deadline"] == 30
    assert state["watchdog"] == processes[77].wire()
    with pytest.raises(ValueError, match="rearmed"):
        watchdog.arm(200, 11, 12, path)


def test_arm_never_takes_over_foreign_gui_processes(tmp_path, processes, monkeypatch):
    processes[11] = watchdog.ProcessIdentity(11, 1, "other-app", os.geteuid())
    with pytest.raises(ValueError, match="does not own"):
        watchdog.arm(10, 11, 12, tmp_path / "state")


def test_child_detaches_and_owns_lifetime_independently(tmp_path, processes, monkeypatch):
    observed = []
    monkeypatch.setattr(watchdog.os, "fork", lambda: 0)
    monkeypatch.setattr(watchdog.os, "setsid", lambda: observed.append("setsid"))
    monkeypatch.setattr(watchdog.os, "dup2", lambda _source, target: observed.append(target))
    monkeypatch.setattr(watchdog, "_container_init", lambda: None)

    def watch(fd, deadline, init, browser, display):
        observed.append((browser.pid, display.pid))
        os.close(fd)

    def exited(code):
        raise SystemExit(code)

    monkeypatch.setattr(watchdog, "_watch", watch)
    monkeypatch.setattr(watchdog.os, "_exit", exited)
    with pytest.raises(SystemExit) as exited:
        watchdog.arm(10, 11, 12, tmp_path / "state")
    assert exited.value.code == 0
    assert observed == ["setsid", 0, 1, 2, (11, 12)]


def test_watchdog_retirement_runs_on_deadline_and_interruption(tmp_path, processes, monkeypatch):
    times = iter([0, 1, 2])
    sleeps = []
    retired = []
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(watchdog.time, "sleep", sleeps.append)
    monkeypatch.setattr(watchdog.signal, "signal", lambda _sig, _handler: None)
    monkeypatch.setattr(watchdog, "retire", lambda *args: retired.append(args))
    fd = os.open(tmp_path / "state", os.O_CREAT | os.O_RDWR, 0o600)
    watchdog._watch(fd, 2, processes[1], processes[11], processes[12])
    assert sleeps == [1, 1]
    assert retired
    with pytest.raises(OSError):
        os.fstat(fd)
    with pytest.raises(InterruptedError):
        watchdog._interrupted(15, None)


def test_state_writes_are_bounded_and_handle_short_writes(tmp_path, monkeypatch):
    fd = os.open(tmp_path / "state", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with pytest.raises(ValueError, match="bound"):
            watchdog._write_state(fd, {"data": "x" * watchdog.MAX_STATE_BYTES})
        original = watchdog.os.write
        monkeypatch.setattr(
            watchdog.os, "write", lambda target, payload: original(target, payload[:3])
        )
        watchdog._write_state(fd, {"value": 1})
        os.lseek(fd, 0, os.SEEK_SET)
        assert json.loads(os.read(fd, 100)) == {"value": 1}
        monkeypatch.setattr(watchdog.os, "write", lambda _target, _payload: 0)
        with pytest.raises(OSError, match="written"):
            watchdog._write_state(fd, {"value": 2})
    finally:
        os.close(fd)


def test_partial_startup_abort_targets_only_proven_gui_and_init(processes, monkeypatch):
    stopped = []
    monkeypatch.setattr(watchdog, "_container_init", lambda: processes[1])
    monkeypatch.setattr(watchdog.os, "kill", lambda pid, _sig: stopped.append(pid))
    monkeypatch.setattr(watchdog, "_stop_gui", lambda process: stopped.append(process.pid))
    watchdog.abort(11, 12)
    assert stopped == [1, 11, 12]
    watchdog.abort(0, 0)
    processes[11] = watchdog.ProcessIdentity(11, 1, "other-app", os.geteuid())
    stopped.clear()
    watchdog.abort(11, 12)
    assert stopped == [1, 12]


def test_main_validates_arguments_before_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog, "existing", lambda: calls.append("existing"))
    monkeypatch.setattr(watchdog, "arm", lambda *args: calls.append(args))
    monkeypatch.setattr(watchdog, "abort", lambda *args: calls.append(args))
    watchdog.main(["existing"])
    watchdog.main(["arm", "10", "11", "12"])
    watchdog.main(["abort", "11", "12"])
    assert calls == ["existing", (10, 11, 12), (11, 12)]
    with pytest.raises(ValueError):
        watchdog.main([])


def test_abort_can_fall_back_without_init_or_a_live_gui(processes, monkeypatch):
    stopped = []
    monkeypatch.setattr(watchdog, "_container_init", lambda: None)
    monkeypatch.setattr(watchdog, "_stop_gui", lambda process: stopped.append(process.pid))
    watchdog.retire(None, processes[11], processes[12])
    assert stopped == [11, 12]

    def gone(_pid):
        raise FileNotFoundError

    monkeypatch.setattr(watchdog, "identity", gone)
    watchdog.abort(11, 12)


def test_abort_attempts_gui_even_if_init_signal_is_refused(processes, monkeypatch):
    stopped = []
    monkeypatch.setattr(watchdog, "_container_init", lambda: processes[1])

    def denied(_pid, _sig):
        raise PermissionError

    monkeypatch.setattr(watchdog.os, "kill", denied)
    monkeypatch.setattr(watchdog, "_stop_gui", lambda process: stopped.append(process.pid))
    watchdog.abort(11, 12)
    assert stopped == [11, 12]


def test_executable_source_dispatches_only_its_explicit_command(monkeypatch):
    import runpy

    monkeypatch.setattr(watchdog.sys, "argv", ["watchdog", "invalid"])
    with pytest.raises(ValueError, match="Invalid"):
        runpy.run_path(watchdog.__file__, run_name="__main__")


def test_child_setup_failure_retires_gui_and_releases_journal(tmp_path, processes, monkeypatch):
    retired = []
    closed = []
    original_close = os.close
    monkeypatch.setattr(watchdog.os, "fork", lambda: 0)
    monkeypatch.setattr(watchdog, "_container_init", lambda: None)
    monkeypatch.setattr(watchdog, "retire", lambda *args: retired.append(args))

    def fail_setup():
        raise OSError("setsid refused")

    def close(fd):
        closed.append(fd)
        original_close(fd)

    def exited(code):
        raise SystemExit(code)

    monkeypatch.setattr(watchdog.os, "setsid", fail_setup)
    monkeypatch.setattr(watchdog.os, "close", close)
    monkeypatch.setattr(watchdog.os, "_exit", exited)
    with pytest.raises(SystemExit) as result:
        watchdog.arm(10, 11, 12, tmp_path / "state")
    assert result.value.code == 1
    assert retired == [(None, processes[11], processes[12])]
    assert len(closed) == 1
    with pytest.raises(OSError):
        os.fstat(closed[0])
