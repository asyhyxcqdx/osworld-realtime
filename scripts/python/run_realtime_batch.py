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

Keys are read once and never written to disk. Three input styles, tried in order:

    1) the environment: each model's own api.key_env from its YAML, then
       REALTIME_API_KEY, then PACKY_API_KEY as a catch-all;
    2) a repository .env file (git-ignored) is loaded first, so a machine only has
       to be configured once: copy .env.example to .env and fill it in;
    3) interactive TTY (no echo) or --keys-file <path>, both taking
       {"<model>": "sk-...", ...}; {"*": "sk-..."} serves every model.
       Only models still missing a key are asked for.

Gateway: --api_base_url, else REALTIME_API_BASE_URL, else PACKY_API_BASE_URL,
else per-protocol ANTHROPIC_BASE_URL / OPENAI_BASE_URL (left to the Agent),
else the built-in Packy default. The Packy-only billing endpoints are used only
when the gateway really is packyapi.ai; otherwise they are skipped and cost can
be computed from the token usage recorded in every trajectory with --prices.

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
DEFAULT_PACKY_BASE_URL = 'https://www.packyapi.ai'
GENERIC_KEY_ENVS = ('REALTIME_API_KEY', 'PACKY_API_KEY')
VARIANT_TO_AGENT_ID = {'agent1': 'vanilla', 'agent2': 'anticipatory',
                       'agent3': 'video', 'agent4': 'combine'}
DEFAULT_MODELS = ['claude-sonnet-5', 'gpt-5.6-sol', 'gemini-3.8-flash',
                  'qwen3.8-max-0902', 'deepseek-flash', 'kimi-k3',
                  'glm-5.3-flash', 'MiniMax-M3',
                  'claude-fable-5-1', 'gpt-6-astra']
SUMMARY_FIELDS = ('model', 'key_env', 'run_id', 'return_code', 'elapsed_s', 'requests',
                  'responses', 'decisions', 'frame_queries', 'pass_at_1', 'pass_at_3',
                  'status', 'tasks_total', 'tasks_scored', 'pass_at_3_mean',
                  'actual_charge_usd', 'computed_charge_usd',
                  'gateway_log_charge_usd', 'gateway_charge_count',
                  'gateway_prompt_tokens', 'gateway_completion_tokens',
                  'input_tokens', 'output_tokens')


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--agent_variant', default='agent4',
                        choices=sorted(VARIANT_TO_AGENT_ID), help='which of the four agents to run')
    parser.add_argument('--models', default=','.join(DEFAULT_MODELS),
                        help='comma-separated model names; each must have a YAML for the variant')
    parser.add_argument('--run_id', required=True,
                        help='batch id; reuse it exactly when resuming')
    parser.add_argument('--result_dir',
                        default=os.environ.get('REALTIME_RESULT_DIR') or 'results_realtime_batches',
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
    parser.add_argument('--path_to_vm',
                        default=os.environ.get('REALTIME_PATH_TO_VM')
                        or 'docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2')
    parser.add_argument('--api_base_url', default=None,
                        help='model API gateway root; default: $REALTIME_API_BASE_URL, '
                             '$PACKY_API_BASE_URL, per-protocol $ANTHROPIC_BASE_URL / '
                             '$OPENAI_BASE_URL, else the Packy default')
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
                        help='run without the gateway billing/ledger calls '
                             '(automatic for gateways that are not packyapi.ai)')
    parser.add_argument('--prices', default=None,
                        help='JSON price table used to derive cost from the token usage in '
                             'each trajectory: {"<model>": {"input_per_mtok": 3, '
                             '"output_per_mtok": 15, "cached_input_per_mtok": 0.3}, "*": {...}}')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the commands without calling models')
    return parser.parse_args()


def model_key_envs(variant, models):
    """Read api.key_env from the same YAMLs the runner will load."""
    sys.path.insert(0, str(REPO))
    from mm_agents.realtime_config import default_config_path, load_realtime_config
    envs = {}
    for model in models:
        config = load_realtime_config(default_config_path(variant, model), variant=variant)
        envs[model] = config['api'].get('key_env') or 'PACKY_API_KEY'
    return envs


