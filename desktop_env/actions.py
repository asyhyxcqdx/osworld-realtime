from typing import Annotated, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, model_validator


X_MAX = 1920
Y_MAX = 1080

KEYBOARD_KEYS = ['\t', '\n', '\r', 'space', '!', '"', '#', '$', '%', '&', "'", '(', ')', '*', '+', ',', '-', '.', '/', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', ':', ';', '<', '=', '>', '?', '@', '[', '\\', ']', '^', '_', '`', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', '{', '|', '}', '~', 'accept', 'add', 'alt', 'altleft', 'altright', 'apps', 'backspace', 'browserback', 'browserfavorites', 'browserforward', 'browserhome', 'browserrefresh', 'browsersearch', 'browserstop', 'capslock', 'clear', 'convert', 'ctrl', 'ctrlleft', 'ctrlright', 'decimal', 'del', 'delete', 'divide', 'down', 'end', 'enter', 'esc', 'escape', 'execute', 'f1', 'f10', 'f11', 'f12', 'f13', 'f14', 'f15', 'f16', 'f17', 'f18', 'f19', 'f2', 'f20', 'f21', 'f22', 'f23', 'f24', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'final', 'fn', 'hanguel', 'hangul', 'hanja', 'help', 'home', 'insert', 'junja', 'kana', 'kanji', 'launchapp1', 'launchapp2', 'launchmail', 'launchmediaselect', 'left', 'modechange', 'multiply', 'nexttrack', 'nonconvert', 'num0', 'num1', 'num2', 'num3', 'num4', 'num5', 'num6', 'num7', 'num8', 'num9', 'numlock', 'pagedown', 'pageup', 'pause', 'pgdn', 'pgup', 'playpause', 'prevtrack', 'print', 'printscreen', 'prntscrn', 'prtsc', 'prtscr', 'return', 'right', 'scrolllock', 'select', 'separator', 'shift', 'shiftleft', 'shiftright', 'sleep', 'stop', 'subtract', 'tab', 'up', 'volumedown', 'volumemute', 'volumeup', 'win', 'winleft', 'winright', 'yen', 'command', 'option', 'optionleft', 'optionright']

ScreenX = Annotated[float, Field(ge=0, le=X_MAX, strict=True)]
ScreenY = Annotated[float, Field(ge=0, le=Y_MAX, strict=True)]
Duration = Annotated[float, Field(ge=0, le=10, strict=True)]
WaitDuration = Annotated[float, Field(ge=0, le=60, strict=True)]
KeyName = Literal[tuple(KEYBOARD_KEYS)]
MouseButton = Literal["left", "right", "middle"]


class ActionParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MoveToParameters(ActionParameters):
    x: ScreenX
    y: ScreenY
    duration_s: Duration


class ClickParameters(ActionParameters):
    button: Optional[MouseButton] = None
    x: Optional[ScreenX] = None
    y: Optional[ScreenY] = None
    num_clicks: Optional[int] = Field(default=None, ge=1, le=3, strict=True)

    @model_validator(mode="after")
    def validate_coordinates(self):
        if (self.x is None) != (self.y is None):
            raise ValueError("Supply x and y together.")
        return self


class MouseButtonParameters(ActionParameters):
    button: Optional[MouseButton] = None


class CoordinateParameters(ActionParameters):
    x: Optional[ScreenX] = None
    y: Optional[ScreenY] = None

    @model_validator(mode="after")
    def validate_coordinates(self):
        if (self.x is None) != (self.y is None):
            raise ValueError("Supply x and y together.")
        return self


class DragToParameters(ActionParameters):
    x: ScreenX
    y: ScreenY
    duration_s: Duration


class ScrollParameters(ActionParameters):
    dx: int = Field(strict=True)
    dy: int = Field(strict=True)


class TypingParameters(ActionParameters):
    text: str = Field(strict=True)


class KeyParameters(ActionParameters):
    key: KeyName


class HotkeyParameters(ActionParameters):
    keys: list[KeyName] = Field(min_length=1)


class WaitParameters(ActionParameters):
    duration_s: WaitDuration


class EmptyParameters(ActionParameters):
    pass


class ActionDefinition(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    action_type: str
    note: str
    parameters_model: Type[ActionParameters]


ACTION_DEFINITIONS = (
    ActionDefinition(
        action_type="MOVE_TO",
        note="move the cursor to the specified position",
        parameters_model=MoveToParameters,
    ),
    ActionDefinition(
        action_type="CLICK",
        note="click at the current position or specified position",
        parameters_model=ClickParameters,
    ),
    ActionDefinition(
        action_type="MOUSE_DOWN",
        note="press the specified mouse button",
        parameters_model=MouseButtonParameters,
    ),
    ActionDefinition(
        action_type="MOUSE_UP",
        note="release the specified mouse button",
        parameters_model=MouseButtonParameters,
    ),
    ActionDefinition(
        action_type="RIGHT_CLICK",
        note="right click at the current or specified position",
        parameters_model=CoordinateParameters,
    ),
    ActionDefinition(
        action_type="DOUBLE_CLICK",
        note="double click at the current or specified position",
        parameters_model=CoordinateParameters,
    ),
    ActionDefinition(
        action_type="DRAG_TO",
        note="drag to the specified position with the left button pressed",
        parameters_model=DragToParameters,
    ),
    ActionDefinition(
        action_type="SCROLL",
        note="scroll the mouse wheel horizontally and vertically",
        parameters_model=ScrollParameters,
    ),
    ActionDefinition(
        action_type="TYPING",
        note="type the specified text",
        parameters_model=TypingParameters,
    ),
    ActionDefinition(
        action_type="PRESS",
        note="press and release the specified key",
        parameters_model=KeyParameters,
    ),
    ActionDefinition(
        action_type="KEY_DOWN",
        note="press and hold the specified key",
        parameters_model=KeyParameters,
    ),
    ActionDefinition(
        action_type="KEY_UP",
        note="release the specified key",
        parameters_model=KeyParameters,
    ),
    ActionDefinition(
        action_type="HOTKEY",
        note="press the specified key combination",
        parameters_model=HotkeyParameters,
    ),
    ActionDefinition(
        action_type="WAIT",
        note="wait for the specified duration",
        parameters_model=WaitParameters,
    ),
    ActionDefinition(
        action_type="DONE",
        note="finish the task",
        parameters_model=EmptyParameters,
    ),
)

ACTION_DEFINITION_BY_TYPE = {
    definition.action_type: definition for definition in ACTION_DEFINITIONS
}

ACTION_SPACE = ACTION_DEFINITIONS
REALTIME_ACTION_SPACE = ACTION_DEFINITIONS
