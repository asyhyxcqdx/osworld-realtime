import base64
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mm_agents.realtime_agent import ModelWire, RealtimeAgent, response_reasoning
from mm_agents.realtime_config import agent_kwargs, load_realtime_config
from mm_agents.realtime_protocol import (
    ACTION_TOOLS,
    GetFramesArgs,
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


TEST_SYSTEM_PROMPT = "Test-only realtime agent system prompt."
TEST_USER_PROMPT = "Test-only realtime agent user prompt."


def test_action_validation_uses_registered_pydantic_models():
    assert validate_action(ACTION) == ACTION
    with pytest.raises(ValueError):
        validate_action({"action_type": "PRESS", "parameters": {"key": " "}})
    with pytest.raises(ValueError):
        validate_action({"action_type": "WAIT", "parameters": {}})
    with pytest.raises(ValueError):
        validate_action({"action_type": "FAIL"})


def test_a_missing_or_empty_system_prompt_is_rejected():
    kwargs = dict(variant="agent1", sequence=False, frames=False, wire=ModelWire("mock", "openai_chat"))
    with pytest.raises(TypeError):  # The system prompt is a required argument.
        RealtimeAgent(**kwargs)
    with pytest.raises(ValueError, match="system_prompt_text"):
        RealtimeAgent(**kwargs, system_prompt_text="   ", user_prompt_text=TEST_USER_PROMPT)


def test_action_tools_are_generated_from_pydantic_models():
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


def test_only_wait_gets_a_realtime_specific_note_and_the_shared_note_is_untouched():
    from desktop_env.actions import ACTION_DEFINITION_BY_TYPE

    from mm_agents.realtime_protocol import REALTIME_ACTION_NOTES

    # The shared note says nothing about what a wait is for in a live game, so this
    # benchmark replaces it: the point is that a wait does not pause anything, it
    # spends the requested time while the world keeps running.
    assert set(REALTIME_ACTION_NOTES) == {"WAIT"}
    wait = next(tool for tool in ACTION_TOOLS if tool["name"] == "computer_wait")
    assert wait["description"] == REALTIME_ACTION_NOTES["WAIT"]
    assert "does not pause" in wait["description"]
    # It must not imply a sequence or a "next action": vanilla/video submit exactly
    # one action per response.
    lowered = wait["description"].lower()
    assert "sequence" not in lowered and "next action" not in lowered
    # Every other tool keeps the shared note, and the shared definitions stay as they
    # are for the other agents that use desktop_env.
    assert ACTION_DEFINITION_BY_TYPE["WAIT"].note == "wait for the specified duration"
    for tool in ACTION_TOOLS:
        action_type = tool["name"].replace("computer_", "").upper()
        if action_type == "WAIT":
            continue
        if action_type in ACTION_DEFINITION_BY_TYPE:
            assert tool["description"] == ACTION_DEFINITION_BY_TYPE[action_type].note


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
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, variant=variant, sequence=mode.sequence, frames=mode.frames, wire=wire)
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
    assert all(r[1]["parallel_tool_calls"] is mode.sequence for r in requests)
    if mode.frames:
        serialized = json.dumps(requests[-1][0])
        assert "q1" in serialized and "q2" in serialized
        assert "actual_time_s" in serialized and IMAGE["data"] in serialized
        assert agent.counters["images_returned"] == 2


