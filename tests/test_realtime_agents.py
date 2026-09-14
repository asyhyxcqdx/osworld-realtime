import base64
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mm_agents.realtime_agent import ModelWire, RealtimeAgent, response_reasoning
from mm_agents.realtime_config import agent_kwargs, load_realtime_config
from mm_agents.realtime_protocol import (
    ACTION_TOOLS,
    GetFramesArgs,
    system_prompt,
    validate_action,
)


ACTION = {"action_type": "PRESS", "parameters": {"key": "space"}}
CAPABILITIES = {
    "agent1": SimpleNamespace(sequence=False, frames=False),
    "agent2": SimpleNamespace(sequence=True, frames=False),
    "agent3": SimpleNamespace(sequence=False, frames=True),
    "agent4": SimpleNamespace(sequence=True, frames=True),
}
IMAGE = {
    "type": "base64",
    "media_type": "image/png",
    "data": base64.b64encode(b"png").decode(),
}


def test_action_validation_uses_registered_pydantic_models():
    assert validate_action(ACTION) == ACTION
    with pytest.raises(ValueError):
        validate_action({"action_type": "PRESS", "parameters": {"key": " "}})
    with pytest.raises(ValueError):
        validate_action({"action_type": "WAIT", "parameters": {}})
    with pytest.raises(ValueError):
        validate_action({"action_type": "FAIL"})


def test_action_tools_are_generated_from_pydantic_models():
    from mm_agents.realtime_protocol import ACTION_TOOLS

    move = next(tool for tool in ACTION_TOOLS if tool["name"] == "computer_move_to")
    wait = next(tool for tool in ACTION_TOOLS if tool["name"] == "computer_wait")
    assert move["parameters"]["required"] == ["x", "y", "duration_s"]
    assert move["parameters"]["properties"]["duration_s"]["maximum"] == 10
    assert wait["parameters"]["required"] == ["duration_s"]
    assert "computer_fail" not in {tool["name"] for tool in ACTION_TOOLS}

    press = next(tool for tool in ACTION_TOOLS if tool["name"] == "computer_press")
    key_enum = press["parameters"]["properties"]["key"]["enum"]
    assert "space" in key_enum
    assert " " not in key_enum


@pytest.mark.parametrize(
    "value", [[-1], [float("nan")], [float("inf")], [], [1] * 9, [True], ["1"]]
)
def test_invalid_frame_times(value):
    with pytest.raises(ValueError):
        GetFramesArgs(times_s=value)


def native_reply(protocol, *, tool=None, text=None, force_text=False):
    action_calls = []
    if not force_text and tool is None and text is not None:
        try:
            value = json.loads(text)
            values = value if isinstance(value, list) else [value]
            if values and all(isinstance(item, dict) and "action_type" in item for item in values):
                for index, item in enumerate(values):
                    action_calls.append((f"a{index}", item["action_type"], item.get("parameters", {})))
        except (TypeError, ValueError):
            pass
    if action_calls:
        if protocol == "anthropic_messages":
            return {"content": [{"type": "tool_use", "id": call_id, "name": "computer_" + action_type.lower(), "input": params} for call_id, action_type, params in action_calls]}
        if protocol == "openai_chat":
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "computer_" + action_type.lower(), "arguments": json.dumps(params)}} for call_id, action_type, params in action_calls]}}]}
        return {"output": [{"type": "function_call", "call_id": call_id, "name": "computer_" + action_type.lower(), "arguments": json.dumps(params)} for call_id, action_type, params in action_calls]}
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
@pytest.mark.parametrize("variant", list(CAPABILITIES))
def test_four_variants_and_repeated_frame_queries(protocol, variant):
    mode = CAPABILITIES[variant]
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
    agent = RealtimeAgent(variant=variant, sequence=mode.sequence, frames=mode.frames, wire=wire)
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
    assert all(
        r[1]["parallel_tool_calls"] is (True if mode.sequence else None)
        for r in requests
    )
    if mode.frames:
        serialized = json.dumps(requests[-1][0])
        assert "q1" in serialized and "q2" in serialized
        assert "actual_time_s" in serialized and IMAGE["data"] in serialized
        assert agent.counters["images_returned"] == 2


