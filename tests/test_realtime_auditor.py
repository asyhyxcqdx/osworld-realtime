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


def test_the_audit_input_is_the_trajectory_verbatim(tmp_path):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation(), tail_events=[
        {'event': 'action_submitted', 'actions': [{'action_type': 'DONE', 'parameters': {}}]},
        {'event': 'evaluation', 'result': 1.0, 'details': {'benchmark_id': 'D33'}},
    ])

    text = auditor.build_audit_input(task)
    payload = json.loads(text)

    # Nothing is rewritten: messages, reasoning, tool calls and results arrive as recorded.
    assert payload['registered_tools'] == TOOLS
    assert payload['request_messages'] == openai_chat_conversation()
    assert payload['events_after_the_final_request'][-1]['event'] == 'evaluation'
    assert payload['request_messages'][1]['reasoning_content'] == 'I will press f12 to inspect.'
    assert payload['request_messages'][2]['content'] == '{"status": "error", "message": "is forbidden."}'
    # The screenshot arrives as the reference the trajectory recorded, not as pixels.
    assert 'data:image/png;base64,[omitted]' in text


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

    # The items are handed over as they were recorded, whichever protocol wrote them.
    messages = json.loads(text)['request_messages']
    assert messages[0]['content'][0]['text'] == TASK_TEXT
    assert messages[1]['type'] == 'reasoning'
    assert messages[2]['name'] == 'computer_typing'
    assert messages[2]['arguments'] == '{"text":"ls -la /"}'
    assert messages[3]['output'] == '{"status":"ok"}'


def test_inline_image_bytes_never_reach_the_audit(tmp_path):
    task = tmp_path / 'task'
    blob = 'A' * 5000
    write_trajectory(task, [
        {'role': 'user', 'content': [{'type': 'text', 'text': TASK_TEXT}]},
        {'role': 'tool', 'content': "[{'type': 'input_text', 'text': '{\"frame\": \"%s\"}'}]" % blob},
    ])

    text = auditor.build_audit_input(task)

    assert '[binary data omitted]' in text
    assert blob not in text
    assert len(text) < 4000


def test_a_truncated_frames_payload_keeps_its_metadata(tmp_path):
    task = tmp_path / 'task'
    write_trajectory(task, [
        {'role': 'user', 'content': [{'type': 'text', 'text': TASK_TEXT}]},
        {'role': 'tool', 'content': 'B' * 6000 + ' "actual_time_s": 96.2}'},
    ])

    text = auditor.build_audit_input(task)

    assert '[binary data omitted]' in text


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


def judge_reply(text, stop_reason='end_turn'):
    return {'stop_reason': stop_reason, 'content': [{'type': 'text', 'text': text}]}


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


def test_an_unfinished_audit_leaves_no_score_so_the_task_is_rerun(tmp_path, monkeypatch):
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


def test_the_cli_skips_tasks_without_a_valid_score(tmp_path):
    task = tmp_path / 'task'
    write_trajectory(task, openai_chat_conversation())
    (task / 'result.json').write_text(json.dumps({'result': 0.0}), encoding='utf-8')

    assert auditor.audit_one(task, settings=None) == 'skipped: no valid score (re-run this task)'


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
