"""A model that breaks the action protocol must score 0, not vanish.

Infrastructure failures (HTTP, streaming, VM) still leave a task without a
valid score and are retried; a protocol violation is a model failure, so the
runner scores 0 through the normal evaluator. The one exception is an
unreadable page: that is an infrastructure condition (the page may even hold an
earlier attempt's pass), so the task is left without a score and re-run.
"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib_run_realtime import run_realtime_example, resume_completed_task
from mm_agents.realtime_agent import ModelWire, RealtimeAgent
from mm_agents.realtime_protocol import (
    AgentProtocolError,
    ForbiddenShortcutError,
    ModelOutputLimitError,
    validate_action,
)

TEST_SYSTEM_PROMPT = "Test-only realtime agent system prompt."
TEST_USER_PROMPT = "Test-only realtime agent user prompt."
OBSERVATION = {"screenshot": b"png", "task_time_s": 1.0}


def text_only_reply():
    return {"content": [{"type": "text", "text": "I will consider it."}]}


def tool_use_reply(name, arguments):
    return {"content": [{"type": "tool_use", "id": "c1", "name": name, "input": arguments}]}


def run_agent_failure(tmp_path, monkeypatch, evaluate, *, replies=None, audit=None):
    """Exercise the real runner and result writer with a controlled model reply."""
    monkeypatch.setattr('lib_run_realtime.time.sleep', lambda _: None)
    monkeypatch.setattr('lib_run_single.setup_logger', lambda *args: None)
    monkeypatch.setattr('lib_run_realtime.log_task_completion', lambda *args: None)
    # The audit runs before any score is written; keep this test offline.
    monkeypatch.setattr('mm_agents.realtime_auditor.audit_task',
                        audit or (lambda task_dir, result, **kwargs: (result, {'label': 'NOT_CHEAT'})))
    monkeypatch.setattr('lib_run_single._evaluate_with_details', evaluate)
    wire = ModelWire('mock', 'anthropic_messages')
    wire.request = Mock(side_effect=replies) if replies is not None else Mock(return_value=text_only_reply())
    agent = RealtimeAgent(system_prompt_text=TEST_SYSTEM_PROMPT,
                          user_prompt_text=TEST_USER_PROMPT,
                          sequence=True, frames=False, wire=wire)
    controller = SimpleNamespace(
        start_realtime_recording=Mock(return_value={}),
        last_observation_time=1, last_capture_interval=(0.9, 1.1),
        end_realtime_recording=Mock(),
    )
    env = SimpleNamespace(
        reset=Mock(), controller=controller,
        _get_obs=lambda: dict(OBSERVATION),
        step_sequence=Mock(return_value=(dict(OBSERVATION), 0, False, {})),
    )
    args = SimpleNamespace(
        result_dir=str(tmp_path), environment_ready_wait_s=0, recording_fragment_ms=100,
        model='mock', max_sequence_actions=100, max_frame_queries=0, evaluation_settle_s=0,
    )
    scores = []
    run_realtime_example(agent, env, {}, 10, 'task', args, str(tmp_path), scores)
    return scores


def test_protocol_violation_records_a_real_zero_when_the_page_is_readable(tmp_path, monkeypatch):
    evaluate = Mock(return_value=0.0)
    scores = run_agent_failure(tmp_path, monkeypatch, evaluate)
    assert scores == [0.0]
    assert (tmp_path / 'result.txt').read_text() == '0.0\n'
    details = json.loads((tmp_path / 'result.json').read_text())
    assert details['result'] == 0.0
    assert details['judge']['label'] == 'NOT_CHEAT'
    assert evaluate.call_args.kwargs['termination_reason'] == 'run_error'
    events = [json.loads(line) for line in (tmp_path / 'trajectory.jsonl').read_text().splitlines()]
    run_errors = [event for event in events if event['event'] == 'run_error']
    assert [event['type'] for event in run_errors] == ['AgentProtocolError']
    assert run_errors[0]['message'].startswith('Native tool use is required')


def test_protocol_violation_without_a_readable_page_leaves_no_score(tmp_path, monkeypatch):
    evaluate = Mock(side_effect=RuntimeError('the VM is gone'))
    scores = run_agent_failure(tmp_path, monkeypatch, evaluate)
    assert scores == []
    # No score at all: the resume re-runs and re-judges this task.
    assert not (tmp_path / 'result.txt').exists()
    assert not (tmp_path / 'result.json').exists()
    # The violation itself stays on record, so nothing about the model is lost.
    events = [json.loads(line) for line in (tmp_path / 'trajectory.jsonl').read_text().splitlines()]
    assert events[-1]['event'] == 'run_error'
    assert events[-1]['type'] == 'AgentProtocolError'
    stats = json.loads((tmp_path / 'agent_metrics.json').read_text())
    assert stats['termination_reason'] == 'run_error'
    assert stats['run_error']['type'] == 'AgentProtocolError'


def test_forbidden_shortcut_is_a_protocol_violation():
    assert issubclass(ForbiddenShortcutError, AgentProtocolError)
    assert issubclass(AgentProtocolError, ValueError)


def output_limit_reply():
    return {'stop_reason': 'max_tokens',
            'content': [{'type': 'tool_use', 'id': 'cutoff', 'name': 'computer_press', 'input': '{"key":"sp'}],
            'usage': {'input_tokens': 100, 'output_tokens': 128000}}


@pytest.mark.parametrize('page_score', [0.0, 1.0])
def test_output_limit_finishes_with_existing_page_score_and_resume_skips(tmp_path, monkeypatch, page_score):
    # A normal earlier action can already have won. The later truncated action
    # must not execute, and its failure must not erase the page's existing win.
    replies = [tool_use_reply('computer_press', {'key': 'space'}), output_limit_reply()]
    evaluate = Mock(return_value=page_score)
    scores = run_agent_failure(tmp_path, monkeypatch, evaluate, replies=replies)
    assert scores == [page_score]
    assert float((tmp_path / 'result.txt').read_text()) == page_score
    assert evaluate.call_args.kwargs['termination_reason'] == 'model_output_limit'
    metrics = json.loads((tmp_path / 'agent_metrics.json').read_text())
    assert metrics['executed_actions'] == 1
    assert metrics['model_requests'] == 2
    assert metrics['run_error']['type'] == 'ModelOutputLimitError'
    before = (tmp_path / 'trajectory.jsonl').read_bytes()
    assert resume_completed_task(tmp_path, {}, SimpleNamespace(), []) is True
    assert (tmp_path / 'trajectory.jsonl').read_bytes() == before


def test_output_limit_before_first_action_scores_zero_and_audit_retries_only_audit(tmp_path, monkeypatch):
    from mm_agents.realtime_auditor import AuditError
    audit = Mock(side_effect=AuditError('temporary judge outage'))
    scores = run_agent_failure(tmp_path, monkeypatch, Mock(return_value=0.0),
                               replies=[output_limit_reply()], audit=audit)
    assert scores == []
    assert not (tmp_path / 'result.txt').exists()
    assert json.loads((tmp_path / 'game_result.json').read_text())['result'] == 0.0
    metrics = json.loads((tmp_path / 'agent_metrics.json').read_text())
    assert metrics['executed_actions'] == 0
    assert metrics['model_requests'] == 1
    before = (tmp_path / 'trajectory.jsonl').read_bytes()
    assert resume_completed_task(tmp_path, {}, SimpleNamespace(), []) is True
    assert not (tmp_path / 'result.txt').exists()
    audit.side_effect = None
    audit.return_value = (0.0, {'label': 'NOT_CHEAT'})
    scores = []
    assert resume_completed_task(tmp_path, {}, SimpleNamespace(), scores) is True
    assert scores == [0.0]
    assert (tmp_path / 'result.txt').read_text() == '0.0\n'
    assert (tmp_path / 'trajectory.jsonl').read_bytes() == before


@pytest.mark.parametrize('error', [RuntimeError('HTTP 401 quota exhausted'),
                                  RuntimeError('response incomplete without an explicit output limit')])
def test_infrastructure_error_still_leaves_no_score(tmp_path, monkeypatch, error):
    evaluate = Mock()
    with pytest.raises(RuntimeError):
        run_agent_failure(tmp_path, monkeypatch, evaluate, replies=[error])
    evaluate.assert_not_called()
    assert not (tmp_path / 'game_result.json').exists()
    assert not (tmp_path / 'result.txt').exists()


@pytest.mark.parametrize('key', ['tab', 'enter', 'return', 'esc', 'escape'])
def test_focus_keys_are_blocked_with_a_correction_that_names_the_mistake(key):
    # No game maps these keys, but they move focus out of the page; Enter on the
    # address bar then reloads the page and voids the run (C39, C30 both died that
    # way). The message the model reads must describe that, not "refreshing".
    with pytest.raises(ForbiddenShortcutError) as error:
        validate_action({'action_type': 'PRESS', 'parameters': {'key': key}})
    assert 'move keyboard focus out of the game page' in str(error.value)
    with pytest.raises(ForbiddenShortcutError):
        validate_action({'action_type': 'HOTKEY', 'parameters': {'keys': ['ctrl', 'tab']}})


@pytest.mark.parametrize('key', ['Tab', 'Escape'])
def test_capitalised_focus_keys_are_rejected_by_the_schema(key):
    # The key enum is lowercase, so these never reach the blocked-key check; they
    # are recorded here so the two rejection paths stay distinguishable.
    with pytest.raises(ValueError) as error:
        validate_action({'action_type': 'PRESS', 'parameters': {'key': key}})
    assert not isinstance(error.value, ForbiddenShortcutError)


@pytest.mark.parametrize('key', ['space', 'r', 'e', 'j', 'd', 'f', 'k', 'up', 'down', 'left', 'right'])
def test_keys_the_games_actually_use_stay_allowed(key):
    assert validate_action({'action_type': 'PRESS', 'parameters': {'key': key}})


def test_three_text_only_replies_raise_a_protocol_error():
    wire = ModelWire("mock", "anthropic_messages")
    wire.request = Mock(return_value=text_only_reply())
    agent = RealtimeAgent(
        system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=True, frames=True, wire=wire
    )
    with pytest.raises(AgentProtocolError) as error:
        agent.predict("task", dict(OBSERVATION))
    assert "Native tool use is required" in str(error.value)
    # Two corrections are offered, the third reply ends the episode.
    assert wire.request.call_count == 3
    corrections = [
        event for event in agent.last_events if event["event"] == "format_error"
    ]
    assert [event["correction"] for event in corrections] == [1, 2, 3]


def test_a_forbidden_shortcut_also_raises_the_protocol_error():
    wire = ModelWire("mock", "anthropic_messages")
    wire.request = Mock(
        return_value=tool_use_reply("computer_press", {"key": "f5"})
    )
    agent = RealtimeAgent(
        system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=False, frames=False, wire=wire
    )
    with pytest.raises(ForbiddenShortcutError):
        agent.predict("task", dict(OBSERVATION))


def test_out_of_range_normalized_coordinates_raise_a_protocol_error():
    wire = ModelWire("mock", "anthropic_messages")
    wire.request = Mock(
        return_value=tool_use_reply("computer_click", {"x": 1500, "y": 400})
    )
    agent = RealtimeAgent(
        system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT,
        sequence=False,
        frames=False,
        coordinate_system="normalized_0_1000",
        wire=wire,
    )
    with pytest.raises(AgentProtocolError) as error:
        agent.predict("task", dict(OBSERVATION))
    assert "native screen pixels are not accepted" in str(error.value)


def test_a_forbidden_shortcut_correction_embeds_one_sentence_period():
    """The rejection text is a sentence already, so embedding it must not double the period."""
    wire = ModelWire("mock", "anthropic_messages")
    wire.request = Mock(
        return_value=tool_use_reply("computer_press", {"key": "f5"})
    )
    agent = RealtimeAgent(
        system_prompt_text=TEST_SYSTEM_PROMPT, user_prompt_text=TEST_USER_PROMPT, sequence=False, frames=False, wire=wire
    )
    with pytest.raises(ForbiddenShortcutError):
        agent.predict("task", dict(OBSERVATION))

    # Messages sent on the second request carry the correction for the first one.
    sent = json.dumps(wire.request.call_args_list[1].args[1])
    assert "is forbidden. Correct it; no action was executed." in sent
    assert "is forbidden.. No action in this sequence was executed." not in sent
    assert ".." not in sent
