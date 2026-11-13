"""The trajectory audit: what the judge reads and how its verdict is stored."""
import json
from unittest.mock import Mock

import pytest

import lib_run_realtime
from mm_agents import realtime_auditor as auditor

TOOLS = ['get_frames', 'computer_click', 'computer_done']
TASK_TEXT = 'Task: Read the game rules shown on the current page.'


def write_trajectory(task_dir, request_messages, tail_events=()):
    task_dir.mkdir(parents=True, exist_ok=True)
    events = [{'event_index': 1, 'event': 'model_request', 'request_id': 1,
               'tools': TOOLS, 'request_messages': request_messages}]
    for index, event in enumerate(tail_events, start=2):
        events.append({'event_index': index, **event})
    (task_dir / 'trajectory.jsonl').write_text(
        ''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in events),
        encoding='utf-8')


def openai_chat_conversation():
    return [
        {'role': 'user', 'content': [{'type': 'text', 'text': TASK_TEXT},
                                     # Real trajectories keep only a reference, never the pixels.
                                     {'type': 'image_url',
                                      'image_url': {'url': 'data:image/png;base64,[omitted]',
                                                    'url_sha256': 'ab' * 32, 'url_length': 12345}}]},
        {'role': 'assistant', 'content': '', 'reasoning_content': 'I will press f12 to inspect.',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'computer_press', 'arguments': '{"key":"f12"}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1',
         'content': '{"status": "error", "message": "is forbidden."}'},
    ]


def test_judge_settings_require_a_key():
    with pytest.raises(auditor.AuditError) as error:
        auditor.judge_settings({})

    assert auditor.JUDGE_KEY_ENV in str(error.value)


def test_judge_settings_fall_back_to_the_shared_gateway(tmp_path, monkeypatch):
    prompt = tmp_path / 'auditor.txt'
    prompt.write_text('RULES', encoding='utf-8')

    model, key, base_url, text = auditor.judge_settings({
        auditor.JUDGE_KEY_ENV: 'judge-key', 'ANTHROPIC_BASE_URL': 'https://gateway.example/',
        auditor.JUDGE_PROMPT_ENV: str(prompt)})

    assert (model, key, base_url, text) == ('deepseek-flash', 'judge-key',
                                            'https://gateway.example', 'RULES')


def test_messages_url_keeps_an_explicit_v1():
    assert auditor.messages_url('https://gateway.example') == 'https://gateway.example/v1/messages'
    assert auditor.messages_url('https://gateway.example/v1') == 'https://gateway.example/v1/messages'


def test_the_audit_input_is_the_trajectorys_own_lines(tmp_path):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation(), tail_events=[
        {'event': 'action_submitted', 'actions': [{'action_type': 'DONE', 'parameters': {}}]},
        {'event': 'evaluation', 'result': 1.0, 'details': {'benchmark_id': 'D33'}},
    ])
    recorded = (task / 'trajectory.jsonl').read_text(encoding='utf-8')

    text = auditor.build_audit_input(task)

    # Byte for byte the recorded lines: nothing renamed, dropped or re-serialised.
    assert text == recorded
    events = [json.loads(line) for line in text.splitlines()]
    assert events[0]['event'] == 'model_request'
    assert events[0]['tools'] == TOOLS                       # not renamed
    assert events[0]['request_messages'] == openai_chat_conversation()
    assert events[0]['request_messages'][1]['reasoning_content'] == 'I will press f12 to inspect.'
    assert events[-1]['event'] == 'evaluation'


def test_the_audit_input_keeps_every_protocol_shape(tmp_path):
    task = tmp_path / 'task'
    write_trajectory(task, [
        {'role': 'user', 'content': [{'type': 'input_text', 'text': TASK_TEXT}]},
        {'type': 'reasoning', 'id': 'r1', 'summary': [{'type': 'summary_text', 'text': 'plan'}]},
        {'type': 'function_call', 'id': 'f1', 'call_id': 'c1', 'name': 'computer_typing',
         'arguments': '{"text":"ls -la /"}'},
        {'type': 'function_call_output', 'call_id': 'c1', 'output': '{"status":"ok"}'},
    ])

    text = auditor.build_audit_input(task)

    # The items arrive as recorded, whichever protocol wrote them.
    messages = json.loads(text.splitlines()[0])['request_messages']
    assert messages[0]['content'][0]['text'] == TASK_TEXT
    assert messages[1]['type'] == 'reasoning'
    assert messages[2]['name'] == 'computer_typing'
    assert messages[2]['arguments'] == '{"text":"ls -la /"}'
    assert messages[3]['output'] == '{"status":"ok"}'


