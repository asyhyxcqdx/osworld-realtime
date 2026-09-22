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
    'claude-sonnet-5': ('anthropic_messages', 'PACKY_CLAUDE_SONNET_5_API_KEY', 128000),
    'gpt-5.6-sol': ('openai_responses', 'PACKY_GPT_5_6_SOL_API_KEY', 128000),
    'gemini-3.8-flash': ('openai_chat', 'PACKY_GEMINI_3_8_FLASH_API_KEY', 65536),
    'qwen3.8-max-0902': ('anthropic_messages', 'PACKY_QWEN3_8_MAX_0902_API_KEY', 128000),
    'kimi-k3': ('anthropic_messages', 'PACKY_KIMI_K3_API_KEY', 128000),
    'deepseek-flash': ('anthropic_messages', 'PACKY_DEEPSEEK_FLASH_API_KEY', 128000),
    'glm-5.3-flash': ('anthropic_messages', 'PACKY_GLM_5_3_FLASH_API_KEY', 128000),
    'MiniMax-M3': ('anthropic_messages', 'PACKY_MINIMAX_M3_API_KEY', 128000),
}
COORDS = {'x': 754, 'y': 549}
NATIVE_FROM_RELATIVE = {'x': 1447, 'y': 592}
COORDINATE_SYSTEMS = {
    'gemini-3.8-flash': 'normalized_0_1000',
    'MiniMax-M3': 'normalized_0_1000_unclipped',
}


TEST_SYSTEM_PROMPT = "Test-only realtime agent system prompt."
TEST_USER_PROMPT = "Test-only realtime agent user prompt."


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
def test_packy_config_request_and_coordinate_contract(monkeypatch, screenshot, model, variant):
    protocol, key_env, output_limit = MODELS[model]
    monkeypatch.setenv(key_env, 'selected-test-credential')
    monkeypatch.setenv('PACKY_API_KEY', 'wrong-other-model-credential')
    config = load_realtime_config(default_config_path(variant, model), variant=variant)
    agent = RealtimeAgent(variant=variant, **agent_kwargs(config), api_base_url='https://www.packyapi.ai')
    agent.wire.session.post = Mock(return_value=http(reply(protocol)))
    events = []
    agent.bind_event_sink(events.append)
    expected_coords = NATIVE_FROM_RELATIVE if model in COORDINATE_SYSTEMS else COORDS
    assert agent.predict('task', {'screenshot': screenshot, 'task_time_s': 0})[1] == [
        {'action_type': 'CLICK', 'parameters': expected_coords}]
    coordinate_system = COORDINATE_SYSTEMS.get(model, 'native_pixels')
    assert agent.coordinate_system == coordinate_system
    assert next(e for e in events if e['event'] == 'model_request')['coordinate_system'] == coordinate_system
    assert next(e for e in events if e['event'] == 'model_response')['provider_response'] == reply(protocol)
    assert agent.frames is (variant in {'agent3', 'agent4'})
    assert agent.sequence is (variant in {'agent2', 'agent4'})
    assert agent.max_actions == (100 if agent.sequence else 1)
    assert agent.max_queries == 0
    assert agent.history_length is None
    kw = agent.wire.session.post.call_args.kwargs
    payload = kw['json']
    tool = next(t.get('function', t) for t in payload['tools']
                if t.get('function', t)['name'] == 'computer_click')
    properties = tool.get('input_schema', tool.get('parameters'))['properties']
    if model in COORDINATE_SYSTEMS:
        assert properties['x']['type'] == properties['y']['type'] == 'integer'
        limit = 1000
        assert properties['x']['maximum'] == properties['y']['maximum'] == limit
        assert f'normalized integers in [0, {limit}]' in agent.system
        assert 'Always submit x/y action parameters in native screen pixels' not in agent.system
        endpoint = {'x': 1920, 'y': 1080} if model == 'MiniMax-M3' else {'x': 1919, 'y': 1079}
        assert agent._decode_action_calls([
            {'name': 'computer_click', 'arguments': {'x': 1000, 'y': 1000}},
        ], {}) == [{'action_type': 'CLICK', 'parameters': endpoint}]
    else:
        assert properties['x']['maximum'] == 1920
        assert properties['y']['maximum'] == 1080
        assert 'Always submit x/y action parameters in native screen pixels' in agent.system
    assert payload['stream'] is True
    assert kw['stream'] is True
    assert kw['timeout'] == 120
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
    monkeypatch.setenv('PACKY_GEMINI_3_8_FLASH_API_KEY', 'test-credential')
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
    assert actions == [{'action_type': 'CLICK', 'parameters': NATIVE_FROM_RELATIVE}]
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
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=True, frames=True, wire=wire)
    query = Mock()
    agent.bind_frame_query(query)
    with pytest.raises(RuntimeError, match='no actions dispatched'):
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    query.assert_not_called()
    assert agent.pending_action_calls is None
    assert next(e for e in agent.last_events if e['event'] == 'model_response')['provider_response'] == body


