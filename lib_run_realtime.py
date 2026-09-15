"""Run one task with shared recording and comparable action budgets."""
import base64
from datetime import datetime, timezone
import json
import logging
import os
import time
from pathlib import Path

from lib_results_logger import log_task_completion
from mm_agents.realtime_protocol import FRAME_TOOL, ForbiddenShortcutError


def run_realtime_example(
    agent, env, example, max_steps, instruction, args, example_result_dir, scores
):
    from lib_run_single import _evaluate_with_details, setup_logger

    runtime_logger = setup_logger(example, example_result_dir)
    out = Path(example_result_dir)
    task_started_at = datetime.now(timezone.utc).isoformat()
    env.reset(task_config=example)
    agent.reset(runtime_logger)
    page_config = example.get("evaluator", {}).get("result", {})
    page_identity = None
    if page_config.get("type") == "realtime_gui_bench_state":
        from desktop_env.evaluators.getters.realtime_gui import (
            realtime_page_identity, verify_realtime_page_identity,
        )
        page_identity = realtime_page_identity(env, page_config)
    if getattr(args, "install_realtime_server", False):
        from scripts.python.install_realtime_server import install

        install(env.controller.http_server, password=args.client_password)
    time.sleep(args.environment_ready_wait_s)
    record = env.controller.start_realtime_recording(args.recording_fragment_ms)
    metadata = {
        "run_id": getattr(args, "run_id", None),
        "agent_started_at": getattr(args, "agent_started_at", None),
        "task_started_at": task_started_at,
        "variant": agent.variant,
        "sequence": agent.sequence,
        "frame_query": agent.frames,
        "action_space": "computer_13",
        "model": args.model,
        "agent_config": getattr(args, "realtime_config_path", None),
        "api_format": agent.wire.protocol,
        "api_key_env": getattr(agent.wire, "api_key_env", None),
        "tool_format": agent.tool_format,
        "implicit_action_sleep_s": 0,
        "environment_ready_wait_s": args.environment_ready_wait_s,
        "recording_fragment_ms": args.recording_fragment_ms,
        "recording": record,
        "page_identity": page_identity,
        "max_decision_rounds": max_steps,
        "max_sequence_actions": args.max_sequence_actions,
        "max_frame_queries_per_decision": args.max_frame_queries,
        "max_frame_queries_per_response": (
            None if agent.frames and agent.sequence else 1 if agent.frames else 0
        ),
        "frame_queries_unlimited": args.max_frame_queries == 0,
        "thinking_enabled": getattr(agent.wire, "thinking_enabled", False),
        "thinking_effort": getattr(agent.wire, "thinking_effort", None),
        "thinking_summary_requested": getattr(agent.wire, "thinking_summary", False),
        "instruction": instruction,
        "trajectory_file": "trajectory.jsonl",
        "trajectory_html_file": "trajectory.html",
        "system_prompt_file": "system_prompt.txt",
        "frame_tool": FRAME_TOOL if agent.frames else None,
        "model_log_version": 3,
        "clock": "First recorded X11 frame after environment preparation is t=0; screenshot time is measured in the VM.",
    }
    (out / "experiment.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (out / "system_prompt.txt").write_text(agent.system, encoding="utf-8")
    action_count = decision_count = query_count = event_count = 0
    execution_error = None
    run_error = None
    done = False

    def write_event(event, decision_id=None):
        # Persist every model and environment event in one chronological stream.
        nonlocal event_count
        event_count += 1
        record = {
            "event_index": event_count,
            "wall_time": datetime.now(timezone.utc).isoformat(),
            "decision_id": (
                decision_count if event.get("event") == "action_tool_result"
                else decision_count + 1
            ) if decision_id is None else decision_id,
            **event,
        }
        with (out / "trajectory.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def query(times_s):
        nonlocal query_count
        query_count += 1
        result = env.controller.get_frames(times_s)
        images = []
        # Only requested images are saved separately, not every recorded frame.
        for i, frame in enumerate(result.get("frames", [])):
            if frame.get("image"):
                image_file = f"query_{query_count}_{i}.png"
                (out / image_file).write_bytes(
                    base64.b64decode(frame["image"]["data"])
                )
                images.append({
                    "file": image_file,
                    "requested_time_s": frame.get("requested_time_s"),
                    "actual_time_s": frame.get("actual_time_s"),
                })
        write_event({"event": "frame_query_artifacts", "times_s": times_s, "images": images})
        return result

    agent.bind_frame_query(query if agent.frames else None)
    agent.bind_event_sink(write_event)
    try:
        obs = env._get_obs()
        obs["task_time_s"] = env.controller.last_observation_time
        (out / "initial_state.png").write_bytes(obs["screenshot"])
        obs["screenshot_file"] = "initial_state.png"
        write_event(
            {
                "event": "initial_observation",
                "observation": {
                    "screenshot_file": "initial_state.png",
                    "task_time_s": obs["task_time_s"],
                },
            },
            decision_id=0,
        )
        while not done and decision_count < max_steps:
            response, actions = agent.predict(instruction, obs)
            decision_count += 1
            write_event(
                {
                    "event": "action_submitted",
                    "actions": actions,
                    "response": response,
                },
                decision_id=decision_count,
            )
            dispatched = time.monotonic()
            try:
                if agent.sequence:
                    obs, reward, done, info = env.step_sequence(
                        actions
                    )
                else:
                    obs, reward, done, info = env.step(actions[0], 0)
            except Exception as exc:
                if isinstance(exc, ForbiddenShortcutError):
                    write_event(
                        {
                            "event": "action_rejected",
                            "reason": "forbidden_browser_shortcut",
                            "action": actions,
                            "timestamp_s": env.controller.last_observation_time,
                        },
                        decision_id=decision_count,
                    )
                execution_error = getattr(
                    exc, "details", {"status": "unknown", "message": type(exc).__name__}
                )
                action_count += len(execution_error.get("actions", []))
                write_event(
                    {
                        "event": "action_execution_error",
                        "actions": actions,
                        "execution_error": execution_error,
                    },
                    decision_id=decision_count,
                )
                raise
            action_count += len(actions)
            obs["task_time_s"] = env.controller.last_observation_time
            screenshot_file = f"step_{decision_count}.png"
            obs["screenshot_file"] = screenshot_file
            (out / screenshot_file).write_bytes(obs["screenshot"])
            write_event(
                {
                    "event": "action_executed",
                    "step_num": action_count,
                    "actions": actions,
                    "reward": reward,
                    "done": done,
                    "info": info,
                    "dispatch_to_observation_s": time.monotonic() - dispatched,
                    "observation": {
                        "task_time_s": obs["task_time_s"],
                        "capture_interval_s": env.controller.last_capture_interval,
                        "screenshot_file": screenshot_file,
                    },
                },
                decision_id=decision_count,
            )
            if page_identity is not None:
                verify_realtime_page_identity(env, page_config, page_identity)
            record_action_result = getattr(agent, "record_action_result", None)
            if record_action_result is not None:
                record_action_result(
                    actions,
                    reward=reward,
                    done=done,
                    info=info,
                )
        time.sleep(args.evaluation_settle_s)
        if page_identity is not None:
            verify_realtime_page_identity(env, page_config, page_identity)
        result = _evaluate_with_details(env, str(out), result_root=args.result_dir)
        write_event(
            {
                "event": "evaluation",
                "result": result,
                "details": getattr(env, "_evaluation_details", None),
            },
            decision_id=decision_count,
        )
        scores.append(result)
        (out / "result.txt").write_text(f"{result}\n")
        log_task_completion(example, result, str(out), args)
    except Exception as exc:
        run_error = {"type": type(exc).__name__, "message": str(exc)}
        write_event({"event": "run_error", **run_error}, decision_id=decision_count)
        raise
    finally:
        agent.bind_event_sink(None)
        (out / "agent_metrics.json").write_text(
            json.dumps(
                {
                    **agent.counters,
                    "run_id": getattr(args, "run_id", None),
                    "task_started_at": task_started_at,
                    "task_finished_at": datetime.now(timezone.utc).isoformat(),
                    "executed_actions": action_count,
                    "action_decisions": decision_count,
                    "trajectory_events": event_count,
                    "done": done,
                    "execution_error": execution_error,
                    "run_error": run_error,
                },
                indent=2,
            )
            + "\n"
        )
        try:
            env.controller.end_realtime_recording(str(out))
        finally:
            # Export after recording stops so visualization work never delays actions.
            # A report failure must not replace the task's score or original error.
            try:
                from lib_realtime_trajectory import render_trajectory
                render_trajectory(out / "trajectory.jsonl")
            except Exception as exc:
                logging.getLogger(__name__).warning("Cannot render trajectory HTML in %s: %s", out, exc)