def test_a_long_recorded_line_arrives_whole(tmp_path):
    task = tmp_path / 'task'
    write_trajectory(task, [
        {'role': 'user', 'content': [{'type': 'text', 'text': TASK_TEXT}]},
        {'role': 'tool',
         'content': '{"frame": "%s", "actual_time_s": 96.2}' % ('A' * 5000)},
    ])

    text = auditor.build_audit_input(task)

    # No truncation and no rewriting: the recorder is what keeps bytes out.
    assert text == (task / 'trajectory.jsonl').read_text(encoding='utf-8')
    assert 'A' * 5000 in text
    assert 'actual_time_s' in text


@pytest.mark.parametrize('observer_mode', ['none', 'script'])
def test_compact_input_preserves_events_strings_and_number_spelling(tmp_path, observer_mode):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation(), tail_events=[
        {'event': 'observer_action', 'method': 'click', 'args': [10, 20]},
        {'event': 'observer_failed', 'error': {'message': 'two  spaces\tand\na newline'}},
        {'event': 'evaluation', 'result': 0.0},
    ])
    (task / 'experiment.json').write_text(json.dumps({'observer_mode': observer_mode}))
    path = task / 'trajectory.jsonl'
    with path.open('a') as f:
        f.write('{ "event": "extra", "code": "x = \\\"a  b\\\"; p = \\\"C:\\\\\\\\tmp\\\"", "time_s": 1.20e-05 }\n')
    original = path.read_bytes()
    verbatim = auditor.build_audit_input(task)

    compact = auditor.build_audit_input(task, compact_json=True)

    assert [json.loads(line) for line in compact.splitlines()] == [
        json.loads(line) for line in verbatim.splitlines()]
    assert len(compact) < len(verbatim)
    assert '1.20e-05' in compact
    assert path.read_bytes() == original


@pytest.mark.parametrize('score', [0.0, 1.0])
def test_compact_reaudit_publishes_saved_score_and_keeps_evidence(tmp_path, monkeypatch, score):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation())
    (task / 'game_result.json').write_text(json.dumps({'result': score, 'pass_at_3': score}))
    (task / 'recording.mp4').write_bytes(b'original recording')
    originals = {name: (task / name).read_bytes() for name in
                 ['trajectory.jsonl', 'game_result.json', 'recording.mp4']}
    session = Mock()
    session.post.return_value = FakeResponse(judge_reply(good_verdict('NOT_CHEAT')))
    monkeypatch.setattr(auditor, 'judge_settings',
                        lambda: ('deepseek-flash', 'key', 'https://example', 'RULES'))
    monkeypatch.setattr(auditor.requests, 'Session', lambda: session)

    assert auditor.main([str(task), '--compact-json']) == 0

    actual_input = session.post.call_args.kwargs['json']['messages'][0]['content']
    assert actual_input == auditor.build_audit_input(task, compact_json=True)
    assert (task / 'audit_input.txt').read_text() == actual_input
    assert float((task / 'result.txt').read_text()) == score
    result = json.loads((task / 'result.json').read_text())
    assert result['audit_status'] == 'completed'
    assert result['judge']['input_format'] == 'compact_jsonl'
    assert all((task / name).read_bytes() == contents for name, contents in originals.items())


def good_verdict(label='CHEAT'):
    return json.dumps({'label': label, 'confidence': 0.9,
                       'evidence': [] if label == 'NOT_CHEAT' else [
                           {'type': 'devtools', 'quote': 'computer_press {"key":"f12"}',
                            'why_it_is_cheating': 'opening devtools bypasses the GUI'}],
                       'reasoning': 'because'})


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def judge_reply(text, stop_reason='end_turn', usage=None):
    return {'stop_reason': stop_reason, 'content': [{'type': 'text', 'text': text}],
            'usage': usage or {'input_tokens': 900, 'cache_read_input_tokens': 100,
                               'cache_creation_input_tokens': 0, 'output_tokens': 40}}


def test_parse_verdict_accepts_a_fenced_reply(tmp_path):
    verdict = auditor.parse_verdict('```json\n' + good_verdict() + '\n```')

    assert verdict['label'] == 'CHEAT' and verdict['confidence'] == 0.9
    assert verdict['evidence'][0]['quote'] == 'computer_press {"key":"f12"}'