def test_text_json_tool_output_is_rejected():
    wire = ModelWire("mock", "anthropic_messages")
    with pytest.raises(ValueError, match="native tool use"):
        RealtimeAgent(
            variant="agent3", sequence=False, frames=True, wire=wire, tool_format="json"
        )


def test_single_action_repair_does_not_execute_first_invalid_action():
    wire = ModelWire("mock", "openai_chat")
    wire.request = Mock(
        side_effect=[
            native_reply(wire.protocol, text=json.dumps([ACTION, ACTION])),
            native_reply(wire.protocol, text=json.dumps(ACTION)),
        ]
    )
    agent = RealtimeAgent(sequence=False, frames=False, wire=wire)
    assert agent.predict("t", {"screenshot": b"png", "task_time_s": 0})[1] == [ACTION]
    assert agent.counters["model_requests"] == 2


def test_sequence_action_limit_is_not_silently_truncated():
    wire = ModelWire("mock", "openai_chat")
    wire.request = Mock(
        side_effect=[
            native_reply(wire.protocol, text=json.dumps([ACTION, ACTION])),
            native_reply(wire.protocol, text=json.dumps(ACTION)),
        ]
    )
    agent = RealtimeAgent(
        variant="agent2", sequence=True, frames=False, wire=wire,
        max_sequence_actions=1,
    )
    assert agent.predict(
        "t", {"screenshot": b"png", "task_time_s": 0}
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
            {"action_type": "MOVE_TO", "parameters": {"x": 10, "y": 20, "duration_s": 0.25}},
            ACTION,
            {"action_type": "WAIT", "parameters": {"duration_s": 0.5}},
        ]
    )
    assert controller._realtime_request.call_count == 1
    payload = controller._realtime_request.call_args.args[2]
    assert "0.25" in payload["groups"][0]["commands"][0]
    assert payload["groups"][2]["commands"] == []
    assert "pause" not in payload


def test_env_sequence_observes_once():
    from desktop_env.desktop_env import DesktopEnv

    env = object.__new__(DesktopEnv)
    env.action_space, env.action_history, env._step_no = "computer_13", [], 0
    env._traj_no = 0
    env.is_environment_used = False
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


def test_realtime_single_wait_is_executed_by_vm_without_host_sleep(monkeypatch):
    from desktop_env.desktop_env import DesktopEnv

    env = object.__new__(DesktopEnv)
    env.action_space, env.action_history, env._step_no = "computer_13", [], 0
    env._traj_no = 0
    env.is_environment_used = False
    env.controller = SimpleNamespace(
        realtime_session="session",
        execute_sequence=Mock(
            return_value={"actions": [{"action": {"action_type": "WAIT", "parameters": {"duration_s": 0.5}}}], "done": False, "info": {}}
        ),
    )
    env._get_obs = Mock(return_value={"screenshot": b"png"})
    host_sleep = Mock()
    monkeypatch.setattr("desktop_env.desktop_env.time.sleep", host_sleep)

    _, _, done, info = env.step({"action_type": "WAIT", "parameters": {"duration_s": 0.5}}, 0)

    env.controller.execute_sequence.assert_called_once_with([
        {"action_type": "WAIT", "parameters": {"duration_s": 0.5}}
    ])
    host_sleep.assert_not_called()
    assert not done
    assert info["sequence_actions"][0]["action"]["action_type"] == "WAIT"


def test_drag_to_without_realtime_duration_keeps_legacy_default(monkeypatch):
    from desktop_env.controllers.python import PythonController

    controller = object.__new__(PythonController)
    commands = []
    controller.execute_python_command = commands.append
    monkeypatch.setattr("desktop_env.controllers.python.random.uniform", lambda *_: 0.75)
    controller.execute_action({"action_type": "DRAG_TO", "parameters": {"x": 10, "y": 20}})

    assert "duration=0.75" in commands[0]