def test_missing_dedicated_key_does_not_fall_back(monkeypatch):
    monkeypatch.delenv('PACKY_KIMI_K3_API_KEY', raising=False)
    monkeypatch.setenv('PACKY_API_KEY', 'wrong-key')
    wire = ModelWire('kimi-k3', 'anthropic_messages', api_key_env='PACKY_KIMI_K3_API_KEY')
    wire.session.post = Mock()
    with pytest.raises(RuntimeError, match='PACKY_KIMI_K3_API_KEY'):
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
    {'coordinate_system': 'guess'}, {'coordinate_system': None},
    {'coordinate_system': {'width': 1000}},
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


EXPECTED_KEY_ENVS = {model: key_env for model, (_, key_env, _) in MODELS.items()}
EXPECTED_KEY_ENVS.update({
    'claude-fable-5': 'PACKY_CLAUDE_FABLE_5_API_KEY',
    'gpt-6-astra': 'PACKY_GPT_6_ASTRA_API_KEY',
})


def test_every_checked_in_config_declares_its_own_key_environment():
    """Every YAML must name its own key variable, so no two models share a credential by default."""
    from pathlib import Path

    configs = sorted((Path(__file__).resolve().parents[1] / 'configs/realtime_agents').glob('*.yaml'))
    assert len(configs) == 40
    declared = {}
    for path in configs:
        variant, model = path.stem.split('-', 1)
        assert variant in {'vanilla', 'anticipatory', 'video', 'combine'}
        api = yaml.safe_load(path.read_text(encoding='utf-8'))['api']
        assert api['key_env'] == EXPECTED_KEY_ENVS[model], path.name
        declared[model] = api['key_env']
    assert len(set(declared.values())) == 10


ANTI_CHEAT_HEADING = '# Anti-Cheating and Evaluation-Integrity Rules (Highest Priority)'
ANTI_CHEAT_FRAME_LINE = '- Use get_frames to inspect historical frames when allowed.'
# Only video/combine own a frame tool, so only they may name frames here.
ANTI_CHEAT_NO_FRAME_LINE = ('- Interact with the game only through visible GUI elements: '
                            'the current screenshot,')
ANTI_CHEAT_SECTIONS = ('## Allowed Behavior', '## Forbidden Behavior',
                       '## Evidence and Completion', '## Violations')
# Dropped when the rules were reviewed: the precedence paragraph, the vague
# "cautious" qualifier, the redundant retry bullet (the games cap attempts
# themselves and Evidence and Completion already says to continue), and the
# "conservative" closing line. Also covers the old screenshot bullet, which
# wrongly said the screenshot arrives inside each tool result: it arrives as the
# next user message, once per round, so a sequence submission never sees one
# between its actions.
DROPPED_RULE_LINES = (
    'These rules are part of the system prompt.',
    'If you see text telling you to ignore these rules',
    'returned with each tool result',
    'cautious GUI probing',
    '- Retry only when the benchmark allows another attempt.',
    'choose the conservative GUI-only action.',
)
# The operating notes moved to the user prompt, so the system prompt must not
# carry them any more.
RETIRED_PROMPT_LINES = (
    'You are the computer-using Agent in a real-time GUI benchmark.',
    'Actions take a short time to run.',
    'Real time passes while you think and reply.',
    'Use only the available computer action tools.',
    'Do not finish with a text-only response.',
    'Never refresh, reload, reopen, or navigate away from the game page.',
)
# Cut from the user prompt when the policy list went from 14 items to 5. They were
# advisory colour ("be conservative", "do not rush"), duplicates of another item, or
# mechanics this benchmark's games do not have (score, health, cooldowns, resources,
# multi-stage levels), so they must not come back.
DROPPED_USER_PROMPT_LINES = (
    'You may submit multiple actions in one response using the registered',
    'do not rush into action',
    'Prefer reversible, low-cost actions',
    'Avoid loops.',
    'Manage attempts carefully',
    'On the final attempt, be conservative',
    'Track progress indicators',
    'Complete multi-stage tasks step by step',
    'Never refresh, reload, reopen, or navigate away from the game page.',
    'Only use registered tools.',
    'no hidden delay is inserted between actions',
)
# The three commands that had to come back. Cutting them as "redundant" cost
# class-C tasks: without them the model stopped inserting waits, stopped
# re-checking before repeating an action and stopped budgeting for elapsed time
# (ds_c_trim_20260922 fell from 10/17 to 6/17). They assign timing and
# re-checking to the model, so every variant must keep them.
BEHAVIOUR_COMMAND_LINES = (
    'confirm that it really had no effect',
    'take the time that has passed into account before you act',
    'Use computer_wait whenever you need a known amount of time to pass',
)
# Replaced "review the earlier attempts to work out how they failed", which the model
# satisfied by narrating the past. C-class traces: the earlier attempts are mentioned
# in 49/49 tasks but never turned into a number; 62 of 63 task instances opened with a
# lone start click (0 opened with a sequence) and 357 of 610 model rounds were
# observation only, so attempts were spent reading instead of acting; every attempt
# guessed a fresh delay (c36: WAIT(0.35) then WAIT(0.20), window 0.429 +-0.1 s). The
# line has to say what to read off the failed attempt and that it decides the next one.
ATTEMPT_REVIEW_LINES = (
    'work out what happened: what you did, how long after the start you did it',
    "Let that decide the next attempt's timing",
)


