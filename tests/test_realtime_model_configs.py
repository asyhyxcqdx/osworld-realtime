"""Exercise production config -> wire -> native action paths for the Packy models."""
import base64
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from PIL import Image

from mm_agents.realtime_agent import ModelWire, RealtimeAgent
from mm_agents.realtime_config import agent_kwargs, default_config_path, load_realtime_config


MODELS = {
    'claude-sonnet-5': ('anthropic_messages', 'PACKY_COMMON_API_KEY', 128000),
    'gpt-5.6-sol': ('openai_responses', 'PACKY_COMMON_API_KEY', 128000),
    'gemini-3.8-flash': ('openai_chat', 'PACKY_COMMON_API_KEY', 65536),
    'qwen3.8-max-0902': ('anthropic_messages', 'PACKY_COMMON_API_KEY', 128000),
    'kimi-k3': ('anthropic_messages', 'PACKY_KIMI_API_KEY', 128000),
    'deepseek-flash': ('anthropic_messages', 'PACKY_COMMON_API_KEY', 128000),
    'glm-5.3-flash': ('anthropic_messages', 'PACKY_GLM_MINIMAX_API_KEY', 128000),
    'MiniMax-M3': ('anthropic_messages', 'PACKY_GLM_MINIMAX_API_KEY', 128000),
}
COORDS = {'x': 754, 'y': 549}


