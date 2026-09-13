#!/usr/bin/env python3
"""
Thread-safe results logging for OSWorld evaluations.
Appends task completion results to results.json in real-time.
"""

import json
import os
import time
import fcntl
import re
from pathlib import Path
from typing import Dict, Any, Optional


_BENCHMARK_ID_PATTERN = re.compile(r"^([ABCD])(\d+)$")


def _realtime_gui_bench_expected_ids() -> Dict[str, set[str]]:
    """Use the active task list, not ranges: the final suite has sparse IDs."""
    examples = Path(__file__).resolve().parent / "evaluation_examples"
    task_ids = json.loads(
        (examples / "test_realtime_gui_bench.json").read_text(encoding="utf-8")
    )["realtime_gui_bench"]
    expected = {category: set() for category in "ABCD"}
    for task_id in task_ids:
        task = json.loads(
            (examples / "examples/realtime_gui_bench" / f"{task_id}.json").read_text(encoding="utf-8")
        )
        benchmark_id = task["benchmark_id"]
        match = _BENCHMARK_ID_PATTERN.fullmatch(benchmark_id)
        if not match or benchmark_id in expected[match[1]]:
            raise ValueError(f"Invalid or duplicate benchmark ID: {benchmark_id}")
        expected[match[1]].add(benchmark_id)
    return expected


def extract_domain_from_path(result_path: str) -> str:
    """
    Extract domain/application from result directory path.
    Expected structure: results/{action_space}/{observation_type}/{model}/{domain}/{task_id}/
    """
    path_parts = Path(result_path).parts
    if len(path_parts) >= 2:
        return path_parts[-2]  # Second to last part should be domain
    return "unknown"


def update_realtime_gui_bench_summary(
    task_result_dir: str, *, result_root: Optional[str] = None
) -> None:
    """Rebuild the model-specific pass@1/pass@3 summary from task sidecars."""

    task_dir = Path(task_result_dir).resolve()
    domain_dir = task_dir.parent
    if domain_dir.name != "realtime_gui_bench":
        return
    if result_root is None and len(task_dir.parents) < 5:
        raise ValueError(f"Unexpected OSWorld result path: {task_dir}")

    expected_ids = _realtime_gui_bench_expected_ids()
    # Four-agent runs pass their model/agent root explicitly. Legacy callers
    # retain the original action_space/observation/model/domain/task layout.
    result_root = (
        Path(result_root).resolve() if result_root is not None else task_dir.parents[4]
    )
    summary_dir = result_root / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "realtime_gui_bench_metrics.json"
    lock_path = summary_dir / ".realtime_gui_bench_metrics.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            details_by_id: Dict[str, Dict[str, Any]] = {}
            for result_path in domain_dir.glob("*/result.json"):
                try:
                    details = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(details, dict):
                    continue
                benchmark_id = details.get("benchmark_id")
                match = (
                    _BENCHMARK_ID_PATTERN.fullmatch(benchmark_id)
                    if isinstance(benchmark_id, str)
                    else None
                )
                if not match or benchmark_id not in expected_ids[match.group(1)]:
                    continue
                if details.get("pass_at_1") not in {0, 0.0, 1, 1.0}:
                    continue
                if details.get("pass_at_3") not in {0, 0.0, 1, 1.0}:
                    continue
                if details.get("status") not in {"passed", "failed"}:
                    continue
                details_by_id[benchmark_id] = details

            def summarize(ids: set[str]) -> Dict[str, Any]:
                scored = [
                    details_by_id[item]
                    for item in sorted(ids)
                    if item in details_by_id
                ]
                scored_count = len(scored)
                return {
                    "tasks": len(ids),
                    "scored_tasks": scored_count,
                    "unscored_tasks": len(ids) - scored_count,
                    "pass_at_1": (
                        sum(float(item["pass_at_1"]) for item in scored)
                        / scored_count
                        if scored_count
                        else 0.0
                    ),
                    "pass_at_3": (
                        sum(float(item["pass_at_3"]) for item in scored)
                        / scored_count
                        if scored_count
                        else 0.0
                    ),
                }

            all_ids = set().union(*expected_ids.values())
            payload = {
                "benchmark": "realtime_gui_bench",
                "overall": summarize(all_ids),
                **{
                    category: summarize(ids)
                    for category, ids in expected_ids.items()
                },
            }
            temporary_path = summary_path.with_name(
                f".{summary_path.name}.tmp.{os.getpid()}"
            )
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, summary_path)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def append_task_result(
    task_id: str,
    domain: str, 
    score: float,
    result_dir: str,
    args: Any,
    error_message: Optional[str] = None
) -> None:
    """
    Thread-safely append a task result to results.json.
    
    Args:
        task_id: UUID of the task
        domain: Application domain (chrome, vlc, etc.)
        score: Task score (0.0 or 1.0)
        result_dir: Full path to the task result directory
        args: Command line arguments object
        error_message: Error message if task failed
    """
    # Create result entry
    result_entry = {
        "application": domain,
        "task_id": task_id,
        "status": "error" if error_message else "success",
        "score": score,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    
    if error_message:
        result_entry["err_message"] = error_message
    
    # Determine summary directory and results file path
    # Extract base result directory from args
    base_result_dir = Path(args.result_dir)
    summary_dir = base_result_dir / "summary"
    results_file = summary_dir / "results.json"
    
    # Ensure summary directory exists
    summary_dir.mkdir(parents=True, exist_ok=True)
    
    # Thread-safe JSON append with file locking
    try:
        with open(results_file, 'a+') as f:
            # Lock the file for exclusive access
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            
            try:
                # Move to beginning to read existing content
                f.seek(0)
                content = f.read().strip()
                
                # Parse existing JSON array or create new one
                if content:
                    try:
                        existing_results = json.loads(content)
                        if not isinstance(existing_results, list):
                            existing_results = []
                    except json.JSONDecodeError:
                        existing_results = []
                else:
                    existing_results = []
                
                # Add new result
                existing_results.append(result_entry)
                
                # Write back the complete JSON array
                f.seek(0)
                f.truncate()
                json.dump(existing_results, f, indent=2)
                f.write('\n')  # Add newline for readability
                
            finally:
                # Always unlock the file
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                
        print(f"📝 Logged result: {domain}/{task_id} -> {result_entry['status']} (score: {score})")
        
    except Exception as e:
        # Don't let logging errors break the main evaluation
        print(f"⚠️  Failed to log result for {task_id}: {e}")


def log_task_completion(example: Dict, result: float, result_dir: str, args: Any) -> None:
    """
    Convenience wrapper for logging successful task completion.
    
    Args:
        example: Task configuration dictionary
        result: Task score
        result_dir: Path to task result directory  
        args: Command line arguments
    """
    task_id = example.get('id', 'unknown')
    domain = extract_domain_from_path(result_dir)
    append_task_result(task_id, domain, result, result_dir, args)


def log_task_error(example: Dict, error_msg: str, result_dir: str, args: Any) -> None:
    """
    Convenience wrapper for logging task errors.
    
    Args:
        example: Task configuration dictionary
        error_msg: Error message
        result_dir: Path to task result directory
        args: Command line arguments
    """
    task_id = example.get('id', 'unknown')
    domain = extract_domain_from_path(result_dir) 
    append_task_result(task_id, domain, 0.0, result_dir, args, error_msg)
