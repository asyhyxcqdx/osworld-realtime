"""Store authoritative per-task outcomes without generating category summaries."""
import json

import pytest

from desktop_env.evaluators.getters.realtime_gui import _normalize_realtime_gui_bench_state
from desktop_env.evaluators.metrics.realtime_gui import realtime_gui_bench_result
from lib_run_single import _evaluate_with_details


@pytest.mark.parametrize('reason,status', [
    ('decision_limit', 'running'), ('decision_limit', 'ready'),
    ('done', 'running'), ('done', 'ready'), ('done', 'passed'),
    ('decision_limit', 'passed'),
])
def test_completed_run_persists_result_and_reason_without_summary(tmp_path, reason, status):
    task = tmp_path / 'realtime_gui_bench/c1'
    task.mkdir(parents=True)
    passed = status == 'passed'
    raw = dict(protocol_version='realtime-gui-bench/1.1', task='double_jump',
               max_attempts=3, attempts_completed=1 if passed else 0,
               passed=passed, status=status, pass_at_1=int(passed), pass_at_3=int(passed))

    class Env:
        def evaluate(self):
            self._evaluation_details = _normalize_realtime_gui_bench_state(raw, 'C1')
            return realtime_gui_bench_result(self._evaluation_details)

    env = Env()
    score = _evaluate_with_details(env, str(task), termination_reason=reason)
    stored = json.loads((task / 'result.json').read_text())
    assert score == int(passed)
    assert stored['status'] == status
    assert stored['raw_bench'] == raw
    assert 'run_status' not in stored
    assert stored['termination_reason'] == reason
    assert env._evaluation_details == stored
    assert not (tmp_path / 'summary').exists()
    assert {p.name for p in task.iterdir()} == {'result.json'}


@pytest.mark.parametrize('raw', [None, {}, {
    'protocol_version': 'realtime-gui-bench/1.1', 'task': 'double_jump',
    'max_attempts': 3, 'attempts_completed': 1, 'passed': True,
    'status': 'passed', 'pass_at_1': 1, 'pass_at_3': 0,
}])
def test_invalid_game_score_does_not_create_a_failure_result(tmp_path, raw):
    task = tmp_path / 'realtime_gui_bench/c1'
    task.mkdir(parents=True)
    class Env:
        def evaluate(self):
            self._evaluation_details = _normalize_realtime_gui_bench_state(raw, 'C1')
            return realtime_gui_bench_result(self._evaluation_details)

    with pytest.raises(ValueError):
        _evaluate_with_details(Env(), str(task), termination_reason='decision_limit')
    assert not (task / 'result.json').exists()
    assert not (tmp_path / 'summary').exists()