@pytest.mark.parametrize("protocol", ["anthropic_messages", "openai_chat", "openai_responses"])
@pytest.mark.parametrize("history_length", [None, 1, 0])
def test_task_message_is_a_single_prefix_across_decisions_queries_and_reset(protocol, history_length):
    wire = ModelWire("mock", protocol)
    action_reply = native_reply(protocol, text=json.dumps(ACTION))
    replies = iter([action_reply, native_reply(protocol, tool="q1"), action_reply,
                    action_reply, action_reply])
    requests = []

    def request(system, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        return next(replies)

    wire.request = request
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, variant="agent4", sequence=True, frames=True, wire=wire,
                          max_trajectory_length=history_length)
    agent.bind_frame_query(lambda _: {"frames": [{"status": "not_ready"}]})
    events = []
    agent.bind_event_sink(events.append)
    for step in range(1, 4):
        _, actions = agent.predict("original task", {
            "screenshot": f"image-{step}".encode(), "task_time_s": float(step),
        })
        agent.record_action_result(actions)

    assert len(requests) == 4  # The historical-frame result needs another request.
    for messages in requests:
        assert json.dumps(messages).count(TEST_USER_PROMPT) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"][0]["text"] == TEST_USER_PROMPT
        for message in messages[1:]:
            if message.get("role") == "user":
                for block in message["content"]:
                    if block.get("type") in {"text", "input_text"}:
                        assert TEST_USER_PROMPT not in block["text"]
    assert len(requests[0]) == 2  # Task, then timestamp plus image.
    assert "Screenshot task time: 2.000 seconds." in json.dumps(requests[1][-1])
    assert base64.b64encode(b"image-2").decode() in json.dumps(requests[1][-1])
    assert requests[2][:len(requests[1])] == requests[1]
    assert "q1" in json.dumps(requests[2][len(requests[1]):])
    if history_length != 0:
        previous_assistant = wire.unpack(action_reply)[2][0]
        assert previous_assistant in requests[1]
        result_message = requests[1][-2]
        if protocol == "anthropic_messages":
            result_text = result_message["content"][0]["content"][0]["text"]
        elif protocol == "openai_responses":
            result_text = result_message["output"][0]["text"]
        else:
            result_text = result_message["content"]
        assert json.loads(result_text)["executed"] is True
    expected_observations = 3 if history_length is None else history_length + 1
    assert json.dumps(requests[-1]).count("Screenshot task time:") == expected_observations
    logged = [e["request_messages"] for e in events if e["event"] == "model_request"]
    assert all(json.dumps(m).count(TEST_USER_PROMPT) == 1 for m in logged)

    # The task message comes from the config, so a new instruction argument does
    # not reach the provider; the conversation still opens with one task message.
    agent.reset()
    agent.predict("next task", {"screenshot": b"new-image", "task_time_s": 0.0})
    assert len(requests[-1]) == 2
    assert json.dumps(requests[-1]).count(TEST_USER_PROMPT) == 1
    assert "next task" not in json.dumps(requests[-1])


def test_text_json_tool_output_is_rejected():
    wire = ModelWire("mock", "anthropic_messages")
    with pytest.raises(ValueError, match="native tool use"):
        RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT,
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
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=False, frames=False, wire=wire)
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
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT,
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


def test_chat_frame_details_appear_once_and_stay_with_their_images():
    wire = ModelWire("mock", "openai_chat")
    second_image = {**IMAGE, "data": base64.b64encode(b"second png").decode()}
    results = [
        ({"id": "a"}, {
            "task_time_s": 5.123456,
            "frames": [
                {"requested_time_s": 1.0, "actual_time_s": 1.003456, "status": "ok", "image": IMAGE},
                {"requested_time_s": 6.0, "status": "not_ready"},
                {"requested_time_s": 2.0, "status": "error", "message": "Frame decode failed"},
            ],
        }),
        ({"id": "b"}, {
            "task_time_s": 5.234567,
            "frames": [{"requested_time_s": 3.0, "actual_time_s": 3.005678, "status": "ok", "image": second_image}],
        }),
    ]
    original = copy.deepcopy(results)
    messages = wire.tool_results(results)
    assert [m["role"] for m in messages] == ["tool", "tool", "user"]
    for message, call_id in zip(messages[:2], ("a", "b")):
        assert message["tool_call_id"] == call_id
        assert f"Images for tool_call_id={call_id}" in message["content"]
        assert "requested_time_s" not in message["content"]

    groups = {}
    for block in messages[-1]["content"]:
        if block["type"] == "text" and block["text"].startswith("Images for tool_call_id="):
            current = block["text"].split("=", 1)[1]
            groups[current] = []
        else:
            groups[current].append(block)
    assert list(groups) == ["a", "b"]
    for call_id, image in (("a", IMAGE), ("b", second_image)):
        group = groups[call_id]
        assert group[1]["type"] == "image_url"
        assert group[1]["image_url"]["url"] == "data:image/png;base64," + image["data"]
        assert json.loads(group[0]["text"])["status"] == "ok"
    assert json.loads(groups["a"][0]["text"])["actual_time_s"] == 1.003
    assert json.loads(groups["a"][2]["text"])["status"] == "not_ready"
    assert json.loads(groups["a"][3]["text"])["message"] == "Frame decode failed"
    text = "\n".join(m["content"] for m in messages[:2]) + "\n".join(
        b["text"] for b in messages[-1]["content"] if b["type"] == "text"
    )
    assert '"requested_time_s"' not in text
    assert '"query_completed_time_s"' not in text
    assert text.count('"actual_time_s"') == 2
    assert results == original


