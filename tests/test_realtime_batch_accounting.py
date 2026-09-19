"""Key resolution, gateway selection and cost math of the batch runner."""
import argparse
import datetime
import json
from pathlib import Path

import pytest

from scripts.python.run_realtime_batch import (DEFAULT_PACKY_BASE_URL, build_report, charge_usd,
                                               charged_usd, is_packy, keys_from_environment,
                                               keys_from_payload, load_prices, price_for,
                                               resolve_gateway, resolve_keys, usage_tokens)


def args(**overrides):
    base = {'api_base_url': None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_keys_prefer_the_models_own_variable_then_the_catch_all():
    env = {'PACKY_KIMI_API_KEY': 'kimi-key', 'REALTIME_API_KEY': 'shared'}

    found = keys_from_environment(['kimi-k3', 'glm-5.3-flash'], {
        'kimi-k3': 'PACKY_KIMI_API_KEY', 'glm-5.3-flash': 'PACKY_GLM_MINIMAX_API_KEY'}, env)

    assert found == {'kimi-k3': 'kimi-key', 'glm-5.3-flash': 'shared'}


def test_environment_keys_need_no_prompt(tmp_path):
    env = {'REALTIME_API_KEY': 'shared'}
    key_envs = {model: 'MISSING_VAR' for model in ('a', 'b')}

    keys, sources = resolve_keys(['a', 'b'], key_envs, None, environ=env)

    assert keys == {'a': 'shared', 'b': 'shared'}
    assert set(sources.values()) == {'environment'}


def test_only_missing_models_come_from_the_keys_file(tmp_path):
    env = {'PACKY_COMMON_API_KEY': 'from-env'}
    keys_file = tmp_path / 'keys.json'
    keys_file.write_text(json.dumps({'b': 'from-file'}), encoding='utf-8')

    keys, sources = resolve_keys(['a', 'b'], {'a': 'PACKY_COMMON_API_KEY', 'b': 'OTHER'},
                                 str(keys_file), environ=env)

    assert keys == {'a': 'from-env', 'b': 'from-file'}
    assert sources['a'] == 'environment' and sources['b'].endswith('keys.json')


def test_keys_file_wildcard_fills_every_missing_model(tmp_path):
    keys_file = tmp_path / 'keys.json'
    keys_file.write_text(json.dumps({'*': 'shared'}), encoding='utf-8')

    keys, _ = resolve_keys(['a', 'b'], {'a': 'X', 'b': 'Y'}, str(keys_file), environ={})

    assert keys == {'a': 'shared', 'b': 'shared'}


def test_a_still_missing_key_is_a_clear_error(tmp_path):
    keys_file = tmp_path / 'keys.json'
    keys_file.write_text(json.dumps({'a': 'one'}), encoding='utf-8')

    with pytest.raises(SystemExit, match='missing keys for: b'):
        resolve_keys(['a', 'b'], {'a': 'X', 'b': 'Y'}, str(keys_file), environ={})


def test_keys_from_payload_requires_every_model():
    assert keys_from_payload({'a': 'one', 'b': 'two'}, ['a', 'b']) == {'a': 'one', 'b': 'two'}


@pytest.mark.parametrize('host,expected', [
    ('https://www.packyapi.ai', True),
    ('https://packyapi.ai', True),
    ('https://gateway.example.com/v1', False),
    (None, False),
])
def test_is_packy(host, expected):
    assert is_packy(host) is expected


def test_gateway_resolution_order(monkeypatch):
    for name in ('REALTIME_API_BASE_URL', 'PACKY_API_BASE_URL', 'ANTHROPIC_BASE_URL',
                 'OPENAI_BASE_URL'):
        monkeypatch.delenv(name, raising=False)

    assert resolve_gateway(args())[0] == DEFAULT_PACKY_BASE_URL

    monkeypatch.setenv('OPENAI_BASE_URL', 'https://company.example.com/v1')
    url, source = resolve_gateway(args())
    assert url is None and 'OPENAI_BASE_URL' in source

    # A single protocol variable is only reused for every model when it is Packy.
    monkeypatch.delenv('OPENAI_BASE_URL')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://www.packyapi.ai')
    assert resolve_gateway(args()) == ('https://www.packyapi.ai', '$ANTHROPIC_BASE_URL')

    monkeypatch.setenv('OPENAI_BASE_URL', 'https://company.example.com/v1')
    url, source = resolve_gateway(args())
    assert url is None and 'per-protocol' in source

    monkeypatch.setenv('PACKY_API_BASE_URL', 'https://pkg.example.com')
    assert resolve_gateway(args()) == ('https://pkg.example.com', 'PACKY_API_BASE_URL')

    monkeypatch.setenv('REALTIME_API_BASE_URL', 'https://rt.example.com')
    assert resolve_gateway(args()) == ('https://rt.example.com', 'REALTIME_API_BASE_URL')

    assert resolve_gateway(args(api_base_url='https://flag.example.com'))[1] == '--api_base_url'


def test_usage_tokens_accepts_both_wire_formats():
    assert usage_tokens({'prompt_tokens': 10, 'completion_tokens': 4,
                         'prompt_tokens_details': {'cached_tokens': 3}}) == {
        'input': 10, 'output': 4, 'cached_input': 3}
    assert usage_tokens({'input_tokens': 7, 'output_tokens': 2}) == {
        'input': 7, 'output': 2, 'cached_input': 0}
    assert usage_tokens(None) == {'input': 0, 'output': 0, 'cached_input': 0}


def test_price_table_and_charge(tmp_path):
    path = tmp_path / 'prices.json'
    path.write_text(json.dumps({'deepseek-flash': {'input_per_mtok': 3, 'output_per_mtok': 15},
                                '*': {'input_per_mtok': 1, 'output_per_mtok': 2}}), encoding='utf-8')
    prices = load_prices(str(path))

    assert price_for(prices, 'deepseek-flash') == {'input': 3.0, 'cached_input': 3.0, 'output': 15.0}
    assert price_for(prices, 'unknown-model')['output'] == 2.0
    assert price_for({}, 'anything') is None
    assert charge_usd({'input_tokens': 1_000_000, 'output_tokens': 1_000_000,
                       'cached_input_tokens': 0}, price_for(prices, 'deepseek-flash')) == 18.0
    assert charge_usd({'input_tokens': 0, 'output_tokens': 0, 'cached_input_tokens': 0},
                      None) is None


def test_price_table_rejects_entries_without_an_output_rate(tmp_path):
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps({'m': {'input_per_mtok': 1}}), encoding='utf-8')

    with pytest.raises(SystemExit):
        load_prices(str(path))


