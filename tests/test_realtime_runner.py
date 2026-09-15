import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, Response

from desktop_env.server.realtime import LiveRecording, register_realtime
from lib_run_realtime import run_realtime_example
CAPABILITIES = {
    "agent1": SimpleNamespace(sequence=False, frames=False),
    "agent2": SimpleNamespace(sequence=True, frames=False),
    "agent3": SimpleNamespace(sequence=False, frames=True),
    "agent4": SimpleNamespace(sequence=True, frames=True),
}


@pytest.mark.parametrize("variant", list(CAPABILITIES))
@pytest.mark.parametrize("export_fails", [False, True])
def test_runner_counts_rounds_and_actions_separately_and_cleans_recording(
    tmp_path, monkeypatch, variant, export_fails
):
    monkeypatch.setattr("lib_run_realtime.time.sleep", lambda _: None)
    monkeypatch.setattr("lib_run_single.setup_logger", lambda *args: None)
    monkeypatch.setattr("lib_run_single._evaluate_with_details", lambda *args, **kwargs: 1)
    monkeypatch.setattr("lib_run_realtime.log_task_completion", lambda *args: None)
    if export_fails:
        monkeypatch.setattr("lib_realtime_trajectory.render_trajectory", Mock(side_effect=RuntimeError('HTML export failed')))
    mode = CAPABILITIES[variant]
    controller = SimpleNamespace(
        start_realtime_recording=Mock(return_value={"task_time_s": 0}),
        last_observation_time=0.1,
        last_capture_interval=(0.09, 0.11),
        end_realtime_recording=Mock(),
    )
    observation = {"screenshot": b"png"}
    env = SimpleNamespace(
        controller=controller,
        reset=Mock(),
        _get_obs=Mock(return_value=observation),
        step=Mock(return_value=(observation, 0, True, {})),
        step_sequence=Mock(return_value=(observation, 0, True, {})),
    )
    actions = (
        [{"action_type": "WAIT"}, {"action_type": "DONE"}]
        if mode.sequence
        else [{"action_type": "DONE"}]
    )
    agent = SimpleNamespace(
        reset=Mock(),
        variant=variant,
        mode=mode,
        sequence=mode.sequence,
        frames=mode.frames,
        bind_frame_query=Mock(),
        bind_event_sink=Mock(),
        system="test system prompt",
        wire=SimpleNamespace(protocol="anthropic_messages"),
        tool_format="native",
        predict=Mock(return_value=(json.dumps(actions), actions)),
        counters={"model_requests": 3, "tool_calls": 2},
        last_events=[],
    )
    args = SimpleNamespace(
        result_dir=str(tmp_path),
        environment_ready_wait_s=60,
        recording_fragment_ms=100,
        sleep_after_execution=0,
        model="test",
        max_sequence_actions=16,
        max_frame_queries=4,
        evaluation_settle_s=20,
    )
    scores = []
    run_realtime_example(agent, env, {}, 5, "task", args, str(tmp_path), scores)
    assert scores == [1]
    assert env.step.call_count == (0 if mode.sequence else 1)
    assert env.step_sequence.call_count == (1 if mode.sequence else 0)
    assert controller.end_realtime_recording.call_count == 1
    stats = json.loads((tmp_path / "agent_metrics.json").read_text())
    assert stats["executed_actions"] == len(actions)
    assert stats["action_decisions"] == 1
    assert (tmp_path / "trajectory.html").exists() is not export_fails