@pytest.mark.parametrize("result", [
    {"task_time_s": 5.123456, "frames": [{"requested_time_s": 6.0, "status": "not_ready"}]},
    {"frames": [{"requested_time_s": 2.0, "status": "error", "message": "Frame decode failed"}]},
    {"status": "error", "message": "Frame query failed"},
])
def test_chat_query_without_images_keeps_details_in_tool_reply(result):
    wire = ModelWire("mock", "openai_chat")
    messages = wire.tool_results([({"id": "query"}, result)])
    assert len(messages) == 1
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "query"
    metadata = [json.loads(line) for line in messages[0]["content"].splitlines()]
    # The VM receipt's requested_time_s is withheld: only the returned frame's own
    # timestamp reaches the model.
    expected = {k: v for k, v in result.get("frames", [result])[-1].items()
                if k != "requested_time_s"}
    assert metadata[-1] == expected
    assert '"requested_time_s"' not in messages[0]["content"]
    assert '"query_completed_time_s"' not in messages[0]["content"]


@pytest.mark.parametrize('protocol', ['anthropic_messages', 'openai_responses', 'openai_chat'])
def test_model_tool_results_round_only_time_fields_without_mutating_measurements(protocol):
    wire = ModelWire('mock', protocol)
    result = {
        'task_time_s': 31.715886116,
        'frames': [{'status': 'ok', 'requested_time_s': 15.234567,
                    'actual_time_s': 15.235027, 'available_until_s': 29.874705,
                    'image': IMAGE}],
    }
    execution = {'info': {'sequence_actions': [{
        'action': {'action_type': 'CLICK', 'parameters': {'x': 960.123456, 'y': 722}},
        'started_s': 7.128817319869995, 'finished_s': 7.132425546646118,
        'duration_s': 0.003609571000001921,
        'capture_interval_s': [7.136947870, 7.273297310],
    }]}}
    original = copy.deepcopy((result, execution))
    messages = wire.tool_results([({'id': 'frame'}, result), ({'id': 'action'}, execution)])
    serialized = json.dumps(messages)
    for expected in ('15.235', '29.875', '7.129', '7.132', '0.004', '7.137', '7.273'):
        assert expected in serialized
    for withheld in ('31.716', '31.715886116', 'query_completed_time_s'):
        assert withheld not in serialized
    for raw in ('7.128817319869995', '0.003609571000001921'):
        assert raw not in serialized
    assert '960.123456' in serialized  # Coordinates are not rounded.
    assert IMAGE['data'] in serialized
    assert (result, execution) == original


def test_the_configured_user_prompt_is_the_only_task_message():
    wire = ModelWire('mock', 'anthropic_messages')
    wire.request = Mock(return_value=native_reply(wire.protocol, text=json.dumps(ACTION)))
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT,
                          user_prompt_text='You are the Agent.\nComplete the task.',
                          sequence=False, frames=False, wire=wire)

    agent.predict('game rules task', {'screenshot': b'png', 'task_time_s': 1})

    messages = wire.request.call_args.args[1]
    assert messages[0]['role'] == 'user'
    assert messages[0]['content'][0]['text'] == 'You are the Agent.\nComplete the task.'
    assert 'Task: game rules task' not in json.dumps(messages)


