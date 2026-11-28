"""Desktop menu lookup and launch preserve XDG semantics without a model."""

import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pydantic_ai_backends.desktop import apps as _desktop_apps
from pydantic_ai_backends.desktop._sandbox import read_apps


def _entry(data_dir: Path, name: str, text: str) -> None:
    path = data_dir / "applications" / f"{name}.desktop"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def menu(tmp_path: Path) -> dict[str, str]:
    """A Polish desktop's search path: the user's own directory, then two system ones."""
    home, local, system = tmp_path / "home", tmp_path / "local", tmp_path / "system"
    _entry(home, "firefox-esr", "[Desktop Entry]\nType=Application\nName=My Firefox\n")
    _entry(
        home,
        "xfce4-web-browser",
        "[Desktop Entry]\nType=Application\nName=Web Browser\nHidden=true\n",
    )
    _entry(local, "org.mozilla.firefox", "[Desktop Entry]\nType=Application\nName=Firefox\n")
    _entry(system, "firefox", "[Desktop Entry]\nType=Application\nName=Firefox\nName[pl]=\n")
    _entry(system, "firefox-esr", "[Desktop Entry]\nType=Application\nName=Firefox ESR\n")
    _entry(
        system,
        "xfce4-terminal-emulator",
        "[Desktop Entry]\nType=Application\nName=Terminal Emulator\nName[de]=Terminal\n"
        "Name[pl_PL]=Emulator terminala\nName[pl]=Terminal\n",
    )
    _entry(
        system,
        "xfce4-terminal",
        "[Desktop Entry]\nType=Application\nName=Xfce Terminal\nName[pl]=Terminal Xfce\n\n"
        "[Desktop Action window]\nName=New Window\n",
    )
    _entry(system, "xfce4-web-browser", "[Desktop Entry]\nType=Application\nName=Web Browser\n")
    _entry(
        system,
        "thunar-settings",
        "[Desktop Entry]\nType=Application\nName=Settings\nNoDisplay=true\n",
    )
    _entry(system, "kde/konsole", "[Desktop Entry]\nType=Application\nName=Konsole\n")
    _entry(system, "broken", "")
    (system / "applications" / "unreadable.desktop").mkdir()
    return {"XDG_DATA_HOME": str(home), "XDG_DATA_DIRS": f"{local}:{system}", "LANG": "pl_PL.UTF-8"}


