"""Load and validate the self-contained real-time Agent configurations."""
from pathlib import Path
import re

import yaml

from mm_agents.realtime_coordinates import CoordinateAdapter


VARIANT_TO_AGENT_ID = {
    "agent1": "vanilla",
    "agent2": "anticipatory",
    "agent3": "video",
    "agent4": "combine",
}


def validate_thinking(model, protocol, enabled, effort):
    if model == "MiniMax-M3" and enabled:
        if protocol != "anthropic_messages":
            raise ValueError("MiniMax-M3 adaptive thinking is configured through Anthropic Messages")
        if effort is not None:
            raise ValueError("MiniMax-M3 exposes adaptive thinking, not a documented effort level; use null")
        return
    efforts = {"low", "medium", "high", "max"}
    if protocol == "openai_responses":
        efforts.add("xhigh")
    if protocol == "openai_chat" and enabled:
        if model != "gemini-3.8-flash":
            raise ValueError("Chat thinking is currently supported only for gemini-3.8-flash")
        efforts = {"low", "medium", "high"}
    if effort is not None and effort not in efforts:
        raise ValueError(f"thinking effort must be one of {sorted(efforts)}")


def validate_key_env(name):
    if name is not None and (
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
    ):
        raise ValueError("api.key_env must name an environment variable, not contain a key")


def default_config_path(variant, model, root=None):
    agent_id = VARIANT_TO_AGENT_ID[variant]
    base = Path(root) if root else Path(__file__).resolve().parents[1]
    return base / "configs" / "realtime_agents" / f"{agent_id}-{model}.yaml"


