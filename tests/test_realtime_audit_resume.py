"""A judge outage cannot give a completed game another attempt."""
import argparse
import ast
import builtins
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib_run_realtime import run_realtime_example
from mm_agents import realtime_auditor as auditor


@pytest.fixture
def runner():
    # Exercise the actual resume scan and worker without importing CLI globals.
    path = Path(__file__).resolve().parents[1] / 'scripts/python/run_multienv.py'
    selected = {'get_result_dir', 'get_unfinished', 'run_env_tasks'}
    tree = ast.parse(path.read_text())
    module = ast.Module(body=[n for n in tree.body
                             if isinstance(n, ast.FunctionDef) and n.name in selected],
                        type_ignores=[])
    namespace = dict(os=os, json=json, argparse=argparse, Queue=object,
                     logger=logging.getLogger(__name__),
                     current_process=lambda: SimpleNamespace(name='test-worker'))
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace


def completed_game(task, score):
    task.mkdir(parents=True)
    payload = {'benchmark_id': 'C1', 'result': score, 'pass_at_3': score,
               'termination_reason': 'decision_limit',
               'status': 'passed' if score else 'running'}
    (task / 'result.json').write_text(json.dumps(payload))
    events = [{'event': 'model_request', 'request_messages': [{'role': 'user', 'content': 'play'}]},
              {'event': 'evaluation', 'result': score, 'details': payload}]
    (task / 'trajectory.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    (task / 'recording.mp4').write_bytes(b'original recording')
    (task / 'initial_state.png').write_bytes(b'original screenshot')
    (task / 'agent_metrics.json').write_text('{"action_decisions": 100}')


@pytest.mark.parametrize('score', [0.0, 1.0])
@pytest.mark.parametrize('legacy', [False, True])
def test_resume_retries_only_the_judge_for_wins_and_losses(tmp_path, monkeypatch, runner, score, legacy):
    task = tmp_path / 'results/computer_13/screenshot/realtime_gui_bench/task'
    completed_game(task, score)
    if not legacy:
        auditor.save_game_result(task, score)
    evidence = {p.name: p.read_bytes() for p in task.iterdir() if p.name != 'result.json'}
    config = tmp_path / 'config/examples/realtime_gui_bench/task.json'
    config.parent.mkdir(parents=True)
    config.write_text('{"id": "task", "instruction": "play"}')
    args = SimpleNamespace(result_dir=str(tmp_path / 'results'), action_space='computer_13',
                           observation_type='screenshot', model='test', agent_variant='agent4',
                           test_config_base_dir=str(tmp_path / 'config'))
    monkeypatch.setattr(auditor, 'judge_settings', lambda: ('judge', 'key', 'https://example', 'rules'))
    successful_usage = {'input_tokens': 100, 'cached_input_tokens': 0, 'output_tokens': 20}
    verdict = {'label': 'NOT_CHEAT', 'confidence': 0.9, 'evidence': [], 'reasoning': 'ok',
               'usage': successful_usage, 'calls': 1}
    judge = Mock(side_effect=[auditor.AuditError('judge offline'),
                             auditor.AuditError('still offline'), verdict])
    monkeypatch.setattr(auditor, 'call_judge', judge)
    original_import = builtins.__import__
    vm = Mock(side_effect=AssertionError('audit-only resume must not load the VM backend'))

    def checked_import(name, *args, **kwargs):
        if name == 'desktop_env.desktop_env':
            return vm()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', checked_import)
    agent = Mock(side_effect=AssertionError('audit-only resume must not create a gameplay agent'))
    monkeypatch.setattr('mm_agents.realtime_agent.RealtimeAgent', agent)
    scores = []

    for attempt in range(3):
        unfinished = runner['get_unfinished'](
            args.action_space, args.model, args.observation_type, args.result_dir,
            {'realtime_gui_bench': ['task']}, args.agent_variant)
        assert unfinished == {'realtime_gui_bench': ['task']}
        queue = SimpleNamespace(get=Mock(side_effect=[('realtime_gui_bench', 'task'), RuntimeError('empty')]))
        runner['run_env_tasks'](queue, args, scores)
        if attempt < 2:
            assert not (task / 'result.txt').exists()
            assert json.loads((task / 'result.json').read_text())['audit_status'] == 'pending'
        for name, data in evidence.items():
            assert (task / name).read_bytes() == data

    vm.assert_not_called()
    agent.assert_not_called()
    assert scores == [score]
    assert float((task / 'result.txt').read_text()) == score
    assert json.loads((task / 'game_result.json').read_text())['result'] == score
    saved_judge = json.loads((task / 'result.json').read_text())['judge']
    assert saved_judge['usage'] == successful_usage
    assert saved_judge['calls'] == 1  # only the successful audit, not prior failures
    assert len({call.args[0] for call in judge.call_args_list}) == 1  # same exact audit input
    assert runner['get_unfinished'](
        args.action_space, args.model, args.observation_type, args.result_dir,
        {'realtime_gui_bench': ['task']}, args.agent_variant) == {'realtime_gui_bench': []}
    summary = json.loads((Path(args.result_dir) / 'summary/results.json').read_text())
    assert len(summary) == 1 and summary[0]['score'] == score


def test_direct_runner_resumes_before_touching_the_environment(tmp_path, monkeypatch):
    task = tmp_path / 'task'
    completed_game(task, 0.0)
    audit = Mock(side_effect=auditor.AuditError('judge offline'))
    monkeypatch.setattr(auditor, 'audit_task', audit)
    # None would fail immediately if the runner reset the agent or environment.
    run_realtime_example(None, None, {}, 100, 'play', None, task, [])
    run_realtime_example(None, None, {}, 100, 'play', None, task, [])
    assert audit.call_count == 2
    assert not (task / 'result.txt').exists()
    assert json.loads((task / 'game_result.json').read_text())['result'] == 0.0


def test_cheat_verdict_keeps_the_original_score_for_explicit_reaudit(tmp_path, monkeypatch):
    task = tmp_path / 'task'
    completed_game(task, 1.0)
    audit = Mock(side_effect=[(0.0, {'label': 'CHEAT', 'confidence': 0.9}),
                              auditor.AuditError('offline'),
                              (1.0, {'label': 'NOT_CHEAT', 'confidence': 0.9})])
    monkeypatch.setattr(auditor, 'audit_task', audit)
    assert auditor.audit_one(task).startswith('CHEAT ')
    original = (task / 'game_result.json').read_bytes()
    judged = (task / 'result.json').read_bytes()
    assert auditor.audit_one(task).startswith('skipped: already judged')
    assert auditor.audit_one(task, force=True).startswith('audit failed:')
    assert (task / 'result.json').read_bytes() == judged
    assert (task / 'result.txt').read_text() == '0.0\n'
    assert auditor.audit_one(task, force=True).startswith('NOT_CHEAT ')
    assert (task / 'game_result.json').read_bytes() == original
    assert [call.args[1] for call in audit.call_args_list] == [1.0, 1.0, 1.0]


def test_saved_verdict_recovers_missing_scalar_without_another_judge_call(tmp_path, monkeypatch):
    task = tmp_path / 'task'
    completed_game(task, 1.0)
    auditor.save_game_result(task, 1.0)
    (task / 'result.json').write_text(json.dumps({'result': 0.0, 'judge': {'label': 'CHEAT', 'confidence': 0.9}}))
    audit = Mock(side_effect=AssertionError('already judged'))
    monkeypatch.setattr(auditor, 'audit_task', audit)
    assert auditor.audit_one(task).startswith('CHEAT ')
    assert (task / 'result.txt').read_text() == '0.0\n'
    audit.assert_not_called()


def test_invalid_saved_result_does_not_allow_replay_or_evidence_deletion(tmp_path, monkeypatch, runner):
    task = tmp_path / 'computer_13/screenshot/realtime_gui_bench/task'
    completed_game(task, 0.0)
    (task / 'result.json').write_text('{broken')
    runner['get_unfinished']('computer_13', 'test', 'screenshot', str(tmp_path),
                             {'realtime_gui_bench': ['task']}, 'agent4')
    with pytest.raises(auditor.AuditError, match='cannot read completed game'):
        run_realtime_example(None, None, {}, 100, 'play', None, task, [])
    assert (task / 'result.json').read_text() == '{broken'
    assert (task / 'recording.mp4').read_bytes() == b'original recording'


def test_auditor_cli_reports_pending_failures(tmp_path, monkeypatch):
    task = tmp_path / 'task'
    completed_game(task, 0.0)
    monkeypatch.setattr(auditor, 'judge_settings', lambda: ('judge', 'key', 'https://example', 'rules'))
    monkeypatch.setattr(auditor, 'audit_task', Mock(side_effect=auditor.AuditError('offline')))
    assert auditor.main([str(task)]) == 1
    assert not (task / 'result.txt').exists()