class TestTheDesktopsApplications:
    def test_the_menu_lists_each_shown_entry_once_by_its_name_in_the_desktops_language(
        self, menu: dict[str, str]
    ):
        """An earlier directory shadows a later one, even hidden; an action's `Name` is not
        the entry's.
        """
        assert [(app.id, app.name) for app in _desktop_apps.installed(menu)] == [
            ("xfce4-terminal-emulator", "Emulator terminala"),
            ("org.mozilla.firefox", "Firefox"),
            ("firefox", "Firefox"),
            ("kde-konsole", "Konsole"),
            ("firefox-esr", "My Firefox"),
            ("xfce4-terminal", "Terminal Xfce"),
        ]
        assert _desktop_apps.installed(menu)[-1].path.endswith(
            "system/applications/xfce4-terminal.desktop"
        )

    @pytest.mark.parametrize(
        ("keys", "desktop", "shown"),
        [
            ("Type=Application", None, True),
            ("Type=Link\nURL=https://example.com", None, False),
            ("", None, False),
            ("Type=Application\nOnlyShowIn=XFCE;", None, True),
            ("Type=Application\nOnlyShowIn=KDE;GNOME;", None, False),
            ("Type=Application\nOnlyShowIn=GNOME;", "ubuntu:GNOME", True),
            ("Type=Application\nNotShowIn=XFCE;", None, False),
            ("Type=Application\nNotShowIn=XFCE;", "GNOME", True),
            ("Type=Application\nTryExec=present", None, True),
            ("Type=Application\nTryExec=absent", None, False),
        ],
    )
    def test_an_entry_is_listed_only_where_the_menu_would_show_it(
        self, tmp_path: Path, keys: str, desktop: str | None, shown: bool
    ):
        """A desktop that does not say which it is is taken for XFCE, which every driven one
        runs.
        """
        programs = tmp_path / "bin"
        programs.mkdir()
        (programs / "present").touch(mode=0o755)
        _entry(tmp_path, "app", f"[Desktop Entry]\nName=App\n{keys}\n")
        env = {"XDG_DATA_HOME": str(tmp_path), "XDG_DATA_DIRS": "/none", "PATH": str(programs)}
        if desktop:
            env["XDG_CURRENT_DESKTOP"] = desktop
        assert [app.id for app in _desktop_apps.installed(env)] == (["app"] if shown else [])

    def test_an_escaped_name_is_listed_and_found_as_it_reads(self, tmp_path: Path):
        _entry(
            tmp_path,
            "editor",
            "[Desktop Entry]\nType=Application\nName=Text\\sEditor\\\\Pro\\;\n",
        )
        [app] = _desktop_apps.installed({"XDG_DATA_HOME": str(tmp_path), "XDG_DATA_DIRS": "/none"})
        assert app.name == "Text Editor\\Pro\\;"
        assert _desktop_apps.find("text editor\\pro\\;", [app]) == app

    def test_the_users_own_directory_defaults_to_their_home(self, tmp_path: Path):
        _entry(tmp_path / ".local/share", "mine", "[Desktop Entry]\nType=Application\nName=Mine\n")
        env = {"HOME": str(tmp_path), "XDG_DATA_DIRS": str(tmp_path / "none")}
        assert [app.id for app in _desktop_apps.installed(env)] == ["mine"]

    @pytest.mark.parametrize(
        ("env", "shown"),
        [
            ({"LANG": "sr_RS.UTF-8@latin"}, "sr_RS@latin"),
            ({"LANG": "sr_RS.UTF-8"}, "sr_RS"),
            ({"LANG": "sr@latin"}, "sr@latin"),
            ({"LANG": "sr_ME@latin"}, "sr@latin"),
            ({"LANG": "sr_ME"}, "sr"),
            ({"LANG": "sr", "LC_MESSAGES": "C.UTF-8"}, "untranslated"),
            ({"LC_ALL": "sr", "LC_MESSAGES": "C.UTF-8"}, "sr"),
            ({}, "untranslated"),
        ],
    )
    def test_a_translation_is_chosen_in_the_specifications_order(
        self, tmp_path: Path, env: dict[str, str], shown: str
    ):
        names = "".join(
            f"Name[{key}]={key}\n" for key in ("sr_RS@latin", "sr_RS", "sr@latin", "sr")
        )
        _entry(tmp_path, "term", f"[Desktop Entry]\nType=Application\nName=untranslated\n{names}")
        [app] = _desktop_apps.installed(
            {**env, "XDG_DATA_HOME": str(tmp_path), "XDG_DATA_DIRS": "/none"}
        )
        assert app.name == shown

    @pytest.mark.parametrize(
        ("query", "app_id"),
        [
            ("xfce4-terminal", "xfce4-terminal"),
            ("TERMINAL XFCE", "xfce4-terminal"),
            ("Emulator terminala", "xfce4-terminal-emulator"),
            ("firefox", "firefox"),
            ("org.mozilla.firefox", "org.mozilla.firefox"),
            ("kde-konsole", "kde-konsole"),
        ],
    )
    def test_an_id_or_a_name_finds_its_application_exactly(
        self, menu: dict[str, str], query: str, app_id: str
    ):
        assert _desktop_apps.find(query, _desktop_apps.installed(menu)).id == app_id

    def test_a_name_two_applications_share_is_answered_with_their_ids(self):
        """A system and a Flatpak Firefox are both "Firefox"; the ids the answer names launch
        each.
        """
        flatpak = _desktop_apps.App(id="org.mozilla.firefox", name="Firefox", path="/flatpak")
        system = _desktop_apps.App(id="firefox-bin", name="Firefox", path="/system")
        with pytest.raises(
            _desktop_apps.AppError,
            match=r"^'Firefox' is the name of 2 applications\. "
            r"Launch one by its id: org\.mozilla\.firefox, firefox-bin\.$",
        ):
            _desktop_apps.find("Firefox", [flatpak, system])
        assert _desktop_apps.find("firefox-bin", [flatpak, system]) == system

    def test_an_id_is_matched_before_a_name(self):
        """Or an id the ambiguity refusal points at could be refused as ambiguous in turn."""
        named = _desktop_apps.App(id="org.mozilla.firefox", name="Firefox", path="/flatpak")
        system = _desktop_apps.App(id="firefox", name="Firefox", path="/system")
        assert _desktop_apps.find("firefox", [named, system]) == system

    def test_ids_that_differ_only_in_case_are_each_launched_by_their_own(self):
        """The specification makes an id's case significant; ignoring it would refuse both as
        ambiguous.
        """
        upper = _desktop_apps.App(id="org.example.App", name="Upper", path="/upper")
        lower = _desktop_apps.App(id="org.example.app", name="Lower", path="/lower")
        assert _desktop_apps.find("org.example.App", [upper, lower]) == upper
        assert _desktop_apps.find("org.example.app", [upper, lower]) == lower
        with pytest.raises(
            _desktop_apps.AppError, match=r"org\.example\.App, org\.example\.app\.$"
        ):
            _desktop_apps.find("ORG.EXAMPLE.APP", [upper, lower])

    @pytest.mark.parametrize(
        "query", ["terminal", "Xfce Terminal", "Firefox ESR", "Web Browser", "fire"]
    )
    def test_anything_else_is_never_guessed(self, menu: dict[str, str], query: str):
        """Not a part of a name, another language's, nor an entry shadowed or hidden."""
        with pytest.raises(
            _desktop_apps.AppError,
            match=rf"^No application is called '{query}'\. "
            r"Call computer_apps for the installed ones\.$",
        ):
            _desktop_apps.find(query, _desktop_apps.installed(menu))