def keys_from_environment(models, key_envs, environ=None):
    """Per model: its own key_env, then the generic catch-all variables."""
    environ = os.environ if environ is None else environ
    found = {}
    for model in models:
        for name in (key_envs[model],) + GENERIC_KEY_ENVS:
            if environ.get(name):
                found[model] = environ[name]
                break
    return found


def keys_from_payload(payload, models):
    if payload.get('*'):
        return {model: payload['*'] for model in models}
    return {model: payload[model] for model in models if payload.get(model)}


def read_keys_interactive(models):
    attributes = termios.tcgetattr(sys.stdin.fileno())
    attributes[3] &= ~termios.ECHO
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attributes)
    print('READY_KEYS_NO_ECHO', flush=True)
    payload = json.loads(sys.stdin.readline())
    return keys_from_payload(payload, models)


def resolve_keys(models, key_envs, keys_file, environ=None):
    """Environment/.env first; only the still-missing models are asked for."""
    keys = keys_from_environment(models, key_envs, environ)
    sources = {model: 'environment' for model in keys}
    missing = [model for model in models if model not in keys]
    if missing and keys_file:
        payload = json.loads(Path(keys_file).read_text(encoding='utf-8'))
        keys.update(keys_from_payload(payload, missing))
        sources.update({model: str(keys_file) for model in missing if model in keys})
        missing = [model for model in missing if model not in keys]
        if missing:
            raise SystemExit('missing keys for: ' + ', '.join(missing))
    elif missing:
        keys.update(read_keys_interactive(missing))
        sources.update({model: 'stdin' for model in missing})
    return {model: keys[model] for model in models}, sources


def is_packy(base_url):
    from urllib.parse import urlparse
    host = urlparse(base_url or '').hostname or ''
    return host == 'packyapi.ai' or host.endswith('.packyapi.ai')


def resolve_gateway(args, environ=None):
    """Return (base_url_or_None, source); None lets the Agent use per-protocol env."""
    environ = os.environ if environ is None else environ
    if args.api_base_url:
        return args.api_base_url, '--api_base_url'
    for name in ('REALTIME_API_BASE_URL', 'PACKY_API_BASE_URL'):
        if environ.get(name):
            return environ[name], name
    protocols = [(name, environ[name]) for name in ('ANTHROPIC_BASE_URL', 'OPENAI_BASE_URL')
                 if environ.get(name)]
    if protocols:
        # A single protocol variable may be reused for every model only when it is
        # the Packy gateway, which serves all three protocols; any other gateway is
        # left to the Agent's per-protocol resolution rather than being forced on
        # models that speak a different wire format.
        if len(protocols) == 1 and is_packy(protocols[0][1]):
            return protocols[0][1], '$' + protocols[0][0]
        return None, 'per-protocol ' + '/'.join('$' + name for name, _ in protocols)
    return DEFAULT_PACKY_BASE_URL, 'built-in default'


def load_prices(path):
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if 'models' in payload and isinstance(payload['models'], dict):
        payload = payload['models']
    prices = {}
    for name, value in payload.items():
        if not isinstance(value, dict) or 'output_per_mtok' not in value:
            raise SystemExit(f'price entry {name!r} must map to '
                             '{"input_per_mtok": ..., "output_per_mtok": ...}')
        prices[name] = {
            'input': float(value.get('input_per_mtok', 0)),
            'cached_input': float(value.get('cached_input_per_mtok',
                                            value.get('input_per_mtok', 0))),
            'output': float(value['output_per_mtok']),
        }
    return prices


def price_for(prices, model):
    return prices.get(model) or prices.get('*')


def charge_usd(tokens, price):
    """Cost of one task from its token usage; None without a price entry."""
    if not price:
        return None
    cached = min(tokens.get('cached_input_tokens', 0), tokens.get('input_tokens', 0))
    fresh = max(tokens.get('input_tokens', 0) - cached, 0)
    return round((fresh * price['input'] + cached * price['cached_input']
                  + tokens.get('output_tokens', 0) * price['output']) / 1_000_000, 6)


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