def test_charge_prefers_gateway_over_computed():
    assert charged_usd({'gateway_log_charge_usd': 1.0, 'computed_charge_usd': 2.0}) == 1.0
    assert charged_usd({'actual_charge_usd': 3.0, 'computed_charge_usd': 2.0}) == 3.0
    assert charged_usd({'computed_charge_usd': 2.0}) == 2.0
    assert charged_usd({}) is None


def test_report_totals_survive_a_resume(tmp_path):
    cost_dir = tmp_path / 'cost'
    cost_dir.mkdir()
    (cost_dir / 'a_summary.json').write_text(json.dumps(
        {'model': 'a', 'gateway_log_charge_usd': 1.5}), encoding='utf-8')
    (cost_dir / 'b_summary.json').write_text(json.dumps(
        {'model': 'b', 'computed_charge_usd': 0.5}), encoding='utf-8')

    report = build_report(cost_dir, {'models': ['a', 'b'], 'run_id': 'r'})

    assert [row['model'] for row in report['models']] == ['a', 'b']
    assert report['total_charge_usd'] == 2.0
    assert report['run_id'] == 'r' and 'runs' not in report


def test_model_row_aggregates_every_task():
    """The model row must sum counters and average pass rates, not copy one task."""
    from scripts.python.run_realtime_batch import SUMMARY_FIELDS, aggregate_model_row

    summaries = [
        {'task_dir': 'a', 'requests': 10, 'responses': 9, 'decisions': 4, 'frame_queries': 1,
         'pass_at_1': 1.0, 'pass_at_3': 1.0, 'status': 'passed', 'input_tokens': 100, 'output_tokens': 5},
        {'task_dir': 'b', 'requests': 20, 'responses': 20, 'decisions': 6, 'frame_queries': 0,
         'pass_at_1': 0.0, 'pass_at_3': 1.0, 'status': 'passed', 'input_tokens': 300, 'output_tokens': 7},
        {'task_dir': 'c', 'requests': 5, 'responses': 5, 'decisions': 2, 'frame_queries': 3,
         'pass_at_1': None, 'pass_at_3': None, 'status': None, 'input_tokens': 50, 'output_tokens': 1},
    ]
    row = aggregate_model_row(summaries, model='m', key_env='K', run_id='r',
                              return_code=0, elapsed_s=12.5)
    assert (row['requests'], row['responses'], row['decisions'], row['frame_queries']) == (35, 34, 12, 4)
    assert row['tasks_total'] == 3 and row['tasks_scored'] == 2
    assert row['pass_at_1'] == round(1 / 3, 4)
    assert row['pass_at_3'] == row['pass_at_3_mean'] == round(2 / 3, 4)
    assert (row['input_tokens'], row['output_tokens']) == (450, 13)
    assert row['status'] is None
    for field in ('tasks_total', 'tasks_scored', 'pass_at_3_mean'):
        assert field in SUMMARY_FIELDS