def test_a_missing_or_empty_user_prompt_is_rejected():
    for empty in (None, '', '   '):
        with pytest.raises(ValueError, match='user_prompt_text'):
            RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=empty,
                          sequence=True, frames=False)


def test_screenshot_time_precision_does_not_change_action_execution_precision():
    wire = ModelWire('mock', 'anthropic_messages')
    action = {'action_type': 'WAIT', 'parameters': {'duration_s': 0.123456}}
    wire.request = Mock(return_value=native_reply(wire.protocol, text=json.dumps(action)))
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=True, frames=False, wire=wire)
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 1.234567})[1] == [action]
    messages = wire.request.call_args.args[1]
    assert 'Screenshot task time: 1.235 seconds.' in json.dumps(messages)
    assert agent.observations[-1]['task_time_s'] == 1.234567


@pytest.mark.parametrize('variant', CAPABILITIES)
@pytest.mark.parametrize('protocol,coordinate_system', [
    ('anthropic_messages', 'native_pixels'),
    ('anthropic_messages', 'normalized_0_1000_unclipped'),
    ('openai_chat', 'normalized_0_1000'),
    ('openai_responses', 'native_pixels'),
])
def test_next_request_reports_actions_without_any_timestamps_or_vm_coordinates(variant, protocol, coordinate_system):
    caps = CAPABILITIES[variant]
    relative = coordinate_system != 'native_pixels'
    raw_actions = [{
        'action_type': 'CLICK',
        'parameters': {'x': 518, 'y': 670} if relative else {'x': 994, 'y': 723},
    }]
    if caps.sequence:
        raw_actions.extend([
            {'action_type': 'WAIT', 'parameters': {'duration_s': .3}},
            ACTION,
        ])
    wire = ModelWire('mock', protocol)
    first_reply = native_reply(protocol, text=json.dumps(raw_actions))
    raw_snapshot = copy.deepcopy(first_reply)
    wire.request = Mock(side_effect=[
        first_reply,
        native_reply(protocol, text=json.dumps({'action_type': 'DONE', 'parameters': {}})),
    ])
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT,
        variant=variant, sequence=caps.sequence, frames=caps.frames,
        coordinate_system=coordinate_system, wire=wire,
    )
    events = []
    agent.bind_event_sink(events.append)
    _, actions = agent.predict('task', {'screenshot': b'input png', 'task_time_s': 1.0})
    assert actions[0]['parameters'] == {'x': 994, 'y': 723}
    measurements = [
        (2.123456, 2.127891, .004435),
        (2.128456, 2.428891, .300435),
        (2.429456, 2.431891, .002435),
    ][:len(actions)]
    info = {'sequence_actions': [
        {'action': copy.deepcopy(action), 'started_s': start, 'finished_s': end, 'duration_s': duration}
        for action, (start, end, duration) in zip(actions, measurements)
    ]}
    original_info = copy.deepcopy(info)
    agent.record_action_result(actions, reward=0, done=False, info=info)
    recorded = next(e for e in events if e['event'] == 'action_tool_result')
    for _, result in recorded['calls']:
        assert 'started_s' not in result and 'finished_s' not in result and 'duration_s' not in result
        assert 'info' not in result
        assert 'execution_coordinate_system' not in result
    agent.predict('task', {'screenshot': b'after png', 'task_time_s': 3.0})
    sent = wire.request.call_args.args[1]
    if protocol == 'anthropic_messages':
        outputs = [(b['tool_use_id'], json.loads(b['content'][0]['text']))
                   for m in sent for b in m.get('content', []) if b.get('type') == 'tool_result']
    elif protocol == 'openai_responses':
        outputs = [(m['call_id'], json.loads(m['output'][0]['text']))
                   for m in sent if m.get('type') == 'function_call_output']
    else:
        outputs = [(m['tool_call_id'], json.loads(m['content'])) for m in sent if m.get('role') == 'tool']
    assert len(outputs) == len(actions)
    for index, ((call_id, result), action) in enumerate(zip(outputs, actions)):
        assert call_id == f'a{index}'
        assert result == {
            'action_type': action['action_type'], 'executed': True,
            'reward': 0, 'done': False, 'last_in_decision': index == len(actions) - 1,
        }
    assert info == original_info
    assert first_reply == raw_snapshot
    assert all(message in sent for message in wire.unpack(raw_snapshot)[2])
    assert 'Screenshot task time: 3.000 seconds.' in json.dumps(sent[-1])
    assert base64.b64encode(b'after png').decode() in json.dumps(sent[-1])


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
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, variant=variant, sequence=mode.sequence, frames=mode.frames, wire=wire)
    agent.bind_frame_query(Mock(return_value={"frames": [{"status": "not_ready"}]}))
    assert agent.predict("t", {"screenshot": b"png", "task_time_s": 1})[1] == [ACTION]
    assert agent.counters["frame_queries"] == 12
    assert wire.request.call_count == 13
    assert all(c.kwargs["tools_enabled"] for c in wire.request.call_args_list)
    assert agent.max_queries == 0  # 0 means the default has no query-count limit


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
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, variant="agent3", sequence=False, frames=True, wire=wire)
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


