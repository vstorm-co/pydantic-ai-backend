"""Typed desktop actions, and the one place coordinates are scaled.

A client sees a screenshot no larger than the capability's bound - a 1280x800
picture of a 2560x1600 Retina screen, say - and answers in that picture's pixels.
Every backend takes coordinates in its *own* input space: X11 pixels on Linux,
points on macOS. So a click is translated exactly once, here, by the factor the
screenshot was shrunk by, and no backend has to know the client saw anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Button = Literal["left", "right", "middle"]

MAX_TEXT = 2000
"""The longest string one `type` action may carry.

Long enough for a paragraph or a short script, short enough that a model pasting
a document keystroke by keystroke is told to stop rather than left typing for
minutes.
"""


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Click(_Action):
    """Press and release a mouse button at a point."""

    action: Literal["click"] = "click"
    x: int = Field(ge=0, description="Horizontal position, in the latest screenshot's pixels.")
    y: int = Field(ge=0, description="Vertical position, in the latest screenshot's pixels.")
    button: Button = "left"
    count: int = Field(
        default=1, ge=1, le=3, description="2 for a double click, 3 for a triple click."
    )


class Move(_Action):
    """Move the pointer without clicking, to hover or reveal something."""

    action: Literal["move"] = "move"
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class Drag(_Action):
    """Hold the left button at one point and release it at another."""

    action: Literal["drag"] = "drag"
    x: int = Field(ge=0, description="Where the drag starts.")
    y: int = Field(ge=0, description="Where the drag starts.")
    to_x: int = Field(ge=0, description="Where the drag ends.")
    to_y: int = Field(ge=0, description="Where the drag ends.")


class Scroll(_Action):
    """Turn the mouse wheel with the pointer over a point."""

    action: Literal["scroll"] = "scroll"
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = Field(
        default=3, ge=1, le=20, description="Wheel notches; 3 is roughly a few lines."
    )


class TypeText(_Action):
    """Type text at the keyboard focus, as if on a keyboard."""

    action: Literal["type"] = "type"
    text: str = Field(min_length=1, max_length=MAX_TEXT)


class Key(_Action):
    """Press a key or a chord."""

    action: Literal["key"] = "key"
    keys: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "One key or a chord joined with '+': 'enter', 'escape', 'tab', 'ctrl+c', "
            "'cmd+space', 'alt+f4', 'shift+tab', 'f5', 'pagedown'."
        ),
    )


class Launch(_Action):
    """Open an installed application by its name or id, rather than hunting for its icon."""

    action: Literal["launch"] = "launch"
    # Never a path or an option, though it is only ever compared with the
    # menu's names and ids. Other punctuation is harmless, and real names have
    # it: "ImageMagick (color depth=q16)". No control characters: computerd's
    # launcher reads one line. 255 is a file name's limit, so every id
    # `computer_apps` lists, however deep its subdirectory, can be launched.
    app: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^\w[^/\\\x00-\x1f\x7f]*$",
        description=(
            "An application's name or id exactly as `computer_apps` lists it: "
            "'Firefox ESR', 'xfce4-terminal'. The action returns once its window is up."
        ),
    )


class Wait(_Action):
    """Do nothing for a moment, to let something load or animate."""

    action: Literal["wait"] = "wait"
    seconds: float = Field(default=1.0, gt=0, le=10)


NativeAction = Click | Move | Drag | Scroll | TypeText | Key | Launch | Wait

Action = Annotated[NativeAction, Field(discriminator="action")]


@dataclass(frozen=True)
class Screen:
    """How the screenshot the client saw relates to the desktop's input space.

    `width` and `height` are the screenshot's; `native_width` and `native_height`
    are what the backend's pointer moves across. The ratio between them is the
    scale - one number for each axis, because a resize preserves the aspect but
    rounds each side on its own.
    """

    width: int
    height: int
    native_width: int
    native_height: int

    def contains(self, x: int, y: int) -> bool:
        """Whether a point the client named is on the screenshot it was shown."""
        return 0 <= x < self.width and 0 <= y < self.height

    def to_native(self, x: int, y: int) -> tuple[int, int]:
        """A point in the screenshot, as the backend's pointer addresses it."""
        nx = round(x * self.native_width / self.width)
        ny = round(y * self.native_height / self.height)
        return min(nx, self.native_width - 1), min(ny, self.native_height - 1)


def points_of(action: NativeAction) -> list[tuple[int, int]]:
    """Every point an action names, in the order it names them."""
    if isinstance(action, Drag):
        return [(action.x, action.y), (action.to_x, action.to_y)]
    if isinstance(action, Click | Move | Scroll):
        return [(action.x, action.y)]
    return []


def to_native(action: NativeAction, screen: Screen) -> NativeAction:
    """The same action, with every point moved into the backend's input space."""
    if isinstance(action, Drag):
        x, y = screen.to_native(action.x, action.y)
        to_x, to_y = screen.to_native(action.to_x, action.to_y)
        return action.model_copy(update={"x": x, "y": y, "to_x": to_x, "to_y": to_y})
    if isinstance(action, Click | Move | Scroll):
        x, y = screen.to_native(action.x, action.y)
        return action.model_copy(update={"x": x, "y": y})
    return action


def describe(action: NativeAction) -> tuple[str, str]:
    """An action as the live panel lists it: an operation and what it applied to.

    Typed text is shown, because the person watching is the person whose task this
    is and a step reading "TYPE" with nothing after it is a step they cannot check.
    It is cut short, the way the panel cuts a long element label.
    """
    match action:
        case Click():
            label = {1: "CLICK", 2: "DOUBLE_CLICK", 3: "TRIPLE_CLICK"}[action.count]
            suffix = "" if action.button == "left" else f" ({action.button})"
            return label, f"({action.x}, {action.y}){suffix}"
        case Move():
            return "MOVE", f"({action.x}, {action.y})"
        case Drag():
            return "DRAG", f"({action.x}, {action.y}) → ({action.to_x}, {action.to_y})"
        case Scroll():
            return "SCROLL", f"{action.direction} {action.amount} at ({action.x}, {action.y})"
        case TypeText():
            text = action.text if len(action.text) <= 60 else f"{action.text[:57]}..."
            return "TYPE", text
        case Key():
            return "KEY", action.keys
        case Launch():
            return "LAUNCH", action.app
        case _:
            return "WAIT", f"{action.seconds:g}s"
