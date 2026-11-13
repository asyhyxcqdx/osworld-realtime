import base64
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask, Response
from PIL import Image
import pytest
from werkzeug.serving import make_server

from desktop_env.server.realtime import register_realtime
from mm_agents.realtime_agent import RealtimeAgent, ModelWire
from mm_agents.realtime_config import load_realtime_config, default_config_path
from mm_agents.realtime_coordinates import CoordinateAdapter
from mm_agents.realtime_observers import ObserverManager, decode_observer_call, observer_tools
from mm_agents.realtime_protocol import ACTION_TOOLS


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def vm(tmp_path, monkeypatch):
    monkeypatch.setattr('desktop_env.controllers.python.PYAUTOGUI_PKGS_PREFIX', '{command}')
    app = Flask(__name__)
    buf = io.BytesIO()
    Image.new('RGB', (640, 360), 'green').save(buf, 'PNG')
    image = buf.getvalue()
    monkeypatch.setattr('desktop_env.server.realtime.capture_png', lambda: image)
    gui = SimpleNamespace(PAUSE=0, FAILSAFE=False, events=[], keyUp=Mock(), mouseUp=Mock())
    gui.click = lambda **kwargs: gui.events.append((time.monotonic(), kwargs))
    gui.screenshot = lambda **kwargs: Image.open(io.BytesIO(image))
    register_realtime(app, lambda: Response(image, mimetype='image/png'), gui)
    recorder = app.extensions['osworld_realtime']
    recorder.path = tmp_path / 'vm'
    recorder.path.mkdir()
    recorder.session_id = 'test-session'
    origin = time.monotonic()
    recorder.elapsed = lambda: time.monotonic() - origin
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    controller = SimpleNamespace(http_server=f'http://127.0.0.1:{server.server_port}', realtime_session=recorder.session_id)
    env = SimpleNamespace(controller=controller, _get_obs=lambda: {'screenshot': image, 'task_time_s': recorder.elapsed()})
    yield env, gui, app, recorder
    server.shutdown()
    thread.join()


def manager(vm, tmp_path, mode='jev', classifier=None):
    events = []
    instance = ObserverManager(vm[0], mode, tmp_path / 'host', events.append,
                               {'timeout_s': 1}, classifier=classifier)
    return instance, events


@pytest.mark.parametrize('mode', ['jev', 'script'])
def test_configs_and_tool_schemas_are_separate(mode):
    root = Path(__file__).resolve().parents[1] / 'configs/realtime_agents/observers' / mode
    for path in root.glob('*.yaml'):
        config = load_realtime_config(path, variant='agent4')
        assert config['observer_mode'] == mode
        if mode == 'script':
            assert 'Jev' not in config['system_prompt'] + config['user_prompt']
        else:
            assert 'submit a complete Python' not in config['system_prompt']
    tools = observer_tools(mode, ACTION_TOOLS)
    assert {t['name'] for t in tools} == {'create_observer', 'stop_observer'}
    assert set(tools[0]['parameters']['properties']) == {'name', 'cases' if mode == 'jev' else 'code'}


def test_nested_case_coordinates_are_converted_once():
    call = {'name': 'create_observer', 'arguments': {'name': 'target', 'cases': {
        'green': [{'action_type': 'CLICK', 'parameters': {'x': 500, 'y': 500}}]}}}
    result = decode_observer_call(call, 'jev', CoordinateAdapter('normalized_0_1000'))
    assert result['parameters']['cases']['green'][0]['parameters'] == {'x': 960, 'y': 540}
    assert call['arguments']['cases']['green'][0]['parameters']['x'] == 500
    with pytest.raises(ValueError):
        decode_observer_call({'name': 'create_observer', 'arguments': {'name': 'x', 'code': 'pass'}},
                             'jev', CoordinateAdapter())


def test_none_does_nothing_repeated_case_executes_and_stop_cancels(vm, tmp_path):
    count = 0
    def classify(image, criteria):
        nonlocal count
        count += 1
        time.sleep(.025)
        return {'answers': {'case': {'choice': 'NONE' if count <= 2 else 'C0'}}}
    runtime, events = manager(vm, tmp_path, classifier=classify)
    try:
        value = runtime.create({'name': 'green', 'cases': {'green': [
            {'action_type': 'CLICK', 'parameters': {'x': 12, 'y': 30}}]}})
        assert value['status'] == 'running'
        eventually(lambda: len(vm[1].events) >= 3)
        assert runtime.stop('green')['status'] == 'stopped'
        n = len(vm[1].events)
        time.sleep(.15)
        assert len(vm[1].events) == n
        assert runtime.stop('green')['status'] == 'stopped'
    finally:
        runtime.close()
    responses = [e for e in events if e['event'] == 'observer_jev_response']
    assert [e['choice'] for e in responses[:2]] == ['NONE', 'NONE']
    assert (tmp_path / 'host' / 'observer_summary.json').exists()


