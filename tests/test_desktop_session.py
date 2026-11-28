"""Only isolated groups of this display's actual GUI executables can be retired."""

import ast
import os
import signal
from pathlib import Path
from unittest.mock import Mock

import pytest

from pydantic_ai_backends.desktop import _desktop_session as module


def process(proc: Path, identity: int, executable: str, display: str = ":0") -> None:
    entry = proc / str(identity)
    entry.mkdir()
    binary = proc / executable
    binary.touch(exist_ok=True)
    (entry / "exe").symlink_to(binary)
    (entry / "cmdline").write_bytes(executable.encode() + b"\0" + display.encode() + b"\0")
    (entry / "environ").write_bytes(b"PRIVATE_KEY=never-print\0DISPLAY=" + display.encode() + b"\0")


def test_only_actual_owned_display_gui_groups_are_selected(tmp_path, monkeypatch):
    process(tmp_path, 10, "xfce4-session")
    process(tmp_path, 11, "xfce4-panel")
    process(tmp_path, 12, "Xvfb")
    process(tmp_path, 13, "xfwm4", ":99")
    process(tmp_path, 14, "Xvfb", ":99")
    process(tmp_path, 15, "python3")
    process(tmp_path, 16, "xfdesktop")
    (tmp_path / "16" / "exe").unlink()
    (tmp_path / "16" / "exe").symlink_to(tmp_path / "python3")
    (tmp_path / "gone").mkdir()
    (tmp_path / "17").mkdir()
    monkeypatch.setattr(module.os, "getpgid", lambda pid: 20 if pid < 12 else 30)
    monkeypatch.setattr(module.os, "getpgrp", lambda: 99)
    assert module.groups(":0", tmp_path) == {20, 30}


@pytest.mark.parametrize("group", [0, 1, 99])
def test_critical_or_rpc_groups_are_refused_before_signaling(tmp_path, monkeypatch, group):
    process(tmp_path, 10, "xfce4-session")
    monkeypatch.setattr(module.os, "getpgid", lambda _pid: group)
    monkeypatch.setattr(module.os, "getpgrp", lambda: 99)
    with pytest.raises(RuntimeError, match="isolated GUI"):
        module.groups(":0", tmp_path)


def test_a_gui_belonging_to_another_uid_is_not_signaled(tmp_path, monkeypatch):
    process(tmp_path, 10, "xfce4-session")
    monkeypatch.setattr(module.os, "getpgid", lambda _pid: 20)
    monkeypatch.setattr(module.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(RuntimeError, match="isolated GUI"):
        module.groups(":0", tmp_path)


def test_process_disappearing_during_identity_check_is_already_retired(tmp_path, monkeypatch):
    process(tmp_path, 10, "xfce4-session")

    def gone(_pid):
        raise ProcessLookupError

    monkeypatch.setattr(module.os, "getpgid", gone)
    assert module.groups(":0", tmp_path) == set()


def test_retirement_signals_only_selected_groups_then_waits_for_complete_exit(monkeypatch):
    monkeypatch.setattr(module, "groups", lambda _display: {20})
    killed = Mock(side_effect=[None, None, ProcessLookupError])
    monkeypatch.setattr(module.os, "killpg", killed)
    slept = Mock()
    monkeypatch.setattr(module.time, "sleep", slept)
    module.retire(":0")
    assert killed.call_args_list == [((20, signal.SIGTERM),), ((20, 0),), ((20, 0),)]
    slept.assert_called_once_with(0.1)


def test_group_disappearing_before_signal_is_success(monkeypatch):
    monkeypatch.setattr(module, "groups", lambda _display: {20})
    monkeypatch.setattr(module.os, "killpg", Mock(side_effect=ProcessLookupError))
    module.retire(":0")


def test_uncooperative_group_fails_at_the_bound_without_force_kill(monkeypatch):
    monkeypatch.setattr(module, "groups", lambda _display: {20})
    killed = Mock()
    monkeypatch.setattr(module.os, "killpg", killed)
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 10]))
    with pytest.raises(RuntimeError, match="did not stop"):
        module.retire(":0")
    assert killed.call_args_list == [((20, signal.SIGTERM),), ((20, 0),)]


def test_no_gui_needs_no_signal(monkeypatch):
    monkeypatch.setattr(module, "groups", lambda _display: set())
    killed = Mock()
    monkeypatch.setattr(module.os, "killpg", killed)
    module.retire(":0")
    killed.assert_not_called()


def test_sandbox_helper_parses_on_the_desktop_image_python() -> None:
    ast.parse(Path(module.__file__).read_text(), feature_version=(3, 12))