def load_realtime_config(path, *, variant=None):
    path = Path(path)
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError("Realtime Agent config must be a YAML mapping.")
    required = {
        "config_version", "agent_id", "display_name", "description",
        "observation", "action", "context", "api", "constraints", "system_prompt",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError("Realtime Agent config is missing: " + ", ".join(missing))
    if set(config) - required:
        raise ValueError("Unknown realtime Agent config fields")
    for name in ("action", "api", "observation", "context", "constraints"):
        if not isinstance(config[name], dict):
            raise ValueError(f"{name} must be a mapping")
    if config["config_version"] != "realtime-agent-config/1.0":
        raise ValueError("Unsupported realtime Agent config version.")
    if variant is not None and VARIANT_TO_AGENT_ID.get(variant) != config["agent_id"]:
        raise ValueError("Config agent_id does not match the selected variant.")
    if config["agent_id"] not in VARIANT_TO_AGENT_ID.values():
        raise ValueError("Unknown agent_id")
    action = config["action"]
    api = config["api"]
    observation = config["observation"]
    context = config["context"]
    constraints = config["constraints"]
    protocol = api.get("protocol")
    thinking = api.get("thinking")
    if "coordinate_mapping" in api:
        raise ValueError("coordinate_mapping was removed; use api.coordinate_system")
    CoordinateAdapter(api.get("coordinate_system", "native_pixels"))
    if not isinstance(api.get("model"), str) or not api["model"].strip():
        raise ValueError("api.model must be a non-empty string")
    if (action.get("allow_done") is not True or action.get("allow_fail") is not False
            or action.get("allow_wait") is not True):
        raise ValueError("Realtime Agents require DONE/WAIT and forbid FAIL.")
    if action.get("mode") not in {"atomic", "sequence"}:
        raise ValueError("action.mode must be atomic or sequence.")
    if type(action.get("max_actions_per_turn")) is not int or not 1 <= action["max_actions_per_turn"] <= 100:
        raise ValueError("max_actions_per_turn must be between 1 and 100.")
    if action["mode"] == "atomic" and action["max_actions_per_turn"] != 1:
        raise ValueError("atomic Agents must allow exactly one action per turn.")
    if observation.get("current_screenshot") is not True:
        raise ValueError("current_screenshot must be enabled.")
    video = observation.get("historical_video")
    if not isinstance(video, dict):
        raise ValueError("observation.historical_video must be a mapping")
    expected_sequence = config["agent_id"] in {"anticipatory", "combine"}
    expected_frames = config["agent_id"] in {"video", "combine"}
    if action["mode"] != ("sequence" if expected_sequence else "atomic"):
        raise ValueError("action.mode conflicts with agent_id")
    if video.get("enabled") is not expected_frames:
        raise ValueError("historical_video.enabled conflicts with agent_id")
    if video.get("tool_name") != "get_frames" or type(video.get("max_times_per_query")) is not int or video["max_times_per_query"] != 8:
        raise ValueError("Historical frame tool must be get_frames with max_times_per_query=8")
    if type(video.get("max_queries_per_turn")) is not int or video["max_queries_per_turn"] < 0:
        raise ValueError("max_queries_per_turn must be a nonnegative integer")
    if not expected_frames and video["max_queries_per_turn"] != 0:
        raise ValueError("Agents without video must set max_queries_per_turn=0")
    if api.get("tool_format") != "native":
        raise ValueError("Realtime configs must use native tool use.")
    if protocol not in {"anthropic_messages", "openai_responses", "openai_chat"}:
        raise ValueError("Unsupported realtime API protocol.")
    validate_key_env(api.get("key_env"))
    if not isinstance(thinking, dict):
        raise ValueError("api.thinking must be a mapping.")
    if thinking.get("enabled") is not True:
        raise ValueError("api.thinking.enabled must be true for realtime experiments.")
    if thinking.get("summary") is not True:
        raise ValueError("api.thinking.summary must be true for realtime experiments.")
    if thinking.get("effort") is None and api["model"] != "MiniMax-M3":
        raise ValueError("api.thinking.effort is required")
    validate_thinking(api["model"], protocol, True, thinking.get("effort"))
    if type(api.get("context_window_tokens")) is not int or api["context_window_tokens"] < 128000:
        raise ValueError("Realtime context window must be at least 128000 tokens.")
    if type(api.get("max_output_tokens")) is not int or api["max_output_tokens"] <= 0:
        raise ValueError("max_output_tokens must be a positive integer.")
    if api["max_output_tokens"] > api["context_window_tokens"]:
        raise ValueError("max_output_tokens must not exceed context_window_tokens.")
    if api["model"] == "gemini-3.8-flash" and api["max_output_tokens"] > 65536:
        raise ValueError("gemini-3.8-flash supports at most 65536 output tokens.")
    if "temperature" not in api:
        raise ValueError("api.temperature is required; use null when thinking is enabled")
    temperature = api["temperature"]
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0 <= temperature <= 2
    ):
        raise ValueError("api.temperature must be null or a number between 0 and 2")
    if (context.get("history_policy") != "full" or context.get("include_screenshots") is not True
            or context.get("on_context_limit") != "fail_with_explicit_error"):
        raise ValueError("Realtime configs must preserve complete screenshot history.")
    if set(constraints) != {"forbid_refresh", "forbid_navigation", "require_done_action", "forbid_text_only_completion"} or any(value is not True for value in constraints.values()):
        raise ValueError("Realtime safety and completion constraints cannot be disabled.")
    if not isinstance(config["system_prompt"], str) or not config["system_prompt"].strip():
        raise ValueError("system_prompt must be a non-empty string.")
    return config


def agent_kwargs(config):
    """Translate validated YAML fields to ``RealtimeAgent`` constructor values."""
    observation = config["observation"]["historical_video"]
    action = config["action"]
    api = config["api"]
    thinking = api["thinking"]
    return {
        "model": api["model"],
        "sequence": action["mode"] == "sequence",
        "frames": observation["enabled"],
        "api_format": api["protocol"],
        "api_key_env": api.get("key_env"),
        "coordinate_system": api.get("coordinate_system", "native_pixels"),
        "max_tokens": api["max_output_tokens"],
        "temperature": api["temperature"],
        "max_trajectory_length": None,
        "max_sequence_actions": action["max_actions_per_turn"],
        "max_frame_queries": observation["max_queries_per_turn"],
        "tool_format": api["tool_format"],
        "thinking_enabled": thinking["enabled"],
        "thinking_effort": thinking.get("effort"),
        "thinking_summary": thinking["summary"],
        "system_prompt_text": config["system_prompt"],
    }
