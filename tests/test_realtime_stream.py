import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from mm_agents.realtime_agent import ModelWire, RealtimeAgent, response_reasoning
from mm_agents.realtime_protocol import AgentProtocolError
from mm_agents.realtime_stream import RateLimitedStreamError, collect_stream


PROTOCOLS = ['anthropic_messages', 'openai_chat', 'openai_responses']


TEST_SYSTEM_PROMPT = "Test-only realtime agent system prompt."
TEST_USER_PROMPT = "Test-only realtime agent user prompt."


def sse(events, *, fail_after=False):
    def lines():
        yield b': keepalive'
        yield b''
        for event in events:
            encoded = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
            yield ('data: ' + encoded).encode('utf-8')
            yield b''
        if fail_after:
            raise requests.ReadTimeout('stream paused before completion')
    return SimpleNamespace(status_code=200, headers={'content-type': 'text/event-stream; charset=utf-8'},
                           iter_lines=lines, close=Mock())


def tool_events(protocol, *, key='space', complete=True):
    if protocol == 'anthropic_messages':
        result = [
            {'type': 'message_start', 'message': {'id': 'm1', 'role': 'assistant', 'content': [], 'usage': {'input_tokens': 123, 'output_tokens': 1}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'a1', 'name': 'computer_press', 'input': {}}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{"key":'}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(key) + '}'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 10}},
        ]
        return result + ([{'type': 'message_stop'}] if complete else [])
    if protocol == 'openai_chat':
        result = [
            {'id': 'm1', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'a1', 'type': 'function', 'function': {'name': 'computer_press', 'arguments': '{"key":'}}]}}]},
            {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': json.dumps(key) + '}'}}]}, 'finish_reason': 'tool_calls'}]},
            {'choices': [], 'usage': {'prompt_tokens': 123, 'completion_tokens': 10}},
        ]
        return result + (['[DONE]'] if complete else [])
    body = {'id': 'm1', 'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'a1', 'name': 'computer_press', 'arguments': json.dumps({'key': key})}], 'usage': {'input_tokens': 123, 'output_tokens': 10}}
    result = [{'type': 'response.output_item.done', 'item': body['output'][0]}]
    return result + ([{'type': 'response.completed', 'response': body}] if complete else [])


def agent_with_http(monkeypatch, protocol, responses):
    monkeypatch.setenv('TEST_STREAM_API_KEY', 'test-placeholder')
    monkeypatch.setattr('mm_agents.realtime_agent.time.sleep', Mock())
    wire = ModelWire('test-model', protocol, api_key_env='TEST_STREAM_API_KEY')
    wire.session.post = Mock(side_effect=responses)
    return RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=True, frames=True, wire=wire)


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_complete_stream_decodes_action_and_records_actual_transport(monkeypatch, protocol):
    response = sse(tool_events(protocol))
    agent = agent_with_http(monkeypatch, protocol, [response])
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1] == [
        {'action_type': 'PRESS', 'parameters': {'key': 'space'}}]
    assert next(e for e in agent.last_events if e['event'] == 'model_request')['stream_requested'] is True
    reply = next(e for e in agent.last_events if e['event'] == 'model_response')
    assert reply['stream_received'] is True and reply['usage']
    response.close.assert_called_once()


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_complete_tool_json_without_stream_termination_never_dispatches(monkeypatch, protocol):
    responses = [sse(tool_events(protocol, complete=False)) for _ in range(5)]
    agent = agent_with_http(monkeypatch, protocol, responses)
    query = Mock(); agent.bind_frame_query(query)
    with pytest.raises(RuntimeError, match='no actions dispatched'):
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    assert agent.pending_action_calls is None
    query.assert_not_called()
    assert agent.wire.session.post.call_count == 5
    for response in responses:
        response.close.assert_called_once()


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_read_timeout_retries_whole_stream_without_reusing_partial_actions(monkeypatch, protocol):
    partial = sse(tool_events(protocol, key='d', complete=False), fail_after=True)
    complete = sse(tool_events(protocol, key='w'))
    agent = agent_with_http(monkeypatch, protocol, [partial, complete])
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1] == [
        {'action_type': 'PRESS', 'parameters': {'key': 'w'}}]
    assert agent.wire.session.post.call_count == 2
    assert all(call.kwargs['timeout'] == 240 for call in agent.wire.session.post.call_args_list)
    assert all(call.kwargs['stream'] is True for call in agent.wire.session.post.call_args_list)
    partial.close.assert_called_once(); complete.close.assert_called_once()


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_stream_read_timeouts_stop_after_five_attempts(monkeypatch, protocol):
    responses = [sse(tool_events(protocol, complete=False), fail_after=True) for _ in range(5)]
    agent = agent_with_http(monkeypatch, protocol, responses)
    with pytest.raises(requests.ReadTimeout):
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    assert agent.wire.session.post.call_count == 5
    assert agent.pending_action_calls is None
    for response in responses:
        response.close.assert_called_once()


