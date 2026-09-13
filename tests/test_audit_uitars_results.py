import json
from pathlib import Path

import pytest

from audit_uitars_results import (
    audit_tasks,
    load_tasks,
    parse_score,
    print_report,
)


def write_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


@pytest.mark.parametrize("value", ["", "nan", "inf", "-0.01", "1.01", "score"])
def test_parse_score_rejects_invalid_values(tmpdir, value):
    tmp_path = Path(str(tmpdir))
    result_path = tmp_path / "result.txt"
    result_path.write_text(value, encoding="utf-8")

    score, error = parse_score(result_path)

    assert score is None
    assert error


def test_audit_reports_strict_failures_with_global_shards(tmpdir):
    tmp_path = Path(str(tmpdir))
    meta_path = tmp_path / "test_all.json"
    metadata = {
        "chrome": ["valid", "missing", "nonfinite", "duplicate"],
        "os": ["client-failure"],
    }
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    tasks = load_tasks(meta_path, expected_total=5)
    result_root = tmp_path / "results"

    write_text(result_root / "chrome" / "valid" / "result.txt", "0.25\n")
    write_text(
        result_root / "chrome" / "missing" / "traj.jsonl",
        json.dumps({"Error": "connection reset by peer"}) + "\n",
    )
    write_text(result_root / "chrome" / "nonfinite" / "result.txt", "nan\n")
    write_text(result_root / "chrome" / "duplicate" / "result.txt", "0\n")
    write_text(
        result_root / "chrome" / "duplicate" / "retry" / "result.txt",
        "1\n",
    )
    write_text(result_root / "os" / "client-failure" / "result.txt", "0\n")
    write_text(
        result_root / "os" / "client-failure" / "traj.jsonl",
        json.dumps({"response": "client error"}) + "\n",
    )

    report = audit_tasks(tasks, result_root, shard_count=4)

    assert not report.passed
    assert [task.task_id for task, _ in report.valid_scores] == [
        "valid",
        "client-failure",
    ]
    assert [(item.task.task_id, item.task.index % 4) for item in report.missing] == [
        ("missing", 1)
    ]
    assert [item.task.task_id for item in report.corrupt] == [
        "nonfinite",
        "duplicate",
    ]
    assert [item.task.task_id for item in report.no_score_traj_errors] == [
        "missing"
    ]
    assert {
        item.task.task_id for item in report.suspicious_transport_failures
    } == {"missing", "client-failure"}


def test_audit_passes_one_canonical_finite_score(tmpdir):
    tmp_path = Path(str(tmpdir))
    meta_path = tmp_path / "test_all.json"
    meta_path.write_text(json.dumps({"os": ["complete"]}), encoding="utf-8")
    tasks = load_tasks(meta_path, expected_total=1)
    result_root = tmp_path / "results"
    write_text(result_root / "os" / "complete" / "result.txt", "1.0\n")

    report = audit_tasks(tasks, result_root, shard_count=4)

    assert report.passed
    assert report.valid_scores[0][1] == 1.0


def test_runtime_logs_map_task_context_and_retain_global_failures(tmpdir):
    tmp_path = Path(str(tmpdir))
    meta_path = tmp_path / "test_all.json"
    meta_path.write_text(
        json.dumps({"os": ["task-alpha", "task-beta"]}), encoding="utf-8"
    )
    tasks = load_tasks(meta_path, expected_total=2)
    result_root = tmp_path / "results"
    for task_id in ("task-alpha", "task-beta"):
        write_text(result_root / "os" / task_id / "result.txt", "0\n")

    worker_log = tmp_path / "runner.log"
    worker_log.write_text(
        "[python/1-EnvProcess-2] [Example ID]: task-alpha\n"
        "[python/2-EnvProcess-2] processing\n"
        "[python/3-EnvProcess-2] Error when fetching response from client\n",
        encoding="utf-8",
    )
    adjacent_log = tmp_path / "adjacent.log"
    adjacent_log.write_text(
        "starting task-beta\n"
        "request started\n"
        "httpx.RemoteProtocolError: server disconnected\n",
        encoding="utf-8",
    )
    server_log = tmp_path / "vllm.log"
    server_log.write_text(
        "AsyncEngineDeadError: engine process is unavailable\n",
        encoding="utf-8",
    )

    report = audit_tasks(
        tasks,
        result_root,
        shard_count=4,
        runtime_logs=[worker_log, adjacent_log, server_log],
    )

    assert not report.passed
    assert {
        finding.task.task_id
        for finding in report.suspicious_transport_failures
    } == {"task-alpha", "task-beta"}
    assert len(report.global_suspicious_failures) == 1
    assert report.global_suspicious_failures[0].source == server_log
    assert report.global_suspicious_failures[0].line_number == 1


def test_summary_only_hides_finding_lists(tmpdir, capsys):
    tmp_path = Path(str(tmpdir))
    meta_path = tmp_path / "test_all.json"
    meta_path.write_text(json.dumps({"os": ["missing"]}), encoding="utf-8")
    tasks = load_tasks(meta_path, expected_total=1)
    result_root = tmp_path / "results"
    report = audit_tasks(tasks, result_root, shard_count=4)

    print_report(report, meta_path, result_root, summary_only=True)

    output = capsys.readouterr().out
    assert "Missing: 1" in output
    assert "Missing tasks (" not in output
    assert "Status: FAIL" in output
