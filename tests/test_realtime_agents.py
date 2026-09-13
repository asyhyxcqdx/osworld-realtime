import base64
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mm_agents.realtime_agent import ModelWire, RealtimeAgent, response_reasoning
from mm_agents.realtime_protocol import (
    GetFramesArgs,
    parse_actions,
    MODES,
    system_prompt,
)


ACTION = {"action_type": "PRESS", "parameters": {"key": " "}}
IMAGE = {
    "type": "base64",
    "media_type": "image/png",
    "data": base64.b64encode(b"png").decode(),
}


def test_current_vocabulary_and_strict_single_action():
    assert parse_actions(json.dumps(ACTION)) == [ACTION]
    with pytest.raises(ValueError):
        parse_actions(json.dumps([ACTION, ACTION]))
    with pytest.raises(ValueError):
        parse_actions(
            "```json\n"
            + json.dumps(ACTION)
            + "\n```\n```json\n"
            + json.dumps(ACTION)
            + "\n```",
            sequence=True,
        )
    with pytest.raises(ValueError):
        parse_actions('{"action_type":"WAIT","parameters":{"duration_s":0.5}}')
    with pytest.raises(ValueError):
        parse_actions(json.dumps([{"action_type": "DONE"}, ACTION]), sequence=True)
    assert parse_actions(json.dumps([ACTION, ACTION]), sequence=True) == [
        ACTION,
        ACTION,
    ]


def test_legacy_prompt_initialization_preserves_json_braces():
    from mm_agents.agent import PromptAgent

    agent = PromptAgent(
        action_space="computer_13",
        observation_type="screenshot",
        client_password="p{q}",
    )
    assert '"action_type"' in agent.system_message


@pytest.mark.parametrize(
    "value", [[-1], [float("nan")], [float("inf")], [], [1] * 9, [True], ["1"]]
)
def test_invalid_frame_times(value):
    with pytest.raises(ValueError):
        GetFramesArgs(times_s=value)