def _http_error(status):
    return SimpleNamespace(status_code=status, json=lambda: {'error': {'message': 'temporary'}}, close=Mock())


@pytest.mark.parametrize('status', [402, 429, 500, 502, 503, 504])
def test_existing_http_retry_statuses_are_retained(monkeypatch, status):
    error = _http_error(status)
    agent = agent_with_http(monkeypatch, 'openai_chat', [error, sse(tool_events('openai_chat'))])
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1]
    assert agent.wire.session.post.call_count == 2
    error.close.assert_called_once()


def test_upstream_balance_402_keeps_retrying_past_the_plain_error_budget(monkeypatch):
    responses = [_http_error(402) for _ in range(4)] + [sse(tool_events('openai_chat'))]
    agent = agent_with_http(monkeypatch, 'openai_chat', responses)
    sleep = time.sleep
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1]
    assert agent.wire.session.post.call_count == 5
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4, 8]
    for response in responses:
        response.close.assert_called_once()


def test_upstream_balance_402_gives_up_after_five_attempts(monkeypatch):
    responses = [_http_error(402) for _ in range(5)]
    agent = agent_with_http(monkeypatch, 'openai_chat', responses)
    with pytest.raises(RuntimeError, match='HTTP 402'):
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    assert agent.wire.session.post.call_count == 5
    for response in responses:
        response.close.assert_called_once()


def test_plain_server_errors_still_stop_after_five_attempts(monkeypatch):
    responses = [_http_error(503) for _ in range(5)]
    agent = agent_with_http(monkeypatch, 'openai_chat', responses)
    with pytest.raises(RuntimeError, match='HTTP 503'):
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    assert agent.wire.session.post.call_count == 5


def _rate_limit_stream(protocol):
    """HTTP 200 whose stream reports the provider's quota refusal in each protocol's shape."""
    if protocol == 'anthropic_messages':
        events = [{'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'Number of request tokens has exceeded your rate limit.'}}]
    elif protocol == 'openai_chat':
        events = [{'error': {'message': 'Your requests have exceeded token rate limit.', 'code': 'rate_limit_exceeded'}}]
    else:
        events = [{'type': 'error', 'error': {'type': 'too_many_requests', 'code': 'rate_limit_exceeded',
                                              'message': 'Your requests to test-model have exceeded token rate limit.'}}]
    return sse(events)


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_rate_limited_stream_is_retried_without_dispatching_actions(monkeypatch, protocol):
    limited = _rate_limit_stream(protocol)
    agent = agent_with_http(monkeypatch, protocol, [limited, sse(tool_events(protocol))])
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1] == [
        {'action_type': 'PRESS', 'parameters': {'key': 'space'}}]
    assert agent.wire.session.post.call_count == 2
    assert agent.pending_action_calls
    limited.close.assert_called_once()


def test_rate_limit_gets_five_spaced_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr('mm_agents.realtime_agent.random', SimpleNamespace(uniform=lambda a, b: 0.0))
    responses = [_rate_limit_stream('openai_responses') for _ in range(5)] + [sse(tool_events('openai_responses'))]
    agent = agent_with_http(monkeypatch, 'openai_responses', responses)
    sleep = time.sleep
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1]
    assert agent.wire.session.post.call_count == 6
    assert [call.args[0] for call in sleep.call_args_list] == [10, 20, 30, 45, 60]
    for response in responses:
        response.close.assert_called_once()


def test_rate_limit_gives_up_after_five_retries(monkeypatch):
    responses = [_rate_limit_stream('openai_responses') for _ in range(6)]
    agent = agent_with_http(monkeypatch, 'openai_responses', responses)
    with pytest.raises(RateLimitedStreamError, match='rate_limit_exceeded'):
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    assert agent.wire.session.post.call_count == 6
    assert agent.pending_action_calls is None
    for response in responses:
        response.close.assert_called_once()


