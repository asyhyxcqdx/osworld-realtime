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

from lib_run_realtime import run_realtime_example
from mm_agents.realtime_agent import ModelWire, RealtimeAgent
from mm_agents.realtime_protocol import (
    AgentProtocolError,
    ForbiddenShortcutError,
    validate_action,
)

TEST_SYSTEM_PROMPT = "Test-only realtime agent system prompt."
TEST_USER_PROMPT = "Test-only realtime agent user prompt."
OBSERVATION = {"screenshot": b"png", "task_time_s": 1.0}


def text_only_reply():
    return {"content": [{"type": "text", "text": "I will consider it."}]}


def tool_use_reply(name, arguments):
    return {"content": [{"type": "tool_use", "id": "c1", "name": name, "input": arguments}]}


def run_text_only_violation(tmp_path, monkeypatch, evaluate):
    """Drive one episode that ends in AgentProtocolError, then return its dir."""
    monkeypatch.setattr('lib_run_realtime.time.sleep', lambda _: None)
    monkeypatch.setattr('lib_run_single.setup_logger', lambda *args: None)
    monkeypatch.setattr('lib_run_realtime.log_task_completion', lambda *args: None)
    # The audit runs before any score is written; keep this test offline.
    monkeypatch.setattr('mm_agents.realtime_auditor.audit_task',
                        lambda task_dir, result, **kwargs: (result, {'label': 'NOT_CHEAT'}))
    monkeypatch.setattr('lib_run_single._evaluate_with_details', evaluate)
    wire = ModelWire('mock', 'anthropic_messages')
    wire.request = Mock(return_value=text_only_reply())
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
    run_realtime_example(agent, env, {}, 1, 'task', args, str(tmp_path), scores)
    return scores


def test_protocol_violation_records_a_real_zero_when_the_page_is_readable(tmp_path, monkeypatch):
    evaluate = Mock(return_value=0.0)
    scores = run_text_only_violation(tmp_path, monkeypatch, evaluate)
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
    scores = run_text_only_violation(tmp_path, monkeypatch, evaluate)
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
