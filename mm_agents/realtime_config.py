"""Load and validate the self-contained real-time Agent configurations."""
from pathlib import Path

import yaml


VARIANT_TO_AGENT_ID = {
    "agent1": "vanilla",
    "agent2": "anticipatory",
    "agent3": "video",
    "agent4": "combine",
}


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
    if config["config_version"] != "realtime-agent-config/1.0":
        raise ValueError("Unsupported realtime Agent config version.")
    if variant is not None and VARIANT_TO_AGENT_ID.get(variant) != config["agent_id"]:
        raise ValueError("Config agent_id does not match the selected variant.")
    action = config["action"]
    api = config["api"]
    observation = config["observation"]
    context = config["context"]
    constraints = config["constraints"]
    protocol = api.get("protocol")
    thinking = api.get("thinking")
    coordinate_mapping = api.get("coordinate_mapping")
    if action.get("allow_done") is not True or action.get("allow_fail") is not False:
        raise ValueError("Realtime Agents allow DONE and forbid FAIL.")
    if action.get("mode") not in {"atomic", "sequence"}:
        raise ValueError("action.mode must be atomic or sequence.")
    if not isinstance(action.get("max_actions_per_turn"), int) or not 1 <= action["max_actions_per_turn"] <= 100:
        raise ValueError("max_actions_per_turn must be between 1 and 100.")
    if action["mode"] == "atomic" and action["max_actions_per_turn"] != 1:
        raise ValueError("atomic Agents must allow exactly one action per turn.")
    if observation.get("current_screenshot") is not True:
        raise ValueError("current_screenshot must be enabled.")
    if api.get("tool_format") != "native":
        raise ValueError("Realtime configs must use native tool use.")
    if protocol not in {"anthropic_messages", "openai_responses"}:
        raise ValueError("Realtime configs require Anthropic Messages or OpenAI Responses.")
    if not isinstance(thinking, dict):
        raise ValueError("api.thinking must be a mapping.")
    if thinking.get("enabled") is not True:
        raise ValueError("api.thinking.enabled must be true for realtime experiments.")
    if thinking.get("summary") is not True:
        raise ValueError("api.thinking.summary must be true for realtime experiments.")
    efforts = {"low", "medium", "high", "max"}
    if protocol == "openai_responses":
        efforts.add("xhigh")
    if thinking.get("effort") not in efforts:
        raise ValueError(f"api.thinking.effort must be one of {sorted(efforts)}.")
    if coordinate_mapping is not None:
        if not isinstance(coordinate_mapping, dict):
            raise ValueError("api.coordinate_mapping must be a mapping.")
        required_mapping = {"source_width", "source_height", "target_width", "target_height"}
        if set(coordinate_mapping) != required_mapping:
            raise ValueError(
                "api.coordinate_mapping must contain exactly: "
                + ", ".join(sorted(required_mapping))
            )
        if any(
            not isinstance(coordinate_mapping[key], int)
            or coordinate_mapping[key] <= 0
            for key in required_mapping
        ):
            raise ValueError("api.coordinate_mapping dimensions must be positive integers.")
        if protocol != "anthropic_messages":
            raise ValueError("api.coordinate_mapping is only supported for Anthropic Messages.")
    if api.get("context_window_tokens", 0) < 128000 or api.get("max_output_tokens", 0) < 128000:
        raise ValueError("Realtime API limits must be at least 128000 tokens.")
    if context.get("history_policy") != "full" or context.get("include_screenshots") is not True:
        raise ValueError("Realtime configs must preserve complete screenshot history.")
    if any(value is not True for value in constraints.values()):
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
        "max_tokens": api["max_output_tokens"],
        "temperature": api["temperature"],
        "max_trajectory_length": None,
        "max_sequence_actions": action["max_actions_per_turn"],
        "max_frame_queries": observation["max_queries_per_turn"],
        "tool_format": api["tool_format"],
        "thinking_enabled": thinking["enabled"],
        "thinking_effort": thinking["effort"],
        "thinking_summary": thinking["summary"],
        "coordinate_mapping": config["api"].get("coordinate_mapping"),
        "system_prompt_text": config["system_prompt"],
    }