class _Screen:
    """`xdotool` and `exo-open` as `launch` sees them: windows that change as it polls."""

    def __init__(self, *polls: tuple[str, str], exit_code: int = 0, hangs: bool = False) -> None:
        self.polls = list(polls)
        self.exit_code = exit_code
        self.hangs = hangs
        self.opened: list[tuple[list[str], dict[str, object]]] = []
        self.waited = 0

    def run(self, argv: list[str], **kwargs: object) -> MagicMock:
        if argv[0] == "exo-open":
            self.opened.append((argv, kwargs))
            if self.hangs:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return MagicMock(returncode=self.exit_code)
        # One look is a `search` then a `getactivewindow`; the last one repeats.
        windows, focus = self.polls[0]
        if argv[1] == "search":
            return MagicMock(stdout=windows)
        if len(self.polls) > 1:
            self.polls.pop(0)
        return MagicMock(stdout=f"{focus}\n")

    def sleep(self, seconds: float) -> None:
        self.waited += 1


_APP = _desktop_apps.App(
    id="firefox-esr", name="Firefox ESR", path="/usr/share/applications/firefox-esr.desktop"
)


class TestLaunchingAnApplication:
    @pytest.fixture
    def screen(self, monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Screen]:
        def make(*polls: tuple[str, str], **kwargs: object) -> _Screen:
            fake = _Screen(*polls, **kwargs)
            monkeypatch.setattr(_desktop_apps.subprocess, "run", fake.run)
            monkeypatch.setattr(_desktop_apps.time, "sleep", fake.sleep)
            return fake

        return make

    @pytest.mark.parametrize("appeared", [("1\n2\n3", "1"), ("1\n2", "2")])
    def test_a_launch_returns_once_a_window_appears_or_the_focus_moves(
        self, screen: Callable[..., _Screen], appeared: tuple[str, str]
    ):
        fake = screen(("1\n2", "1"), ("1\n2", "1"), appeared)
        _desktop_apps.launch(_APP)

        [(argv, kwargs)] = fake.opened
        assert argv == ["exo-open", _APP.path]
        # Nothing the application inherits may hold the launch open until it quits.
        assert kwargs["start_new_session"] is True
        assert {kwargs[name] for name in ("stdin", "stdout", "stderr")} == {subprocess.DEVNULL}
        assert (fake.waited, fake.polls) == (1, [appeared])

    def test_an_application_that_shows_no_window_fails_the_launch(
        self, screen: Callable[..., _Screen], monkeypatch: pytest.MonkeyPatch
    ):
        """Or the rest of the batch types into the window that had the focus before."""
        monkeypatch.setattr(_desktop_apps.time, "monotonic", iter([0.0, 0.0, 10.5]).__next__)
        fake = screen(("1", "1"))
        with pytest.raises(
            _desktop_apps.AppError,
            match=r"^Firefox ESR was started, but no window of it came up within 10 seconds\. "
            r"Take a screenshot to see whether it opened before acting on it\.$",
        ):
            _desktop_apps.launch(_APP)
        assert (len(fake.opened), fake.waited) == (1, 1)

    def test_a_failed_or_hung_open_is_a_sentence(self, screen: Callable[..., _Screen]):
        screen(("1", "1"), exit_code=2)
        with pytest.raises(
            _desktop_apps.AppError, match=r"^Firefox ESR could not be opened \(exo-open exit 2\)\.$"
        ):
            _desktop_apps.launch(_APP)
        screen(("1", "1"), hangs=True)
        with pytest.raises(
            _desktop_apps.AppError, match=r"^Firefox ESR did not open within 10 seconds\.$"
        ):
            _desktop_apps.launch(_APP)