def test_responses_reasoning_text_survives_every_gateway_shape():
    """DashScope returns the chain in content[], OpenAI in summary[]; keep both.

    The compatible-mode gateway used for qwen3.8-max-0902 / kimi-k3 answers with
    ``summary: []`` and ``content: [{"type": "reasoning_text", ...}]``. Reading only
    ``summary`` logged an empty reasoning field for every such turn even though the
    provider had returned the text.
    """
    assert response_reasoning({"output": [{"type": "reasoning", "summary": [], "content": [
        {"type": "reasoning_text", "text": "先看画面"},
        {"type": "reasoning_text", "text": "再点击"},
    ]}]}, "openai_responses") == {
        "reasoning": ["先看画面", "再点击"], "reasoning_status": "returned",
    }
    assert response_reasoning({"output": [{"type": "reasoning", "summary": [
        {"type": "summary_text", "text": "摘要"},
    ]}]}, "openai_responses") == {
        "reasoning": ["摘要"], "reasoning_status": "returned",
    }
    # The detailed chain wins when a gateway sends both, so nothing is duplicated.
    assert response_reasoning({"output": [{"type": "reasoning", "summary": [
        {"type": "summary_text", "text": "摘要"},
    ], "content": [{"type": "reasoning_text", "text": "全文"}]}]}, "openai_responses") == {
        "reasoning": ["全文"], "reasoning_status": "returned",
    }
    assert response_reasoning({"output": [{"type": "reasoning", "summary": [], "content": []}]}, "openai_responses") == {
        "reasoning": [], "reasoning_status": "not_returned",
    }
    assert response_reasoning({"output": [
        {"type": "reasoning", "summary": [], "encrypted_content": "opaque"},
    ]}, "openai_responses") == {
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
@pytest.mark.parametrize("model", ["claude-fable-5", "gpt-6-astra"])
def test_checked_in_realtime_configs_map_all_runtime_capabilities(
    variant, agent_id, sequence, frames, model
):
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "realtime_agents"
        / f"{agent_id}-{model}.yaml"
    )
    config = load_realtime_config(path, variant=variant)
    kwargs = agent_kwargs(config)
    assert config["agent_id"] == agent_id
    assert kwargs["sequence"] is sequence
    assert kwargs["frames"] is frames
    assert kwargs["model"] == model
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

    monkeypatch.setenv("PACKY_GPT_6_ASTRA_API_KEY", "test-key")
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


