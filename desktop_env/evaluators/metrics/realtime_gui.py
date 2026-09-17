from typing import Any


def realtime_gui_bench_result(result: Any) -> float:
    """Return the game-provided pass@3 score after validating the result shape."""

    if not isinstance(result, dict):
        raise ValueError("RealtimeGUI-Bench result must be an object")
    required = {
        "benchmark_id",
        "protocol_version",
        "task",
        "max_attempts",
        "passed",
        "result",
        "pass_at_1",
        "pass_at_3",
        "attempts_completed",
        "status",
    }
    missing = sorted(required.difference(result))
    if missing:
        raise ValueError(
            f"RealtimeGUI-Bench result is missing fields: {', '.join(missing)}"
        )
    if result["protocol_version"] != "realtime-gui-bench/1.1":
        raise ValueError("RealtimeGUI-Bench protocol_version is invalid")
    if not isinstance(result["task"], str) or not result["task"].strip():
        raise ValueError("RealtimeGUI-Bench task must be a non-empty string")
    if type(result["max_attempts"]) is not int or result["max_attempts"] != 3:
        raise ValueError("RealtimeGUI-Bench max_attempts must equal 3")
    if type(result["passed"]) is not bool:
        raise ValueError("RealtimeGUI-Bench passed must be boolean")
    for field in ("result", "pass_at_1", "pass_at_3"):
        value = result[field]
        if type(value) not in {int, float} or value not in {0.0, 1.0}:
            raise ValueError(f"RealtimeGUI-Bench {field} must be 0.0 or 1.0")
    attempts_completed = result["attempts_completed"]
    if type(attempts_completed) is not int or not 0 <= attempts_completed <= 3:
        raise ValueError("RealtimeGUI-Bench attempts_completed must be an integer from 0 to 3")
    score = result["result"]
    if result["pass_at_3"] != score:
        raise ValueError("RealtimeGUI-Bench result must equal game-provided pass_at_3")
    if result["status"] not in {"ready", "running", "passed", "failed"}:
        raise ValueError("RealtimeGUI-Bench status is invalid")
    if result["passed"] != (result["pass_at_3"] == 1.0):
        raise ValueError("RealtimeGUI-Bench passed must equal pass_at_3")
    status = result["status"]
    attempts_completed = result["attempts_completed"]
    if status == "ready" and (
        attempts_completed != 0
        or result["passed"]
        or result["pass_at_1"] != 0.0
        or result["pass_at_3"] != 0.0
    ):
        raise ValueError("RealtimeGUI-Bench ready state is inconsistent")
    if status == "running" and (
        attempts_completed >= 3
        or result["passed"]
        or result["pass_at_1"] != 0.0
        or result["pass_at_3"] != 0.0
    ):
        raise ValueError("RealtimeGUI-Bench running state is inconsistent")
    if status == "passed" and (
        not result["passed"] or result["pass_at_3"] != 1.0 or attempts_completed < 1
    ):
        raise ValueError("RealtimeGUI-Bench passed state is inconsistent")
    if status == "failed" and (
        result["passed"]
        or result["pass_at_1"] != 0.0
        or result["pass_at_3"] != 0.0
        or attempts_completed != 3
    ):
        raise ValueError("RealtimeGUI-Bench failed state is inconsistent")
    if result["pass_at_1"] == 1.0 and attempts_completed != 1:
        raise ValueError("pass_at_1=1 requires first-attempt completion")
    return float(score)