def usage_tokens(usage):
    """Prompt/completion/cached counts exactly as the gateway ledger reports them.

    ``input`` here is the ledger's prompt count, which is what
    ``task_request_records`` has to match against. On the anthropic wire format
    the gateway bills only the uncached bucket (``input_tokens``) as prompt
    tokens, so this must NOT add the cache buckets. Use ``context_tokens`` for
    the number of tokens the model actually read.
    """
    usage = usage or {}
    prompt = usage.get('prompt_tokens')
    if prompt is None:
        prompt = usage.get('input_tokens')
    completion = usage.get('completion_tokens')
    if completion is None:
        completion = usage.get('output_tokens')
    details = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
    return {'input': int(prompt or 0), 'output': int(completion or 0),
            'cached_input': int(details.get('cached_tokens') or 0)}


def context_tokens(usage):
    """Input/output tokens the model actually read and wrote, per response.

    Both wire formats describe the same three disjoint buckets, but they report
    them differently:

    * anthropic: ``input_tokens`` is only the part that was neither served from
      the cache nor written to it; ``cache_read_input_tokens`` and
      ``cache_creation_input_tokens`` are separate buckets, so the prompt the
      model read is their sum.
    * openai: ``prompt_tokens`` already is that whole prompt and the cached
      tokens are a subset of it, so the total must not add them a second time.

    Reporting and cost use this; gateway reconciliation uses ``usage_tokens``.
    """
    usage = usage or {}
    if 'cache_read_input_tokens' in usage or 'cache_creation_input_tokens' in usage:
        total = (int(usage.get('input_tokens') or 0)
                 + int(usage.get('cache_read_input_tokens') or 0)
                 + int(usage.get('cache_creation_input_tokens') or 0))
    else:
        prompt = usage.get('prompt_tokens')
        total = int(prompt if prompt is not None else usage.get('input_tokens') or 0)
    return {'input': total, 'output': usage_tokens(usage)['output']}


def load_previous_tasks(cost_dir, model):
    """Per-task rows written by an earlier invocation of the same model run."""
    path = Path(cost_dir) / f'{model}_summary.json'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    tasks = payload.get('tasks') if isinstance(payload, dict) else None
    return [row for row in tasks or [] if isinstance(row, dict) and row.get('task_dir')]


def merge_task_rows(previous, current):
    """Keep every task ever summarized; a re-run replaces its own row."""
    merged = {}
    for row in [*previous, *current]:
        merged[row['task_dir']] = row
    return [merged[key] for key in sorted(merged)]


def aggregate_model_row(summaries, *, model, key_env, run_id, return_code, elapsed_s):
    """One model-level row: counters summed, pass rates averaged over every task."""
    total = len(summaries)

    def mean(field):
        if not total:
            return None
        return round(sum(float(row.get(field) or 0) for row in summaries) / total, 4)

    def total_of(field):
        return sum(row.get(field) or 0 for row in summaries)

    return {
        'model': model, 'key_env': key_env, 'run_id': run_id,
        'return_code': return_code, 'elapsed_s': elapsed_s,
        'requests': total_of('requests'), 'responses': total_of('responses'),
        'decisions': total_of('decisions'), 'frame_queries': total_of('frame_queries'),
        'pass_at_1': mean('pass_at_1'), 'pass_at_3': mean('pass_at_3'),
        'status': None,
        'tasks_total': total,
        'tasks_scored': sum(1 for row in summaries if row.get('status') is not None),
        'pass_at_3_mean': mean('pass_at_3'),
        'input_tokens': total_of('input_tokens'),
        'output_tokens': total_of('output_tokens'),
        'cached_input_tokens': total_of('cached_input_tokens'),
    }


