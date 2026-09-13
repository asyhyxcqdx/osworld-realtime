"""Run one task with shared recording and comparable action budgets."""
import base64
from datetime import datetime, timezone
import json
import os
import time
from pathlib import Path

from lib_results_logger import log_task_completion
from mm_agents.realtime_protocol import FRAME_TOOL


def run_realtime_example(
    agent, env, example, max_steps, instruction, args, example_result_dir, scores
):
    from lib_run_single import _evaluate_with_details, setup_logger

    runtime_logger = setup_logger(example, example_result_dir)
    out = Path(example_result_dir)
    task_started_at = datetime.now(timezone.utc).isoformat()
    env.reset(task_config=example)
    agent.reset(runtime_logger)
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
        "sequence": agent.mode.sequence,
        "frame_query": agent.mode.frames,
        "action_space": "computer_13",
        "model": args.model,
        "api_format": agent.wire.protocol,
        "tool_format": agent.tool_format,
        "pause_s": args.sleep_after_execution,
        "environment_ready_wait_s": args.environment_ready_wait_s,
        "recording_fragment_ms": args.recording_fragment_ms,
        "recording": record,
        "max_actions": max_steps,
        "max_sequence_actions": args.max_sequence_actions,
        "max_frame_queries_per_decision": args.max_frame_queries,
        "frame_queries_unlimited": args.max_frame_queries == 0,
        "thinking_summary_requested": getattr(agent.wire, "thinking_summary", False),
        "instruction": instruction,
        "system_prompt_file": "system_prompt.txt",
        "frame_tool": FRAME_TOOL if agent.mode.frames else None,
        "model_log_version": 2,
        "clock": "First recorded X11 frame after environment preparation is t=0; screenshot time is measured in the VM.",
    }
    (out / "experiment.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (out / "system_prompt.txt").write_text(agent.system, encoding="utf-8")
    action_count = decision_count = query_count = 0
    execution_error = None
    done = False

    def write_event(event):
        # Persist each request/response immediately, including queries preceding
        # an error or interruption, instead of waiting for an action decision.
        with (out / "model_calls.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"decision": decision_count + 1, **event}, ensure_ascii=False) + "\n")

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

    agent.bind_frame_query(query if agent.mode.frames else None)
    agent.bind_event_sink(write_event)
    try:
        obs = env._get_obs()
        obs["task_time_s"] = env.controller.last_observation_time
        (out / "initial_state.png").write_bytes(obs["screenshot"])
        obs["screenshot_file"] = "initial_state.png"
        while not done and action_count < max_steps:
            obs["remaining_actions"] = max_steps - action_count
            response, actions = agent.predict(instruction, obs)
            decision_count += 1
            dispatched = time.monotonic()
            try:
                if agent.mode.sequence:
                    obs, reward, done, info = env.step_sequence(
                        actions, args.sleep_after_execution
                    )
                else:
                    obs, reward, done, info = env.step(
                        actions[0], args.sleep_after_execution
                    )
            except Exception as exc:
                execution_error = getattr(
                    exc, "details", {"status": "unknown", "message": type(exc).__name__}
                )
                action_count += len(execution_error.get("actions", []))
                with (out / "traj.jsonl").open("a") as f:
                    f.write(
                        json.dumps(
                            {
                                "decision_num": decision_count,
                                "planned_actions": actions,
                                "execution_error": execution_error,
                            }
                        )
                        + "\n"
                    )
                raise
            action_count += len(actions)
            obs["task_time_s"] = env.controller.last_observation_time
            screenshot_file = f"step_{decision_count}.png"
            obs["screenshot_file"] = screenshot_file
            (out / screenshot_file).write_bytes(obs["screenshot"])
            with (out / "traj.jsonl").open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "step_num": action_count,
                            "decision_num": decision_count,
                            "action": actions[0] if len(actions) == 1 else actions,
                            "response": response,
                            "reward": reward,
                            "done": done,
                            "info": info,
                            "dispatch_to_observation_s": time.monotonic() - dispatched,
                            "observation_time_s": obs["task_time_s"],
                            "capture_interval_s": env.controller.last_capture_interval,
                            "screenshot_file": screenshot_file,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        time.sleep(args.evaluation_settle_s)
        result = _evaluate_with_details(env, str(out), result_root=args.result_dir)
        scores.append(result)
        (out / "result.txt").write_text(f"{result}\n")
        log_task_completion(example, result, str(out), args)
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
                    "done": done,
                    "execution_error": execution_error,
                },
                indent=2,
            )
            + "\n"
        )
        env.controller.end_realtime_recording(str(out))