def test_stopping_then_reusing_name_discards_late_old_reply(vm, tmp_path):
    entered, release = threading.Event(), threading.Event()
    def classify(image, criteria):
        entered.set()
        release.wait(2)
        return {'answers': {'case': {'choice': 'C0'}}}
    runtime, events = manager(vm, tmp_path, classifier=classify)
    try:
        old = runtime.create({'name': 'same', 'cases': {'green': [{'action_type': 'CLICK', 'parameters': {}}]}})
        assert entered.wait(1)
        assert runtime.create({'name': 'same', 'cases': {'green': []}})['status'] == 'error'
        runtime.stop('same')
        new = runtime.create({'name': 'same', 'cases': {'green': []}})
        assert old['id'] != new['id']
        release.set()
        time.sleep(.1)
        runtime.stop('same')
        assert vm[1].events == []
    finally:
        release.set()
        runtime.close()


def test_syntax_error_is_a_tool_result_not_a_started_process(vm, tmp_path):
    runtime, _ = manager(vm, tmp_path, mode='script')
    try:
        result = runtime.create({'name': 'bad', 'code': 'while True\n    pass'})
        assert result['status'] == 'failed'
        assert result['error']['error_type'] == 'SyntaxError'
        assert result['error']['line'] == 1
        runtime.poll()
        assert runtime.drain_feedback()[0]['error']['error_type'] == 'SyntaxError'
    finally:
        runtime.close()


def test_main_wait_does_not_block_observer_input_or_screenshot(vm, tmp_path):
    runtime, _ = manager(vm, tmp_path, mode='jev', classifier=lambda *_: {'answers': {'case': {'choice': 'NONE'}}})
    result = runtime.rpc('POST', '/observers/create', {'name': 'manual'})
    try:
        wait_done = threading.Event()
        def main_wait():
            runtime.rpc('POST', '/sequence', {'groups': [
                {'action': {'action_type': 'WAIT', 'parameters': {'duration_s': .6}}, 'commands': []}]})
            wait_done.set()
        thread = threading.Thread(target=main_wait)
        thread.start()
        time.sleep(.1)
        start = time.monotonic()
        runtime.rpc('GET', '/observation')
        runtime.rpc('POST', '/observers/call', {'observer_id': result['id'], 'method': 'click',
                                              'kwargs': {'x': 10, 'y': 20}})
        assert time.monotonic() - start < .3
        assert not wait_done.is_set()
        thread.join()
    finally:
        runtime.close()


def test_legacy_observations_finish_reading_shared_capture_before_next_write(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    app = Flask(__name__)
    target = tmp_path / 'screenshot.png'
    in_flight = 0
    peak = 0
    counter_lock = threading.Lock()
    def capture():
        nonlocal in_flight, peak
        with counter_lock:
            in_flight += 1
            peak = max(peak, in_flight)
        Image.new('RGB', (64, 36), 'green').save(target, 'PNG')
        # Like send_file(), the response reads the file only when consumed.
        def stream():
            nonlocal in_flight
            try:
                with target.open('rb') as source:
                    yield source.read(16)
                    time.sleep(.03)
                    yield source.read()
            finally:
                with counter_lock:
                    in_flight -= 1
        return Response(stream(), mimetype='image/png')
    gui = SimpleNamespace(keyUp=Mock(), mouseUp=Mock())
    register_realtime(app, capture, gui)
    recorder = app.extensions['osworld_realtime']
    recorder.session_id = 'parallel-capture'
    recorder.elapsed = time.monotonic
    with app.test_client() as client:
        configured = client.post('/realtime/observers/configure', json={
            'session_id': recorder.session_id, 'mode': 'none'})
        assert configured.status_code == 200
    barrier = threading.Barrier(4)
    def observe():
        barrier.wait(timeout=2)
        with app.test_client() as client:
            response = client.get('/realtime/observation?session_id=parallel-capture')
            assert response.status_code == 200
            payload = base64.b64decode(response.json['image'])
            with Image.open(io.BytesIO(payload)) as image:
                image.verify()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: observe(), range(4)))
    assert peak == 1 and in_flight == 0