def test_non_quota_stream_failure_is_not_retried(monkeypatch):
    failed = sse([{'type': 'response.failed', 'response': {
        'status': 'failed', 'error': {'code': 'server_error', 'message': 'the model refused this request'}}}])
    agent = agent_with_http(monkeypatch, 'openai_responses', [failed, sse(tool_events('openai_responses'))])
    with pytest.raises(RuntimeError) as raised:
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    assert not isinstance(raised.value, RateLimitedStreamError)
    assert agent.wire.session.post.call_count == 1
    failed.close.assert_called_once()


def test_quota_refusal_reported_as_response_failed_is_retried(monkeypatch):
    failed = sse([{'type': 'response.failed', 'response': {
        'status': 'failed', 'error': {'code': 'rate_limit_exceeded', 'message': 'exceeded token rate limit'}}}])
    agent = agent_with_http(monkeypatch, 'openai_responses', [failed, sse(tool_events('openai_responses'))])
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1]
    assert agent.wire.session.post.call_count == 2


def test_messages_keep_thinking_signatures_redaction_and_cumulative_usage():
    events = [
        {'type': 'message_start', 'message': {'id': 'm', 'model': 'test', 'role': 'assistant', 'content': [], 'usage': {'input_tokens': 100, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 5, 'output_tokens': 1}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': '观察'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': '画面'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'sig-'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'end'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'redacted_thinking', 'data': 'opaque'}},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'name': 'computer_done', 'id': 'done1', 'input': {}}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': ''}},
        {'type': 'content_block_stop', 'index': 2},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 8}},
        {'type': 'message_delta', 'delta': {}, 'usage': {'output_tokens': 10, 'output_tokens_details': {'thinking_tokens': 7}}},
        {'type': 'message_stop'},
    ]
    body = collect_stream(sse(events), 'anthropic_messages')
    assert body['content'][0] == {'type': 'thinking', 'thinking': '观察画面', 'signature': 'sig-end'}
    assert body['content'][1] == {'type': 'redacted_thinking', 'data': 'opaque'}
    assert body['content'][2]['input'] == {}
    assert body['usage']['input_tokens'] == 100
    assert body['usage']['cache_read_input_tokens'] == 20
    assert body['usage']['cache_creation_input_tokens'] == 5
    assert body['usage']['output_tokens'] == 10
    wire = ModelWire('test', 'anthropic_messages')
    assert wire.unpack(body)[2][0]['content'] == body['content']
    assert response_reasoning(body, wire.protocol)['reasoning'] == ['观察画面']


