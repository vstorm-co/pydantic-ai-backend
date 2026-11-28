"""Native X11 commands; text goes through stdin rather than audited argv."""

from __future__ import annotations

from ._actions import Click, Drag, Key, Move, Scroll, TypeText
from ._types import DesktopError

_X11_KEYS = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "page_up": "Prior",
    "pagedown": "Next",
    "page_down": "Next",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "insert": "Insert",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "cmd": "super",
    "command": "super",
    "super": "super",
    "meta": "super",
    "win": "super",
}

_SCROLL_BUTTONS = {"up": 4, "down": 5, "left": 6, "right": 7}
_MOUSE_BUTTONS = {"left": 1, "middle": 2, "right": 3}


def x11_keys(chord: str) -> str:
    """A chord as `xdotool key` spells it: `ctrl+shift+t`, `Return`, `super`."""
    parts = [part.strip() for part in chord.split("+") if part.strip()]
    if not parts:
        raise DesktopError(f"{chord!r} names no key.")
    return "+".join(_X11_KEYS.get(part.lower(), part) for part in parts)


_PRIVATE_TEXT_ENV = "PYDANTIC_DESKTOP_INPUT_TEXT"
_PRIVATE_TEXT_COMMAND = (
    "import os,subprocess,sys; "
    "payload=os.environ.pop('PYDANTIC_DESKTOP_INPUT_TEXT').encode('utf-8'); "
    "result=subprocess.run(['xdotool','type','--clearmodifiers','--file','-'],"
    "input=payload,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
    "sys.exit(result.returncode)"
)


def xdotool(action: Click | Move | Drag | Scroll | TypeText | Key) -> list[str]:
    """The `xdotool` command that carries out an action on an X11 display.

    Fixed arguments rather than a shell line. Typed text is supplied separately
    through the workspace environment and piped to xdotool, never audited argv.
    """
    match action:
        case Click():
            return [
                *("xdotool", "mousemove", "--sync", str(action.x), str(action.y)),
                *("click", "--repeat", str(action.count), str(_MOUSE_BUTTONS[action.button])),
            ]
        case Move():
            return ["xdotool", "mousemove", "--sync", str(action.x), str(action.y)]
        case Drag():
            return [
                *("xdotool", "mousemove", "--sync", str(action.x), str(action.y), "mousedown", "1"),
                *("mousemove", "--sync", str(action.to_x), str(action.to_y), "mouseup", "1"),
            ]
        case Scroll():
            return [
                *("xdotool", "mousemove", "--sync", str(action.x), str(action.y)),
                *("click", "--repeat", str(action.amount), str(_SCROLL_BUTTONS[action.direction])),
            ]
        case TypeText():
            return ["python3", "-c", _PRIVATE_TEXT_COMMAND]
        case _:
            return ["xdotool", "key", "--", x11_keys(action.keys)]
