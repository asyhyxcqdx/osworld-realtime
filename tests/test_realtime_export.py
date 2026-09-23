import json
import sys

import pytest

from scripts.python.export_realtime_results import (COLUMNS, collect_row, compute_charge,
                                                    load_charges, load_prices, lark_cell, main,
                                                    termination_label)


def write_task(task_dir, *, scored=True, termination='done'):
    task_dir.mkdir(parents=True)
    (task_dir / 'agent_metrics.json').write_text(json.dumps({
        'model_requests': 5, 'executed_actions': 7, 'action_decisions': 3,
        'termination_reason': termination, 'done': termination == 'done',
        'execution_error': None if termination == 'done' else 'boom',
        'run_error': 'boom' if termination == 'run_error' else None,
    }), encoding='utf-8')
    if scored:
        (task_dir / 'result.json').write_text(json.dumps({
            'benchmark_id': 'C1', 'result': 1.0, 'pass_at_1': 0.0, 'pass_at_3': 1.0,
            'attempts_completed': 2, 'termination_reason': termination,
        }), encoding='utf-8')
        # result.txt is the scalar the design trusts; result.json on its own is a
        # diagnostic left behind by an audit that could not reach a verdict.
        (task_dir / 'result.txt').write_text('1.0\n', encoding='utf-8')
    events = [
        {'event': 'model_response', 'calls': [{'name': 'computer_left_click'}] * 4,
         'usage': {'prompt_tokens': 1000, 'completion_tokens': 100,
                   'prompt_tokens_details': {'cached_tokens': 200}}},
        {'event': 'model_response', 'calls': [{'name': 'computer_done'}] * 2,
         'usage': {'prompt_tokens': 500, 'completion_tokens': 50}},
    ]
    (task_dir / 'trajectory.jsonl').write_text(
        ''.join(json.dumps(event) + '\n' for event in events), encoding='utf-8')


def test_collect_row_maps_the_table_columns(tmp_path):
    task_dir = tmp_path / 'task'
    write_task(task_dir)

    row = collect_row(task_dir, 'deepseek-flash', 'agent4', {}, {}, {})

    assert list(COLUMNS) == ['benchmark_id', 'agent', 'model', 'effort', 'step', '模型请求数',
                             '执行动作数', '工具调用数', 'result', 'attempts_completed',
                             'pass@1', 'pass@3', '结束状态', '成本', '输入Token数量',
                             '输出Token数量', '判官结论', '判官成本']
    assert row['benchmark_id'] == 'C1'
    assert row['agent'] == 'agent4'
    assert row['model'] == 'deepseek-flash'
    assert row['effort'] == 'high'
    assert row['step'] == 3
    assert row['模型请求数'] == 5
    assert row['执行动作数'] == 7
    assert row['工具调用数'] == 6
    assert row['result'] == 'yes'
    assert row['attempts_completed'] == 2
    assert row['pass@1'] == 0.0 and row['pass@3'] == 1.0
    assert row['结束状态'] == '正常结束'
    assert row['成本'] is None
    assert row['输入Token数量'] == '1500'
    assert row['输出Token数量'] == '150'
    assert row['判官结论'] is None and row['判官成本'] is None


def test_judge_verdict_and_cost_come_from_the_result_block(tmp_path):
    task_dir = tmp_path / 'task'
    write_task(task_dir)
    payload = json.loads((task_dir / 'result.json').read_text(encoding='utf-8'))
    payload['judge'] = {
        'label': 'CHEAT', 'confidence': 0.97, 'model': 'deepseek-flash',
        'usage': {'input_tokens': 20_000, 'cached_input_tokens': 12_000, 'output_tokens': 500},
    }
    (task_dir / 'result.json').write_text(json.dumps(payload), encoding='utf-8')
    prices = {'deepseek-flash': {'input': 0.27, 'cached_input': 0.027, 'output': 1.1}}

    row = collect_row(task_dir, 'deepseek-flash', 'agent4', prices, {}, {})

    assert row['判官结论'] == 'CHEAT'
    # 8k fresh at 0.27 + 12k cached at 0.027 + 500 out at 1.1, per million tokens
    assert row['判官成本'] == '0.003034'