def test_subset_rerun_merges_with_previous_task_rows():
    """A --task/--meta re-run keeps the tasks it did not touch."""
    from scripts.python.run_realtime_batch import merge_task_rows

    previous = [{'task_dir': 'a', 'requests': 1}, {'task_dir': 'b', 'requests': 2}]
    current = [{'task_dir': 'b', 'requests': 99}, {'task_dir': 'c', 'requests': 3}]
    merged = merge_task_rows(previous, current)
    assert [row['task_dir'] for row in merged] == ['a', 'b', 'c']
    assert {row['task_dir']: row['requests'] for row in merged} == {'a': 1, 'b': 99, 'c': 3}


def test_charges_accumulate_across_invocations(tmp_path):
    """A subset re-run must add to the run's charge, not replace it."""
    from scripts.python.run_realtime_batch import append_charge_record

    ledger, totals = append_charge_record(
        tmp_path, 'm', {'tasks_run': 69, 'actual_charge_usd': 34.6097,
                        'gateway_log_charge_usd': 34.6097, 'gateway_charge_count': 704})
    assert len(ledger) == 1 and totals['actual_charge_usd'] == 34.6097

    ledger, totals = append_charge_record(
        tmp_path, 'm', {'tasks_run': 11, 'actual_charge_usd': 8.807698,
                        'gateway_log_charge_usd': 8.807698, 'gateway_charge_count': 118,
                        'billing_error': None})
    assert len(ledger) == 2
    assert totals['actual_charge_usd'] == round(34.6097 + 8.807698, 6)
    assert totals['gateway_log_charge_usd'] == round(34.6097 + 8.807698, 6)
    assert totals['gateway_charge_count'] == 822

    # A failed window keeps its record; totals only add what was measured.
    ledger, totals = append_charge_record(
        tmp_path, 'm', {'tasks_run': 1, 'actual_charge_usd': None,
                        'gateway_log_charge_usd': None, 'gateway_charge_count': None,
                        'billing_error': 'gateway request failed'})
    assert len(ledger) == 3
    assert totals['actual_charge_usd'] == round(34.6097 + 8.807698, 6)
    assert totals['gateway_charge_count'] == 822
    again = json.loads((tmp_path / 'm_charges.json').read_text())
    assert [row['tasks_run'] for row in again['charges']] == [69, 11, 1]


def ledger_row(created_at, prompt_tokens, completion_tokens, quota=500000):
    """One gateway ledger row; the default quota is exactly one USD."""
    return {'created_at': created_at, 'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens, 'quota': quota}


def test_task_request_records_skip_responses_without_usage(tmp_path):
    from scripts.python.run_realtime_batch import task_request_records

    (tmp_path / 'trajectory.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in [
        {'event': 'model_request', 'wall_time': '2026-09-19T04:00:00+00:00'},
        {'event': 'model_response', 'wall_time': '2026-09-19T04:00:05+00:00',
         'usage': {'prompt_tokens': 6779, 'completion_tokens': 256}},
        {'event': 'model_response', 'wall_time': '2026-09-19T04:00:20+00:00'},
    ]), encoding='utf-8')

    records = task_request_records(tmp_path)

    assert len(records) == 1
    assert records[0][1:] == (6779, 256)


