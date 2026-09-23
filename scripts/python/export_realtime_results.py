#!/usr/bin/env python3
"""Export realtime results as the rows the Feishu result table expects.

One row per (model, task). Columns, in table order:

    benchmark_id, agent, model, effort, step, 模型请求数, 执行动作数, 工具调用数,
    result, attempts_completed, pass@1, pass@3, 结束状态, 成本, 输入Token数量, 输出Token数量,
    判官结论, 判官成本

Values come from the run directory itself: ``result.json`` for the score and the
judge verdict, ``agent_metrics.json`` for the counters, ``trajectory.jsonl`` for
tool calls and token usage, and the Agent YAML for ``effort``. ``判官成本`` is the
judge's own tokens priced with the judge model's row in the price table; ``成本``
is still the tested model's, so the two are separate lines of the bill.

Examples:

    # CSV/JSON next to the results, no cost column filled in
    python scripts/python/export_realtime_results.py --result_dir results_realtime_batches

    # cost from the token usage recorded in every trajectory
    python scripts/python/export_realtime_results.py --prices prices.json

    # cost from the runner's per-task attribution (exact, one amount per task)
    python scripts/python/export_realtime_results.py \
        --charges <cost_dir>/<model>_per_task_charge.json

    # push the rows into the Feishu table (one lark-cli call per 200 rows).
    # Two tables live in one base and share the same 18 columns, so only the
    # table id changes:
    #   tblCmj1K4kAqlmWb  新版(v1.1(5)) — current v1.1(5) scores
    #   tblhBdTpZMEqX5Qh  旧版(v1.1(3)) — historical v1.1(3) scores, read-only
    python scripts/python/export_realtime_results.py \
        --lark-base-token DVwrbns4LaLi8oswq9XcTdlHnGf --lark-table-id tblCmj1K4kAqlmWb

Add ``--lark-dry-run`` to print the request without sending it. Rows are never
rewritten: re-importing a task that is already in the table creates a second
record, so import a finished run once (the design keeps one row per
task+agent+model+effort combination).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

TASK_DIR = Path('evaluation_examples/examples/realtime_gui_bench')
COLUMNS = ['benchmark_id', 'agent', 'model', 'effort', 'step', '模型请求数', '执行动作数',
           '工具调用数', 'result', 'attempts_completed', 'pass@1', 'pass@3', '结束状态',
           '成本', '输入Token数量', '输出Token数量', '判官结论', '判官成本']
TERMINATION_LABELS = {
    'done': '正常结束',
    'decision_limit': '回合上限',
    'execution_error': '执行异常',
    'run_error': '其他运行异常',
    'interrupted': '中断',
}
AGENT_IDS = {'agent1': 'vanilla', 'agent2': 'anticipatory', 'agent3': 'video', 'agent4': 'combine'}


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--result_dir', default=os.environ.get('REALTIME_RESULT_DIR')
                        or 'results_realtime_batches')
    parser.add_argument('--run_id', default=None, help='only this run id')
    parser.add_argument('--models', default=None, help='comma-separated subset of models')
    parser.add_argument('--out', default=None,
                        help='output basename; writes <out>.csv and <out>.json '
                             '(default: <result_dir>/export_<run_id or all>)')
    parser.add_argument('--prices', default=None,
                        help='JSON price table: {"<model>": {"input_per_mtok": 3, '
                             '"output_per_mtok": 15, "cached_input_per_mtok": 0.3}, "*": {...}}')
    parser.add_argument('--charges', default=None,
                        help='JSON mapping model -> USD, a per-task attribution file '
                             '({"per_task": {task_id: usd}}, written by run_realtime_batch.py), '
                             'or a cost_report.json; takes precedence over --prices')
    parser.add_argument('--lark-base-token', default=None)
    parser.add_argument('--lark-table-id', default=None)
    parser.add_argument('--lark-dry-run', action='store_true',
                        help='print the lark-cli payload instead of writing records')
    return parser.parse_args()


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SystemExit(f'cannot read {path}: {exc}')


def load_benchmark_ids(root=TASK_DIR):
    """task uuid -> benchmark id, for tasks that never produced a result.json."""
    mapping = {}
    directory = Path(root)
    if not directory.exists():
        directory = REPO / root
    for path in sorted(directory.glob('*.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get('benchmark_id'):
            mapping[payload.get('id') or path.stem] = payload['benchmark_id']
    return mapping


def load_prices(path):
    if not path:
        return {}
    payload = load_json(path)
    if 'models' in payload and isinstance(payload['models'], dict):
        payload = payload['models']
    prices = {}
    for name, value in payload.items():
        if not isinstance(value, dict):
            continue
        prices[name] = {'input': float(value.get('input_per_mtok', 0)),
                        'cached_input': float(value.get('cached_input_per_mtok',
                                                       value.get('input_per_mtok', 0))),
                        'output': float(value.get('output_per_mtok', 0))}
    return prices


def load_charges(path):
    """Return ``(per_model_usd, per_task_usd)`` from any supported charges file.

    A per-task attribution file (``{"per_task": {task_id: usd}}``, written by
    ``run_realtime_batch.py``) fills every row with its own amount. A runner
    ``cost_report.json`` only knows one total per model, which cannot be split
    over a per-task table; that is reported instead of duplicated onto rows.
    """
    if not path:
        return {}, {}
    payload = load_json(path)
    if not isinstance(payload, dict):
        return {}, {}
    if isinstance(payload.get('per_task'), dict):
        return {}, {str(key): float(value) for key, value in payload['per_task'].items()}
    if isinstance(payload.get('models'), list):
        charges = {}
        for row in payload['models']:
            for field in ('gateway_log_charge_usd', 'actual_charge_usd', 'computed_charge_usd'):
                if row.get(field) is not None:
                    charges[row.get('model')] = float(row[field])
                    break
        return charges, {}
    return {name: float(value) for name, value in payload.items()}, {}


def effort_for(agent_variant, model):
    try:
        from mm_agents.realtime_config import default_config_path, load_realtime_config
        agent_id = AGENT_IDS.get(agent_variant, agent_variant)
        config = load_realtime_config(default_config_path(agent_variant, model), variant=agent_variant)
        return str(config['api']['thinking'].get('effort') or ''), config['agent_id']
    except Exception:
        return '', AGENT_IDS.get(agent_variant, agent_variant)


def summarize_trajectory(task_dir):
    """Tool calls and token usage recorded in the run's trajectory."""
    calls = input_tokens = output_tokens = cached_input = 0
    trajectory = task_dir / 'trajectory.jsonl'
    if trajectory.exists():
        for line in trajectory.read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('event') != 'model_response':
                continue
            raw_calls = event.get('calls')
            if isinstance(raw_calls, list):
                calls += len(raw_calls)
            usage = event.get('usage') or event.get('provider_response', {}).get('usage') or {}
            prompt = usage.get('prompt_tokens')
            completion = usage.get('completion_tokens')
            details = usage.get('prompt_tokens_details') or {}
            input_tokens += int(prompt if prompt is not None else usage.get('input_tokens') or 0)
            output_tokens += int(completion if completion is not None
                                 else usage.get('output_tokens') or 0)
            cached_input += int(details.get('cached_tokens') or 0)
    return {'tool_calls': calls, 'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'cached_input_tokens': cached_input}


def termination_label(metrics, scored_payload):
    """Map termination_reason to the table's fixed vocabulary."""
    reason = None
    if scored_payload:
        reason = scored_payload.get('termination_reason')
    if not reason:
        reason = (metrics or {}).get('termination_reason')
    if not reason:
        if metrics is None and scored_payload is None:
            return None
        if metrics and metrics.get('done') is True:
            return TERMINATION_LABELS['done']
        return None
    return TERMINATION_LABELS.get(reason, reason)


def price_of(prices, model):
    return prices.get(model) or prices.get('*')


def compute_charge(tokens, price):
    if not price:
        return None
    cached = min(tokens['cached_input_tokens'], tokens['input_tokens'])
    fresh = max(tokens['input_tokens'] - cached, 0)
    return round((fresh * price['input'] + cached * price['cached_input']
                  + tokens['output_tokens'] * price['output']) / 1_000_000, 6)


def collect_row(task_dir, model, agent_variant, prices, charges, benchmark_ids, per_task=None):
    metrics = None
    metrics_path = task_dir / 'agent_metrics.json'
    if metrics_path.exists():
        metrics = load_json(metrics_path)
    scored = None
    result_path = task_dir / 'result.json'
    if result_path.exists():
        scored = load_json(result_path)
    tokens = summarize_trajectory(task_dir)
    effort, _ = effort_for(agent_variant, model)

    score = (scored or {}).get('result')
    if score is None:
        result_cell = ''
    else:
        result_cell = 'yes' if float(score) == 1 else 'no'
    charge = (per_task or {}).get(task_dir.name)
    source = 'per_task' if charge is not None else None
    if charge is None:
        charge = charges.get(model)
        source = 'model' if charge is not None else None
    if charge is None:
        charge = compute_charge(tokens, price_of(prices, model))
        source = 'prices' if charge is not None else None

    judge = (scored or {}).get('judge') or {}
    judge_usage = judge.get('usage') or {}
    judge_charge = compute_charge(judge_usage, price_of(prices, judge.get('model'))) \
        if judge_usage else None

    row = {
        'benchmark_id': (scored or {}).get('benchmark_id') or benchmark_ids.get(task_dir.name),
        'agent': agent_variant,
        'model': model,
        'effort': effort,
        'step': (metrics or {}).get('action_decisions'),
        '模型请求数': (metrics or {}).get('model_requests'),
        '执行动作数': (metrics or {}).get('executed_actions'),
        '工具调用数': tokens['tool_calls'],
        'result': result_cell,
        'attempts_completed': (scored or {}).get('attempts_completed'),
        'pass@1': (scored or {}).get('pass_at_1'),
        'pass@3': (scored or {}).get('pass_at_3'),
        '结束状态': termination_label(metrics, scored),
        '成本': None if charge is None else f'{charge:.6f}',
        '输入Token数量': str(tokens['input_tokens']) if tokens['input_tokens'] else None,
        '输出Token数量': str(tokens['output_tokens']) if tokens['output_tokens'] else None,
        '判官结论': judge.get('label') or ('ERROR' if judge.get('error') else None),
        '判官成本': None if judge_charge is None else f'{judge_charge:.6f}',
        'task_id': task_dir.name,
        'task_dir': str(task_dir),
        '_charge_source': source,
    }
    return row


def iter_task_dirs(result_dir, models=None, run_id=None):
    """Yield (model, run_id, agent_variant, task_dir) in a stable order."""
    root = Path(result_dir)
    if not root.is_absolute():
        root = REPO / root
    if not root.exists():
        raise SystemExit(f'result dir not found: {root}')
    for task_dir in sorted(root.glob('*/*/*/*/*/*/*')):
        parts = task_dir.relative_to(root).parts
        if len(parts) != 7 or not task_dir.is_dir():
            continue
        model, run, agent_variant = parts[0], parts[1], parts[2]
        if models and model not in models:
            continue
        if run_id and run != run_id:
            continue
        if not any((task_dir / name).exists()
                   for name in ('result.json', 'agent_metrics.json', 'trajectory.jsonl')):
            continue
        yield model, run, agent_variant, task_dir


def lark_cell(row):
    """Table field map for one row; blank values are omitted, not sent."""
    cell = {}
    for column in COLUMNS:
        value = row.get(column)
        if value is None or value == '':
            continue
        if column in {'agent', 'result', '结束状态'}:
            cell[column] = [value]
        elif column == 'benchmark_id':
            cell[column] = str(value)
        else:
            cell[column] = value
    return cell


def push_to_lark(rows, base_token, table_id, dry_run=False):
    payloads = [lark_cell(row) for row in rows]
    payloads = [cell for cell in payloads if cell]
    written = 0
    for start in range(0, len(payloads), 200):
        chunk = payloads[start:start + 200]
        command = ['lark-cli', 'base', '+record-batch-create',
                   '--base-token', base_token, '--table-id', table_id,
                   '--json', json.dumps({'create_records': chunk}, ensure_ascii=False)]
        if dry_run:
            print(' '.join(command))
            continue
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0 or '"ok": false' in completed.stdout:
            raise SystemExit(f'lark-cli failed for rows {start}-{start + len(chunk)}:\n'
                             f'{completed.stdout[:800]}\n{completed.stderr[:400]}')
        written += len(chunk)
        print(f'LARK_WRITTEN {written}/{len(payloads)}', flush=True)
    return written


def main():
    args = parse_args()
    models = {name.strip() for name in args.models.split(',')} if args.models else None
    prices = load_prices(args.prices)
    charges, per_task = load_charges(args.charges)
    benchmark_ids = load_benchmark_ids()

    rows = []
    for model, run, agent_variant, task_dir in iter_task_dirs(args.result_dir, models, args.run_id):
        row = collect_row(task_dir, model, agent_variant, prices, charges, benchmark_ids, per_task)
        row['run_id'] = run
        rows.append(row)
    rows.sort(key=lambda row: (row['model'], row['benchmark_id'] or '', row['task_id']))
    if not rows:
        raise SystemExit('no task directories found; check --result_dir/--run_id/--models')

    # One model total cannot be split over that model's tasks, so the column stays
    # empty and the model is reported, instead of the same number on every row.
    rows_per_model = {}
    for row in rows:
        rows_per_model[row['model']] = rows_per_model.get(row['model'], 0) + 1
    total_only = set()
    for row in rows:
        if row.pop('_charge_source', None) == 'model' and rows_per_model[row['model']] > 1:
            row['成本'] = None
            total_only.add(row['model'])
    if total_only:
        print('CHARGES_TOTAL_ONLY ' + ','.join(sorted(total_only)) +
              ': the charges file only has a model total, which cannot be split per task; '
              'the cost column is left empty (pass a per-task charges file or --prices)')

    for row in rows:
        row.pop('task_dir', None)
    base = args.out or str(Path(args.result_dir) / f"export_{args.run_id or 'all'}")
    csv_path = Path(base + '.csv')
    json_path = Path(base + '.json')
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    scored = [row for row in rows if row['result']]
    costs = [float(row['成本']) for row in rows if row['成本']]
    judge_costs = [float(row['判官成本']) for row in rows if row['判官成本']]
    models_seen = sorted({row['model'] for row in rows})
    print(f'EXPORTED {len(rows)} rows for {len(models_seen)} model(s) -> {csv_path}')
    print(f'scored={len(scored)} unscored={len(rows) - len(scored)} '
          f'cost_filled={len(costs)}' + (f' cost_total={sum(costs):.6f}' if costs else ''))
    if judge_costs:
        print(f'judge_cost_filled={len(judge_costs)} judge_cost_total={sum(judge_costs):.6f}')
    if not costs:
        print('cost column empty: pass --charges <cost_report.json> or --prices <prices.json>')

    if args.lark_base_token and args.lark_table_id:
        written = push_to_lark(rows, args.lark_base_token, args.lark_table_id, args.lark_dry_run)
        print('LARK_DRY_RUN' if args.lark_dry_run else f'LARK_IMPORTED {written} rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
