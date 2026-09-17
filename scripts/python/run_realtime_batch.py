#!/usr/bin/env python3
"""Run a realtime GUI batch over models, recording per-model spend in USD.

One command drives every model of one agent variant, sequentially or with
parallel environments per model, and keeps the money record next to the run:

    python scripts/python/run_realtime_batch.py \
        --agent_variant agent4 \
        --run_id packy_v11_batch01 \
        --result_dir results_realtime_batches \
        --proxy http://127.0.0.1:7890 \
        --exclusive-keys-confirmed

Keys are read once and never written to disk. Two input styles:

    1) interactive TTY (no echo): the tool prints READY_KEYS_NO_ECHO and reads
       one JSON line: {"<model>": "sk-...", ...}
    2) --keys-file <path> with the same JSON, for scripted runs. A single key for
       every model is accepted as {"*": "sk-..."}.

Per-model results go to
    <result_dir>/<model>/<run_id>/<agent_variant>/<action_space>/<observation_type>/<domain>/<task>/
Billing snapshots, the gateway ledger, per-model summaries and cost_report.json
go to --cost_dir (default: <result_dir>/_cost/<run_id>, inside the git-ignored
results tree; pass an absolute path to keep them outside the repository).

Resume is built into the runner: rerun the same command with the same
--result_dir/--run_id and run_multienv skips every task that already has
result.txt and clears the ones that do not.

Accounting rules used by the earlier combine cost runs:
  * actual charge is the gateway ledger, filtered by exact model_name and by the
    before-run timestamp, converted with quota / 500000 = USD;
  * the instant usage endpoint is a cross-check only: it lags, and a zero delta
    is never reported as "free";
  * give every model its own key when possible; a shared key is still attributed
    correctly because models run one at a time.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import termios
import time

import requests

REPO = Path(__file__).resolve().parents[2]
QUOTA_PER_USD = 500000
VARIANT_TO_AGENT_ID = {'agent1': 'vanilla', 'agent2': 'anticipatory',
                       'agent3': 'video', 'agent4': 'combine'}
DEFAULT_MODELS = ['claude-sonnet-5', 'gpt-5.6-sol', 'gemini-3.8-flash',
                  'qwen3.8-max-0902', 'deepseek-flash', 'kimi-k3',
                  'glm-5.3-flash', 'MiniMax-M3']
SUMMARY_FIELDS = ('model', 'key_env', 'run_id', 'return_code', 'elapsed_s', 'requests',
                  'responses', 'decisions', 'frame_queries', 'pass_at_1', 'pass_at_3',
                  'status', 'actual_charge_usd', 'gateway_log_charge_usd',
                  'gateway_charge_count', 'gateway_prompt_tokens',
                  'gateway_completion_tokens')


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--agent_variant', default='agent4',
                        choices=sorted(VARIANT_TO_AGENT_ID), help='which of the four agents to run')
    parser.add_argument('--models', default=','.join(DEFAULT_MODELS),
                        help='comma-separated model names; each must have a YAML for the variant')
    parser.add_argument('--run_id', required=True,
                        help='batch id; reuse it exactly when resuming')
    parser.add_argument('--result_dir', default='results_realtime_batches',
                        help='root for model results (contents are git-ignored)')
    parser.add_argument('--cost_dir', default=None,
                        help='where billing and summaries go; default <result_dir>/_cost/<run_id>')
    parser.add_argument('--meta', default=None,
                        help='task list JSON; default evaluation_examples/test_realtime_gui_bench.json')
    parser.add_argument('--task', default=None,
                        help='single task id, a shortcut for a one-entry task list')
    parser.add_argument('--action_space', default='computer_13')
    parser.add_argument('--observation_type', default='screenshot')
    parser.add_argument('--domain', default='realtime_gui_bench')
    parser.add_argument('--path_to_vm', default='docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2')
    parser.add_argument('--api_base_url', default='https://www.packyapi.ai')
    parser.add_argument('--proxy', default=os.environ.get('HTTPS_PROXY') or None,
                        help='HTTP(S) proxy for gateway calls and model requests; '
                             'default: $HTTPS_PROXY if set')
    parser.add_argument('--max_steps', type=int, default=100)
    parser.add_argument('--num_envs', type=int, default=1,
                        help='environments per model (parallel tasks); one VM each')
    parser.add_argument('--keys-file', default=None,
                        help='read the {model: key} JSON from this 0600 file instead of no-echo stdin')
    parser.add_argument('--exclusive-keys-confirmed', action='store_true',
                        help='required: no other traffic uses these keys during the run window')
    parser.add_argument('--keep-going', action='store_true',
                        help='continue after a model produces no result.txt (default: stop)')
    parser.add_argument('--pause-between', action='store_true',
                        help='wait for a review_continue_<model> marker before the next model')
    parser.add_argument('--skip-billing', action='store_true',
                        help='run without the gateway billing/ledger calls')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the commands without calling models')
    return parser.parse_args()


def model_key_env(variant, model):
    """Read api.key_env from the same YAML the runner will load."""
    sys.path.insert(0, str(REPO))
    from mm_agents.realtime_config import default_config_path, load_realtime_config
    config = load_realtime_config(default_config_path(variant, model), variant=variant)
    return config['api'].get('key_env') or 'PACKY_API_KEY'


def read_keys(models, keys_file):
    if keys_file:
        payload = json.loads(Path(keys_file).read_text(encoding='utf-8'))
    else:
        attributes = termios.tcgetattr(sys.stdin.fileno())
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attributes)
        print('READY_KEYS_NO_ECHO', flush=True)
        payload = json.loads(sys.stdin.readline())
    if payload.get('*'):
        return {model: payload['*'] for model in models}
    missing = [model for model in models if not payload.get(model)]
    if missing:
        raise SystemExit('missing keys for: ' + ', '.join(missing))
    return {model: payload[model] for model in models}


def clean(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]').replace(secret.removeprefix('sk-'), '[REDACTED]')
        return value
    if isinstance(value, dict):
        return {key: clean(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item, secrets) for item in value]
    return value


def save(path, data, secrets):
    Path(path).write_text(json.dumps(clean(data, secrets), ensure_ascii=False, indent=2) + '\n',
                          encoding='utf-8')


def load_tasks(args):
    if args.task:
        return {args.domain: [args.task]}
    meta = Path(args.meta) if args.meta else (REPO / 'evaluation_examples/test_realtime_gui_bench.json')
    if not meta.exists():
        raise SystemExit(f'task list not found: {meta}')
    payload = json.loads(meta.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise SystemExit(f'unsupported task list layout in {meta}')
    return payload


def gateway_get(session, base_url, path, key, params=None):
    for attempt in range(5):
        try:
            response = session.get(base_url + path, headers={'Authorization': 'Bearer ' + key},
                                   params=params, timeout=30)
        except requests.RequestException:
            if attempt == 4:
                raise RuntimeError('gateway request failed')
            time.sleep(10)
            continue
        if response.status_code == 200:
            return response.json()
        if response.status_code == 429 and attempt < 4:
            print('BILLING_GET_RATE_LIMITED; no model request; waiting 20s', flush=True)
            time.sleep(20)
            continue
        raise RuntimeError(f'billing endpoint {path}: HTTP {response.status_code}')
    raise RuntimeError(f'billing endpoint {path}: retries exhausted')


def balance(session, base_url, key):
    return {'time_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'body': gateway_get(session, base_url, '/v1/dashboard/billing/usage', key)}


def usage_usd(snapshot):
    return snapshot['body']['total_usage'] / 100


def settle(session, base_url, key, attempts=12, interval=10):
    readings, previous, after = [], None, None
    for _ in range(attempts):
        after = balance(session, base_url, key)
        readings.append(after)
        total = after['body']['total_usage']
        if total == previous:
            break
        previous = total
        time.sleep(interval)
    return after, readings


def summarize(task_dir):
    result = {'task_dir': str(task_dir), 'requests': 0, 'responses': 0, 'decisions': 0,
              'frame_queries': 0, 'usage_records': [], 'evaluations': [], 'errors': [],
              'stream_received': [], 'pass_at_1': None, 'pass_at_3': None, 'status': None}
    trajectory = task_dir / 'trajectory.jsonl'
    if trajectory.exists():
        for line in trajectory.read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                result['errors'].append({'event': 'invalid_jsonl'})
                continue
            kind = event.get('event')
            if kind == 'model_request':
                result['requests'] += 1
            elif kind == 'model_response':
                result['responses'] += 1
                result['stream_received'].append(event.get('stream_received'))
                result['usage_records'].append({
                    'request_id': event.get('request_id'),
                    'usage': event.get('usage', event.get('provider_response', {}).get('usage', {})),
                })
            elif kind == 'action_submitted':
                result['decisions'] += 1
            elif kind == 'tool_result':
                result['frame_queries'] += 1
            elif kind == 'evaluation':
                result['evaluations'].append(event)
            elif kind in ('run_error', 'model_error', 'action_execution_error'):
                result['errors'].append(event)
    metrics = task_dir / 'agent_metrics.json'
    if metrics.exists():
        result['metrics'] = json.loads(metrics.read_text(encoding='utf-8'))
    scored = task_dir / 'result.json'
    if scored.exists():
        payload = json.loads(scored.read_text(encoding='utf-8'))
        result.update(pass_at_1=payload.get('pass_at_1'), pass_at_3=payload.get('pass_at_3'),
                      status=payload.get('status'), attempts=payload.get('attempts_completed'))
    return result


def build_report(cost_dir, manifest):
    """Rebuild cost_report.json from every per-model summary so resume never drops rows."""
    order = {model: index for index, model in enumerate(manifest['models'])}
    rows = []
    for path in sorted(Path(cost_dir).glob('*_summary.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            continue
        if data.get('model'):
            rows.append({field: data.get(field) for field in SUMMARY_FIELDS})
    rows.sort(key=lambda row: order.get(row['model'], 99))
    report = {key: value for key, value in manifest.items() if key != 'runs'}
    report['models'] = rows
    report['total_charge_usd'] = round(sum(
        row['gateway_log_charge_usd'] if row.get('gateway_log_charge_usd') is not None
        else (row.get('actual_charge_usd') or 0) for row in rows), 6)
    return report


def main():
    args = parse_args()
    models = [model.strip() for model in args.models.split(',') if model.strip()]
    result_dir = Path(args.result_dir)
    if not result_dir.is_absolute():
        result_dir = REPO / result_dir
    cost_dir = Path(args.cost_dir) if args.cost_dir else (result_dir / '_cost' / args.run_id)
    meta = load_tasks(args)

    plans = []
    for model in models:
        task_dirs = {}
        for domain, ids in meta.items():
            for task_id in ids:
                task_dirs[task_id] = (result_dir / model / args.run_id / args.agent_variant /
                                      args.action_space / args.observation_type / domain / task_id)
        command = [
            sys.executable, 'scripts/python/run_multienv.py',
            '--agent_variant', args.agent_variant, '--model', model, '--run_id', args.run_id,
            '--action_space', args.action_space, '--observation_type', args.observation_type,
            '--provider_name', 'docker', '--path_to_vm', str(args.path_to_vm),
            '--headless', '--api_base_url', args.api_base_url,
            '--test_all_meta_path', str(cost_dir / 'batch_tasks.json'),
            '--max_steps', str(args.max_steps), '--sleep_after_execution', '0',
            '--environment_ready_wait_s', '3', '--evaluation_settle_s', '3',
            '--num_envs', str(args.num_envs), '--result_dir', str(result_dir),
        ]
        plans.append({'model': model, 'command': command, 'task_dirs': task_dirs})

    if args.dry_run:
        print(json.dumps({'result_dir': str(result_dir), 'cost_dir': str(cost_dir),
                          'task_count': sum(len(v) for v in meta.values()),
                          'commands': [' '.join(plan['command']) for plan in plans]}, indent=2))
        return 0

    if not args.exclusive_keys_confirmed:
        raise SystemExit('pass --exclusive-keys-confirmed after checking no other traffic uses these keys')
    if not meta.get(args.domain):
        raise SystemExit(f'task list has no {args.domain} entries')

    session = requests.Session()
    if args.proxy:
        session.proxies.update({'http': args.proxy, 'https': args.proxy})

    keys = read_keys(models, args.keys_file)
    secrets = list(keys.values())
    cost_dir.mkdir(parents=True, exist_ok=True)
    save(cost_dir / 'batch_tasks.json', meta, secrets)

    key_envs = {model: model_key_env(args.agent_variant, model) for model in models}
    previous_runs = []
    if (cost_dir / 'manifest.json').exists():
        try:
            previous_runs = json.loads((cost_dir / 'manifest.json').read_text()).get('runs', [])
        except (ValueError, OSError):
            previous_runs = []
    manifest = {
        'run_id': args.run_id, 'agent_variant': args.agent_variant,
        'agent_id': VARIANT_TO_AGENT_ID[args.agent_variant], 'models': models,
        'task_count': sum(len(ids) for ids in meta.values()), 'domain': args.domain,
        'max_steps': args.max_steps, 'num_envs': args.num_envs,
        'result_dir': str(result_dir), 'cost_dir': str(cost_dir),
        'path_to_vm': str(args.path_to_vm), 'api_base_url': args.api_base_url,
        'quota_per_usd': QUOTA_PER_USD,
        'currency_note': 'Gateway ledger charge in USD, quota/500000. No RMB conversion or extrapolation.',
        'exclusive_key_use_confirmed_by_user': True,
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
        'key_env_per_model': key_envs,
        'runs': previous_runs,
    }
    save(cost_dir / 'manifest.json', manifest, secrets)
    save(cost_dir / 'cost_report.json', build_report(cost_dir, manifest), secrets)

    for plan in plans:
        model = plan['model']
        key_env = key_envs[model]
        key = keys[model]
        print('START_BATCH_MODEL', model, args.run_id, flush=True)

        before = None
        if not args.skip_billing:
            before = balance(session, args.api_base_url, key)
            save(cost_dir / f'{model}_billing_before.json', before, secrets)

        environment = os.environ.copy()
        for name in ('PACKY_COMMON_API_KEY', 'PACKY_KIMI_API_KEY', 'PACKY_GLM_MINIMAX_API_KEY', 'PACKY_API_KEY'):
            environment.pop(name, None)
        environment[key_env] = key
        environment['PYTHONPATH'] = str(REPO)
        environment['PYTHONUNBUFFERED'] = '1'
        if args.proxy:
            environment.update({'HTTP_PROXY': args.proxy, 'HTTPS_PROXY': args.proxy,
                                'http_proxy': args.proxy, 'https_proxy': args.proxy})
        environment.setdefault('NO_PROXY', 'localhost,127.0.0.1,::1')
        environment.setdefault('no_proxy', environment['NO_PROXY'])

        started = time.monotonic()
        with (cost_dir / f'{model}.log').open('w') as log:
            process = subprocess.Popen(plan['command'], cwd=REPO, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT)
            save(cost_dir / 'active.json', {'model': model, 'pid': process.pid,
                                            'task_dirs': {k: str(v) for k, v in plan['task_dirs'].items()}},
                 secrets)
            return_code = process.wait()
        elapsed = time.monotonic() - started

        # One summarized row per task; models normally run the whole task list.
        summaries = [summarize(task_dir) for task_dir in plan['task_dirs'].values()]
        result = summaries[0].copy()
        result['tasks'] = summaries
        result.update(model=model, key_env=key_env, run_id=args.run_id,
                      return_code=return_code, elapsed_s=round(elapsed, 1),
                      tasks_total=len(summaries),
                      tasks_scored=sum(1 for row in summaries if row['status'] is not None),
                      pass_at_3_mean=round(sum(row['pass_at_3'] or 0 for row in summaries) / len(summaries), 4))

        if not args.skip_billing:
            # A local proxy or gateway blip must never abort a batch that already
            # spent money; record it on the row and keep going.
            try:
                after, readings = settle(session, args.api_base_url, key)
                save(cost_dir / f'{model}_billing_after.json', after, secrets)
                save(cost_dir / f'{model}_billing_checks.json', readings, secrets)
                result.update(billing_before_usage=before['body']['total_usage'],
                              billing_after_usage=after['body']['total_usage'],
                              actual_charge_usd=round(usage_usd(after) - usage_usd(before), 6))
            except Exception as exc:
                result['billing_error'] = clean(str(exc), secrets)
                print('BILLING_ERROR', model, result['billing_error'][:160], flush=True)
            cutoff = datetime.datetime.fromisoformat(before['time_utc']).timestamp()
            try:
                logs = gateway_get(session, args.api_base_url, '/api/log/token', key, params={'key': key})
                save(cost_dir / f'{model}_gateway_logs.json', logs, secrets)
                rows = [row for row in logs.get('data', [])
                        if row.get('model_name') == model and (row.get('created_at') or 0) >= cutoff]
                result['gateway_log_charge_usd'] = round(
                    sum(row.get('quota', 0) for row in rows) / QUOTA_PER_USD, 6)
                result['gateway_charge_count'] = len(rows)
                result['gateway_prompt_tokens'] = sum(row.get('prompt_tokens', 0) or 0 for row in rows)
                result['gateway_completion_tokens'] = sum(row.get('completion_tokens', 0) or 0 for row in rows)
            except Exception as exc:
                result['ledger_detail_error'] = clean(str(exc), secrets)
        save(cost_dir / f'{model}_summary.json', result, secrets)

        manifest['runs'] = [row for row in manifest['runs'] if row.get('model') != model] + [result]
        save(cost_dir / 'manifest.json', manifest, secrets)
        report = build_report(cost_dir, manifest)
        save(cost_dir / 'cost_report.json', report, secrets)
        print('FINISHED_BATCH_MODEL', model,
              json.dumps(next(row for row in report['models'] if row['model'] == model), ensure_ascii=False),
              flush=True)

        if result['tasks_scored'] < len(summaries):
            print('INCOMPLETE_TASKS', model,
                  f"{result['tasks_scored']}/{len(summaries)} tasks have result.txt", flush=True)
            if not args.keep_going:
                print('STOPPING; pass --keep-going to continue with the remaining models', flush=True)
                break

        if args.pause_between and model != models[-1]:
            marker = cost_dir / f'review_continue_{model}'
            print('AWAITING_REVIEW', model, '| create', marker, flush=True)
            while not marker.exists():
                time.sleep(1)

    report = build_report(cost_dir, manifest)
    save(cost_dir / 'cost_report.json', report, secrets)
    save(cost_dir / 'active.json', {'status': 'complete', 'total_charge_usd': report['total_charge_usd']}, secrets)
    print('BATCH_COMPLETE', json.dumps({'total_charge_usd': report['total_charge_usd']}), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
