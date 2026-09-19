"""A model that breaks the action protocol must score 0, not vanish.

Infrastructure failures (HTTP, streaming, VM) still leave a task without a
valid score and are retried; a protocol violation is a model failure, so the
runner writes ``result.txt = 0`` (see ``_write_agent_failure_result``).
"""
import json
from unittest.mock import Mock

import pytest

from lib_run_realtime import _write_agent_failure_result
from mm_agents.realtime_agent import ModelWire, RealtimeAgent
from mm_agents.realtime_protocol import (
    AgentProtocolError,
    ForbiddenShortcutError,
)

TEST_SYSTEM_PROMPT = "Test-only realtime agent system prompt."
OBSERVATION = {"screenshot": b"png", "task_time_s": 1.0}


def text_only_reply():
    return {"content": [{"type": "text", "text": "I will consider it."}]}


def tool_use_reply(name, arguments):
    return {"content": [{"type": "tool_use", "id": "c1", "name": name, "input": arguments}]}


def test_forbidden_shortcut_is_a_protocol_violation():
    assert issubclass(ForbiddenShortcutError, AgentProtocolError)
    assert issubclass(AgentProtocolError, ValueError)


def test_three_text_only_replies_raise_a_protocol_error():
    wire = ModelWire("mock", "anthropic_messages")
    wire.request = Mock(return_value=text_only_reply())
    agent = RealtimeAgent(
        system_prompt_text=TEST_SYSTEM_PROMPT, sequence=True, frames=True, wire=wire
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
        system_prompt_text=TEST_SYSTEM_PROMPT, sequence=False, frames=False, wire=wire
    )
    with pytest.raises(ForbiddenShortcutError):
        agent.predict("task", dict(OBSERVATION))


def test_out_of_range_normalized_coordinates_raise_a_protocol_error():
    wire = ModelWire("mock", "anthropic_messages")
    wire.request = Mock(
        return_value=tool_use_reply("computer_click", {"x": 1500, "y": 400})
    )
    agent = RealtimeAgent(
        system_prompt_text=TEST_SYSTEM_PROMPT,
        sequence=False,
        frames=False,
        coordinate_system="normalized_0_1000",
        wire=wire,
    )
    with pytest.raises(AgentProtocolError) as error:
        agent.predict("task", dict(OBSERVATION))
    assert "native screen pixels are not accepted" in str(error.value)


def test_agent_failure_is_recorded_as_a_zero_score(tmp_path):
    example = {
        "benchmark_id": "b4",
        "source": "evaluation_examples/websites/realtime_gui_bench/games/b4/index.html",
    }
    result = _write_agent_failure_result(
        tmp_path, example, AgentProtocolError("Native tool use is required."), 7
    )
    assert result == 0.0
    assert (tmp_path / "result.txt").read_text() == "0.0\n"
    details = json.loads((tmp_path / "result.json").read_text())
    assert details["benchmark_id"] == "B4"
    assert details["task"] == "b4"
    assert details["status"] == "failed"
    assert details["termination_reason"] == "run_error"
    assert (details["pass_at_1"], details["pass_at_3"], details["result"]) == (0.0, 0.0, 0.0)
    assert details["decisions"] == 7
    assert details["agent_protocol_error"]["type"] == "AgentProtocolError"