@pytest.mark.parametrize("protocol", ["anthropic_messages", "openai_chat", "openai_responses"])
@pytest.mark.parametrize("variant", ["agent3", "agent4"])
def test_default_queries_have_no_query_or_request_count_limit(protocol, variant):
    wire = ModelWire("mock", protocol)
    action = [ACTION] if CAPABILITIES[variant].sequence else ACTION
    wire.request = Mock(side_effect=[
        *(native_reply(protocol, tool=f"q{i}") for i in range(12)),
        native_reply(protocol, text=json.dumps(action)),
    ])
    mode = CAPABILITIES[variant]
    agent = RealtimeAgent(variant=variant, sequence=mode.sequence, frames=mode.frames, wire=wire)
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
    first["usage"] = {"input_tokens": 321, "output_tokens": 17}
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
    agent = RealtimeAgent(variant="agent3", sequence=False, frames=True, wire=wire)
    agent.bind_frame_query(lambda _: {"frames": []})
    events = []
    agent.bind_event_sink(events.append)
    agent.predict("t", {"screenshot": b"png", "task_time_s": 1, "screenshot_file": "initial_state.png"})
    response = next(e for e in events if e["event"] == "model_response")
    assert response["provider_response"] == captured[0]
    assert response["usage"] == {"input_tokens": 321, "output_tokens": 17}
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


def test_anthropic_thinking_effort_is_forwarded(monkeypatch):
    monkeypatch.setenv("PACKY_API_KEY", "test-key")
    wire = ModelWire(
        "mock",
        "anthropic_messages",
        thinking_enabled=True,
        thinking_effort="high",
    )
    wire.session.post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {}))
    wire.request("system", [], tools_enabled=False, native=True, max_tokens=128000, temperature=1)
    payload = wire.session.post.call_args.kwargs["json"]
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}
    assert "temperature" not in payload


@pytest.mark.parametrize(
    ("variant", "agent_id", "sequence", "frames"),
    [
        ("agent1", "vanilla", False, False),
        ("agent2", "anticipatory", True, False),
        ("agent3", "video", False, True),
        ("agent4", "combine", True, True),
    ],
)
def test_checked_in_realtime_configs_map_all_runtime_capabilities(
    variant, agent_id, sequence, frames
):
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "realtime_agents"
        / f"{agent_id}-claude-sonnet-5.yaml"
    )
    config = load_realtime_config(path, variant=variant)
    kwargs = agent_kwargs(config)
    assert config["agent_id"] == agent_id
    assert kwargs["sequence"] is sequence
    assert kwargs["frames"] is frames
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["tool_format"] == "native"
    assert kwargs["max_tokens"] == 128000
    assert kwargs["thinking_enabled"] is True
    assert kwargs["thinking_effort"] == "high"
    assert kwargs["thinking_summary"] is True
    assert kwargs["system_prompt_text"] == config["system_prompt"]
    assert kwargs["max_sequence_actions"] == (1 if not sequence else 100)


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses"])
def test_openai_native_tool_schema_is_sent_without_text_fallback(monkeypatch, protocol):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    wire = ModelWire("openai-test", protocol)
    wire.session.post = Mock(
        return_value=SimpleNamespace(status_code=200, json=lambda: {})
    )
    wire.request(
        "system prompt",
        [{"role": "user", "content": "task"}],
        tools_enabled=True,
        native=True,
        max_tokens=128000,
        temperature=0.2,
        tools=[ACTION_TOOLS[0]],
    )
    payload = wire.session.post.call_args.kwargs["json"]
    assert payload["tools"] == [
        (
            {
                "type": "function",
                "name": ACTION_TOOLS[0]["name"],
                "description": ACTION_TOOLS[0]["description"],
                "parameters": ACTION_TOOLS[0]["parameters"],
                "strict": False,
            }
            if protocol == "openai_responses"
            else {
                "type": "function",
                "function": {
                    "name": ACTION_TOOLS[0]["name"],
                    "description": ACTION_TOOLS[0]["description"],
                    "parameters": ACTION_TOOLS[0]["parameters"],
                },
            }
        )
    ]
    if protocol == "openai_responses":
        assert payload["max_output_tokens"] == 128000
        assert payload["instructions"] == "system prompt"
        assert "parallel_tool_calls" not in payload
    else:
        assert payload["max_tokens"] == 128000
        assert payload["messages"][0] == {"role": "system", "content": "system prompt"}
        assert "parallel_tool_calls" not in payload


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses"])
def test_sequence_mode_explicitly_enables_parallel_tool_calls(monkeypatch, protocol):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    wire = ModelWire("openai-test", protocol)
    wire.session.post = Mock(
        return_value=SimpleNamespace(status_code=200, json=lambda: {})
    )
    wire.request(
        "system prompt",
        [{"role": "user", "content": "task"}],
        tools_enabled=True,
        native=True,
        max_tokens=128000,
        temperature=0.2,
        tools=[ACTION_TOOLS[0]],
        parallel_tool_calls=True,
    )
    assert wire.session.post.call_args.kwargs["json"]["parallel_tool_calls"] is True