def native_reply(protocol, *, tool=None, text=None):
    if protocol == "anthropic_messages":
        return {
            "content": (
                [
                    {
                        "type": "tool_use",
                        "id": tool,
                        "name": "get_frames",
                        "input": {"times_s": [0.1]},
                    }
                ]
                if tool
                else [{"type": "text", "text": text}]
            )
        }
    if protocol == "openai_chat":
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        **(
                            {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": tool,
                                        "type": "function",
                                        "function": {
                                            "name": "get_frames",
                                            "arguments": '{"times_s":[0.1]}',
                                        },
                                    }
                                ],
                            }
                            if tool
                            else {"content": text}
                        ),
                    }
                }
            ]
        }
    return {
        "output": (
            [
                {
                    "type": "function_call",
                    "call_id": tool,
                    "name": "get_frames",
                    "arguments": '{"times_s":[0.1]}',
                }
            ]
            if tool
            else [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        )
    }


@pytest.mark.parametrize(
    "protocol", ["anthropic_messages", "openai_chat", "openai_responses"]
)
@pytest.mark.parametrize("variant", list(MODES))
def test_four_variants_and_repeated_frame_queries(protocol, variant):
    mode = MODES[variant]
    wire = ModelWire("mock", protocol)
    actions = [ACTION, ACTION] if mode.sequence else ACTION
    replies = (
        [native_reply(protocol, tool="q1"), native_reply(protocol, tool="q2")]
        if mode.frames
        else []
    )
    replies.append(native_reply(protocol, text=json.dumps(actions)))
    requests = []

    def request(system, messages, **kwargs):
        requests.append((copy.deepcopy(messages), kwargs))
        return replies.pop(0)

    wire.request = request
    agent = RealtimeAgent(variant=variant, wire=wire)
    query = Mock(
        return_value={
            "frames": [
                {
                    "requested_time_s": 0.1,
                    "actual_time_s": 0.1,
                    "status": "ok",
                    "image": IMAGE,
                }
            ]
        }
    )
    agent.bind_frame_query(query)
    response, result = agent.predict("test", {"screenshot": b"png", "task_time_s": 1.0})
    assert result == ([ACTION, ACTION] if mode.sequence else [ACTION])
    assert query.call_count == (2 if mode.frames else 0)
    assert agent.counters["model_requests"] == (3 if mode.frames else 1)
    assert all(r[1]["tools_enabled"] == mode.frames for r in requests)
    if mode.frames:
        serialized = json.dumps(requests[-1][0])
        assert "q1" in serialized and "q2" in serialized
        assert "actual_time_s" in serialized and IMAGE["data"] in serialized
        assert agent.counters["images_returned"] == 2


def test_json_tool_output_and_query_budget():
    wire = ModelWire("mock", "anthropic_messages")
    replies = [
        native_reply(
            wire.protocol, text='{"tool_call":{"tool_name":"get_frames","times_s":[1]}}'
        ),
        native_reply(wire.protocol, text=json.dumps(ACTION)),
    ]
    wire.request = Mock(side_effect=replies)
    agent = RealtimeAgent(
        variant="agent3", wire=wire, tool_format="json", max_frame_queries=1
    )
    agent.bind_frame_query(
        lambda _: {
            "frames": [
                {"status": "not_ready", "requested_time_s": 1, "available_until_s": 0.9}
            ]
        }
    )
    assert agent.predict("t", {"screenshot": b"png", "task_time_s": 1})[1] == [ACTION]
    assert not wire.request.call_args.kwargs["tools_enabled"]
    messages = wire.request.call_args.args[1]
    assert "not_ready" in json.dumps(messages)
    assert agent.counters["images_returned"] == 0


def test_single_action_repair_does_not_execute_first_invalid_action():
    wire = ModelWire("mock", "openai_chat")
    wire.request = Mock(
        side_effect=[
            native_reply(wire.protocol, text=json.dumps([ACTION, ACTION])),
            native_reply(wire.protocol, text=json.dumps(ACTION)),
        ]
    )
    agent = RealtimeAgent(wire=wire)
    assert agent.predict("t", {"screenshot": b"png", "task_time_s": 0})[1] == [ACTION]
    assert agent.counters["model_requests"] == 2


def test_action_budget_is_not_silently_truncated():
    wire = ModelWire("mock", "openai_chat")
    wire.request = Mock(
        side_effect=[
            native_reply(wire.protocol, text=json.dumps([ACTION, ACTION])),
            native_reply(wire.protocol, text=json.dumps(ACTION)),
        ]
    )
    agent = RealtimeAgent(variant="agent2", wire=wire)
    assert agent.predict(
        "t", {"screenshot": b"png", "task_time_s": 0, "remaining_actions": 1}
    )[1] == [ACTION]


def test_chat_parallel_results_precede_images():
    wire = ModelWire("mock", "openai_chat")
    result = {"frames": [{"status": "ok", "image": IMAGE}]}
    messages = wire.tool_results([({"id": "a"}, result), ({"id": "b"}, result)])
    assert [m["role"] for m in messages] == ["tool", "tool", "user"]
    assert messages[0]["tool_call_id"] == "a"


def test_sequence_controller_uses_one_http_request_and_original_durations(monkeypatch):
    from desktop_env.controllers.python import PythonController

    controller = object.__new__(PythonController)
    controller._realtime_request = Mock(return_value={"done": False})
    monkeypatch.setattr(
        "desktop_env.controllers.python.random.uniform", lambda *_: 0.75
    )
    controller.execute_sequence(
        [
            {"action_type": "MOVE_TO", "parameters": {"x": 10, "y": 20}},
            ACTION,
            {"action_type": "WAIT"},
        ],
        0.5,
    )
    assert controller._realtime_request.call_count == 1
    payload = controller._realtime_request.call_args.args[2]
    assert "0.75" in payload["groups"][0]["commands"][0]
    assert payload["groups"][2]["commands"] == []
    assert payload["pause"] == 0.5


def test_env_sequence_observes_once():
    from desktop_env.desktop_env import DesktopEnv

    env = object.__new__(DesktopEnv)
    env.action_space, env.action_history, env._step_no = "computer_13", [], 0
    env.controller = SimpleNamespace(
        execute_sequence=Mock(
            return_value={
                "actions": [{"action": ACTION}, {"action": ACTION}],
                "done": False,
                "info": {},
            }
        )
    )
    env._get_obs = Mock(return_value={"screenshot": b"png"})
    env.step_sequence([ACTION, ACTION])
    assert env._get_obs.call_count == 1
    assert env.action_history == [ACTION, ACTION] and env._step_no == 2


@pytest.mark.parametrize("protocol", ["anthropic_messages", "openai_chat", "openai_responses"])
@pytest.mark.parametrize("variant", ["agent3", "agent4"])
def test_default_queries_have_no_query_or_request_count_limit(protocol, variant):
    wire = ModelWire("mock", protocol)
    action = [ACTION] if MODES[variant].sequence else ACTION
    wire.request = Mock(side_effect=[
        *(native_reply(protocol, tool=f"q{i}") for i in range(12)),
        native_reply(protocol, text=json.dumps(action)),
    ])
    agent = RealtimeAgent(variant=variant, wire=wire)
    agent.bind_frame_query(Mock(return_value={"frames": [{"status": "not_ready"}]}))
    assert agent.predict("t", {"screenshot": b"png", "task_time_s": 1})[1] == [ACTION]
    assert agent.counters["frame_queries"] == 12
    assert wire.request.call_count == 13
    assert all(c.kwargs["tools_enabled"] for c in wire.request.call_args_list)
    assert "no query-count limit" in agent.system


@pytest.mark.parametrize("protocol", ["anthropic_messages", "openai_chat", "openai_responses"])
def test_provider_reasoning_is_logged_and_preserved_in_tool_history(protocol):
    wire = ModelWire("mock", protocol)
    first = native_reply(protocol, tool="q1")
    if protocol == "anthropic_messages":
        first["content"].insert(0, {"type": "thinking", "thinking": "Query the earlier image.", "signature": "opaque-signature"})
    elif protocol == "openai_responses":
        first["output"].insert(0, {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Query the earlier image."}], "encrypted_content": "opaque-signature"})
    else:
        first["choices"][0]["message"]["reasoning_content"] = "Query the earlier image."
    captured = []

    def request(system, messages, **kwargs):
        if not captured:
            captured.append(copy.deepcopy(first))
            return first
        assert "Query the earlier image." in json.dumps(messages)
        if protocol != "openai_chat":
            assert "opaque-signature" in json.dumps(messages)
        return native_reply(protocol, text=json.dumps(ACTION))

    wire.request = request
    agent = RealtimeAgent(variant="agent3", wire=wire)
    agent.bind_frame_query(lambda _: {"frames": []})
    events = []
    agent.bind_event_sink(events.append)
    agent.predict("t", {"screenshot": b"png", "task_time_s": 1, "screenshot_file": "initial_state.png"})
    response = next(e for e in events if e["event"] == "model_response")
    assert response["provider_response"] == captured[0]
    assert response["reasoning"] == ["Query the earlier image."]
    assert response["reasoning_status"] == "returned"
    assert events[0]["observation"]["screenshot_file"] == "initial_state.png"
    assert "reasoning" not in agent.predict("t", {"screenshot": b"next", "task_time_s": 2})[1][0]
    request_events = [e for e in events if e["event"] == "model_request"]
    assert request_events[-1]["history_observations"][0]["screenshot_file"] == "initial_state.png"


def test_absent_or_opaque_reasoning_is_not_invented():
    assert response_reasoning({"content": [{"type": "text", "text": "6"}]}, "anthropic_messages") == {
        "reasoning": [], "reasoning_status": "not_returned",
    }
    assert response_reasoning({"content": [{"type": "thinking", "thinking": "", "signature": "opaque"}]}, "anthropic_messages") == {
        "reasoning": [], "reasoning_status": "opaque",
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_anthropic_thinking_summary_payload_is_explicit(monkeypatch, enabled):
    monkeypatch.setenv("PACKY_API_KEY", "test-key")
    wire = ModelWire("mock", "anthropic_messages", thinking_summary=enabled)
    wire.session.post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {}))
    wire.request("system", [], tools_enabled=False, native=True, max_tokens=1500, temperature=1)
    payload = wire.session.post.call_args.kwargs["json"]
    if enabled:
        assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert "temperature" not in payload
    else:
        assert "thinking" not in payload
        assert payload["temperature"] == 1
    assert payload["max_tokens"] == 1500
