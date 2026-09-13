import json

from lib_results_logger import update_realtime_gui_bench_summary
from lib_run_single import _evaluate_with_details


def _write_result(path, benchmark_id, pass_at_1, pass_at_3):
    path.mkdir(parents=True)
    (path / "result.json").write_text(
        json.dumps(
            {
                "benchmark_id": benchmark_id,
                "result": pass_at_3,
                "pass_at_1": pass_at_1,
                "pass_at_3": pass_at_3,
                "attempt_results": [bool(pass_at_3)],
                "status": "passed" if pass_at_3 else "running",
            }
        ),
        encoding="utf-8",
    )


def test_update_realtime_gui_bench_summary(tmp_path):
    domain_dir = (
        tmp_path
        / "pyautogui"
        / "screenshot"
        / "model"
        / "realtime_gui_bench"
    )
    first_task = domain_dir / "task-a1"
    _write_result(first_task, "A1", 1.0, 1.0)
    _write_result(domain_dir / "task-b1", "B1", 0.0, 1.0)

    update_realtime_gui_bench_summary(str(first_task))

    summary = json.loads(
        (tmp_path / "summary/realtime_gui_bench_metrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["overall"]["tasks"] == 69
    assert summary["overall"]["scored_tasks"] == 2
    assert summary["overall"]["unscored_tasks"] == 67
    assert summary["overall"]["pass_at_1"] == 0.5
    assert summary["overall"]["pass_at_3"] == 1.0
    assert summary["A"]["scored_tasks"] == 1
    assert summary["B"]["scored_tasks"] == 1
    assert [summary[category]["tasks"] for category in "ABCD"] == [22, 12, 17, 18]
    for category in ("overall", "A", "B", "C", "D"):
        counts = summary[category]
        assert counts["unscored_tasks"] == counts["tasks"] - counts["scored_tasks"]
        assert "error_tasks" not in counts


def test_summary_without_results_counts_all_tasks_as_unscored(tmp_path):
    task_dir = tmp_path / "computer_13/screenshot/model/realtime_gui_bench/task-a1"
    task_dir.mkdir(parents=True)

    update_realtime_gui_bench_summary(str(task_dir))

    summary = json.loads((tmp_path / "summary/realtime_gui_bench_metrics.json").read_text())
    for category in ("overall", "A", "B", "C", "D"):
        counts = summary[category]
        assert counts["scored_tasks"] == 0
        assert counts["unscored_tasks"] == counts["tasks"]
        assert counts["pass_at_1"] == counts["pass_at_3"] == 0.0
        assert "error_tasks" not in counts


def test_summary_zero_score_is_scored_not_unscored(tmp_path):
    task_dir = tmp_path / "computer_13/screenshot/model/realtime_gui_bench/task-a1"
    _write_result(task_dir, "A1", 0.0, 0.0)

    update_realtime_gui_bench_summary(str(task_dir))

    summary = json.loads((tmp_path / "summary/realtime_gui_bench_metrics.json").read_text())
    assert summary["overall"]["scored_tasks"] == 1
    assert summary["overall"]["unscored_tasks"] == 68
    assert summary["A"]["scored_tasks"] == 1
    assert summary["A"]["unscored_tasks"] == 21
    assert summary["overall"]["pass_at_1"] == summary["overall"]["pass_at_3"] == 0.0


def test_summary_includes_sparse_high_ids_and_ignores_removed_ids(tmp_path):
    domain = tmp_path / "computer_13/screenshot/model/realtime_gui_bench"
    _write_result(domain / "task-a46", "A46", 1.0, 1.0)
    _write_result(domain / "task-d37", "D37", 0.0, 1.0)
    _write_result(domain / "old-a2", "A2", 1.0, 1.0)
    update_realtime_gui_bench_summary(str(domain / "task-a46"))
    summary = json.loads((tmp_path / "summary/realtime_gui_bench_metrics.json").read_text())
    assert summary["overall"]["scored_tasks"] == 2
    assert summary["A"]["scored_tasks"] == summary["D"]["scored_tasks"] == 1


def test_evaluate_with_details_writes_sidecar_and_summary(tmp_path):
    task_dir = (
        tmp_path
        / "pyautogui"
        / "screenshot"
        / "model"
        / "realtime_gui_bench"
        / "task-a2"
    )
    task_dir.mkdir(parents=True)

    class Env:
        _evaluation_details = {"stale": True}

        def evaluate(self):
            assert self._evaluation_details is None
            self._evaluation_details = {
                "benchmark_id": "A2",
                "result": 1.0,
                "pass_at_1": 0.0,
                "pass_at_3": 1.0,
                "attempt_results": [False, True],
                "status": "passed",
            }
            return 1.0

    result = _evaluate_with_details(Env(), str(task_dir))

    assert result == 1.0
    details = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
    assert details["attempt_results"] == [False, True]
    summary_path = tmp_path / "summary/realtime_gui_bench_metrics.json"
    assert summary_path.is_file()


def test_raw_events_are_preserved_separately_from_derived_attempt_history(tmp_path):
    from desktop_env.evaluators.getters.realtime_gui import _normalize_realtime_gui_bench_state
    from desktop_env.evaluators.metrics.realtime_gui import realtime_gui_bench_result

    task_dir = tmp_path / "pyautogui/screenshot/model/realtime_gui_bench/task-a41"
    task_dir.mkdir(parents=True)
    state = {"task": "kitchen_order_match", "attempts": 1, "maxAttempts": 3,
             "passed": True, "status": "passed", "results": ["hit", "miss", "hit", "hit", "hit"]}

    class Env:
        def evaluate(self):
            self._evaluation_details = _normalize_realtime_gui_bench_state(state, "A41")
            return realtime_gui_bench_result(self._evaluation_details)

    assert _evaluate_with_details(Env(), str(task_dir)) == 1.0
    saved = json.loads((task_dir / "result.json").read_text())
    assert saved["pass_at_1"] == 0.0
    assert saved["pass_at_3"] == 1.0
    assert saved["attempt_results"] == [False, True]
    assert saved["raw_bench"] == state
    assert len(saved["raw_bench"]["results"]) == 5