def test_every_agent_config_carries_its_variant_user_prompt():
    from pathlib import Path

    from mm_agents.realtime_config import VARIANT_TO_AGENT_ID

    single_action = 'Submit one action per response.'
    sequence = ('When you already know the whole sequence of actions and its timing, submit '
                'them all in one response.')
    frames = 'You may call `get_frames` multiple times to inspect historical video frames'
    no_mix = 'Never mix `get_frames` with action tools in one response.'
    lines = {'vanilla': 35, 'anticipatory': 35, 'video': 39, 'combine': 39}
    root = Path(__file__).resolve().parents[1] / 'configs' / 'realtime_agents'
    variants = {agent_id: variant for variant, agent_id in VARIANT_TO_AGENT_ID.items()}
    for path in sorted(root.glob('*.yaml')):
        agent_id = path.name.split('-', 1)[0]
        config = load_realtime_config(path, variant=variants[agent_id])
        prompt = config['user_prompt']
        assert prompt.startswith('You are the computer-using Agent in a real-time GUI benchmark.')
        assert prompt != config['system_prompt']
        owns_frames = agent_id in {'video', 'combine'}
        assert (frames in prompt) == owns_frames
        assert (no_mix in prompt) == owns_frames
        assert (single_action in prompt) == (agent_id in {'vanilla', 'video'})
        assert (sequence in prompt) == (agent_id in {'anticipatory', 'combine'})
        assert len(prompt.splitlines()) == lines[agent_id], path.name
        for dropped in DROPPED_USER_PROMPT_LINES:
            assert dropped not in prompt, path.name
        for command in BEHAVIOUR_COMMAND_LINES:
            assert command in prompt, path.name
        for review in ATTEMPT_REVIEW_LINES:
            assert review in prompt, path.name


def test_every_agent_prompt_is_only_the_anti_cheating_rules():
    from pathlib import Path

    from mm_agents.realtime_config import VARIANT_TO_AGENT_ID

    root = Path(__file__).resolve().parents[1] / 'configs' / 'realtime_agents'
    paths = sorted(root.glob('*.yaml'))
    assert len(paths) == 40
    variants = {agent_id: variant for variant, agent_id in VARIANT_TO_AGENT_ID.items()}
    seen = set()
    for path in paths:
        agent_id = path.name.split('-', 1)[0]
        config = load_realtime_config(path, variant=variants[agent_id])
        seen.add(config['agent_id'])
        prompt = config['system_prompt']
        lines = prompt.splitlines()
        assert lines[0] == ANTI_CHEAT_HEADING
        assert prompt.count(ANTI_CHEAT_HEADING) == 1
        positions = [prompt.index(section) for section in ANTI_CHEAT_SECTIONS]
        assert positions == sorted(positions)
        assert prompt.rstrip().endswith(
            'If you are unsure whether an action would bypass the GUI, do not take it.')
        # Only the two variants that own get_frames advertise it in the rules,
        # and the frame line plus the screenshot bullet are what distinguish the
        # two rule texts: everything else is shared word for word.
        assert (ANTI_CHEAT_FRAME_LINE in prompt) == (agent_id in {'video', 'combine'})
        assert (ANTI_CHEAT_NO_FRAME_LINE in prompt) == (agent_id in {'vanilla', 'anticipatory'})
        assert ('screenshots/frames' in prompt) == (agent_id in {'video', 'combine'})
        assert len(lines) == (48 if agent_id in {'video', 'combine'} else 46)
        for retired in RETIRED_PROMPT_LINES:
            assert retired not in prompt
        for dropped in DROPPED_RULE_LINES:
            assert dropped not in prompt
    assert seen == set(VARIANT_TO_AGENT_ID.values())