def test_a_failed_audit_shows_as_an_error_row_without_a_score(tmp_path):
    """A task whose audit never finished must not reach the sheet as a result.

    The runner deletes result.txt and keeps result.json (with judge.error) so the
    task gets re-run; the page's numbers are still in that file. The row keeps the
    ERROR verdict as a diagnostic, but its score and cost cells stay empty.
    """
    task_dir = tmp_path / 'task'
    write_task(task_dir, scored=False)
    (task_dir / 'result.json').write_text(json.dumps({
        'benchmark_id': 'C1', 'result': 1.0, 'pass_at_1': 0.0, 'pass_at_3': 1.0,
        'attempts_completed': 3, 'status': 'passed',
        'judge': {'error': 'judge reply is not valid JSON'}}), encoding='utf-8')

    row = collect_row(task_dir, 'deepseek-flash', 'agent4', {}, {}, {},
                      per_task={'task': 0.5})

    assert row['判官结论'] == 'ERROR' and row['判官成本'] is None
    assert row['result'] == '' and row['pass@1'] is None and row['pass@3'] is None
    assert row['attempts_completed'] is None and row['成本'] is None
    # 结束状态 describes the run, not the score, so it stays as a diagnostic.
    assert row['结束状态'] == '正常结束'


def test_unscored_task_keeps_scores_blank(tmp_path):
    task_dir = tmp_path / 'task'
    write_task(task_dir, scored=False, termination='run_error')

    row = collect_row(task_dir, 'deepseek-flash', 'agent4', {}, {}, {'task': 'A1'})

    assert row['result'] == ''
    assert row['pass@1'] is None and row['pass@3'] is None
    assert row['attempts_completed'] is None
    assert row['结束状态'] == '其他运行异常'
    assert row['benchmark_id'] == 'A1'


@pytest.mark.parametrize('reason,label', [
    ('done', '正常结束'), ('decision_limit', '回合上限'),
    ('execution_error', '执行异常'), ('run_error', '其他运行异常'), ('interrupted', '中断'),
])
def test_termination_labels_follow_the_documented_vocabulary(reason, label):
    assert termination_label({}, {'termination_reason': reason}) == label


def test_termination_falls_back_to_metrics_done_flag():
    assert termination_label({'done': True}, None) == '正常结束'
    assert termination_label(None, None) is None


def test_prices_and_charges_are_read_from_runner_artifacts(tmp_path):
    prices = load_prices(_write(tmp_path / 'prices.json', {
        'deepseek-flash': {'input_per_mtok': 1.0, 'output_per_mtok': 2.0},
        '*': {'input_per_mtok': 9.0, 'output_per_mtok': 9.0},
    }))
    assert prices['deepseek-flash'] == {'input': 1.0, 'cached_input': 1.0, 'output': 2.0}
    assert prices['*']['input'] == 9.0

    charges, per_task = load_charges(_write(tmp_path / 'cost_report.json', {
        'models': [{'model': 'deepseek-flash', 'gateway_log_charge_usd': 0.5},
                   {'model': 'glm-5.3-flash', 'actual_charge_usd': 0.25},
                   {'model': 'kimi-k3', 'computed_charge_usd': 1.5}],
    }))
    assert charges == {'deepseek-flash': 0.5, 'glm-5.3-flash': 0.25, 'kimi-k3': 1.5}
    assert per_task == {}

    # A per-task attribution file fills every row with its own amount.
    _, per_task = load_charges(_write(tmp_path / 'per_task.json', {
        'per_task': {'task-a': 0.5, 'task-b': 0.25}, 'total_usd': 0.75,
    }))
    assert per_task == {'task-a': 0.5, 'task-b': 0.25}


def test_compute_charge_prices_cached_tokens_separately():
    tokens = {'input_tokens': 1000, 'output_tokens': 100, 'cached_input_tokens': 400}
    price = {'input': 3.0, 'cached_input': 0.3, 'output': 15.0}

    # 600 fresh * 3 + 400 cached * 0.3 + 100 * 15, all per million tokens
    assert compute_charge(tokens, price) == pytest.approx(0.00342)
    assert compute_charge(tokens, None) is None