def test_concurrent_tasks_are_separated_request_by_request():
    from scripts.python.run_realtime_batch import attribute_gateway_charge

    tasks = {'task-a': [(1000.0, 100, 10), (1002.0, 200, 20)],
             'task-b': [(1001.0, 150, 15), (1003.0, 250, 25)]}
    rows = [ledger_row(*fields) for fields in
            [(1003, 250, 25), (1000, 100, 10), (1002, 200, 20), (1001, 150, 15)]]

    per_task, stats = attribute_gateway_charge(tasks, rows)

    assert per_task == {'task-a': 2.0, 'task-b': 2.0}
    assert stats == {'ledger_rows': 4, 'matched_rows': 4, 'unmatched_rows': 0,
                     'matched_usd': 4.0, 'unmatched_usd': 0.0}


def test_a_row_that_matches_nothing_stays_unattributed():
    from scripts.python.run_realtime_batch import attribute_gateway_charge

    per_task, stats = attribute_gateway_charge(
        {'task-a': [(1000.0, 100, 10)]}, [ledger_row(1000, 100, 10), ledger_row(1000, 999, 999)])

    assert per_task == {'task-a': 1.0}
    assert stats['matched_rows'] == 1 and stats['unmatched_rows'] == 1
    assert stats['unmatched_usd'] == 1.0


def test_a_token_pair_far_away_in_time_is_not_claimed():
    from scripts.python.run_realtime_batch import attribute_gateway_charge

    per_task, stats = attribute_gateway_charge(
        {'task-a': [(1000.0, 100, 10)]}, [ledger_row(999999, 100, 10)], tolerance_s=900)

    assert per_task == {} and stats['unmatched_rows'] == 1


def test_per_task_charge_accumulates_across_invocations(tmp_path):
    from scripts.python.run_realtime_batch import append_per_task_charge, load_per_task_charge

    stats = {'ledger_rows': 2, 'matched_rows': 2, 'unmatched_rows': 0,
             'matched_usd': 0.5, 'unmatched_usd': 0.0}
    first = append_per_task_charge(tmp_path, 'm', {'task-a': 0.5}, stats,
                                   run_id='run1', tasks_run=69)
    assert first['per_task'] == {'task-a': 0.5} and first['total_usd'] == 0.5

    second = append_per_task_charge(tmp_path, 'm', {'task-a': 0.25, 'task-b': 0.75},
                                    {**stats, 'matched_usd': 1.0}, run_id='run1', tasks_run=11)
    assert second['per_task'] == {'task-a': 0.75, 'task-b': 0.75}
    assert second['total_usd'] == 1.5
    assert [row['tasks_run'] for row in second['windows']] == [69, 11]

    totals, windows = load_per_task_charge(tmp_path, 'm')
    assert totals == {'task-a': 0.75, 'task-b': 0.75} and len(windows) == 2


def test_record_per_task_charge_walks_from_result_dirs_to_the_file(tmp_path):
    """The run loop's whole path: task dirs -> trajectories -> attribution -> file."""
    from scripts.python.run_realtime_batch import record_per_task_charge

    tasks = tmp_path / 'tasks'
    for name, prompt, completion, when in (('task-a', 100, 10, 1000),
                                           ('task-b', 200, 20, 1001)):
        task_dir = tasks / name
        task_dir.mkdir(parents=True)
        (task_dir / 'trajectory.jsonl').write_text(json.dumps({
            'event': 'model_response',
            'wall_time': datetime.datetime.fromtimestamp(when, datetime.timezone.utc).isoformat(),
            'usage': {'prompt_tokens': prompt, 'completion_tokens': completion},
        }) + '\n', encoding='utf-8')
    rows = [ledger_row(1000, 100, 10), ledger_row(1001, 200, 20),
            ledger_row(1001, 999, 999)]

    payload, attribution = record_per_task_charge(
        tmp_path, 'm', [str(tasks / 'task-a'), str(tasks / 'task-b')], rows, run_id='run1')

    assert payload['per_task'] == {'task-a': 1.0, 'task-b': 1.0}
    assert payload['total_usd'] == 2.0
    assert payload['unattributed_usd'] == 1.0
    assert payload['windows'][0]['tasks_run'] == 2
    assert attribution['matched_rows'] == 2 and attribution['unmatched_rows'] == 1
    saved = json.loads((tmp_path / 'm_per_task_charge.json').read_text())
    assert saved['per_task'] == {'task-a': 1.0, 'task-b': 1.0}