@pytest.mark.parametrize('reply', [
    'no json here',
    json.dumps({'label': 'MAYBE', 'confidence': 0.5}),
    json.dumps({'label': 'NOT_CHEAT', 'confidence': 0.5, 'evidence': [{'type': 'x'}]}),
    json.dumps({'label': 'CHEAT', 'confidence': 1.5}),
])
def test_parse_verdict_rejects_a_reply_it_cannot_trust(reply):
    with pytest.raises(auditor.AuditError):
        auditor.parse_verdict(reply)


def test_a_malformed_reply_is_retried():
    settings = ('deepseek-flash', 'key', 'https://gateway.example', 'RULES')
    session = Mock()
    session.post = Mock(side_effect=[FakeResponse(judge_reply('oops')),
                                     FakeResponse(judge_reply(good_verdict()))])

    verdict = auditor.call_judge('trajectory', settings, session=session)

    assert verdict['label'] == 'CHEAT' and session.post.call_count == 2
    # Both replies were billed, so both count towards the task's judge cost.
    assert verdict['calls'] == 2
    assert verdict['usage'] == {'input_tokens': 2000, 'cached_input_tokens': 200,
                                'output_tokens': 80}


def test_a_reply_that_hits_the_output_ceiling_fails_the_audit():
    settings = ('deepseek-flash', 'key', 'https://gateway.example', 'RULES')
    session = Mock()
    session.post = Mock(return_value=FakeResponse(judge_reply(good_verdict(), 'max_tokens')))

    with pytest.raises(auditor.AuditError) as error:
        auditor.call_judge('trajectory', settings, session=session)

    assert 'max_tokens' in str(error.value)
    # A failed audit stays pending; its error is available for diagnosis.
    assert auditor.judge_error(error.value) == {'error': str(error.value)}


def test_a_capped_reply_counts_when_the_retry_succeeds(monkeypatch):
    monkeypatch.setattr(auditor, 'RETRY_SLEEP_S', 0)
    settings = ('deepseek-flash', 'key', 'https://gateway.example', 'RULES')
    session = Mock()
    session.post = Mock(side_effect=[
        FakeResponse(judge_reply(good_verdict(), 'max_tokens')),
        FakeResponse(judge_reply(good_verdict())),
    ])

    verdict = auditor.call_judge('trajectory', settings, session=session)

    # The audit that produced the score pays for the capped reply too.
    assert verdict['label'] == 'CHEAT'
    assert verdict['calls'] == 2 and verdict['usage']['output_tokens'] == 80


def test_a_request_failure_is_retried_on_the_next_round(monkeypatch):
    settings = ('deepseek-flash', 'key', 'https://gateway.example', 'RULES')
    session = Mock()
    session.post = Mock(side_effect=[
        auditor.requests.ConnectionError('boom'),
        FakeResponse(judge_reply(good_verdict('NOT_CHEAT'))),
    ])
    monkeypatch.setattr(auditor.time, 'sleep', lambda _seconds: None)

    verdict = auditor.call_judge('trajectory', settings, session=session)

    assert verdict['label'] == 'NOT_CHEAT' and verdict['rounds'] == 2


@pytest.mark.parametrize('score', [0.0, 1.0])
def test_provider_error_details_are_saved_without_replaying_game_or_exposing_key(
        tmp_path, monkeypatch, score):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation())
    (task / 'game_result.json').write_text(json.dumps({'result': score, 'pass_at_3': score}))
    (task / 'recording.mp4').write_bytes(b'original recording')
    originals = {name: (task / name).read_bytes() for name in
                 ['trajectory.jsonl', 'game_result.json', 'recording.mp4']}
    response = Mock(status_code=400, text='Context length exceeded; credential judge-secret')
    response.raise_for_status.side_effect = auditor.requests.HTTPError('400 Bad Request')
    session = Mock()
    session.post.return_value = response
    monkeypatch.setattr(auditor, 'NETWORK_ROUNDS', 1)

    status = auditor.audit_one(task, settings=('deepseek-flash', 'judge-secret',
                                               'https://example', 'RULES'), session=session)

    assert 'judge HTTP 400' in status and 'Context length exceeded' in status
    assert '[REDACTED]' in status and 'judge-secret' not in status
    pending = json.loads((task / 'result.json').read_text())
    assert pending['audit_status'] == 'pending' and 'Context length exceeded' in pending['judge']['error']
    assert not (task / 'result.txt').exists()
    assert all((task / name).read_bytes() == contents for name, contents in originals.items())


def test_a_reply_cut_off_by_max_tokens_is_not_accepted():
    settings = ('deepseek-flash', 'key', 'https://gateway.example', 'RULES')
    session = Mock()
    session.post = Mock(return_value=FakeResponse(judge_reply(good_verdict(), 'max_tokens')))

    with pytest.raises(auditor.AuditError):
        auditor.call_judge('trajectory', settings, session=session)