def test_mixed_frame_and_action_calls_are_rejected_and_retried():
    wire = ModelWire("claude-fable-5", "anthropic_messages")
    mixed = {
        "content": [
            {"type": "tool_use", "id": "a0", "name": "computer_click", "input": {"x": 754, "y": 549}},
            {"type": "tool_use", "id": "q0", "name": "get_frames", "input": {"times_s": [1.5]}},
        ]
    }
    wire.request = Mock(side_effect=[mixed, native_reply(wire.protocol, text=json.dumps(ACTION))])
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=True, frames=True, wire=wire)
    query = Mock()
    agent.bind_frame_query(query)
    assert agent.predict("task", {"screenshot": b"png", "task_time_s": 0})[1] == [ACTION]
    query.assert_not_called()
    assert any(e["event"] == "format_error" for e in agent.last_events)
    correction_messages = wire.request.call_args_list[1].args[1]
    results = [m for m in correction_messages if m.get("role") == "user" and any(
        b.get("type") == "tool_result" for b in m.get("content", [])
    )]
    assert results
    assert {b["tool_use_id"] for b in results[0]["content"]} == {"a0", "q0"}


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


def test_anthropic_frame_inside_a_tool_result_is_omitted():
    from mm_agents.realtime_agent import loggable_messages

    wire = ModelWire("claude-sonnet-5", "anthropic_messages")
    messages = wire.tool_results(
        [({"id": "t0"}, {"frames": [{"status": "ok", "image": IMAGE}]})]
    )
    original = copy.deepcopy(messages)
    logged = loggable_messages(messages)
    image = logged[0]["content"][0]["content"][1]
    assert image["type"] == "image"
    assert "data" not in image["source"]
    assert image["source"]["data_sha256"] == hashlib.sha256(
        IMAGE["data"].encode("ascii")).hexdigest()
    assert image["source"]["data_length"] == len(IMAGE["data"])
    assert messages == original


def test_responses_frame_inside_a_tool_result_is_omitted():
    from mm_agents.realtime_agent import loggable_messages

    wire = ModelWire("gpt-6-astra", "openai_responses")
    messages = wire.tool_results(
        [({"id": "t0"}, {"frames": [{"status": "ok", "image": IMAGE}]})]
    )
    url = "data:image/png;base64," + IMAGE["data"]
    original = copy.deepcopy(messages)
    logged = loggable_messages(messages)
    image = [b for b in logged[0]["output"] if b["type"] == "input_image"][0]
    assert image["image_url"] == "data:image/png;base64,[omitted]"
    assert image["image_url_sha256"] == hashlib.sha256(url.encode("ascii")).hexdigest()
    assert image["image_url_length"] == len(url)
    assert messages == original


def test_image_logging_leaves_reasoning_and_signatures_alone():
    from mm_agents.realtime_agent import loggable_messages

    messages = [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "t" * 9000, "signature": "s" * 9000},
        {"type": "text", "text": "pressed space"},
    ]}]
    assert loggable_messages(messages) == messages


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
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=True, frames=False, wire=wire)
    if complete:
        assert agent.predict("task", {"screenshot": b"png", "task_time_s": 0})[1] == [ACTION, ACTION]
    else:
        with pytest.raises(RuntimeError, match="ended before response.completed"):
            agent.predict("task", {"screenshot": b"png", "task_time_s": 0})
        assert agent.pending_action_calls is None
    assert wire.session.post.call_args.kwargs["json"]["stream"] is True
    assert response.close.call_count == (1 if complete else 3)


@pytest.mark.parametrize("protocol", ["anthropic_messages", "openai_chat", "openai_responses"])
def test_invalid_key_call_is_closed_before_model_correction(protocol):
    wire = ModelWire("mock", protocol)
    invalid = {"action_type": "PRESS", "parameters": {"key": "not-a-registered-key"}}
    wire.request = Mock(side_effect=[
        native_reply(protocol, text=json.dumps(invalid)),
        native_reply(protocol, text=json.dumps(ACTION)),
    ])
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=True, frames=False, wire=wire)
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


def frame_batch_reply(protocol):
    first = native_reply(protocol, tool='q1')
    second = native_reply(protocol, tool='q2')
    if protocol == 'anthropic_messages':
        first['content'].extend(second['content'])
    elif protocol == 'openai_responses':
        first['output'].extend(second['output'])
    else:
        first['choices'][0]['message']['tool_calls'].extend(second['choices'][0]['message']['tool_calls'])
    return first


