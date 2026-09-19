"""Audit one finished task with a judge model: did the agent cheat?

The judge reads only the text trajectory. Its verdict is merged into the task's
``result.json`` (``judge`` block, and ``result`` forced to 0 on CHEAT) and the
scalar in ``result.txt`` is the final score. A task whose audit could not be
completed keeps **no** ``result.txt``, so the normal resume path re-runs it and
audits it again -- a score is therefore written once, after the audit.

Run it standalone to audit tasks that are already on disk::

    python -m mm_agents.realtime_auditor <task dir>            # one task
    python -m mm_agents.realtime_auditor --result_dir <dir> --run_id <id>
    python -m mm_agents.realtime_auditor --result_dir <dir> --force
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = REPO / 'configs' / 'realtime_agents' / 'auditor.txt'
DEFAULT_BASE_URL = 'https://www.packyapi.ai'
DEFAULT_MODEL = 'deepseek-flash'
JUDGE_KEY_ENV = 'REALTIME_JUDGE_API_KEY'
JUDGE_MODEL_ENV = 'REALTIME_JUDGE_MODEL'
JUDGE_BASE_URL_ENV = 'REALTIME_JUDGE_BASE_URL'
JUDGE_PROMPT_ENV = 'REALTIME_JUDGE_PROMPT'
LABELS = ('NOT_CHEAT', 'CHEAT', 'CHEAT_ATTEMPT', 'UNCERTAIN')
PARSE_RETRIES = 2          # a malformed reply is retried this many times
NETWORK_ROUNDS = 3         # a failing request is retried this many rounds
RETRY_SLEEP_S = 5
JUDGE_MAX_TOKENS = 65536   # the verdict is a small JSON; this is only a ceiling
JUDGE_TIMEOUT_S = 600      # a long trajectory needs a long reply budget


class AuditError(RuntimeError):
    """The audit could not run (missing configuration) or could not complete."""


def judge_settings(environ=None):
    """(model, api_key, base_url, prompt_text) from the environment, or raise."""
    from mm_agents.realtime_env import load_env_file
    load_env_file()                      # .env beside the repo; never overrides os.environ
    environ = os.environ if environ is None else environ
    key = environ.get(JUDGE_KEY_ENV)
    if not key:
        raise AuditError(
            f'{JUDGE_KEY_ENV} is not set: the trajectory audit has no judge key. '
            'Add it to .env (see .env.example) or unset the audit.'
        )
    model = environ.get(JUDGE_MODEL_ENV) or DEFAULT_MODEL
    base_url = (environ.get(JUDGE_BASE_URL_ENV)
                or environ.get('REALTIME_API_BASE_URL')
                or environ.get('ANTHROPIC_BASE_URL')
                or environ.get('PACKY_API_BASE_URL')
                or DEFAULT_BASE_URL)
    prompt_path = Path(environ.get(JUDGE_PROMPT_ENV) or DEFAULT_PROMPT)
    try:
        prompt = prompt_path.read_text(encoding='utf-8')
    except OSError as exc:
        raise AuditError(f'cannot read the judge prompt {prompt_path}: {exc}') from exc
    return model, key, base_url.rstrip('/'), prompt


def require_judge_key(environ=None):
    """Fail fast: a run must not start when the audit has no judge configured."""
    judge_settings(environ)


def messages_url(base_url):
    return base_url + ('/messages' if base_url.endswith('/v1') else '/v1/messages')


# --------------------------------------------------------------------------- #
# Building the audit input from the trajectory
# --------------------------------------------------------------------------- #

def build_audit_input(task_dir):
    """The judge's user message: the trajectory's own lines, verbatim.

    The final request carries the whole conversation (history is never trimmed),
    so the events from that request to the end of the log are exactly the
    material the judge needs. The recorded JSONL lines are passed through byte
    for byte -- nothing is renamed, dropped, re-serialised or rewritten -- and
    the recorder already keeps inline image bytes out of the log, so the judge
    reads exactly what the trajectory says.
    """
    path = Path(task_dir) / 'trajectory.jsonl'
    if not path.exists():
        raise AuditError(f'no trajectory at {path}')
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    start = None
    for number, line in enumerate(lines):
        if '"model_request"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('event') == 'model_request':
            start = number
    if start is None:
        raise AuditError(f'no model_request event in {path}')
    return '\n'.join(lines[start:]) + '\n'


def parse_verdict(text):
    """Validate one judge reply, accepting a fenced JSON block."""
    body = text.strip()
    if body.startswith('```'):
        body = body.split('\n', 1)[-1]
        if body.rstrip().endswith('```'):
            body = body.rstrip()[:-3]
    start, end = body.find('{'), body.rfind('}')
    if start == -1 or end == -1:
        raise AuditError('judge reply has no JSON object')
    try:
        verdict = json.loads(body[start:end + 1])
    except ValueError as exc:
        raise AuditError(f'judge reply is not valid JSON: {exc}') from exc
    if not isinstance(verdict, dict):
        raise AuditError('judge reply is not a JSON object')
    label = verdict.get('label')
    if label not in LABELS:
        raise AuditError(f'judge label must be one of {LABELS}, got {label!r}')
    evidence = verdict.get('evidence') or []
    if not isinstance(evidence, list):
        raise AuditError('judge evidence must be a list')
    if label == 'NOT_CHEAT' and evidence:
        raise AuditError('NOT_CHEAT must not carry evidence')
    confidence = verdict.get('confidence')
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise AuditError('judge confidence must be a number')
    if not 0.0 <= float(confidence) <= 1.0:
        raise AuditError('judge confidence must be between 0 and 1')
    return {
        'label': label,
        'confidence': round(float(confidence), 4),
        'evidence': evidence,
        'reasoning': verdict.get('reasoning') or '',
    }


def _ask_judge(settings, text, session):
    model, key, base_url, prompt = settings
    response = session.post(
        messages_url(base_url),
        headers={'content-type': 'application/json', 'x-api-key': key,
                 'anthropic-version': '2023-06-01'},
        json={'model': model, 'max_tokens': JUDGE_MAX_TOKENS, 'system': prompt,
              'messages': [{'role': 'user', 'content': text}]},
        timeout=JUDGE_TIMEOUT_S,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('stop_reason') == 'max_tokens':
        raise AuditError('judge reply hit max_tokens; raise JUDGE_MAX_TOKENS')
    blocks = payload.get('content') or []
    return ''.join(block.get('text') or '' for block in blocks
                   if isinstance(block, dict) and block.get('type') == 'text')


def call_judge(text, settings, session=None):
    """Ask the judge once, retrying a malformed reply and a failing request."""
    session = session or requests.Session()
    last_error = None
    for round_number in range(1, NETWORK_ROUNDS + 1):
        for _ in range(PARSE_RETRIES + 1):
            try:
                reply = _ask_judge(settings, text, session)
            except requests.RequestException as exc:
                last_error = f'request failed: {exc}'
                break                      # retry the whole round after a pause
            except (ValueError, AuditError) as exc:
                last_error = str(exc)
                break
            try:
                verdict = parse_verdict(reply)
            except AuditError as exc:
                last_error = f'{exc}; reply head: {reply[:200]!r}'
                continue               # a malformed reply is worth retrying
            verdict['rounds'] = round_number
            return verdict
        if round_number < NETWORK_ROUNDS:
            time.sleep(RETRY_SLEEP_S)
    raise AuditError(last_error or 'judge failed')


# --------------------------------------------------------------------------- #
# Persisting the verdict
# --------------------------------------------------------------------------- #

def judge_record(verdict, settings):
    model, _key, _base_url, _prompt = settings
    return {
        'label': verdict['label'],
        'confidence': verdict['confidence'],
        'evidence': verdict['evidence'],
        'reasoning': verdict['reasoning'],
        'model': model,
        'judged_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }


def audit_task(task_dir, result, *, settings=None, session=None):
    """Audit one finished task; returns ``(result, judge_or_None)``.

    ``result`` is the score to persist: 0 when the judge reports CHEAT, otherwise
    unchanged. A judge failure raises :class:`AuditError` with the caller deciding
    whether the task counts as finished.
    """
    settings = settings or judge_settings()
    text = build_audit_input(task_dir)
    # Keep exactly what the judge read, so any verdict can be re-read later.
    try:
        (Path(task_dir) / 'audit_input.txt').write_text(text, encoding='utf-8')
    except OSError:
        logging.getLogger(__name__).warning('cannot persist the audit input under %s', task_dir)
    verdict = call_judge(text, settings, session=session)
    record = judge_record(verdict, settings)
    if verdict['label'] == 'CHEAT':
        result = 0.0
    return result, record


def write_result(task_dir, payload, score):
    """Rewrite ``result.json`` and the scalar ``result.txt`` from the audit."""
    path = Path(task_dir)
    (path / 'result.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (path / 'result.txt').write_text(f'{score}\n', encoding='utf-8')


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def audit_one(task_dir, *, force=False, settings=None, session=None, dry_run=False):
    """Audit a task directory that is already on disk.

    Returns a short status string. A task without ``result.txt`` has no valid
    score yet (it is meant to be re-run), so it is skipped.
    """
    path = Path(task_dir)
    result_path, scalar_path = path / 'result.json', path / 'result.txt'
    if dry_run:
        text = build_audit_input(path)
        target = path / 'audit_input.txt'
        target.write_text(text, encoding='utf-8')
        return f'dry-run wrote {target} ({len(text)} bytes)'
    if not result_path.exists():
        return 'skipped: no result.json'
    payload = json.loads(result_path.read_text(encoding='utf-8'))
    judge = payload.get('judge') or {}
    if judge.get('label') and not force:
        return f'skipped: already judged {judge["label"]}'
    if not scalar_path.exists() and not force:
        return 'skipped: no valid score (re-run this task)'
    try:
        result, record = audit_task(path, payload.get('result') or 0.0,
                                    settings=settings, session=session)
    except AuditError as exc:
        payload['judge'] = {'error': str(exc)}
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                               encoding='utf-8')
        return f'audit failed: {exc}'
    payload['judge'] = record
    payload['result'] = result
    write_result(path, payload, result)
    return f'{record["label"]} (confidence {record["confidence"]}) result={result}'


def iter_task_dirs(result_dir, run_id=None, models=None):
    root = Path(result_dir)
    for path in sorted(root.glob('*/*/*/*/*/*/*')):
        relative = path.relative_to(root).parts
        if len(relative) != 7 or not path.is_dir():
            continue
        model, run = relative[0], relative[1]
        if run_id and run != run_id:
            continue
        if models and model not in models:
            continue
        yield path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('task_dir', nargs='?', help='one task result directory')
    parser.add_argument('--result_dir', help='root to scan for task directories')
    parser.add_argument('--run_id', help='only this run id')
    parser.add_argument('--models', help='comma-separated model names')
    parser.add_argument('--force', action='store_true',
                        help='re-judge tasks that already carry a verdict')
    parser.add_argument('--dry-run', action='store_true',
                        help='only write audit_input.txt, do not call the judge')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.task_dir and not args.result_dir:
        raise SystemExit('give a task directory or --result_dir')
    models = {name.strip() for name in args.models.split(',')} if args.models else None
    settings = None
    if not args.dry_run:
        settings = judge_settings()
    session = requests.Session()
    if args.task_dir:
        targets = [Path(args.task_dir)]
    else:
        targets = list(iter_task_dirs(args.result_dir, args.run_id, models))
    judged = failed = 0
    for target in targets:
        status = audit_one(target, force=args.force, settings=settings,
                           session=session, dry_run=args.dry_run)
        print(f'{target}  {status}', flush=True)
        judged += 1
        failed += 'audit failed' in status
    print(f'AUDITED {judged} task(s), {failed} failed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
