"""Jev outages recover on fresh frames or leave an unscored, resumable run."""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from PIL import Image

from test_realtime_observers import vm, manager, eventually
from test_realtime_audit_resume import runner
from mm_agents import realtime_observers as observers
from mm_agents.realtime_run_status import pending_rerun, mark_pending_rerun


def test_recovery_uses_fresh_frames_and_resets_failure_budget(vm, tmp_path, monkeypatch):
    monkeypatch.setattr(observers, 'JEV_RETRY_DELAYS', (.001, .001))
    seen = []
    captures = 0
    def capture():
        nonlocal captures
        captures += 1
        buf = io.BytesIO()
        Image.new('RGB', (640, 360), (captures % 255, 0, 0)).save(buf, 'PNG')
        return buf.getvalue()
    monkeypatch.setattr('desktop_env.server.realtime.capture_png', capture)
    def classify(image, criteria):
        seen.append(image)
        if len(seen) in {1, 2, 4, 5}:
            raise requests.exceptions.SSLError('unexpected EOF')
        return {'answers': {'case': {'choice': 'C0' if len(seen) in {3, 6} else 'NONE'}}}
    runtime, events = manager(vm, tmp_path, classifier=classify)
    try:
        runtime.create({'name': 'target', 'cases': {'visible': [
            {'action_type': 'CLICK', 'parameters': {'x': 10, 'y': 20}}]}})
        eventually(lambda: len(vm[1].events) == 2)
        runtime.stop('target')
        assert runtime.network_error is None
        assert len(set(seen[:6])) == 6
        assert len([e for e in events if e['event'] == 'observer_jev_error']) == 4
        assert len([e for e in events if e['event'] == 'observer_jev_recovered']) == 2
    finally:
        runtime.close()
    assert len(vm[1].events) == 2


@pytest.mark.parametrize('error,requests_expected', [
    (requests.exceptions.SSLError('EOF'), 3),
    (requests.Timeout('timeout'), 3),
    (requests.HTTPError('unauthorized', response=SimpleNamespace(status_code=401)), 1),
    (requests.HTTPError('unavailable', response=SimpleNamespace(status_code=503)), 3),
])
def test_unrecoverable_api_error_is_infrastructure_failure(vm, tmp_path, monkeypatch, error, requests_expected):
    monkeypatch.setattr(observers, 'JEV_RETRY_DELAYS', (.001, .001))
    classify = Mock(side_effect=error)
    runtime, events = manager(vm, tmp_path, classifier=classify)
    try:
        runtime.create({'name': 'target', 'cases': {'visible': [{'action_type': 'CLICK', 'parameters': {}}]}})
        eventually(lambda: runtime.network_error is not None)
        with pytest.raises(observers.ObserverNetworkError):
            runtime.drain_feedback()
        with pytest.raises(observers.ObserverNetworkError):
            runtime.execute([{'action_type': 'CLICK', 'parameters': {}}])
    finally:
        runtime.close()
    assert classify.call_count == requests_expected
    assert vm[1].events == []
    assert json.loads((tmp_path/'host/observer_summary.json').read_text())[0]['status'] == 'failed'


def test_stop_during_retry_wait_never_retries_or_marks_task_failed(vm, tmp_path, monkeypatch):
    monkeypatch.setattr(observers, 'JEV_RETRY_DELAYS', (10, 10))
    classify = Mock(side_effect=requests.ConnectionError('offline'))
    runtime, events = manager(vm, tmp_path, classifier=classify)
    try:
        runtime.create({'name': 'target', 'cases': {'visible': []}})
        eventually(lambda: any(e['event'] == 'observer_jev_error' for e in events))
        runtime.stop('target')
    finally:
        runtime.close()
    assert classify.call_count == 1
    assert runtime.network_error is None