def test_agent_gets_background_failure_without_an_extra_model_call():
    wire = ModelWire('mock', 'anthropic_messages')
    wire.request = Mock(side_effect=[
        {'content': [{'type': 'tool_use', 'id': 'c', 'name': 'create_observer',
                      'input': {'name': 'bad', 'code': 'missing_name'}}]},
        {'content': [{'type': 'tool_use', 'id': 's', 'name': 'stop_observer', 'input': {'name': 'bad'}}]},
    ])
    agent = RealtimeAgent(sequence=True, frames=True, observer_mode='script', wire=wire,
                          system_prompt_text='test', user_prompt_text='test')
    obs = {'screenshot': b'png', 'task_time_s': 1}
    _, actions = agent.predict('', obs)
    assert actions[0]['action_type'] == 'OBSERVER_CREATE'
    agent.record_action_result(actions, info={'observer_results': {0: {'name': 'bad', 'status': 'running'}}})
    agent.bind_observer_feedback(lambda: [{'name': 'bad', 'status': 'failed', 'error_type': 'NameError', 'message': 'missing_name is not defined'}])
    agent.predict('', obs)
    assert wire.request.call_count == 2
    assert 'NameError' in json.dumps(wire.request.call_args.args[1])
    tools = wire.request.call_args.kwargs['tools']
    assert 'code' in next(t for t in tools if t['name'] == 'create_observer')['parameters']['properties']


def test_repeated_invalid_observer_arguments_are_model_protocol_failure():
    from mm_agents.realtime_protocol import AgentProtocolError
    wire = ModelWire('mock', 'anthropic_messages')
    wire.request = Mock(return_value={'content': [{'type': 'tool_use', 'id': 'bad',
        'name': 'create_observer', 'input': {'name': 'x', 'cases': 'invalid'}}]})
    agent = RealtimeAgent(sequence=True, frames=True, observer_mode='jev', wire=wire,
                          system_prompt_text='test', user_prompt_text='test')
    with pytest.raises(AgentProtocolError):
        agent.predict('', {'screenshot': b'png', 'task_time_s': 1})
    assert wire.request.call_count == 3


def test_stop_interrupts_long_pyautogui_input_and_releases_key(vm, tmp_path):
    runtime, _ = manager(vm, tmp_path, classifier=lambda *_: {})
    job = runtime.rpc('POST', '/observers/create', {'name': 'long_input'})
    gui = vm[1]
    gui.time = time
    entered = threading.Event()
    def long_press(key, interval=10):
        entered.set()
        gui.time.sleep(interval)
        gui.events.append(('unexpected_late_input', key))
    gui.press = long_press
    def call():
        try:
            runtime.rpc('POST', '/observers/call', {'observer_id': job['id'], 'method': 'press', 'args': ['a']})
        except Exception:
            pass  # Cancellation rejects the pending call.
    thread = threading.Thread(target=call)
    try:
        thread.start()
        assert entered.wait(1)
        began = time.monotonic()
        result = runtime.stop('long_input')
        assert time.monotonic() - began < 1
        thread.join(timeout=1)
        assert not thread.is_alive() and gui.events == []
        assert not result['held_keys']
        gui.keyUp.assert_called_with('a')
    finally:
        runtime.close()


def test_jev_error_reaches_feedback_and_persists_failed_status(vm, tmp_path):
    def fail(*_):
        raise ValueError('classifier unavailable')
    runtime, _ = manager(vm, tmp_path, classifier=fail)
    try:
        runtime.create({'name': 'bad_jev', 'cases': {'green': []}})
        eventually(lambda: any(j['status'] == 'failed' for j in runtime.poll()['observers']))
        feedback = runtime.drain_feedback()
        assert feedback[-1]['error']['message'] == 'classifier unavailable'
    finally:
        runtime.close()
    assert json.loads((tmp_path / 'host/observer_summary.json').read_text())[0]['status'] == 'failed'


def test_done_stops_background_observers(vm, tmp_path):
    runtime, _ = manager(vm, tmp_path, classifier=lambda *_: {})
    try:
        runtime.rpc('POST', '/observers/create', {'name': 'active'})
        result = runtime.rpc('POST', '/sequence', {'groups': [
            {'action': {'action_type': 'DONE', 'parameters': {}}, 'commands': []}]})
        assert result['done']
        assert runtime.poll()['observers'][0]['status'] == 'stopped'
    finally:
        runtime.close()


def test_audit_includes_early_observer_events_and_uses_correct_permission(tmp_path, monkeypatch):
    from mm_agents.realtime_auditor import audit_task, build_audit_input
    (tmp_path / 'experiment.json').write_text(json.dumps({'observer_mode': 'script'}))
    rows = [{'event': 'model_request', 'old': True}, {'event': 'observer_action', 'method': 'click'},
            {'event': 'model_request', 'request_messages': ['full history']}, {'event': 'observer_failed'}]
    (tmp_path / 'trajectory.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    result = build_audit_input(tmp_path)
    assert 'observer_action' in result and 'full history' in result and '"old"' not in result
    judge = Mock(return_value={'label': 'NOT_CHEAT', 'confidence': 1, 'evidence': [], 'reasoning': '', 'usage': {}})
    monkeypatch.setattr('mm_agents.realtime_auditor.call_judge', judge)
    audit_task(tmp_path, 1, settings=('mock', 'unused', 'http://unused', 'old prompt'))
    assert 'Python scripts in the game VM' in judge.call_args.args[1][3]