def test_model_events_are_saved_before_next_request_and_survive_api_failure(tmp_path, monkeypatch):
    from mm_agents.realtime_agent import ModelWire, RealtimeAgent

    monkeypatch.setattr("lib_run_realtime.time.sleep", lambda _: None)
    monkeypatch.setattr("lib_run_single.setup_logger", lambda *args: None)
    controller = SimpleNamespace(
        start_realtime_recording=Mock(return_value={"task_time_s": 0}),
        last_observation_time=0.1,
        end_realtime_recording=Mock(),
        get_frames=Mock(return_value={"frames": [{
            "status": "ok", "requested_time_s": 0,
            "actual_time_s": 0, "image": {"type": "base64", "media_type": "image/png", "data": "cG5n"},
        }]}),
    )
    env = SimpleNamespace(controller=controller, reset=Mock(), _get_obs=lambda: {"screenshot": b"png"})
    wire = ModelWire("mock", "anthropic_messages")
    calls = []

    def request(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return {"content": [
                {"type": "thinking", "thinking": "Need the earlier image.", "signature": "signature"},
                {"type": "tool_use", "id": "q1", "name": "get_frames", "input": {"times_s": [0]}},
            ]}
        # These records must be visible while predict() is still running.
        events = [json.loads(line) for line in (tmp_path / "trajectory.jsonl").read_text().splitlines()]
        response = next(e for e in events if e["event"] == "model_response")
        assert response["reasoning"] == ["Need the earlier image."]
        artifacts = next(e for e in events if e["event"] == "frame_query_artifacts")
        assert (tmp_path / artifacts["images"][0]["file"]).read_bytes() == b"png"
        raise RuntimeError("Model API HTTP 503: test failure")

    wire.request = request
    agent = RealtimeAgent(variant="agent3", sequence=False, frames=True, wire=wire)
    args = SimpleNamespace(
        result_dir=str(tmp_path), environment_ready_wait_s=60, recording_fragment_ms=100,
        sleep_after_execution=0, model="mock", max_sequence_actions=16,
        max_frame_queries=0, evaluation_settle_s=20,
    )
    with pytest.raises(RuntimeError, match="HTTP 503"):
        run_realtime_example(agent, env, {}, 15, "task", args, str(tmp_path), [])
    events = [json.loads(line) for line in (tmp_path / "trajectory.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events].count("model_response") == 1
    assert events[-2]["event"] == "model_error"
    assert events[-1]["event"] == "run_error"
    assert all(
        e["decision_id"] == 1
        for e in events
        if e["event"] not in {"initial_observation", "run_error"}
    )
    assert events[0]["observation"]["screenshot_file"] == "initial_state.png"
    assert (tmp_path / "system_prompt.txt").read_text() == agent.system
    assert json.loads((tmp_path / "experiment.json").read_text())["frame_queries_unlimited"] is True
    assert agent.event_sink is None
    controller.end_realtime_recording.assert_called_once_with(str(tmp_path))
    assert (tmp_path / 'trajectory.html').exists()


def fake_backend():
    app = Flask(__name__)
    gui = SimpleNamespace(
        PAUSE=0, FAILSAFE=True, keyUp=Mock(), mouseUp=Mock(), events=[]
    )
    capture = Mock(return_value=Response(b"png", mimetype="image/png"))
    register_realtime(app, capture, gui)
    recorder = app.extensions["osworld_realtime"]
    recorder.session_id = "s"
    recorder.elapsed = Mock(side_effect=lambda: 1.0)
    return app.test_client(), gui, capture


def test_task_clock_uses_the_same_clock_as_capture_pts(monkeypatch):
    recording = LiveRecording()
    recording.origin_wall = 1000
    monkeypatch.setattr("desktop_env.server.realtime.time.time", lambda: 1010.25)
    monkeypatch.setattr("desktop_env.server.realtime.time.monotonic", lambda: 9.5)
    assert recording.elapsed() == 10.25


def test_vm_sequence_has_no_intermediate_observation_or_implicit_sleep(monkeypatch):
    client, gui, capture = fake_backend()
    sleep = Mock()
    monkeypatch.setattr("desktop_env.server.realtime.time.sleep", sleep)
    groups = [
        {
            "action": {"action_type": "PRESS", "parameters": {"key": "space"}},
            "commands": ['pyautogui.events.append("a")'],
        },
        {"action": {"action_type": "WAIT", "parameters": {"duration_s": 0.5}}, "commands": []},
        {
            "action": {"action_type": "PRESS", "parameters": {"key": "space"}},
            "commands": ['pyautogui.events.append("b")'],
        },
    ]
    response = client.post(
        "/realtime/sequence", json={"session_id": "s", "groups": groups}
    )
    assert response.status_code == 200
    assert gui.events == ["a", "b"]
    sleep.assert_called_once_with(0.5)
    assert capture.call_count == 0
    assert gui.PAUSE == 0 and gui.FAILSAFE is True
    assert len(response.json["actions"]) == 3


def test_vm_rejects_bad_sequence_before_first_action():
    client, gui, capture = fake_backend()
    response = client.post(
        "/realtime/sequence",
        json={
            "session_id": "s",
            "groups": [
                {"action": {"action_type": "DONE"}, "commands": []},
                {
                    "action": {"action_type": "PRESS"},
                    "commands": ['pyautogui.events.append("bad")'],
                },
            ],
            "pause": 0,
        },
    )
    assert response.status_code == 400
    assert gui.events == []


def test_stale_session_cannot_query_another_tasks_frames():
    client, _, _ = fake_backend()
    response = client.post(
        "/realtime/frames", json={"session_id": "previous", "times_s": [0]}
    )
    assert response.status_code == 400


def test_page_reload_saves_execution_evidence_but_never_scores(tmp_path, monkeypatch):
    from mm_agents.realtime_agent import ModelWire, RealtimeAgent
    from desktop_env.evaluators.getters import realtime_gui
    monkeypatch.setattr('lib_run_realtime.time.sleep', lambda _: None)
    monkeypatch.setattr('lib_run_single.setup_logger', lambda *args: None)
    score = Mock()
    monkeypatch.setattr('lib_run_single._evaluate_with_details', score)
    monkeypatch.setattr(realtime_gui, 'realtime_page_identity', lambda *args: {'target_id': 'game'})
    monkeypatch.setattr(realtime_gui, 'verify_realtime_page_identity', Mock(side_effect=RuntimeError('reloaded')))
    wire = ModelWire('mock', 'anthropic_messages')
    wire.request = Mock(return_value={'content': [{'type': 'tool_use', 'id': 'd', 'name': 'computer_done', 'input': {}}]})
    agent = RealtimeAgent(sequence=True, frames=False, wire=wire)
    obs = {'screenshot': b'png'}
    controller = SimpleNamespace(start_realtime_recording=Mock(return_value={}),
        last_observation_time=1, last_capture_interval=(.9, 1.1), end_realtime_recording=Mock())
    env = SimpleNamespace(reset=Mock(), controller=controller, _get_obs=lambda: dict(obs),
        step_sequence=Mock(return_value=(dict(obs), 0, True, {})))
    args = SimpleNamespace(result_dir=str(tmp_path), environment_ready_wait_s=0,
        recording_fragment_ms=100, model='mock', max_sequence_actions=100,
        max_frame_queries=0, evaluation_settle_s=0)
    with pytest.raises(RuntimeError, match='reloaded'):
        run_realtime_example(agent, env, {'evaluator': {'result': {'type': 'realtime_gui_bench_state'}}},
                             1, 'task', args, str(tmp_path), [])
    score.assert_not_called()
    controller.end_realtime_recording.assert_called_once()
    assert (tmp_path / 'step_1.png').exists()
    events = [json.loads(l) for l in (tmp_path / 'trajectory.jsonl').read_text().splitlines()]
    assert any(e['event'] == 'action_executed' for e in events)
    assert events[-1]['event'] == 'run_error'
    assert not (tmp_path / 'result.txt').exists()