class TestTheDesktopAppsScript:
    """What a sandbox runs with `python3 -c` and computerd's launcher imports."""

    def test_list_prints_the_menu_as_json(
        self, menu: dict[str, str], capsys: pytest.CaptureFixture[str]
    ):
        assert _desktop_apps.main(["list"], menu) == 0
        assert read_apps(capsys.readouterr().out) == _desktop_apps.installed(menu)

    def test_launch_opens_the_application_it_names(
        self, menu: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ):
        opened: list[_desktop_apps.App] = []
        monkeypatch.setattr(_desktop_apps, "launch", opened.append)
        assert _desktop_apps.main(["launch", "My Firefox"], menu) == 0
        assert [app.id for app in opened] == ["firefox-esr"]

    @pytest.mark.parametrize(
        ("argv", "said"),
        [
            (
                ["launch", "gimp"],
                "No application is called 'gimp'. Call computer_apps for the installed ones.\n",
            ),
            ([], "Expected 'list' or 'launch <app>'.\n"),
            (["launch", "a", "b"], "Expected 'list' or 'launch <app>'.\n"),
        ],
    )
    def test_a_refusal_is_a_sentence_on_stderr(
        self, menu: dict[str, str], capsys: pytest.CaptureFixture[str], argv: list[str], said: str
    ):
        assert _desktop_apps.main(argv, menu) == 1
        assert capsys.readouterr() == ("", said)

    def test_run_as_a_script_it_reads_its_arguments_and_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        monkeypatch.setattr(sys, "argv", ["-c", "nonsense"])
        with pytest.raises(SystemExit) as exited:
            runpy.run_path(_desktop_apps.__file__, run_name="__main__")
        assert exited.value.code == 1
        assert capsys.readouterr().err == "Expected 'list' or 'launch <app>'.\n"
