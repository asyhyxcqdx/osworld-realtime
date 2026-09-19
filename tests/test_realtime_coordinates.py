"""Coordinate contracts must reach actual tools, history and VM-bound actions."""
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from mm_agents.realtime_agent import ModelWire, RealtimeAgent
from mm_agents.realtime_coordinates import CoordinateAdapter
from mm_agents.realtime_protocol import ACTION_TOOLS, validate_action

RELATIVE_SYSTEMS = ['normalized_0_1000', 'normalized_0_1000_unclipped']


TEST_SYSTEM_PROMPT = "Test-only realtime agent system prompt."
TEST_USER_PROMPT = "Test-only realtime agent user prompt."


@pytest.mark.parametrize('system,maximum,edge', [
    ('normalized_0_1000', 1000, {'x': 1919, 'y': 1079}),
    ('normalized_0_1000_unclipped', 1000, {'x': 1920, 'y': 1080}),
])
def test_mapping_uses_1000_denominator_and_selected_upstream_endpoint(system, maximum, edge):
    adapter = CoordinateAdapter(system)
    for raw, expected in [
        ({'x': 0, 'y': 0}, {'x': 0, 'y': 0}),
        ({'x': 500, 'y': 500}, {'x': 960, 'y': 540}),
        ({'x': 518, 'y': 670}, {'x': 994, 'y': 723}),
        ({'x': maximum, 'y': maximum}, edge),
    ]:
        action = {'action_type': 'CLICK', 'parameters': raw.copy()}
        converted = adapter.to_native_action(action)
        assert converted['parameters'] == expected
        assert action['parameters'] == raw
        validate_action(converted)


@pytest.mark.parametrize('system', RELATIVE_SYSTEMS)
def test_all_coordinate_actions_convert_without_touching_other_parameters(system):
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=True, frames=True, coordinate_system=system)
    calls = []
    for index, kind in enumerate(['MOVE_TO', 'CLICK', 'RIGHT_CLICK', 'DOUBLE_CLICK', 'DRAG_TO']):
        params = {'x': 500, 'y': 500}
        if kind in {'MOVE_TO', 'DRAG_TO'}:
            params['duration_s'] = 0.25
        calls.append({'id': str(index), 'name': 'computer_' + kind.lower(), 'arguments': params})
    calls.extend([
        {'id': 'k', 'name': 'computer_press', 'arguments': {'key': 'space'}},
        {'id': 'w', 'name': 'computer_wait', 'arguments': {'duration_s': .47}},
        {'id': 's', 'name': 'computer_scroll', 'arguments': {'dx': -2, 'dy': 3}},
        {'id': 'c', 'name': 'computer_click', 'arguments': {'button': 'right'}},
        {'id': 'd', 'name': 'computer_done', 'arguments': {}},
    ])
    before = copy.deepcopy(calls)
    actions = agent._decode_action_calls(calls, {})
    assert calls == before
    for action in actions[:5]:
        assert (action['parameters']['x'], action['parameters']['y']) == (960, 540)
    assert actions[0]['parameters']['duration_s'] == .25
    for call, action in zip(calls[5:], actions[5:]):
        assert action['parameters'] == call['arguments']
    assert 'native full-screen pixel coordinate system' not in agent.system
    assert 'Always submit native screen pixels' not in agent.system


@pytest.mark.parametrize('system', RELATIVE_SYSTEMS)
@pytest.mark.parametrize('value', [-1, 1001, 1920, True, '518', 518.2, float('nan'), float('inf')])
def test_invalid_relative_coordinates_reject_entire_sequence(system, value):
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=True, frames=False, coordinate_system=system)
    with pytest.raises(ValueError, match='normalized integer'):
        agent._decode_action_calls([
            {'name': 'computer_press', 'arguments': {'key': 'd'}},
            {'name': 'computer_click', 'arguments': {'x': value, 'y': 500}},
        ], {})
    assert agent.pending_action_calls is None


def test_normalized_tools_do_not_mutate_native_schemas_or_other_agent():
    original = copy.deepcopy(ACTION_TOOLS)
    normalized = CoordinateAdapter('normalized_0_1000').action_tools(ACTION_TOOLS)
    native = CoordinateAdapter().action_tools(ACTION_TOOLS)
    assert ACTION_TOOLS == native == original
    for tool in normalized:
        for name, field in tool['parameters']['properties'].items():
            if name in {'x', 'y'}:
                assert field['type'] == 'integer'
                assert field['minimum'] == 0
                assert field['maximum'] == 1000
    assert CoordinateAdapter().to_native_action(
        {'action_type': 'CLICK', 'parameters': {'x': 994.5, 'y': 724}}
    )['parameters'] == {'x': 994.5, 'y': 724}
    with pytest.raises(ValueError, match='normalized integer'):
        CoordinateAdapter('normalized_0_1000').to_native_value('x', 1001)