def reply(protocol, name='computer_click', args=None):
    args = COORDS if args is None else args
    if protocol == 'anthropic_messages':
        return {'content': [{'type': 'tool_use', 'id': 'call1', 'name': name, 'input': args}], 'stop_reason': 'tool_use'}
    if protocol == 'openai_responses':
        return {'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'call1', 'name': name, 'arguments': json.dumps(args)}]}
    return {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
        'tool_calls': [{'id': 'call1', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]}


def http(body):
    return SimpleNamespace(status_code=200, headers={}, json=lambda: body)


@pytest.fixture(scope='module')
def screenshot():
    output = io.BytesIO()
    Image.new('RGB', (1920, 1080), '#13579b').save(output, format='PNG')
    return output.getvalue()


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('variant', ['agent1', 'agent2', 'agent3', 'agent4'])
def test_packy_config_request_and_native_coordinates(monkeypatch, screenshot, model, variant):
    protocol, key_env, output_limit = MODELS[model]
    monkeypatch.setenv(key_env, 'selected-test-credential')
    monkeypatch.setenv('PACKY_API_KEY', 'wrong-other-model-credential')
    config = load_realtime_config(default_config_path(variant, model), variant=variant)
    agent = RealtimeAgent(variant=variant, **agent_kwargs(config), api_base_url='https://www.packyapi.ai')
    agent.wire.session.post = Mock(return_value=http(reply(protocol)))
    events = []
    agent.bind_event_sink(events.append)
    assert agent.predict('task', {'screenshot': screenshot, 'task_time_s': 0})[1] == [
        {'action_type': 'CLICK', 'parameters': COORDS}]
    assert agent.frames is (variant in {'agent3', 'agent4'})
    assert agent.sequence is (variant in {'agent2', 'agent4'})
    assert agent.max_actions == (100 if agent.sequence else 1)
    assert agent.max_queries == 0
    assert agent.history_length is None
    kw = agent.wire.session.post.call_args.kwargs
    payload = kw['json']
    assert agent.wire.protocol == protocol
    assert payload.get('max_tokens', payload.get('max_output_tokens')) == output_limit
    assert 'selected-test-credential' in list(kw['headers'].values()) or kw['headers'].get('Authorization') == 'Bearer selected-test-credential'
    assert 'selected-test-credential' not in json.dumps(events)
    assert 'wrong-other-model-credential' not in json.dumps(kw['headers'])
    messages = payload.get('input', payload.get('messages'))
    block = next(b for m in messages if m.get('role') == 'user'
                 for b in m['content'] if b['type'] in {'image', 'input_image', 'image_url'})
    if 'source' in block:
        encoded = block['source']['data']
    else:
        url = block['image_url']
        encoded = (url['url'] if isinstance(url, dict) else url).split(',', 1)[1]
    assert base64.b64decode(encoded) == screenshot
    if protocol == 'anthropic_messages':
        assert payload['thinking']['type'] == 'adaptive'
        if model == 'MiniMax-M3':
            assert payload['thinking'] == {'type': 'adaptive'}
            assert 'output_config' not in payload
            assert agent.wire.thinking_effort is None
        else:
            assert payload['output_config'] == {'effort': 'high'}
        if not agent.sequence:
            assert payload['tool_choice']['disable_parallel_tool_use'] is True
    elif protocol == 'openai_responses':
        assert payload['reasoning'] == {'effort': 'high', 'summary': 'auto'}
        assert payload['parallel_tool_calls'] is agent.sequence
    else:
        assert payload['extra_body'] == {'google': {'thinking_config': {'thinking_level': 'high', 'include_thoughts': True}}}
        assert 'reasoning_effort' not in payload
        assert payload['parallel_tool_calls'] is agent.sequence
        assert 'temperature' not in payload


@pytest.mark.parametrize('variant', ['agent3', 'agent4'])
def test_gemini_history_frame_result_and_reasoning_are_preserved(monkeypatch, screenshot, variant):
    monkeypatch.setenv('PACKY_COMMON_API_KEY', 'test-credential')
    agent = RealtimeAgent(variant=variant, **agent_kwargs(load_realtime_config(default_config_path(variant, 'gemini-3.8-flash'))))
    first = reply('openai_chat', 'get_frames', {'times_s': [0.1]})
    message = first['choices'][0]['message']
    message['reasoning_content'] = 'Inspect the earlier screenshot.'
    message['tool_calls'][0]['extra_content'] = {'google': {'thought_signature': 'opaque-signature'}}
    request_payloads = []
    def post(url, **kwargs):
        request_payloads.append(copy.deepcopy(kwargs['json']))
        return http(first if len(request_payloads) == 1 else reply('openai_chat'))
    agent.wire.session.post = post
    query = Mock(return_value={'task_time_s': 1, 'frames': [{'status': 'ok', 'actual_time_s': 0.1,
        'requested_time_s': 0.1, 'image': {'type': 'base64', 'media_type': 'image/png', 'data': base64.b64encode(screenshot).decode()}}]})
    agent.bind_frame_query(query)
    actions = agent.predict('task', {'screenshot': screenshot, 'task_time_s': 1})[1]
    assert actions == [{'action_type': 'CLICK', 'parameters': COORDS}]
    query.assert_called_once_with([0.1])
    messages = request_payloads[1]['messages']
    assert message in messages
    tool_index = next(i for i, m in enumerate(messages) if m.get('role') == 'tool')
    assert messages[tool_index]['tool_call_id'] == 'call1'
    assert messages[tool_index + 1]['role'] == 'user'
    assert any(b['type'] == 'image_url' for b in messages[tool_index + 1]['content'])
    assert agent.counters['frame_queries'] == 1
    agent.record_action_result(actions, info={})
    agent.predict('task', {'screenshot': screenshot, 'task_time_s': 2})
    assert any(m.get('role') == 'tool' and 'CLICK' in m.get('content', '') for m in request_payloads[2]['messages'])


@pytest.mark.parametrize('protocol', ['anthropic_messages', 'openai_responses', 'openai_chat'])
def test_truncated_response_never_dispatches_valid_partial_actions(protocol):
    body = reply(protocol)
    if protocol == 'anthropic_messages':
        body['stop_reason'] = 'max_tokens'
    elif protocol == 'openai_responses':
        body['status'] = 'incomplete'
    else:
        body['choices'][0]['finish_reason'] = 'length'
    wire = ModelWire('test-model', protocol)
    wire.request = Mock(return_value=body)
    agent = RealtimeAgent(sequence=True, frames=True, wire=wire)
    query = Mock()
    agent.bind_frame_query(query)
    with pytest.raises(RuntimeError, match='no actions dispatched'):
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    query.assert_not_called()
    assert agent.pending_action_calls is None
    assert next(e for e in agent.last_events if e['event'] == 'model_response')['provider_response'] == body


def test_missing_dedicated_key_does_not_fall_back(monkeypatch):
    monkeypatch.delenv('PACKY_KIMI_API_KEY', raising=False)
    monkeypatch.setenv('PACKY_API_KEY', 'wrong-key')
    wire = ModelWire('kimi-k3', 'anthropic_messages', api_key_env='PACKY_KIMI_API_KEY')
    wire.session.post = Mock()
    with pytest.raises(RuntimeError, match='PACKY_KIMI_API_KEY'):
        wire.request('s', [], tools_enabled=False, native=True, max_tokens=100, temperature=1)
    wire.session.post.assert_not_called()


@pytest.mark.parametrize('protocol,effort', [('openai_chat', None), ('anthropic_messages', 'high')])
def test_minimax_does_not_inherit_gemini_or_claude_effort_parameters(protocol, effort):
    with pytest.raises(ValueError):
        ModelWire('MiniMax-M3', protocol, thinking_enabled=True, thinking_effort=effort)


def test_minimax_may_omit_effort_in_a_custom_config(tmp_path):
    cfg = load_realtime_config(default_config_path('agent3', 'MiniMax-M3'))
    cfg['api']['thinking'].pop('effort')
    path = tmp_path / 'minimax.yaml'
    path.write_text(yaml.safe_dump(cfg))
    assert agent_kwargs(load_realtime_config(path))['thinking_effort'] is None


@pytest.mark.parametrize('patch', [
    {'max_output_tokens': 65537}, {'max_output_tokens': 0}, {'max_output_tokens': True},
    {'key_env': 'sk-not-an-environment-name'},
    {'thinking': {'enabled': True, 'summary': True, 'effort': 'max'}},
    {'model': 'unimplemented-chat-thinking-model'},
])
def test_gemini_config_rejects_unsupported_settings(tmp_path, patch):
    cfg = load_realtime_config(default_config_path('agent3', 'gemini-3.8-flash'))
    cfg['api'].update(patch)
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):
        load_realtime_config(path)
