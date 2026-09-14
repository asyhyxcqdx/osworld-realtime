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
        "Each result has requested_time_s, actual_time_s and status. "
        "ok includes an image; not_ready means the requested time is not yet readable "
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
                "description": definition.note,
                "parameters": parameters,
            }
        )
    return tools, names


ACTION_TOOLS, ACTION_TOOL_TYPES = _build_action_tools()


class ForbiddenShortcutError(ValueError):
    """Raised when an action would leave or inspect the game page."""


_BLOCKED_SINGLE_KEYS = {
    "f5",
    "f12",
    "browserrefresh",
    "browserback",
    "browserforward",
    "browserhome",
}
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
        raise ForbiddenShortcutError("Refreshing or navigating away from the game page is forbidden.")
    if action_type == "HOTKEY":
        if _is_blocked_key_sequence(params.get("keys", [])):
            raise ForbiddenShortcutError("Refreshing or navigating away from the game page is forbidden.")
    return action


def validate_action_sequence(actions):
    """Validate actions and reject blocked shortcuts split across key events."""
    held = set()
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
    return actions


def system_prompt(sequence, frames, max_actions, max_queries):
    output = (
        f"Submit 1-{max_actions} native computer action tool calls. "
        "The calls execute consecutively in the VM with one final screenshot. "
        "No automatic gaps are inserted. "
        if sequence
        else "Submit exactly one native computer action tool call per decision. "
    )
    prompt = (
        "You control a desktop using native computer action tools. The VM screen is 1920x1080, "
        "and all x/y action parameters use that native full-screen pixel coordinate system. "
        "Always submit native screen pixels measured from the top-left corner; do not rescale or multiply coordinates. "
        "Use key names exactly as listed; 'space' is the space bar. "
        "Do not invent action parameters or execute Python. Never refresh, reload, reopen, or navigate away from the game page. "
        "DONE is the only terminal action; submit it only when the game is visibly successful or has exhausted its attempts. "
        "Use the registered tool schemas exactly. No hidden delay is inserted between actions. "
        + output
        + "Do not finish with a text-only response."
    )
    if frames:
        query_budget = (
            f"You may use get_frames up to {max_queries} times before committing actions. "
            if max_queries
            else "You may use get_frames repeatedly before committing actions; there is no query-count limit. "
        )
        prompt += (
            "\n" + query_budget
            + "It reads requested timestamps from a target 30 FPS recording, approximately one frame "
            "every 33.3 ms. You may request fractional seconds; actual sampling intervals can vary. "
            "The nearest recorded frame is returned (earlier on ties), without interpolation. "
            "actual_time_s is the timestamp of the returned frame. not_ready has no image. "
            "Only complete video fragments are readable; their target duration of 100 ms is not "
            "the sampling interval or a guaranteed maximum query delay. "
            "Querying is part of deciding and does not execute an action. Only explicitly queried "
            "historical frames are provided. The screen keeps changing during inference. "
            "Call get_frames before committing action tools when historical evidence is needed."
        )
    return prompt
