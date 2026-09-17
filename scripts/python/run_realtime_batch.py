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
                  'glm-5.3-flash', 'MiniMax-M3']
SUMMARY_FIELDS = ('model', 'key_env', 'run_id', 'return_code', 'elapsed_s', 'requests',
                  'responses', 'decisions', 'frame_queries', 'pass_at_1', 'pass_at_3',
                  'status', 'actual_charge_usd', 'computed_charge_usd',
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
    """Prompt/completion/cached token counts from one recorded model response."""
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
                tokens = usage_tokens(usage)
                result['input_tokens'] += tokens['input']
                result['output_tokens'] += tokens['output']
                result['cached_input_tokens'] += tokens['cached_input']
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
        result.update(pass_at_1=payload.get('pass_at_1'), pass_at_3=payload.get('pass_at_3'),
                      status=payload.get('status'), attempts=payload.get('attempts_completed'))
    return result


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
        for name in ('PACKY_COMMON_API_KEY', 'PACKY_KIMI_API_KEY',
                     'PACKY_GLM_MINIMAX_API_KEY') + GENERIC_KEY_ENVS:
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
        price = price_for(prices, model)
        for row in summaries:
            row['computed_charge_usd'] = charge_usd(row, price)
        result = summaries[0].copy()
        result['tasks'] = summaries
        result.update(model=model, key_env=key_env, run_id=args.run_id,
                      return_code=return_code, elapsed_s=round(elapsed, 1),
                      tasks_total=len(summaries),
                      tasks_scored=sum(1 for row in summaries if row['status'] is not None),
                      pass_at_3_mean=round(sum(row['pass_at_3'] or 0 for row in summaries) / len(summaries), 4))
        result.update(
            input_tokens=sum(row['input_tokens'] for row in summaries),
            output_tokens=sum(row['output_tokens'] for row in summaries),
            cached_input_tokens=sum(row['cached_input_tokens'] for row in summaries),
        )
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
