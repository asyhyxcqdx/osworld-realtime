#!/usr/bin/env python3
"""Validate a Realtime GUI Bench 1.1 BENCH object offline."""

import argparse
import copy
import csv
import json
from pathlib import Path


PROTOCOL_VERSION = "realtime-gui-bench/1.1"
STATUSES = {"ready", "running", "passed", "failed"}


def _error_result(benchmark_id, errors, state=None):
    result = {
        "benchmark_id": benchmark_id,
        "scored": False,
        "status": state.get("status") if isinstance(state, dict) else None,
        "classification": "unscored",
        "errors": errors,
    }
    if isinstance(state, dict):
        result["bench"] = copy.deepcopy(state)
    return result


def check_bench(state, benchmark_id=None):
    """Return a JSON-serializable contract result for one BENCH object."""
    if not isinstance(state, dict):
        return _error_result(benchmark_id, ["BENCH must be a JSON object"])

    errors = []
    required = {
        "protocol_version",
        "task",
        "max_attempts",
        "attempts_completed",
        "passed",
        "status",
        "pass_at_1",
        "pass_at_3",
    }
    errors.extend(
        f"missing field: {name}" for name in sorted(required - set(state))
    )
    if errors:
        return _error_result(benchmark_id, errors, state)

    if state["protocol_version"] != PROTOCOL_VERSION:
        errors.append(f"protocol_version must be {PROTOCOL_VERSION}")
    if not isinstance(state["task"], str) or not state["task"].strip():
        errors.append("task must be a non-empty string")
    if type(state["max_attempts"]) is not int or state["max_attempts"] != 3:
        errors.append("max_attempts must equal 3")
    attempts = state["attempts_completed"]
    if type(attempts) is not int or not 0 <= attempts <= 3:
        errors.append("attempts_completed must be an integer from 0 to 3")
    if type(state["passed"]) is not bool:
        errors.append("passed must be boolean")
    for field in ("pass_at_1", "pass_at_3"):
        value = state[field]
        if type(value) not in {int, float} or isinstance(value, bool) or value not in {0, 1}:
            errors.append(f"{field} must be numeric 0 or 1")
    status = state["status"]
    if not isinstance(status, str) or status not in STATUSES:
        errors.append("status must be ready, running, passed, or failed")

    if not errors:
        pass_at_1 = float(state["pass_at_1"])
        pass_at_3 = float(state["pass_at_3"])
        if state["passed"] != (pass_at_3 == 1):
            errors.append("passed must equal (pass_at_3 == 1)")
        if status == "ready" and (attempts != 0 or state["passed"] or pass_at_1 or pass_at_3):
            errors.append("ready state has inconsistent completion fields")
        elif status == "running" and (attempts >= 3 or state["passed"] or pass_at_1 or pass_at_3):
            errors.append("running state has inconsistent completion fields")
        elif status == "passed" and (not state["passed"] or pass_at_3 != 1 or attempts < 1):
            errors.append("passed state has inconsistent completion fields")
        elif status == "failed" and (state["passed"] or pass_at_1 or pass_at_3 or attempts != 3):
            errors.append("failed state has inconsistent completion fields")
        if pass_at_1 == 1 and attempts != 1:
            errors.append("pass_at_1=1 requires attempts_completed=1")

    if errors:
        return _error_result(benchmark_id, errors, state)

    terminal = status in {"passed", "failed"}
    return {
        "benchmark_id": benchmark_id,
        "scored": terminal,
        "status": status,
        "classification": "scored" if terminal else "agent_incomplete",
        "errors": [],
        "pass_at_1": float(state["pass_at_1"]),
        "pass_at_3": float(state["pass_at_3"]),
        "bench": copy.deepcopy(state),
    }


def _load_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _single(path, benchmark_id):
    return check_bench(_load_json(path), benchmark_id)


def _manifest_ids(path):
    if path is None:
        return {}
    with Path(path).open(newline="", encoding="utf-8") as stream:
        rows = csv.DictReader(stream)
        result = {}
        for row in rows:
            benchmark_id = (row.get("benchmark_id") or "").strip()
            source = (row.get("source_directory") or "").strip()
            if benchmark_id and source:
                normalized = Path(source).as_posix().rstrip("/")
                result[normalized] = benchmark_id
                result.setdefault(Path(normalized).name, benchmark_id)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="JSON file containing BENCH")
    source.add_argument("--input-dir", type=Path, help="Directory containing BENCH JSON files")
    parser.add_argument("--benchmark-id", help="External benchmark ID for --input")
    parser.add_argument("--manifest", type=Path, help="Optional manifest used to map paths to IDs")
    parser.add_argument("--output", type=Path, help="Write JSON report to this path")
    args = parser.parse_args()

    if args.input:
        report = _single(args.input, args.benchmark_id)
    else:
        manifest_ids = _manifest_ids(args.manifest)
        paths = sorted(
            path
            for path in args.input_dir.rglob("*.json")
            if path.name not in {"realtime_game_test_report.json", "realtime_gui_contract_report.json"}
        )
        report = []
        for path in paths:
            relative_parent = path.parent.relative_to(args.input_dir).as_posix()
            benchmark_id = manifest_ids.get(
                relative_parent,
                manifest_ids.get(path.parent.name, path.parent.name),
            )
            report.append(_single(path, benchmark_id))

    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    if isinstance(report, list):
        return 0 if all(item["errors"] == [] for item in report) else 1
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
