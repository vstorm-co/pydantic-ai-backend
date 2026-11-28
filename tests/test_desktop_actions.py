"""Validated native actions and one coordinate transform."""

import struct

import pytest
from pydantic import ValidationError

from pydantic_ai_backends.desktop import (
    Click,
    DesktopError,
    Drag,
    Key,
    Launch,
    Move,
    NativeAction,
    Screen,
    Scroll,
    TypeText,
    Wait,
    describe,
    points_of,
    to_native,
)
from pydantic_ai_backends.desktop._commands import x11_keys, xdotool
from pydantic_ai_backends.desktop._types import png_size


def _png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", width, height)


class TestCoordinates:
    """The model answers in the screenshot's pixels; the desktop moves in its own."""

    SCREEN = Screen(width=1280, height=800, native_width=2560, native_height=1600)

    def test_a_point_is_scaled_into_the_desktops_space(self):
        assert self.SCREEN.to_native(640, 400) == (1280, 800)

    def test_the_far_edge_stays_on_the_screen(self):
        assert self.SCREEN.to_native(1279, 799) == (2558, 1598)
        assert Screen(10, 10, 15, 15).to_native(9, 9) == (14, 14)

    def test_contains_only_points_on_the_screenshot(self):
        assert self.SCREEN.contains(0, 0)
        assert not self.SCREEN.contains(1280, 10)
        assert not self.SCREEN.contains(10, 800)

    def test_every_point_of_an_action_is_moved_and_nothing_else(self):
        drag = to_native(Drag(x=10, y=20, to_x=30, to_y=40), self.SCREEN)
        assert drag == Drag(x=20, y=40, to_x=60, to_y=80)
        click = to_native(Click(x=1, y=2, count=2), self.SCREEN)
        assert click == Click(x=2, y=4, count=2)
        typed = TypeText(text="hi")
        assert to_native(typed, self.SCREEN) is typed

    def test_points_of_names_each_point_an_action_touches(self):
        assert points_of(Drag(x=1, y=2, to_x=3, to_y=4)) == [(1, 2), (3, 4)]
        assert points_of(Scroll(x=5, y=6)) == [(5, 6)]
        assert points_of(Key(keys="enter")) == []


class TestTheLivePanelsWords:
    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            (Click(x=1, y=2), ("CLICK", "(1, 2)")),
            (Click(x=1, y=2, count=2, button="right"), ("DOUBLE_CLICK", "(1, 2) (right)")),
            (Move(x=3, y=4), ("MOVE", "(3, 4)")),
            (Drag(x=1, y=2, to_x=3, to_y=4), ("DRAG", "(1, 2) → (3, 4)")),
            (Scroll(x=1, y=2, direction="up", amount=5), ("SCROLL", "up 5 at (1, 2)")),
            (TypeText(text="hello"), ("TYPE", "hello")),
            (Key(keys="ctrl+c"), ("KEY", "ctrl+c")),
            (Launch(app="Firefox"), ("LAUNCH", "Firefox")),
            (Wait(seconds=1.5), ("WAIT", "1.5s")),
        ],
    )
    def test_each_action_is_described(self, action: NativeAction, expected: tuple[str, str]):
        assert describe(action) == expected

    def test_long_typed_text_is_cut_short(self):
        _, target = describe(TypeText(text="x" * 100))
        assert target == "x" * 57 + "..."


class TestX11:
    def test_png_size_reads_the_header(self):
        assert png_size(_png(30, 20)) == (30, 20)

    def test_something_that_is_not_a_png_is_refused(self):
        with pytest.raises(DesktopError, match="PNG screenshot"):
            png_size(b"<html>")

    def test_key_names_are_spelled_the_way_xdotool_wants(self):
        assert x11_keys("ctrl + Shift+t") == "ctrl+shift+t"
        assert x11_keys("cmd+space") == "super+space"
        assert x11_keys("Enter") == "Return"
        assert x11_keys("F5") == "F5"

    def test_a_chord_of_nothing_is_refused(self):
        with pytest.raises(DesktopError, match="names no key"):
            x11_keys(" + ")

    @pytest.mark.parametrize(
        ("action", "command"),
        [
            (
                Click(x=1, y=2, count=2, button="right"),
                "xdotool mousemove --sync 1 2 click --repeat 2 3",
            ),
            (Move(x=1, y=2), "xdotool mousemove --sync 1 2"),
            (
                Drag(x=1, y=2, to_x=3, to_y=4),
                "xdotool mousemove --sync 1 2 mousedown 1 mousemove --sync 3 4 mouseup 1",
            ),
            (
                Scroll(x=1, y=2, direction="left", amount=4),
                "xdotool mousemove --sync 1 2 click --repeat 4 6",
            ),
            (Key(keys="alt+f4"), "xdotool key -- alt+f4"),
        ],
    )
    def test_each_action_becomes_one_command(self, action: NativeAction, command: str):
        assert xdotool(action) == command.split()

    def test_typed_text_is_absent_from_audited_arguments(self):
        text = "a'; rm -rf / #"
        command = xdotool(TypeText(text=text))
        assert command is not None
        assert command[:2] == ["python3", "-c"]
        assert text not in " ".join(command)
        assert command == xdotool(TypeText(text="a different secret"))


class TestTheLaunchAction:
    @pytest.mark.parametrize(
        "app",
        [
            "Firefox",
            "Visual Studio Code",
            "org.gnome.Terminal",
            "Terminál",
            "ImageMagick (color depth=q16)",
            "-".join(["vendor"] * 36),
        ],
    )
    def test_an_applications_name_is_accepted(self, app: str):
        assert Launch(app=app).app == app

    @pytest.mark.parametrize(
        "app",
        [
            "",
            "-a",
            "../../bin/sh",
            "/Applications/Calculator.app",
            "x; rm -rf /",
            "a\\b",
            "a\nb",
            "a" * 256,
        ],
    )
    def test_a_path_an_option_or_a_second_line_is_refused(self, app: str):
        """It is a command's argument in the sandbox and computerd's launcher reads one line."""
        with pytest.raises(ValidationError):
            Launch(app=app)