@pytest.mark.parametrize('system,protocol', [
    ('normalized_0_1000', 'openai_chat'),
    ('normalized_0_1000_unclipped', 'anthropic_messages'),
])
def test_correction_history_retains_raw_coordinates_and_maps_only_once(system, protocol):
    def reply(x, y):
        if protocol == 'anthropic_messages':
            return {'content': [{'type': 'tool_use', 'id': 'a', 'name': 'computer_click', 'input': {'x': x, 'y': y}}]}
        return {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'id': 'a', 'type': 'function', 'function': {'name': 'computer_click', 'arguments': json.dumps({'x': x, 'y': y})}},
        ]}}]}

    wire = ModelWire('test-model', protocol)
    invalid, valid = reply(1600, 880), reply(518, 670)
    original = copy.deepcopy(valid)
    sent = []
    def request(system, messages, **kwargs):
        sent.append(copy.deepcopy(messages))
        return invalid if len(sent) == 1 else valid
    wire.request = Mock(side_effect=request)
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=True, frames=True, wire=wire, coordinate_system=system)
    obs = {'screenshot': b'png', 'task_time_s': 1}
    actions = agent.predict('task', obs)[1]
    assert actions == [{'action_type': 'CLICK', 'parameters': {'x': 994, 'y': 723}}]
    assert valid == original
    assert agent.pending_action_calls[0]['arguments'] == ({'x': 518, 'y': 670} if protocol == 'anthropic_messages' else json.dumps({'x': 518, 'y': 670}))
    assert 'No action in this sequence was executed' in json.dumps(sent[1])
    agent.record_action_result(actions)
    assert agent.predict('task', obs)[1] == actions
    assistant = wire.unpack(original)[2][0]
    assert assistant in sent[2]
    assert any(e['coordinate_system'] == system for e in agent.last_events if e['event'] == 'model_request')


@pytest.mark.parametrize('model', ['gemini-3.8-flash', 'MiniMax-M3'])
@pytest.mark.parametrize('endpoint', [False, True])
def test_production_config_runner_logs_and_viewer_keep_both_coordinate_spaces(tmp_path, monkeypatch, model, endpoint):
    from lib_run_realtime import run_realtime_example
    from lib_realtime_trajectory import build_trajectory_data
    from mm_agents.realtime_config import agent_kwargs, default_config_path, load_realtime_config

    monkeypatch.setattr('lib_run_realtime.time.sleep', lambda _: None)
    monkeypatch.setattr('lib_run_single.setup_logger', lambda *args: None)
    monkeypatch.setattr('lib_run_single._evaluate_with_details', Mock(return_value=1))
    monkeypatch.setattr('lib_run_realtime.log_task_completion', lambda *args: None)
    config = load_realtime_config(default_config_path('agent4', model), variant='agent4')
    agent = RealtimeAgent(variant='agent4', **agent_kwargs(config))
    raw_params = {'x': 1000, 'y': 1000} if endpoint else {'x': 518, 'y': 670}
    native_params = (
        ({'x': 1920, 'y': 1080} if model == 'MiniMax-M3' else {'x': 1919, 'y': 1079})
        if endpoint else {'x': 994, 'y': 723}
    )
    if agent.wire.protocol == 'anthropic_messages':
        raw = {'content': [
            {'type': 'tool_use', 'id': 'c', 'name': 'computer_click', 'input': raw_params},
            {'type': 'tool_use', 'id': 'd', 'name': 'computer_done', 'input': {}},
        ]}
    else:
        raw = {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'type': 'function', 'id': 'c', 'function': {'name': 'computer_click', 'arguments': json.dumps(raw_params)}},
            {'type': 'function', 'id': 'd', 'function': {'name': 'computer_done', 'arguments': '{}'}},
        ]}}]}
    agent.wire.request = Mock(return_value=raw)
    buffer = io.BytesIO()
    Image.new('RGB', (1920, 1080)).save(buffer, format='PNG')
    png = buffer.getvalue()
    executed = [
        {'action_type': 'CLICK', 'parameters': native_params},
        {'action_type': 'DONE', 'parameters': {}},
    ]
    info = {'sequence_actions': [{'action': a, 'duration_s': .1} for a in executed]}
    env = SimpleNamespace(
        reset=Mock(), _get_obs=lambda: {'screenshot': png},
        step_sequence=Mock(return_value=({'screenshot': png}, 0, True, info)),
        controller=SimpleNamespace(
            start_realtime_recording=Mock(return_value={}), end_realtime_recording=Mock(),
            last_observation_time=.1, last_capture_interval=(.09, .1),
        ),
    )
    args = SimpleNamespace(
        environment_ready_wait_s=0, evaluation_settle_s=0, recording_fragment_ms=100,
        model=model, max_sequence_actions=100, max_frame_queries=0,
    )
    run_realtime_example(agent, env, {}, 100, 'task', args, str(tmp_path), [])
    env.step_sequence.assert_called_once_with(executed)
    data = build_trajectory_data(tmp_path)
    assert data['metadata']['coordinate_system'] == config['api']['coordinate_system']
    response = next(e for e in data['events'] if e['event'] == 'model_response')
    assert response['provider_response'] == raw
    assert response['_view']['marks'][0]['x'] == native_params['x']
    assert response['_view']['marks'][0]['y'] == native_params['y']
    execution = next(e for e in data['events'] if e['event'] == 'action_executed')
    assert execution['actions'] == executed
    assert execution['info'] == info
    assert execution['_view']['marks'][0]['x'] == native_params['x']  # No second conversion.
    feedback = next(e for e in data['events'] if e['event'] == 'action_tool_result')
    assert feedback['calls'][0][1] == {
        'action_type': 'CLICK', 'executed': True, 'reward': 0, 'done': True,
        'last_in_decision': False, 'duration_s': .1,
    }
    assert (tmp_path / 'trajectory.html').exists()