def summarize(task_dir):
    result = {'task_dir': str(task_dir), 'requests': 0, 'responses': 0, 'decisions': 0,
              'frame_queries': 0, 'usage_records': [], 'evaluations': [], 'errors': [],
              'stream_received': [], 'pass_at_1': None, 'pass_at_3': None, 'status': None,
              'input_tokens': 0, 'output_tokens': 0, 'cached_input_tokens': 0}
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
                usage = event.get('usage', event.get('provider_response', {}).get('usage', {}))
                tokens = context_tokens(usage)
                result['input_tokens'] += tokens['input']
                result['output_tokens'] += tokens['output']
                result['cached_input_tokens'] += usage_tokens(usage)['cached_input']
                result['usage_records'].append({
                    'request_id': event.get('request_id'),
                    'usage': usage,
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
        if payload.get('judge') is not None:
            result['judge'] = payload['judge']
        # result.txt is the only scalar the design trusts. The audit removes it when
        # it cannot reach a verdict (leaving result.json for diagnosis), so a task
        # without it is unfinished: not scored, and re-run by the resume path.
        if (task_dir / 'result.txt').exists():
            result.update(pass_at_1=payload.get('pass_at_1'), pass_at_3=payload.get('pass_at_3'),
                          status=payload.get('status'), attempts=payload.get('attempts_completed'))
        else:
            result['unscored_result'] = {field: payload.get(field) for field in (
                'result', 'pass_at_1', 'pass_at_3', 'status', 'attempts_completed')}
    return result


CHARGE_FIELDS = ('actual_charge_usd', 'gateway_log_charge_usd', 'gateway_charge_count',
                 'gateway_prompt_tokens', 'gateway_completion_tokens')


def load_charge_ledger(cost_dir, model):
    """Every invocation's charge for this run_id, oldest first."""
    path = Path(cost_dir) / f'{model}_charges.json'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    records = payload.get('charges') if isinstance(payload, dict) else payload
    return [row for row in records or [] if isinstance(row, dict)]


def append_charge_record(cost_dir, model, record, secrets=()):
    """Append one invocation and return the whole ledger plus its totals.

    The gateway only exposes a cumulative meter, so each invocation can only
    measure its own window. Keeping the per-invocation records means the
    run-level charge is the sum instead of whatever ran last.
    """
    ledger = load_charge_ledger(cost_dir, model) + [record]
    save(Path(cost_dir) / f'{model}_charges.json', {'charges': ledger}, secrets)
    totals = {field: round(sum(row.get(field) or 0 for row in ledger), 6)
              for field in CHARGE_FIELDS}
    for field in CHARGE_FIELDS:
        if all(row.get(field) is None for row in ledger):
            totals[field] = None
    return ledger, totals


LEDGER_MATCH_TOLERANCE_S = 900


def task_request_records(task_dir):
    """(epoch_seconds, prompt_tokens, completion_tokens) per recorded response.

    One entry per request whose response the trajectory finished recording; a
    stream that broke off leaves no usage behind and is skipped here. Both wire
    formats are read through ``usage_tokens``: anthropic and responses report
    ``input_tokens``/``output_tokens``, which the gateway ledger also bills as
    prompt/completion tokens, so matching must not assume the chat keys.
    """
    trajectory = Path(task_dir) / 'trajectory.jsonl'
    if not trajectory.exists():
        return []
    records = []
    for line in trajectory.read_text(errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get('event') != 'model_response':
            continue
        usage = event.get('usage') or {}
        if 'prompt_tokens' not in usage and 'input_tokens' not in usage:
            continue
        tokens = usage_tokens(usage)
        prompt, completion = tokens['input'], tokens['output']
        stamp = event.get('wall_time')
        if not stamp:
            continue
        try:
            epoch = datetime.datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            continue
        records.append((epoch, int(prompt), int(completion)))
    return records


def attribute_gateway_charge(tasks, ledger_rows, *, tolerance_s=LEDGER_MATCH_TOLERANCE_S):
    """Split one window's billed requests over the tasks that made them.

    ``tasks`` maps a task id to that task's trajectory request records. A ledger
    row is matched on its token pair first and on the closest timestamp second,
    so tasks sharing one key across parallel VMs still separate cleanly. Returns
    ``(per_task_usd, stats)``; a row that matches nothing stays unattributed
    instead of being pushed onto whichever task ran nearby.
    """
    candidates = {}
    for task_id, records in tasks.items():
        for epoch, prompt, completion in records:
            candidates.setdefault((prompt, completion), []).append([epoch, task_id, False])
    per_task, matched_usd, unmatched = {}, 0.0, 0
    total_usd = sum(row.get('quota') or 0 for row in ledger_rows) / QUOTA_PER_USD
    for row in ledger_rows:
        when = row.get('created_at') or 0
        pool = candidates.get((row.get('prompt_tokens'), row.get('completion_tokens')))
        pick = None
        if pool:
            ranked = sorted(pool, key=lambda item: (item[2], abs(item[0] - when)))
            if abs(ranked[0][0] - when) <= tolerance_s:
                pick = ranked[0]
        if pick is None:
            unmatched += 1
            continue
        pick[2] = True
        usd = (row.get('quota') or 0) / QUOTA_PER_USD
        per_task[pick[1]] = round(per_task.get(pick[1], 0.0) + usd, 6)
        matched_usd += usd
    stats = {
        'ledger_rows': len(ledger_rows),
        'matched_rows': len(ledger_rows) - unmatched,
        'unmatched_rows': unmatched,
        'matched_usd': round(matched_usd, 6),
        'unmatched_usd': round(total_usd - matched_usd, 6),
    }
    return per_task, stats


def load_per_task_charge(cost_dir, model):
    """Accumulated per-task amounts and the per-invocation windows behind them."""
    path = Path(cost_dir) / f'{model}_per_task_charge.json'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}, []
    if not isinstance(payload, dict):
        return {}, []
    totals = {str(key): float(value) for key, value in (payload.get('per_task') or {}).items()}
    windows = [row for row in (payload.get('windows') or []) if isinstance(row, dict)]
    return totals, windows


def append_per_task_charge(cost_dir, model, per_task, stats, *, run_id, tasks_run,
                           billing_error=None, secrets=()):
    """Add one invocation's per-task amounts to ``<model>_per_task_charge.json``.

    Same reasoning as the charge ledger: a subset re-run only sees its own
    window, so the per-task amounts accumulate instead of replacing each other.
    """
    totals, windows = load_per_task_charge(cost_dir, model)
    for task_id, usd in per_task.items():
        totals[task_id] = round(totals.get(task_id, 0.0) + usd, 6)
    windows.append({'window': len(windows) + 1, 'run_id': run_id, 'tasks_run': tasks_run,
                    'billing_error': billing_error, **stats})
    payload = {
        'model': model,
        'run_id': run_id,
        'per_task': {key: totals[key] for key in sorted(totals)},
        'total_usd': round(sum(totals.values()), 6),
        'unattributed_usd': round(sum(row.get('unmatched_usd') or 0 for row in windows), 6),
        'windows': windows,
        'attribution': 'per request: matching token pair first, closest timestamp second',
        'currency_note': 'Gateway ledger charge in USD, quota/500000.',
    }
    save(Path(cost_dir) / f'{model}_per_task_charge.json', payload, secrets)
    return payload


def record_per_task_charge(cost_dir, model, task_dirs, ledger_rows, *, run_id,
                           billing_error=None, secrets=()):
    """Attribute one window's ledger rows over its task directories and persist them.

    Returns ``(payload, attribution)``; the caller only has to map them onto the
    model row. Kept separate from the run loop so the whole path from result
    directories to ``<model>_per_task_charge.json`` can be exercised in a test.

    Only tasks that kept ``result.txt`` own their spend: a task without it has no
    valid score (an unfinished audit, or a directory the resume path will clear),
    so its requests stay in ``unattributed_usd`` instead of being charged to a row
    the sheet reports as having no result.
    """
    task_dirs = [Path(task_dir) for task_dir in task_dirs]
    tasks = {task_dir.name: task_request_records(task_dir)
             for task_dir in task_dirs if (task_dir / 'result.txt').exists()}
    per_task, attribution = attribute_gateway_charge(tasks, ledger_rows)
    payload = append_per_task_charge(cost_dir, model, per_task, attribution, run_id=run_id,
                                     tasks_run=len(task_dirs), billing_error=billing_error,
                                     secrets=secrets)
    return payload, attribution


def charged_usd(row):
    """Gateway ledger first, then the instant delta, then our own token math."""
    for field in ('gateway_log_charge_usd', 'actual_charge_usd', 'computed_charge_usd'):
        value = row.get(field)
        if value is not None:
            return value
    return None


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
    report['total_charge_usd'] = round(sum(charged_usd(row) or 0 for row in rows), 6)
    report['charge_source'] = ('gateway ledger when available, otherwise the token usage '
                               'recorded in each trajectory priced by --prices')
    return report


def main():
    sys.path.insert(0, str(REPO))
    from mm_agents.realtime_env import describe_sources, load_env_file
    # Loaded before parsing so .env can also supply the argument defaults below.
    applied = load_env_file()
    args = parse_args()
    print(describe_sources(applied), flush=True)

    # The audit runs before every score is written, so a missing judge key must
    # stop the batch here instead of after it has spent money on tasks that then
    # keep no result.txt. A dry run spends nothing, so it may skip the check.
    if not args.dry_run:
        from mm_agents.realtime_auditor import AuditError, require_judge_key
        try:
            require_judge_key()
        except AuditError as exc:
            raise SystemExit(f'realtime judge is not configured: {exc}')

    models = [model.strip() for model in args.models.split(',') if model.strip()]
    result_dir = Path(args.result_dir)
    if not result_dir.is_absolute():
        result_dir = REPO / result_dir
    cost_dir = Path(args.cost_dir) if args.cost_dir else (result_dir / '_cost' / args.run_id)
    meta = load_tasks(args)
    api_base_url, base_url_source = resolve_gateway(args)
    billing = not args.skip_billing and is_packy(api_base_url)
    prices = load_prices(args.prices)

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
            '--headless',
        ]
        if api_base_url:
            command += ['--api_base_url', api_base_url]
        command += [
            '--test_all_meta_path', str(cost_dir / 'batch_tasks.json'),
            '--max_steps', str(args.max_steps), '--sleep_after_execution', '0',
            '--environment_ready_wait_s', '3', '--evaluation_settle_s', '3',
            '--num_envs', str(args.num_envs), '--result_dir', str(result_dir),
        ]
        plans.append({'model': model, 'command': command, 'task_dirs': task_dirs})

    if args.dry_run:
        print(json.dumps({'result_dir': str(result_dir), 'cost_dir': str(cost_dir),
                          'api_base_url': api_base_url, 'api_base_url_source': base_url_source,
                          'packy_billing': billing, 'prices_file': args.prices,
                          'task_count': sum(len(v) for v in meta.values()),
                          'commands': [' '.join(plan['command']) for plan in plans]}, indent=2))
        return 0

    if not args.exclusive_keys_confirmed:
        raise SystemExit('pass --exclusive-keys-confirmed after checking no other traffic uses these keys')
    if not meta.get(args.domain):
        raise SystemExit(f'task list has no {args.domain} entries')
    if not billing and not args.skip_billing:
        print('GATEWAY_BILLING_SKIPPED', base_url_source,
              '- not packyapi.ai; cost comes from --prices when a price table is given', flush=True)
    if not prices and not billing:
        print('COST_NOT_RECORDED: pass --prices <json> to price the recorded token usage', flush=True)

    session = requests.Session()
    if args.proxy:
        session.proxies.update({'http': args.proxy, 'https': args.proxy})

    key_envs = model_key_envs(args.agent_variant, models)
    keys, key_sources = resolve_keys(models, key_envs, args.keys_file)
    for model in models:
        print('KEY_SOURCE', model, key_envs[model], key_sources[model], flush=True)
    secrets = list(keys.values())
    cost_dir.mkdir(parents=True, exist_ok=True)
    save(cost_dir / 'batch_tasks.json', meta, secrets)

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
        'path_to_vm': str(args.path_to_vm), 'api_base_url': api_base_url,
        'api_base_url_source': base_url_source, 'packy_billing': billing,
        'prices_file': args.prices,
        'quota_per_usd': QUOTA_PER_USD,
        'currency_note': 'Gateway ledger charge in USD, quota/500000. No RMB conversion or extrapolation.',
        'exclusive_key_use_confirmed_by_user': True,
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
        'key_env_per_model': key_envs, 'key_source_per_model': key_sources,
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
        if billing:
            before = balance(session, api_base_url, key)
            save(cost_dir / f'{model}_billing_before.json', before, secrets)

        environment = os.environ.copy()
        # The child must see this model's key and nothing else: drop every key
        # variable this batch declares, plus the generic catch-alls.
        for name in set(key_envs.values()) | set(GENERIC_KEY_ENVS):
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

        # One summarized row per task. A subset re-run (--task/--meta) merges with
        # the earlier rows instead of shrinking the model summary, and the model
        # row itself is aggregated over every task rather than copied from one.
        current = [summarize(task_dir) for task_dir in plan['task_dirs'].values()]
        summaries = merge_task_rows(load_previous_tasks(cost_dir, model), current)
        price = price_for(prices, model)
        for row in summaries:
            row['computed_charge_usd'] = charge_usd(row, price)
        result = aggregate_model_row(
            summaries, model=model, key_env=key_env, run_id=args.run_id,
            return_code=return_code, elapsed_s=round(elapsed, 1))
        result['tasks'] = summaries
        if price:
            result['computed_charge_usd'] = round(
                sum(row['computed_charge_usd'] or 0 for row in summaries), 6)
            result['price_per_mtok'] = {'input': price['input'], 'output': price['output'],
                                        'cached_input': price['cached_input']}
        elif not billing:
            result['computed_charge_usd'] = None

        if billing:
            # A local proxy or gateway blip must never abort a batch that already
            # spent money; record it on the row and keep going.
            try:
                after, readings = settle(session, api_base_url, key)
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
                logs = gateway_get(session, api_base_url, '/api/log/token', key, params={'key': key})
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
            else:
                # Attribution is derived from the ledger rows above, so it can only
                # run once they were read. A failure here must not look like a
                # ledger failure or abort a run that already spent money.
                try:
                    per_task_payload, attribution = record_per_task_charge(
                        cost_dir, model, [row['task_dir'] for row in current], rows,
                        run_id=args.run_id, billing_error=result.get('billing_error'),
                        secrets=secrets)
                    result['per_task_charge_usd'] = per_task_payload['total_usd']
                    result['unattributed_charge_usd'] = per_task_payload['unattributed_usd']
                    result['per_task_charge_rows'] = attribution['matched_rows']
                    print('PER_TASK_CHARGE', model, per_task_payload['total_usd'],
                          'unattributed', per_task_payload['unattributed_usd'], flush=True)
                except Exception as exc:
                    result['per_task_charge_error'] = clean(str(exc), secrets)
                    print('PER_TASK_CHARGE_ERROR', model,
                          result['per_task_charge_error'][:160], flush=True)
        # Charges accumulate across invocations of the same run_id: the gateway
        # meter only ever reports this window, so summing the records keeps the
        # run-level amount from being overwritten by a later subset re-run.
        ledger, totals = append_charge_record(
            cost_dir, model,
            {
                'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'tasks_run': len(current),
                'return_code': return_code,
                'elapsed_s': round(elapsed, 1),
                **{field: result.get(field) for field in CHARGE_FIELDS},
                'billing_error': result.get('billing_error'),
                'ledger_detail_error': result.get('ledger_detail_error'),
            },
            secrets,
        )
        result['charges'] = ledger
        result.update(totals)
        result['billing_errors'] = [row['billing_error'] for row in ledger if row.get('billing_error')]
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