def test_chat_interleaved_tools_reasoning_signatures_and_final_usage():
    events = [
        {'id': 'm', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': 'Observe ', 'tool_calls': [{'index': 1, 'id': 'b', 'type': 'function', 'function': {'name': 'computer_wait', 'arguments': '{"duration_s":'}}]}}], 'usage': None},
        {'choices': [{'index': 0, 'delta': {'reasoning_content': 'then act.', 'content': '点击', 'tool_calls': [{'index': 0, 'id': 'a', 'type': 'function', 'function': {'name': 'computer_click', 'arguments': '{"x":995,'}, 'extra_content': {'google': {'thought_signature': 'opaque-signature'}}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': '', 'function': {'name': '', 'arguments': '"y":724}'}}, {'index': 1, 'function': {'arguments': '0.2}'}}]}, 'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'prompt_tokens': 222, 'completion_tokens': 12, 'prompt_tokens_details': {'cached_tokens': 100}, 'completion_tokens_details': {'reasoning_tokens': 7}}},
        '[DONE]',
    ]
    body = collect_stream(sse(events), 'openai_chat')
    message = body['choices'][0]['message']
    assert message['reasoning_content'] == 'Observe then act.'
    assert message['content'] == '点击'
    assert [c['id'] for c in message['tool_calls']] == ['a', 'b']
    assert json.loads(message['tool_calls'][0]['function']['arguments']) == {'x': 995, 'y': 724}
    assert message['tool_calls'][0]['extra_content']['google']['thought_signature'] == 'opaque-signature'
    assert body['usage']['completion_tokens'] == 12
    assert body['usage']['prompt_tokens_details']['cached_tokens'] == 100
    assert ModelWire('test', 'openai_chat').unpack(body)[2] == [message]


@pytest.mark.parametrize('protocol', ['anthropic_messages', 'openai_chat'])
def test_truncated_but_terminated_stream_does_not_dispatch(monkeypatch, protocol):
    events = tool_events(protocol)
    if protocol == 'anthropic_messages':
        events[-2]['delta']['stop_reason'] = 'max_tokens'
    else:
        events[1]['choices'][0]['finish_reason'] = 'length'
    agent = agent_with_http(monkeypatch, protocol, [sse(events)])
    with pytest.raises(RuntimeError, match='no actions dispatched'):
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    assert agent.pending_action_calls is None
    assert next(e for e in agent.last_events if e['event'] == 'model_response')['provider_response']['usage']


def test_messages_output_limit_with_partial_json_preserves_usage_without_replay(monkeypatch):
    events = tool_events('anthropic_messages')
    events[3]['delta']['partial_json'] = '"sp'
    events[-2]['delta']['stop_reason'] = 'max_tokens'
    agent = agent_with_http(monkeypatch, 'anthropic_messages', [sse(events)])
    with pytest.raises(RuntimeError, match='output limit'):
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    assert agent.wire.session.post.call_count == 1
    body = next(e for e in agent.last_events if e['event'] == 'model_response')['provider_response']
    assert body['usage']['output_tokens'] == 10
    assert body['content'][0]['input'] == '{"key":"sp'
    assert agent.pending_action_calls is None


def anthropic_tool_events(name, arguments, *, stop_reason='tool_use'):
    """One finished anthropic tool_use block carrying raw argument text."""
    return [
        {'type': 'message_start', 'message': {'id': 'm1', 'role': 'assistant', 'content': [], 'usage': {'input_tokens': 1, 'output_tokens': 1}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'a1', 'name': name, 'input': {}}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': arguments}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': stop_reason}, 'usage': {'output_tokens': 5}},
        {'type': 'message_stop'},
    ]


def anthropic_parallel_tool_events(calls, *, stop_reason='tool_use'):
    """One finished anthropic message carrying several raw tool_use blocks."""
    events = [
        {'type': 'message_start', 'message': {'id': 'm1', 'role': 'assistant', 'content': [], 'usage': {'input_tokens': 1, 'output_tokens': 1}}},
    ]
    for index, (name, arguments) in enumerate(calls):
        events += [
            {'type': 'content_block_start', 'index': index, 'content_block': {'type': 'tool_use', 'id': f'a{index}', 'name': name, 'input': {}}},
            {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': arguments}},
            {'type': 'content_block_stop', 'index': index},
        ]
    events += [
        {'type': 'message_delta', 'delta': {'stop_reason': stop_reason}, 'usage': {'output_tokens': 5}},
        {'type': 'message_stop'},
    ]
    return events


def test_messages_unparsable_arguments_are_kept_verbatim_for_the_log():
    # Seen in the wild: deepseek-flash drops the array brackets of the only
    # array-typed parameter ({"times_s": 6.0, 6.5, 7.0} instead of [6.0, 6.5, 7.0]).
    raw = '{"times_s": 6.0, 6.5, 7.0}'
    body = collect_stream(sse(anthropic_tool_events('get_frames', raw)), 'anthropic_messages')
    assert body['content'][0]['input'] == raw
    assert body['stop_reason'] == 'tool_use'


def test_messages_unparsable_action_arguments_are_corrected_not_fatal(monkeypatch):
    bad = sse(anthropic_tool_events('computer_click', '{"x": 994, "y": 719,}'))
    good = sse(tool_events('anthropic_messages', key='space'))
    agent = agent_with_http(monkeypatch, 'anthropic_messages', [bad, good])
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1] == [
        {'action_type': 'PRESS', 'parameters': {'key': 'space'}}]
    assert agent.wire.session.post.call_count == 2  # same decision, one correction
    assert not [e for e in agent.last_events if e['event'] == 'model_error']
    error = next(e for e in agent.last_events if e['event'] == 'format_error')
    assert 'not valid JSON' in error['message'] and error['will_retry'] is True
    assert agent.pending_action_calls


def test_messages_unparsable_frame_arguments_are_corrected_not_an_error_result(monkeypatch):
    bad = sse(anthropic_tool_events('get_frames', '{"times_s": 6.0, 6.5, 7.0}'))
    good = sse(tool_events('anthropic_messages', key='space'))
    agent = agent_with_http(monkeypatch, 'anthropic_messages', [bad, good])
    query = Mock()
    agent.bind_frame_query(query)
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1] == [
        {'action_type': 'PRESS', 'parameters': {'key': 'space'}}]
    query.assert_not_called()  # nothing was executed, the model just re-asks
    result = next(e for e in agent.last_events if e['event'] == 'tool_result')
    # Bad frame arguments are the model's mistake, not a recording failure: the
    # result says so instead of reporting "error", and the correction is counted.
    assert result['result']['status'] == 'invalid_arguments'
    assert 'times_s' in result['result']['message']
    correction = next(e for e in agent.last_events if e['event'] == 'format_error')
    assert correction['correction'] == 1 and correction['will_retry'] is True
    assert 'times_s' in correction['message']
    assert agent.wire.session.post.call_count == 2


def test_three_bad_frame_argument_replies_in_one_decision_are_fatal(monkeypatch):
    bad = [sse(anthropic_tool_events('get_frames', '{"times_s": 6.0, 6.5, 7.0}')) for _ in range(3)]
    agent = agent_with_http(monkeypatch, 'anthropic_messages', bad)
    agent.bind_frame_query(Mock())
    with pytest.raises(AgentProtocolError):
        agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})
    corrections = [e for e in agent.last_events if e['event'] == 'format_error']
    assert [e['correction'] for e in corrections] == [1, 2, 3]
    assert [e['will_retry'] for e in corrections] == [True, True, False]
    assert agent.wire.session.post.call_count == 3  # same decision, three replies


