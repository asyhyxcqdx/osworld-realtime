import base64
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from desktop_env.controllers.python import PythonController
from desktop_env.realtime_contract import verify_server_source
from mm_agents.realtime_agent import ModelWire, RealtimeAgent
from mm_agents.realtime_config import agent_kwargs, default_config_path, load_realtime_config
from mm_agents.realtime_protocol import ForbiddenShortcutError


@pytest.mark.parametrize('model', ['claude-fable-5', 'gpt-6-astra'])
@pytest.mark.parametrize('variant', ['agent1', 'agent2', 'agent3', 'agent4'])
def test_actual_configs_preserve_image_bytes_and_action_coordinates(model, variant):
    config = load_realtime_config(default_config_path(variant, model), variant=variant)
    wire = ModelWire(model, config['api']['protocol'])
    kwargs = agent_kwargs(config)
    agent = RealtimeAgent(**kwargs, wire=wire)
    screenshot = io.BytesIO()
    Image.new('RGB', (1920, 1080), '#012345').save(screenshot, format='PNG')
    original = screenshot.getvalue()
    coords = {'x': 754, 'y': 549}
    if wire.protocol == 'anthropic_messages':
        raw = {'content': [{'type': 'tool_use', 'id': 'c1', 'name': 'computer_click', 'input': coords}]}
    else:
        raw = {'output': [{'type': 'function_call', 'call_id': 'c1', 'name': 'computer_click', 'arguments': json.dumps(coords)}]}
    wire.request = Mock(return_value=raw)
    _, actions = agent.predict('task', {'screenshot': original, 'task_time_s': 0})
    assert actions == [{'action_type': 'CLICK', 'parameters': coords}]
    parts = wire.request.call_args.args[1][-1]['content']
    block = next(b for b in parts if b['type'] in {'image', 'input_image'})
    encoded = block['source']['data'] if 'source' in block else block['image_url'].split(',', 1)[1]
    assert base64.b64decode(encoded) == original
    assert '1920x1080' in agent.system


@pytest.mark.parametrize('patch', [
    {'agent_id': 'vanilla'},
    {'constraints': {}},
    {'action': {'mode': 'atomic', 'max_actions_per_turn': 1, 'allow_done': True, 'allow_wait': True, 'allow_fail': False}},
])
def test_config_rejects_capability_or_constraint_drift(tmp_path, patch):
    import yaml
    config = load_realtime_config(default_config_path('agent4', 'claude-fable-5'))
    config.update(patch)
    p = tmp_path / 'config.yaml'
    p.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError):
        load_realtime_config(p)


def test_realtime_server_source_check_blocks_old_vm_before_recording(monkeypatch):
    controller = PythonController('127.0.0.1', 1234)
    monkeypatch.setattr('desktop_env.controllers.python.requests.get', Mock(return_value=SimpleNamespace(status_code=200)))
    monkeypatch.setattr('desktop_env.realtime_contract.requests.post', Mock(return_value=SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {'returncode': 0, 'output': '{}'})))
    controller._realtime_request = Mock()
    with pytest.raises(RuntimeError, match='does not match'):
        controller.start_realtime_recording()
    controller._realtime_request.assert_not_called()


def test_matching_source_hashes_are_recorded(monkeypatch):
    root = Path(__file__).resolve().parents[1] / 'desktop_env/server'
    hashes = {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in ('realtime.py', 'fmp4.py')}
    monkeypatch.setattr('desktop_env.realtime_contract.requests.post', Mock(return_value=SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {'returncode': 0, 'output': json.dumps(hashes)})))
    assert verify_server_source('http://vm:123') == hashes


def test_split_shortcut_is_rejected_across_atomic_decisions():
    controller = PythonController('127.0.0.1', 1234)
    controller._realtime_request = Mock(return_value={'done': False})
    controller.execute_sequence([{'action_type': 'KEY_DOWN', 'parameters': {'key': 'ctrl'}}])
    with pytest.raises(ForbiddenShortcutError):
        controller.execute_sequence([{'action_type': 'PRESS', 'parameters': {'key': 'r'}}])
    assert controller._realtime_request.call_count == 1
    controller.execute_sequence([{'action_type': 'KEY_UP', 'parameters': {'key': 'ctrl'}}])
    controller.execute_sequence([{'action_type': 'PRESS', 'parameters': {'key': 'r'}}])
    assert controller._realtime_request.call_count == 3


def _controller_for_start():
    controller = PythonController('127.0.0.1', 1234)
    capabilities = Mock(status_code=200)
    return controller, capabilities


def test_start_retries_a_busy_vm_until_it_answers(monkeypatch):
    import desktop_env.controllers.python as python_module

    controller, capabilities = _controller_for_start()
    monkeypatch.setattr(python_module.requests, 'get', Mock(return_value=capabilities))
    # start_realtime_recording imports it locally from the contract module
    monkeypatch.setattr('desktop_env.realtime_contract.verify_server_source', lambda *_: 'sha')
    monkeypatch.setattr(python_module, 'START_RETRY_DELAYS', (0.0, 0.0))  # no sleeping
    busy = RuntimeError(
        'Realtime endpoint /start failed (400): Set the VM screen to 1920x1080 before starting the experiment'
    )
    controller._realtime_request = Mock(side_effect=[busy, busy, {'session_id': 's-1'}])

    data = controller.start_realtime_recording(fragment_ms=100)

    assert data['session_id'] == 's-1' and data['server_source_sha256'] == 'sha'
    assert controller.realtime_session == 's-1'
    assert controller._realtime_request.call_count == 3


def test_start_gives_up_after_the_retry_budget(monkeypatch):
    import desktop_env.controllers.python as python_module

    controller, capabilities = _controller_for_start()
    monkeypatch.setattr(python_module.requests, 'get', Mock(return_value=capabilities))
    # start_realtime_recording imports it locally from the contract module
    monkeypatch.setattr('desktop_env.realtime_contract.verify_server_source', lambda *_: 'sha')
    monkeypatch.setattr(python_module, 'START_RETRY_DELAYS', (0.0, 0.0))
    timed_out = RuntimeError(
        'Realtime endpoint /start failed (409): Timed out waiting for the first complete fragment'
    )
    controller._realtime_request = Mock(side_effect=timed_out)

    with pytest.raises(RuntimeError, match='realtime recording failed after 3 attempts'):
        controller.start_realtime_recording()
    assert controller._realtime_request.call_count == 3


def test_start_does_not_retry_a_missing_extension(monkeypatch):
    import desktop_env.controllers.python as python_module

    controller = PythonController('127.0.0.1', 1234)
    monkeypatch.setattr(python_module.requests, 'get', Mock(return_value=Mock(status_code=404)))
    controller._realtime_request = Mock()
    with pytest.raises(RuntimeError, match='needs the realtime extension'):
        controller.start_realtime_recording()
    assert controller._realtime_request.call_count == 0
