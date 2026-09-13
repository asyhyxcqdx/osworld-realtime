"""The four ablations share the existing computer_13 action vocabulary."""
import json
import math
import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from desktop_env.actions import ACTION_SPACE, KEYBOARD_KEYS


@dataclass(frozen=True)
class AgentMode:
    sequence: bool
    frames: bool


MODES = {
    "agent1": AgentMode(False, False),
    "agent2": AgentMode(True, False),
    "agent3": AgentMode(False, True),
    "agent4": AgentMode(True, True),
}


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


def load_output(text):
    """One JSON payload only; never silently execute multiple fenced blocks."""
    text = text.strip()
    blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text)
    if blocks:
        if len(blocks) != 1:
            raise ValueError("Return one JSON block; put a sequence in one JSON array.")
        text = blocks[0].strip()
    if text in {"DONE", "FAIL", "WAIT"}:
        return {"action_type": text}

    def reject_constant(value):
        raise ValueError(f"Non-finite JSON number: {value}")

    return json.loads(text, parse_constant=reject_constant)


def validate_action(action):
    if not isinstance(action, dict) or set(action) - {"action_type", "parameters"}:
        raise ValueError("An action has action_type and optional parameters only.")
    spec = next(
        (s for s in ACTION_SPACE if s["action_type"] == action.get("action_type")), None
    )
    if spec is None:
        raise ValueError("Unknown action_type.")
    params = action.get("parameters", {})
    specs = spec.get("parameters", {})
    if not isinstance(params, dict) or set(params) - set(specs):
        raise ValueError("Unknown action parameter (WAIT has no duration parameter).")
    for name, rule in specs.items():
        if name not in params:
            if not rule["optional"]:
                raise ValueError(f"Missing {name} for {spec['action_type']}.")
            continue
        value = params[name]
        kind = rule["type"]
        valid_type = (
            isinstance(value, (int, float))
            if kind is float
            else isinstance(value, kind)
        )
        if isinstance(value, bool) or not valid_type:
            raise ValueError(f"Invalid type for {name}.")
        if kind in {float, int} and not math.isfinite(value):
            raise ValueError(f"Non-finite {name}.")
        limits = rule.get("range")
        if kind is float and limits and not limits[0] <= value <= limits[1]:
            raise ValueError(f"Coordinate {name} outside screen bounds.")
        if name in {"button", "num_clicks", "key"} and value not in limits:
            raise ValueError(f"Invalid {name}; use the existing action vocabulary.")
        if name == "keys" and (not value or any(k not in KEYBOARD_KEYS for k in value)):
            raise ValueError("Invalid HOTKEY keys.")
    if ("x" in params) != ("y" in params):
        raise ValueError("Supply x and y together.")
    return action


def parse_actions(text, *, sequence=False, max_actions=16):
    value = load_output(text)
    actions = value if isinstance(value, list) else [value]
    if not 1 <= len(actions) <= (max_actions if sequence else 1):
        raise ValueError("Invalid action count for this agent group.")
    for i, action in enumerate(actions):
        validate_action(action)
        if action["action_type"] in {"DONE", "FAIL"} and i != len(actions) - 1:
            raise ValueError("DONE/FAIL must be the last action.")
    return actions


def system_prompt(mode, pause, max_actions, max_queries):
    # Serialize the actual vocabulary, avoiding stale examples and str.format braces.
    vocabulary = json.dumps(
        ACTION_SPACE, default=lambda t: t.__name__, ensure_ascii=False
    )
    output = (
        f"Return one JSON array containing 1-{max_actions} actions. "
        "Actions execute consecutively in the VM with one final screenshot. "
        "No automatic gaps are inserted. "
        if mode.sequence
        else "Return exactly one action JSON object, never a list or multiple actions. "
    )
    prompt = (
        "You control a desktop using screenshots and the following existing action space. "
        'Use {"action_type": NAME, "parameters": {...}}. Coordinates are screen pixels. '
        "Use key names exactly as listed (a space character ' ' is the space bar). "
        "Do not invent action parameters or execute Python. DONE ends the task; FAIL gives up. "
        f"WAIT has no parameters. pause={pause} seconds. "
        + (
            f"WAIT sleeps once for {pause} seconds. "
            if mode.sequence
            else f"Ordinary actions sleep {pause} seconds before observation; legacy WAIT sleeps twice. "
        )
        + "MOVE_TO duration stays random 0.5-1 seconds; DRAG_TO stays 1 second. "
        + output
        + "You may explain briefly before the single JSON block.\n"
        + vocabulary
    )
    if mode.frames:
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
            "Request a tool OR commit actions in a reply; do not mix them."
        )
    return prompt
