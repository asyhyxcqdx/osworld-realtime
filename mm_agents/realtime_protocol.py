"""The four ablations share the existing computer_13 action vocabulary."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from desktop_env.actions import (
    ACTION_DEFINITION_BY_TYPE,
    ACTION_DEFINITIONS,
)


class GetFramesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    times_s: list[
        Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
    ] = Field(
        min_length=1,
        max_length=8,
        description="Task-relative timestamps in seconds. Returns the nearest recorded frame for each time.",
    )


FRAME_TOOL = {
    "name": "get_frames",
    "description": (
        "Read historical screen images at 1-8 task-relative times in seconds. "
        "Results come back in the same order as the times you send, so the first "
        "result answers your first time. Each result has actual_time_s and status, "
        "where actual_time_s is the timestamp of the frame actually returned: the "
        "nearest recorded frame to the time you asked for (the recording runs at "
        "about 30 frames per second). Use actual_time_s, not the time you "
        "requested, for every comparison between frames. "
        "ok includes an image; not_ready means that time is not yet readable "
        "and includes available_until_s; error means decoding failed. "
        "No keyboard or mouse action is executed. You may query again before acting."
    ),
    "parameters": GetFramesArgs.model_json_schema(),
}


def _action_tool_name(action_type):
    return f"computer_{action_type.lower()}"


def _provider_parameters_schema(model):
    schema = model.model_json_schema()

    def clean(node):
        if isinstance(node, dict):
            node.pop("title", None)
            node.pop("default", None)
            nullable = node.get("anyOf")
            if isinstance(nullable, list) and len(nullable) == 2:
                non_null = [item for item in nullable if item.get("type") != "null"]
                if len(non_null) == 1:
                    node.clear()
                    node.update(clean(non_null[0]))
                    return node
            for value in node.values():
                clean(value)
        elif isinstance(node, list):
            for value in node:
                clean(value)
        return node

    return clean(schema)


# Notes that only this benchmark's agents see. The shared ACTION_DEFINITIONS in
# desktop_env/actions.py stay untouched, because other agents (and the VM side) use
# them too. WAIT is the only action whose shared note says nothing about what it is
# for in a live game: it does not pause anything, it spends the requested time while
# the world keeps running, which is what makes it the way to control timing. The
# sentence deliberately says nothing about "the next action" or "a sequence":
# vanilla/video submit exactly one action per response and have no sequence at all,
# and the prompt already owns how actions are laid out (single action vs one batch).
REALTIME_ACTION_NOTES = {
    "WAIT": (
        "wait for the specified duration. It does not pause the game: the world keeps running "
        "while it spends exactly that much time, so use it to make a known amount of time pass."
    ),
}


def _build_action_tools():
    tools = []
    names = {}
    for definition in ACTION_DEFINITIONS:
        action_type = definition.action_type
        parameters = _provider_parameters_schema(definition.parameters_model)
        tool_name = _action_tool_name(action_type)
        names[tool_name] = action_type
        tools.append(
            {
                "name": tool_name,
                "description": REALTIME_ACTION_NOTES.get(action_type, definition.note),
                "parameters": parameters,
            }
        )
    return tools, names


ACTION_TOOLS, ACTION_TOOL_TYPES = _build_action_tools()


class AgentProtocolError(ValueError):
    """The model's own reply broke the action protocol, so the episode scores 0.

    Distinct from infrastructure failures (HTTP, streaming, VM): those leave the
    task without a score and are retried, while a protocol violation is a real
    failure of the model that must be recorded.
    """


class ForbiddenShortcutError(AgentProtocolError):
    """Raised when an action would leave or inspect the game page."""


_BLOCKED_SINGLE_KEYS = {
    "f5",
    "f12",
    "browserrefresh",
    "browserback",
    "browserforward",
    "browserhome",
    # Focus and confirmation keys. No game maps them in any encoding (0/69 by
    # e.key, e.code or keyCode), but they walk focus out of the page into the
    # browser chrome, where Enter on the address bar reloads the page and voids
    # the run. Blocking them turns an accidental reload into a correctable error.
    "tab",
    "enter",
    "return",
    "esc",
    "escape",
}
# Subset that is blocked for a different reason, so the correction the model
# reads names the actual mistake instead of talking about refreshing.
_FOCUS_KEYS = {"tab", "enter", "return", "esc", "escape"}
_BLOCKED_COMBINATIONS = {
    frozenset({"ctrl", "r"}),
    frozenset({"ctrl", "shift", "r"}),
    frozenset({"ctrl", "shift", "i"}),
    frozenset({"ctrl", "shift", "j"}),
    frozenset({"ctrl", "shift", "c"}),
    frozenset({"ctrl", "u"}),
    frozenset({"ctrl", "l"}),
    frozenset({"ctrl", "s"}),
    frozenset({"ctrl", "p"}),
    frozenset({"alt", "left"}),
    frozenset({"alt", "right"}),
}


def _normalise_key(key):
    value = str(key).lower()
    modifier_aliases = {
        "ctrlleft": "ctrl",
        "ctrlright": "ctrl",
        "shiftleft": "shift",
        "shiftright": "shift",
        "altleft": "alt",
        "altright": "alt",
        "winleft": "win",
        "winright": "win",
        "optionleft": "option",
        "optionright": "option",
    }
    return modifier_aliases.get(value, value)


def _is_blocked_key_sequence(keys):
    normalised = {_normalise_key(key) for key in keys}
    return bool(normalised & _BLOCKED_SINGLE_KEYS) or any(
        combination <= normalised for combination in _BLOCKED_COMBINATIONS
    )


def _blocked_key_message(keys):
    """Say what actually went wrong, so the correction the model reads is useful."""
    normalised = {_normalise_key(key) for key in keys}
    if normalised & _FOCUS_KEYS:
        return (
            "Tab, Enter and Escape are not game controls: they move keyboard focus out of the "
            "game page, and Enter there reloads it, which invalidates the run. Use the game's "
            "visible controls instead."
        )
    return "Refreshing or navigating away from the game page is forbidden."


def validate_action(action):
    if not isinstance(action, dict) or set(action) - {"action_type", "parameters"}:
        raise ValueError("An action has action_type and optional parameters only.")
    action_type = action.get("action_type")
    definition = ACTION_DEFINITION_BY_TYPE.get(action_type)
    if definition is None:
        if action_type == "FAIL":
            raise ValueError("FAIL is not an agent action; submit DONE only after terminal game state.")
        raise ValueError("Unknown action_type.")
    params = action.get("parameters", {})
    if not isinstance(params, dict):
        raise ValueError("Action parameters must be an object.")
    try:
        definition.parameters_model.model_validate(params)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    if action_type == "PRESS" and _is_blocked_key_sequence([params["key"]]):
        raise ForbiddenShortcutError(_blocked_key_message([params["key"]]))
    if action_type == "HOTKEY":
        if _is_blocked_key_sequence(params.get("keys", [])):
            raise ForbiddenShortcutError(_blocked_key_message(params.get("keys", [])))
    return action


def validate_action_sequence(actions, *, held_keys=None):
    """Validate actions and reject blocked shortcuts split across key events."""
    held = set(held_keys or ())
    for action in actions:
        validate_action(action)
        action_type = action["action_type"]
        params = action.get("parameters", {})
        if action_type == "KEY_DOWN":
            held.add(_normalise_key(params["key"]))
            if _is_blocked_key_sequence(held):
                raise ForbiddenShortcutError("Refreshing or navigating away from the game page is forbidden.")
        elif action_type == "PRESS":
            if _is_blocked_key_sequence(held | {_normalise_key(params["key"])}):
                raise ForbiddenShortcutError("Refreshing or navigating away from the game page is forbidden.")
        elif action_type == "HOTKEY":
            if _is_blocked_key_sequence(held | {_normalise_key(key) for key in params["keys"]}):
                raise ForbiddenShortcutError("Refreshing or navigating away from the game page is forbidden.")
        elif action_type == "KEY_UP":
            held.discard(_normalise_key(params["key"]))
    if held_keys is not None:
        held_keys.clear()
        held_keys.update(held)
    return actions