def test_astra_config_preserves_thinking_and_combine_tools(monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    path = Path(__file__).resolve().parents[1] / "configs/realtime_agents/combine-gpt-6-astra.yaml"
    agent = RealtimeAgent(**agent_kwargs(load_realtime_config(path, variant="agent4")))
    agent.wire.session.post = Mock(return_value=SimpleNamespace(
        status_code=200,
        json=lambda: native_reply("openai_responses", text=json.dumps(ACTION)),
    ))
    assert agent.predict("task", {"screenshot": b"png", "task_time_s": 0})[1] == [ACTION]
    payload = agent.wire.session.post.call_args.kwargs["json"]
    assert payload["model"] == "gpt-6-astra"
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert "temperature" not in payload
    assert {tool["name"] for tool in payload["tools"]} == {
        "get_frames", *(tool["name"] for tool in ACTION_TOOLS)
    }
    assert payload["parallel_tool_calls"] is True
    assert agent.max_actions == 100


def test_responses_image_logging_omits_bytes_without_changing_request():
    from mm_agents.realtime_agent import loggable_messages

    wire = ModelWire("gpt-6-astra", "openai_responses")
    messages = [wire.user([{"type": "image", "source": IMAGE}])]
    original = copy.deepcopy(messages)
    logged = loggable_messages(messages)
    block = logged[0]["content"][0]
    assert block["image_url"] == "data:image/png;base64,[omitted]"
    assert len(block["image_url_sha256"]) == 64
    assert block["image_url_length"] == len(original[0]["content"][0]["image_url"])
    assert messages == original


@pytest.mark.parametrize("complete", [False, True])
def test_responses_stream_requires_complete_turn_before_action_dispatch(monkeypatch, complete):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    reply = native_reply("openai_responses", text=json.dumps([ACTION, ACTION]))
    events = [{"type": "response.output_item.done", "item": reply["output"][0]}]
    if complete:
        events.append({"type": "response.completed", "response": reply})
    lines = [line for event in events for line in [b"data: " + json.dumps(event).encode(), b""]]
    response = SimpleNamespace(
        status_code=200, headers={"content-type": "text/event-stream"},
        iter_lines=lambda: iter(lines), close=Mock(),
    )
    wire = ModelWire("gpt-6-astra", "openai_responses")
    wire.session.post = Mock(return_value=response)
    agent = RealtimeAgent(sequence=True, frames=False, wire=wire)
    if complete:
        assert agent.predict("task", {"screenshot": b"png", "task_time_s": 0})[1] == [ACTION, ACTION]
    else:
        with pytest.raises(RuntimeError, match="ended before response.completed"):
            agent.predict("task", {"screenshot": b"png", "task_time_s": 0})
        assert agent.pending_action_calls is None
    assert wire.session.post.call_args.kwargs["json"]["stream"] is True
    response.close.assert_called_once()


@pytest.mark.parametrize("protocol", ["anthropic_messages", "openai_chat", "openai_responses"])
def test_invalid_key_call_is_closed_before_model_correction(protocol):
    wire = ModelWire("mock", protocol)
    invalid = {"action_type": "PRESS", "parameters": {"key": "not-a-registered-key"}}
    wire.request = Mock(side_effect=[
        native_reply(protocol, text=json.dumps(invalid)),
        native_reply(protocol, text=json.dumps(ACTION)),
    ])
    agent = RealtimeAgent(sequence=True, frames=False, wire=wire)
    assert agent.predict("task", {"screenshot": b"png", "task_time_s": 0})[1] == [ACTION]
    messages = wire.request.call_args_list[1].args[1]
    if protocol == "openai_responses":
        replies = [m for m in messages if m.get("type") == "function_call_output"]
        assert [r["call_id"] for r in replies] == ["a0"]
    elif protocol == "openai_chat":
        replies = [m for m in messages if m.get("role") == "tool"]
        assert [r["tool_call_id"] for r in replies] == ["a0"]
    else:
        replies = [b for m in messages for b in m.get("content", []) if b.get("type") == "tool_result"]
        assert [r["tool_use_id"] for r in replies] == ["a0"]
    assert '"executed": false' in json.dumps(replies).replace('\\"', '"')
    assert "No action in this sequence was executed" in json.dumps(replies)


@pytest.mark.parametrize("frame_query", [False, True])
def test_reported_instruction_replacement_stops_before_any_tool(frame_query):
    from mm_agents.realtime_agent import ProviderInstructionMismatchError

    wire = ModelWire("mock", "openai_responses")
    raw = native_reply(
        wire.protocol, tool="q1" if frame_query else None,
        text="" if frame_query else json.dumps(ACTION),
    )
    raw["instructions"] = "Unrelated provider instructions."
    wire.request = Mock(return_value=raw)
    agent = RealtimeAgent(sequence=True, frames=True, wire=wire)
    query, events = Mock(), []
    agent.bind_frame_query(query)
    agent.bind_event_sink(events.append)

    with pytest.raises(ProviderInstructionMismatchError, match="No tools"):
        agent.predict("task", {"screenshot": b"png", "task_time_s": 0})

    query.assert_not_called()
    assert agent.pending_action_calls is None
    assert agent.counters["tool_calls"] == 0
    assert agent.rounds == []
    response = next(e for e in events if e["event"] == "model_response")
    assert response["provider_response"] == raw
    audit = response["instruction_audit"]
    assert audit["status"] == "mismatch"
    assert audit["requested_sha256"] != audit["reported_sha256"]
    assert events[-1]["event"] == "provider_instruction_mismatch"
    wire.request.assert_called_once()


@pytest.mark.parametrize("echo", ["match", "absent", "null"])
def test_instruction_audit_allows_matching_or_unreported_echo(echo):
    wire = ModelWire("mock", "openai_responses")
    agent = RealtimeAgent(sequence=True, frames=False, wire=wire)
    raw = native_reply(wire.protocol, text=json.dumps(ACTION))
    if echo != "absent":
        raw["instructions"] = agent.system if echo == "match" else None
    wire.request = Mock(return_value=raw)
    assert agent.predict("task", {"screenshot": b"png", "task_time_s": 0})[1] == [ACTION]
    response = next(e for e in agent.last_events if e["event"] == "model_response")
    assert response["instruction_audit"]["status"] == (
        "match" if echo == "match" else "not_reported"
    )
