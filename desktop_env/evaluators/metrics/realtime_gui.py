import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MILLISECONDS_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*ms\s*$", re.IGNORECASE)
_NONNEGATIVE_NUMBER_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")


@lru_cache(maxsize=None)
def _load_histogram(distribution_path: str) -> Tuple[List[Dict[str, int]], int]:
    path = Path(distribution_path)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path

    payload = json.loads(path.read_text(encoding="utf-8"))
    bins = payload.get("bins")
    if not isinstance(bins, list) or not bins:
        raise ValueError(f"Human Benchmark distribution has no bins: {path}")

    normalized_bins = []
    for item in bins:
        bin_id = int(item["id"])
        count = int(item["count"])
        if count < 0:
            raise ValueError(
                f"Human Benchmark distribution contains a negative count: {path}"
            )
        normalized_bins.append({"id": bin_id, "count": count})

    normalized_bins.sort(key=lambda item: item["id"])
    total_count = sum(item["count"] for item in normalized_bins)
    if total_count <= 0:
        raise ValueError(f"Human Benchmark distribution has no samples: {path}")

    declared_total = payload.get("total_count")
    if declared_total is not None and int(declared_total) != total_count:
        raise ValueError(
            f"Human Benchmark distribution total_count mismatch: "
            f"declared={declared_total}, calculated={total_count}"
        )

    return normalized_bins, total_count


def _humanbenchmark_lower_is_better_percentile(
    result: Any,
    distribution_path: str,
    result_key: str,
) -> float:
    if not isinstance(result, dict):
        return 0.0

    raw_value = result.get(result_key)
    if not isinstance(raw_value, str):
        return 0.0

    match = _MILLISECONDS_PATTERN.fullmatch(raw_value)
    if not match:
        return 0.0

    score_ms = float(match.group(1))
    bins, total_count = _load_histogram(distribution_path)
    faster_count = sum(item["count"] for item in bins if score_ms > item["id"])
    percentile = 1.0 - faster_count / total_count
    return max(0.0, min(1.0, percentile))


def humanbenchmark_reaction_time_percentile(
    result: Any,
    distribution_path: str,
    result_key: str = "average_ms",
) -> float:
    """Convert a completed Human Benchmark reaction-time result to a percentile.

    The Human Benchmark test only renders ``view-score`` after five valid trials.
    The result getter therefore returns an empty value for incomplete tests.  For
    completed tests, lower reaction times are better, matching the website's
    bundled percentile calculation.
    """

    return _humanbenchmark_lower_is_better_percentile(
        result, distribution_path, result_key
    )


def humanbenchmark_aim_percentile(
    result: Any,
    distribution_path: str,
    result_key: str = "average_ms",
) -> float:
    """Convert a completed Human Benchmark Aim Trainer result to a percentile.

    The final average is only present after all 30 scored targets have been hit.
    Lower average times are better, matching the website's bundled calculation.
    """

    return _humanbenchmark_lower_is_better_percentile(
        result, distribution_path, result_key
    )


def score_ratio_to_threshold(
    result: Any,
    threshold: float,
    result_key: str = "score",
) -> float:
    """Normalize a non-negative numeric page score to a capped 0–1 value."""

    if threshold <= 0:
        raise ValueError("threshold must be positive")
    if not isinstance(result, dict):
        return 0.0

    raw_value = result.get(result_key)
    if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
        score = float(raw_value)
    elif isinstance(raw_value, str):
        match = _NONNEGATIVE_NUMBER_PATTERN.fullmatch(raw_value)
        if not match:
            return 0.0
        score = float(match.group(1))
    else:
        return 0.0

    return max(0.0, min(score / float(threshold), 1.0))


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
