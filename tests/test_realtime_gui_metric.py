import json
from pathlib import Path

import pytest

from desktop_env.evaluators.metrics.realtime_gui import (
    humanbenchmark_aim_percentile,
    humanbenchmark_reaction_time_percentile,
    realtime_gui_bench_result,
    score_ratio_to_threshold,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_AIM_DISTRIBUTION = (
    _PROJECT_ROOT
    / "evaluation_examples/reference_files/aim_trainer/"
    "humanbenchmark_distribution_2026-08-24.json"
)


def test_aim_percentile_requires_completed_result():
    assert humanbenchmark_aim_percentile({}, str(_AIM_DISTRIBUTION)) == 0.0
    assert (
        humanbenchmark_aim_percentile(
            {"remaining": "Remaining: 1"}, str(_AIM_DISTRIBUTION)
        )
        == 0.0
    )


def test_aim_percentile_matches_bundled_histogram():
    score = humanbenchmark_aim_percentile(
        {"average_ms": "302ms"}, str(_AIM_DISTRIBUTION)
    )
    assert score == pytest.approx(0.942692000245957)


def test_aim_distribution_total_is_self_consistent():
    payload = json.loads(_AIM_DISTRIBUTION.read_text(encoding="utf-8"))
    assert sum(item["count"] for item in payload["bins"]) == payload["total_count"]


def test_reaction_time_metric_still_accepts_completed_result():
    distribution = (
        _PROJECT_ROOT
        / "evaluation_examples/reference_files/reaction_time/"
        "humanbenchmark_distribution_2026-08-24.json"
    )
    assert humanbenchmark_reaction_time_percentile(
        {"average_ms": "481ms"}, str(distribution)
    ) == pytest.approx(0.013462080631519546)


@pytest.mark.parametrize(
    ("raw_score", "expected"),
    [
        ("0", 0.0),
        ("1", 0.1),
        ("5", 0.5),
        ("10", 1.0),
        ("12", 1.0),
    ],
)
def test_score_ratio_to_threshold(raw_score, expected):
    assert score_ratio_to_threshold(
        {"score": raw_score}, threshold=10
    ) == pytest.approx(expected)


def test_score_ratio_rejects_missing_or_invalid_score():
    assert score_ratio_to_threshold({}, threshold=10) == 0.0
    assert score_ratio_to_threshold({"score": "Game Over"}, threshold=10) == 0.0


def test_realtime_gui_bench_result_returns_pass_at_3():
    details = {
        "benchmark_id": "A2",
        "result": 1.0,
        "pass_at_1": 0.0,
        "pass_at_3": 1.0,
        "attempt_results": [False, True],
        "status": "passed",
    }

    assert realtime_gui_bench_result(details) == 1.0


def test_realtime_gui_bench_result_rejects_disagreement():
    details = {
        "benchmark_id": "A2",
        "result": 0.0,
        "pass_at_1": 0.0,
        "pass_at_3": 1.0,
        "attempt_results": [False, True],
        "status": "passed",
    }

    with pytest.raises(ValueError):
        realtime_gui_bench_result(details)


@pytest.mark.parametrize("change", [
    {"pass_at_1": 1.0}, {"pass_at_3": "1"}, {"result": True},
    {"status": "running"}, {"attempt_results": [True, False]},
    {"attempt_results": [True, True]}, {"attempt_results": [False] * 4},
    {"attempt_results": ["miss", "hit"]}, {"attempt_results": None},
])
def test_bench_metric_requires_consistent_derived_scores(change):
    details = {"benchmark_id": "A41", "result": 1.0, "pass_at_1": 0.0,
               "pass_at_3": 1.0, "attempt_results": [False, True], "status": "passed"}
    details.update(change)
    with pytest.raises(ValueError):
        realtime_gui_bench_result(details)


@pytest.mark.parametrize("history,status", [([], "running"), ([False], "running"), ([False, False], "running"), ([False] * 3, "failed")])
def test_bench_metric_scores_incomplete_or_exhausted_game_zero(history, status):
    details = {"benchmark_id": "A41", "result": 0.0, "pass_at_1": 0.0,
               "pass_at_3": 0.0, "attempt_results": history, "status": status,
               "raw_bench": {"results": ["hit"] * 8}}
    assert realtime_gui_bench_result(details) == 0.0