def test_gui_transport_error_is_not_replayed(vm, tmp_path):
    classify = Mock(return_value={'answers': {'case': {'choice': 'C0'}}})
    runtime, events = manager(vm, tmp_path, classifier=classify)
    original_rpc = runtime.rpc
    sequences = []
    def rpc(method, path, data=None):
        if path == '/sequence':
            sequences.append(data)
            raise requests.ConnectionError('reply lost after GUI submission')
        return original_rpc(method, path, data)
    runtime.rpc = rpc
    try:
        runtime.create({'name': 'target', 'cases': {'visible': [{'action_type': 'CLICK', 'parameters': {}}]}})
        eventually(lambda: any(j['status'] == 'failed' for j in runtime.poll()['observers']))
    finally:
        runtime.close()
    assert len(sequences) == classify.call_count == 1
    assert runtime.network_error['stage'] == 'observer_transport'


@pytest.mark.parametrize('recovers', [False, True])
def test_runner_excludes_failed_observer_and_preserves_recording(vm, tmp_path, monkeypatch, recovers):
    from lib_run_realtime import run_realtime_example
    monkeypatch.setattr(observers, 'JEV_RETRY_DELAYS', (.001, .001))
    monkeypatch.setattr('lib_run_single.setup_logger', lambda *args: None)
    monkeypatch.setattr('desktop_env.realtime_contract.verify_server_source', lambda *a, **k: {})
    evaluation = Mock(return_value=1) if recovers else Mock(side_effect=AssertionError('outage must not be scored'))
    monkeypatch.setattr('lib_run_single._evaluate_with_details', evaluation)
    monkeypatch.setattr('mm_agents.realtime_auditor.audit_task', lambda *a, **k: (1, {'label': 'NOT_CHEAT'}))
    monkeypatch.setattr('lib_run_realtime.log_task_completion', lambda *a: None)
    real_manager = observers.ObserverManager
    instances = []
    def make_manager(*args, **kwargs):
        attempts = 0
        def classify(*_):
            nonlocal attempts
            attempts += 1
            if not recovers or attempts <= 2:
                raise requests.ConnectionError('offline')
            return {'answers': {'case': {'choice': 'C0' if attempts == 3 else 'NONE'}}}
        kwargs['classifier'] = classify
        result = real_manager(*args, **kwargs)
        instances.append(result)
        return result
    monkeypatch.setattr(observers, 'ObserverManager', make_manager)
    env = vm[0]
    env.reset = Mock()
    env.step_sequence = Mock(return_value=({'screenshot': b'png'}, 0, True, {}))
    env.controller.last_observation_time = 0
    env.controller.last_capture_interval = (0, 0)
    env.controller.start_realtime_recording = Mock(return_value={})
    def finish(path):
        (tmp_path/'recording.mp4').write_bytes(b'original video')
    env.controller.end_realtime_recording = Mock(side_effect=finish)
    calls = 0
    def predict(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return '', [{'action_type': 'OBSERVER_CREATE', 'parameters': {'name': 'target', 'cases': {'visible': [{'action_type': 'CLICK', 'parameters': {'x': 10, 'y': 20}}]}}}]
        eventually(lambda: len(vm[1].events) == 1 if recovers else instances[0].network_error is not None)
        return '', [{'action_type': 'DONE', 'parameters': {}}]
    agent = SimpleNamespace(variant='agent4', sequence=True, frames=True, observer_mode='jev',
        observer_config={}, coordinate_system='native_pixels', system='test', counters={},
        wire=SimpleNamespace(protocol='test'), tool_format='native', reset=Mock(),
        bind_frame_query=Mock(), bind_event_sink=Mock(), bind_observer_feedback=Mock(), predict=predict)
    args = SimpleNamespace(model='test', observer_mode='jev', environment_ready_wait_s=0,
        recording_fragment_ms=100, max_sequence_actions=100, max_frame_queries=0, evaluation_settle_s=0)
    scores = []
    if recovers:
        run_realtime_example(agent, env, {}, 10, 'test', args, tmp_path, scores)
        assert scores == [1] and not pending_rerun(tmp_path)
        assert (tmp_path/'result.txt').read_text().strip() == '1'
        assert len(vm[1].events) == 1
        return
    with pytest.raises(observers.ObserverNetworkError):
        run_realtime_example(agent, env, {}, 10, 'test', args, tmp_path, scores)
    assert pending_rerun(tmp_path)['termination_reason'] == 'observer_network_error'
    assert json.loads((tmp_path/'agent_metrics.json').read_text())['run_status'] == 'pending_rerun'
    assert (tmp_path/'trajectory.jsonl').exists() and (tmp_path/'recording.mp4').exists()
    assert not (tmp_path/'result.txt').exists() and not (tmp_path/'game_result.json').exists()
    assert scores == []
    evaluation.assert_not_called()


def test_pending_run_is_archived_before_replay_and_excluded_from_averages(tmp_path, monkeypatch):
    from lib_run_realtime import run_realtime_example, resume_completed_task
    from scripts.python import run_realtime_batch as batch
    from mm_agents import realtime_auditor
    out = tmp_path/'task'
    out.mkdir()
    evidence = {'trajectory.jsonl': '{"event":"run_error"}\n', 'recording.mp4': 'old video'}
    for name, content in evidence.items():
        (out/name).write_text(content)
    mark_pending_rerun(out, {'message': 'offline'})
    assert resume_completed_task(out, {}, None, []) is False
    assert realtime_auditor.audit_one(out, force=True) == 'skipped: no completed game'
    row = batch.summarize(out)
    good = dict(row, task_dir='good', run_status=None, status='passed', pass_at_1=1, pass_at_3=1)
    total = batch.aggregate_model_row([row, good], model='test', key_env='KEY', run_id='test', return_code=1, elapsed_s=1)
    assert total['tasks_total'] == 2 and total['tasks_scored'] == 1 and total['tasks_pending_rerun'] == 1
    assert total['pass_at_3'] == 1
    assert batch.aggregate_model_row([row], model='test', key_env='KEY', run_id='test', return_code=1, elapsed_s=1)['pass_at_3'] is None
    monkeypatch.setattr('lib_run_single.setup_logger', lambda *a: None)
    class NewGameStarted(Exception): pass
    def reset(**kwargs):
        assert not pending_rerun(out)
        history = list((out/'_previous_runs').iterdir())
        assert len(history) == 1
        for name, content in evidence.items():
            assert (history[0]/name).read_text() == content
        raise NewGameStarted
    with pytest.raises(NewGameStarted):
        run_realtime_example(SimpleNamespace(), SimpleNamespace(reset=reset), {}, 10, '', None, out, [])


def test_resume_scan_preserves_pending_task_until_worker_starts(tmp_path, runner):
    out = tmp_path/'computer_13/screenshot/realtime_gui_bench/task'
    out.mkdir(parents=True)
    (out/'recording.mp4').write_bytes(b'evidence')
    mark_pending_rerun(out, {'message': 'offline'})
    tasks = runner['get_unfinished']('computer_13', 'test', 'screenshot', str(tmp_path),
                                    {'realtime_gui_bench': ['task']}, 'agent4')
    assert tasks == {'realtime_gui_bench': ['task']}
    assert pending_rerun(out) and (out/'recording.mp4').read_bytes() == b'evidence'


def test_http_service_retry_reconnects_and_then_executes_once(vm, tmp_path, monkeypatch):
    from flask import jsonify
    monkeypatch.setattr(observers, 'JEV_RETRY_DELAYS', (.001, .001))
    monkeypatch.setenv('JEV_API_KEY', 'test-key')
    monkeypatch.setenv('JEV_BASE_URL', vm[0].controller.http_server)
    monkeypatch.delenv('JEV_PROXY', raising=False)
    calls = []
    @vm[2].post('/v1/systemone')
    def classify_api():
        calls.append(1)
        if len(calls) == 1:
            return jsonify(error='temporary outage'), 503
        return jsonify(answers={'case': {'choice': 'C0' if len(calls) == 2 else 'NONE'}})
    runtime, events = manager(vm, tmp_path)
    try:
        runtime.create({'name': 'target', 'cases': {'visible': [
            {'action_type': 'CLICK', 'parameters': {'x': 10, 'y': 20}}]}})
        eventually(lambda: len(vm[1].events) == 1)
        runtime.stop('target')
    finally:
        runtime.close()
    assert len(vm[1].events) == 1 and runtime.network_error is None
    assert sum(e['event'] == 'observer_jev_error' for e in events) == 1
    assert sum(e['event'] == 'observer_jev_recovered' for e in events) == 1