def test_lark_cell_omits_blanks_and_wraps_selects(tmp_path):
    task_dir = tmp_path / 'task'
    write_task(task_dir, scored=False)
    row = collect_row(task_dir, 'deepseek-flash', 'agent4', {}, {}, {})

    cell = lark_cell(row)

    assert cell['agent'] == ['agent4']
    assert 'result' not in cell and '成本' not in cell and 'pass@1' not in cell
    assert cell['结束状态'] == ['正常结束']
    assert cell['模型请求数'] == 5


def test_cli_writes_csv_and_json_for_a_run_tree(tmp_path, capsys):
    result_dir = tmp_path / 'results'
    write_task(result_dir / 'deepseek-flash' / 'run1' / 'agent4' / 'computer_13' /
               'screenshot' / 'realtime_gui_bench' / 'task-a')
    write_task(result_dir / 'deepseek-flash' / 'run1' / 'agent4' / 'computer_13' /
               'screenshot' / 'realtime_gui_bench' / 'task-b', scored=False,
               termination='execution_error')

    argv = ['export_realtime_results.py', '--result_dir', str(result_dir),
            '--out', str(tmp_path / 'out')]
    old_argv = sys.argv
    sys.argv = argv
    try:
        assert main() == 0
    finally:
        sys.argv = old_argv

    rows = json.loads((tmp_path / 'out.json').read_text(encoding='utf-8'))
    assert len(rows) == 2
    assert {row['结束状态'] for row in rows} == {'正常结束', '执行异常'}
    assert (tmp_path / 'out.csv').read_text(encoding='utf-8-sig').splitlines()[0].startswith(
        'benchmark_id,agent,model,effort,step,')
    assert 'EXPORTED 2 rows' in capsys.readouterr().out


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def _run_export(result_dir, out, extra=()):
    argv = ['export_realtime_results.py', '--result_dir', str(result_dir),
            '--out', str(out), *extra]
    old_argv = sys.argv
    sys.argv = argv
    try:
        return main()
    finally:
        sys.argv = old_argv


def test_a_model_total_is_not_copied_onto_every_row(tmp_path, capsys):
    result_dir = tmp_path / 'results'
    for name in ('task-a', 'task-b'):
        write_task(result_dir / 'deepseek-flash' / 'run1' / 'agent4' / 'computer_13' /
                   'screenshot' / 'realtime_gui_bench' / name)
    charges = _write(tmp_path / 'charges.json', {'deepseek-flash': 4.0})

    assert _run_export(result_dir, tmp_path / 'out', ['--charges', str(charges)]) == 0

    rows = json.loads((tmp_path / 'out.json').read_text(encoding='utf-8'))
    assert {row['成本'] for row in rows} == {None}
    assert '_charge_source' not in rows[0]
    assert 'CHARGES_TOTAL_ONLY deepseek-flash' in capsys.readouterr().out


def test_per_task_charges_fill_each_row_with_its_own_amount(tmp_path):
    result_dir = tmp_path / 'results'
    for name in ('task-a', 'task-b'):
        write_task(result_dir / 'deepseek-flash' / 'run1' / 'agent4' / 'computer_13' /
                   'screenshot' / 'realtime_gui_bench' / name)
    charges = _write(tmp_path / 'per_task.json',
                     {'per_task': {'task-a': 0.5, 'task-b': 0.25}})

    assert _run_export(result_dir, tmp_path / 'out', ['--charges', str(charges)]) == 0

    rows = json.loads((tmp_path / 'out.json').read_text(encoding='utf-8'))
    assert {row['task_id']: row['成本'] for row in rows} == {'task-a': '0.500000',
                                                             'task-b': '0.250000'}


def test_csv_keeps_the_eighteen_table_columns(tmp_path):
    result_dir = tmp_path / 'results'
    write_task(result_dir / 'deepseek-flash' / 'run1' / 'agent4' / 'computer_13' /
               'screenshot' / 'realtime_gui_bench' / 'task-a')

    assert _run_export(result_dir, tmp_path / 'out') == 0

    header = (tmp_path / 'out.csv').read_text(encoding='utf-8-sig').splitlines()[0]
    assert header.split(',') == list(COLUMNS)
    assert 'task_id' not in header and 'run_id' not in header
    rows = json.loads((tmp_path / 'out.json').read_text(encoding='utf-8'))
    assert rows[0]['task_id'] == 'task-a' and rows[0]['run_id'] == 'run1'
