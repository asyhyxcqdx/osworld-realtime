import json
from pathlib import Path

import pytest

from desktop_env.evaluators.metrics.realtime_gui import (
    humanbenchmark_aim_percentile,
    humanbenchmark_reaction_time_percentile,
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