def test_three_bad_frame_requests_in_one_reply_count_as_one_correction(monkeypatch):
    # Seen in the wild (C39): one reply splits a single query into three scalar calls.
    # That is one mistake, so it must cost one correction, not the whole budget.
    bad = sse(anthropic_parallel_tool_events([('get_frames', '{"times_s": 8.0}')] * 3))
    good = sse(tool_events('anthropic_messages', key='space'))
    agent = agent_with_http(monkeypatch, 'anthropic_messages', [bad, good])
    query = Mock()
    agent.bind_frame_query(query)
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1] == [
        {'action_type': 'PRESS', 'parameters': {'key': 'space'}}]
    query.assert_not_called()
    results = [e for e in agent.last_events if e['event'] == 'tool_result']
    assert [e['result']['status'] for e in results] == ['invalid_arguments'] * 3
    corrections = [e for e in agent.last_events if e['event'] == 'format_error']
    assert [e['correction'] for e in corrections] == [1]  # once per reply, not per call


def test_gateway_reused_indices_with_distinct_ids_remain_separate(monkeypatch):
    events = [
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'click-id', 'type': 'function', 'function': {'name': 'computer_click', 'arguments': '{"x":995,"y":670}'}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'wait-id', 'type': 'function', 'function': {'name': 'computer_wait', 'arguments': '{"duration_s":0.1}'}}]}}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]},
        '[DONE]',
    ]
    agent = agent_with_http(monkeypatch, 'openai_chat', [sse(events)])
    assert agent.predict('task', {'screenshot': b'png', 'task_time_s': 0})[1] == [
        {'action_type': 'CLICK', 'parameters': {'x': 995, 'y': 670}},
        {'action_type': 'WAIT', 'parameters': {'duration_s': 0.1}},
    ]
    assert [call['id'] for call in agent.pending_action_calls] == ['click-id', 'wait-id']


def test_reused_index_without_an_id_cannot_guess_fragment_owner():
    events = [
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'a', 'type': 'function', 'function': {'name': 'computer_click', 'arguments': '{'}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'b', 'type': 'function', 'function': {'name': 'computer_wait', 'arguments': '{'}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '"x":1}'}}]}}]},
        '[DONE]',
    ]
    with pytest.raises(RuntimeError, match='Ambiguous tool delta'):
        collect_stream(sse(events), 'openai_chat')


def test_reused_index_fragment_can_complete_the_only_unfinished_call():
    events = [
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'a', 'type': 'function', 'function': {'name': 'computer_click', 'arguments': '{"x":995,"y":724}'}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'b', 'type': 'function', 'function': {'name': 'computer_wait', 'arguments': ''}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '{"duration_s":0.1}'}}]}, 'finish_reason': 'tool_calls'}]},
        '[DONE]',
    ]
    body = collect_stream(sse(events), 'openai_chat')
    calls = body['choices'][0]['message']['tool_calls']
    assert [call['id'] for call in calls] == ['a', 'b']
    assert [json.loads(call['function']['arguments']) for call in calls] == [
        {'x': 995, 'y': 724}, {'duration_s': 0.1}]