@pytest.mark.parametrize('protocol', ['anthropic_messages', 'openai_responses', 'openai_chat'])
def test_atomic_frame_batch_rejected_before_queries_and_budget_consumption(protocol):
    wire = ModelWire('mock', protocol)
    query = Mock(return_value={'frames': [{'status': 'ok', 'image': IMAGE}]})
    requests = []
    replies = [frame_batch_reply(protocol), native_reply(protocol, tool='q3'),
               native_reply(protocol, tool='q4'), native_reply(protocol, text=json.dumps(ACTION))]

    def request(system, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 2:
            query.assert_not_called()
        return replies.pop(0)

    wire.request = request
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, variant='agent3', sequence=False, frames=True, wire=wire, max_frame_queries=2)
    agent.bind_frame_query(query)
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 1})[1] == [ACTION]
    assert query.call_count == agent.counters['frame_queries'] == 2
    assert agent.counters['images_returned'] == 2
    errors = [e for e in agent.last_events if e['event'] == 'format_error']
    assert len(errors) == 1 and errors[0]['will_retry'] is True
    assert 'one get_frames call per response' in errors[0]['message']
    messages = requests[1]
    if protocol == 'anthropic_messages':
        outputs = [b for m in messages for b in m.get('content', []) if b['type'] == 'tool_result']
        assert [b['tool_use_id'] for b in outputs] == ['q1', 'q2']
    elif protocol == 'openai_responses':
        outputs = [m for m in messages if m.get('type') == 'function_call_output']
        assert [m['call_id'] for m in outputs] == ['q1', 'q2']
    else:
        outputs = [m for m in messages if m.get('role') == 'tool']
        assert [m['tool_call_id'] for m in outputs] == ['q1', 'q2']
    assert 'No frame queries or actions were executed' in json.dumps(outputs)
    assert GetFramesArgs(times_s=[float(i) for i in range(8)]).times_s == list(range(8))


@pytest.mark.parametrize('protocol', ['anthropic_messages', 'openai_responses', 'openai_chat'])
def test_combine_still_accepts_multiple_frame_queries_in_one_response(protocol):
    wire = ModelWire('mock', protocol)
    wire.request = Mock(side_effect=[frame_batch_reply(protocol), native_reply(protocol, text=json.dumps([ACTION, ACTION]))])
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, variant='agent4', sequence=True, frames=True, wire=wire)
    query = Mock(return_value={'frames': []})
    agent.bind_frame_query(query)
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 1})[1] == [ACTION, ACTION]
    assert query.call_count == 2
    assert not any(e['event'] == 'format_error' for e in agent.last_events)


@pytest.mark.parametrize('model', ['claude-fable-5', 'gpt-6-astra'])
@pytest.mark.parametrize('variant', ['agent1', 'agent3', 'agent4'])
def test_atomic_configs_send_single_tool_policy_to_provider(monkeypatch, model, variant):
    from mm_agents.realtime_config import default_config_path
    monkeypatch.setenv(
        'PACKY_CLAUDE_FABLE_5_API_KEY' if model == 'claude-fable-5' else 'PACKY_GPT_6_ASTRA_API_KEY', 'test-key'
    )
    config = load_realtime_config(default_config_path(variant, model), variant=variant)
    agent = RealtimeAgent(variant=variant, **agent_kwargs(config))
    agent.wire.session.post = Mock(return_value=SimpleNamespace(status_code=200,
        json=lambda: native_reply(agent.wire.protocol, text=json.dumps(ACTION))))
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 1})[1] == [ACTION]
    payload = agent.wire.session.post.call_args.kwargs['json']
    if agent.wire.protocol == 'anthropic_messages':
        if variant == 'agent4':
            assert 'tool_choice' not in payload
        else:
            assert payload['tool_choice'] == {'type': 'auto', 'disable_parallel_tool_use': True}
    else:
        assert payload['parallel_tool_calls'] is (variant == 'agent4')
    # The single-get_frames-call rule moved to the user prompt; the code still
    # rejects a batch and test_atomic_frame_batch_rejected_before_queries_and_
    # budget_consumption covers that rejection.