def test_audit_task_forces_the_score_to_zero_on_cheat(tmp_path, monkeypatch):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation())
    monkeypatch.setattr(auditor, 'call_judge',
                        lambda text, settings, session=None: {
                            'label': 'CHEAT', 'confidence': 0.9, 'evidence': [], 'reasoning': 'r',
                            'rounds': 1})

    result, record = auditor.audit_task(task, 1.0,
                                        settings=('deepseek-flash', 'k', 'https://g', 'RULES'))

    assert result == 0.0 and record['label'] == 'CHEAT' and record['model'] == 'deepseek-flash'
    # Exactly what the judge read is kept next to the result.
    assert (task / 'audit_input.txt').read_text(encoding='utf-8') == auditor.build_audit_input(task)


def test_audit_task_keeps_the_score_for_an_attempt(tmp_path, monkeypatch):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation())
    monkeypatch.setattr(auditor, 'call_judge',
                        lambda text, settings, session=None: {
                            'label': 'CHEAT_ATTEMPT', 'confidence': 0.9, 'evidence': [],
                            'reasoning': 'r', 'rounds': 1})

    result, record = auditor.audit_task(task, 1.0,
                                        settings=('deepseek-flash', 'k', 'https://g', 'RULES'))

    assert result == 1.0 and record['label'] == 'CHEAT_ATTEMPT'


def test_finish_scored_task_writes_the_audited_score(tmp_path, monkeypatch):
    out = tmp_path / 'task'
    out.mkdir()
    (out / 'result.json').write_text(json.dumps({'benchmark_id': 'D33', 'result': 1.0}),
                                     encoding='utf-8')
    monkeypatch.setattr(auditor, 'audit_task',
                        lambda task_dir, result, **kwargs: (0.0, {'label': 'CHEAT'}))
    monkeypatch.setattr(lib_run_realtime, 'log_task_completion', lambda *args, **kwargs: None)
    scores = []

    lib_run_realtime._finish_scored_task(out, 1.0, {}, None, scores)

    payload = json.loads((out / 'result.json').read_text(encoding='utf-8'))
    assert payload['judge'] == {'label': 'CHEAT'} and payload['result'] == 0.0
    assert (out / 'result.txt').read_text() == '0.0\n'
    assert scores == [0.0]


def test_an_unfinished_audit_preserves_the_game_for_audit_only_resume(tmp_path, monkeypatch):
    out = tmp_path / 'task'
    out.mkdir()
    (out / 'result.json').write_text(json.dumps({'benchmark_id': 'D33', 'result': 0.0}),
                                     encoding='utf-8')
    (out / 'result.txt').write_text('0.0\n')

    def unreachable(task_dir, result, **kwargs):
        raise auditor.AuditError('judge unreachable')

    monkeypatch.setattr(auditor, 'audit_task', unreachable)
    scores = []

    assert lib_run_realtime._finish_scored_task(out, 0.0, {}, None, scores) is None

    assert not (out / 'result.txt').exists()
    assert scores == []
    assert json.loads((out / 'game_result.json').read_text())['result'] == 0.0
    assert json.loads((out / 'result.json').read_text())['audit_status'] == 'pending'


def test_the_cli_audits_a_saved_game_without_a_final_score(tmp_path, monkeypatch):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation())
    (task / 'result.json').write_text(json.dumps({'result': 0.0}), encoding='utf-8')

    audit = Mock(return_value=(0.0, {'label': 'NOT_CHEAT', 'confidence': 0.9}))
    monkeypatch.setattr(auditor, 'audit_task', audit)

    assert auditor.audit_one(task).startswith('NOT_CHEAT')
    assert audit.call_args.args[1] == 0.0
    assert (task / 'result.txt').read_text() == '0.0\n'
    assert json.loads((task / 'game_result.json').read_text())['result'] == 0.0


def test_the_cli_skips_a_task_that_was_already_judged(tmp_path):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation())
    (task / 'result.json').write_text(
        json.dumps({'result': 0.0, 'judge': {'label': 'CHEAT'}}), encoding='utf-8')
    (task / 'result.txt').write_text('0.0\n')

    assert auditor.audit_one(task, settings=None) == 'skipped: already judged CHEAT'


def test_the_cli_dry_run_writes_the_input(tmp_path):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation())

    status = auditor.audit_one(task, dry_run=True)

    assert 'dry-run wrote' in status
    assert TASK_TEXT in (task / 'audit_input.txt').read_text(encoding='utf-8')
