from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


EXPECTED_TASK_COUNT = 369

UUID_PATTERN = re.compile(
    r"(?<![0-9a-f])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"(?![0-9a-f])",
    re.IGNORECASE,
)

FAILURE_HINTS = (
    "client error",
    "fetching response from client",
    "retry times to fetch response",
    "connection",
    "timeout",
    "connecterror",
    "readerror",
    "writeerror",
    "protocolerror",
    "transport",
    "server disconnected",
    "peer closed",
    "incomplete chunked",
    "enginedead",
    "cuda out of memory",
    " failed",
    " failure",
)

TRANSPORT_FAILURE_PATTERNS = (
    ("client error", re.compile(r"\bclient error\b", re.IGNORECASE)),
    (
        "model client fetch failure",
        re.compile(
            r"(?:error when fetching response from client|"
            r"reach max retry times to fetch response from client)",
            re.IGNORECASE,
        ),
    ),
    (
        "API connection/timeout error",
        re.compile(
            r"\b(?:APIConnectionError|APITimeoutError|ConnectTimeout|ReadTimeout)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "HTTP transport error",
        re.compile(
            r"\b(?:RemoteProtocolError|ReadError|WriteError|ConnectError)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "connection failure",
        re.compile(
            r"(?:connection (?:reset|refused|aborted|closed|error)|"
            r"server disconnected|peer closed connection)",
            re.IGNORECASE,
        ),
    ),
    (
        "incomplete model response",
        re.compile(r"incomplete chunked read", re.IGNORECASE),
    ),
    (
        "model transport failure",
        re.compile(
            r"(?:model|API|HTTP)[ _-]?(?:server|transport|request|response)?"
            r".{0,40}\b(?:transport error|failed|failure)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "model engine failure",
        re.compile(r"\b(?:AsyncEngineDeadError|EngineDeadError|EngineDead)\b"),
    ),
    (
        "CUDA out of memory",
        re.compile(r"\bCUDA out of memory\b", re.IGNORECASE),
    ),
)


@dataclass(frozen=True)
class TaskRef:
    index: int
    domain: str
    task_id: str


@dataclass(frozen=True)
class Finding:
    task: TaskRef
    detail: str


@dataclass(frozen=True)
class GlobalFinding:
    source: Path
    line_number: int
    detail: str


@dataclass
class AuditReport:
    tasks: list[TaskRef]
    shard_count: int
    valid_scores: list[tuple[TaskRef, float]] = field(default_factory=list)
    missing: list[Finding] = field(default_factory=list)
    corrupt: list[Finding] = field(default_factory=list)
    no_score_traj_errors: list[Finding] = field(default_factory=list)
    suspicious_transport_failures: list[Finding] = field(default_factory=list)
    global_suspicious_failures: list[GlobalFinding] = field(default_factory=list)
    runtime_logs_scanned: list[Path] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            len(self.valid_scores) == len(self.tasks)
            and not self.missing
            and not self.corrupt
            and not self.suspicious_transport_failures
            and not self.global_suspicious_failures
        )


class AuditInputError(ValueError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Strict, read-only audit of an OSWorld result directory. "
            "Exit status is zero only when every expected task has one valid "
            "score and no client/model transport failure is recorded."
        )
    )
    parser.add_argument("--meta", default="evaluation_examples/test_all.json")
    parser.add_argument(
        "--result-dir", default="results_uitars15_full_official"
    )
    parser.add_argument("--model", default="uitars15-7b")
    parser.add_argument("--action-space", default="pyautogui")
    parser.add_argument("--observation-type", default="screenshot")
    parser.add_argument(
        "--expected-total",
        type=int,
        default=EXPECTED_TASK_COUNT,
        help="Expected task count in the authoritative metadata (default: 369)",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=4,
        help="Shard modulus used for finding labels (default: 4)",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print counts and final status without per-task finding lists",
    )
    parser.add_argument(
        "--runtime-log",
        action="append",
        nargs="+",
        default=[],
        metavar="PATH",
        help=(
            "Runner or vLLM log files to scan; repeat the option or pass "
            "multiple shell-expanded paths"
        ),
    )
    return parser.parse_args()


def load_tasks(meta_path: Path, expected_total: int) -> list[TaskRef]:
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise AuditInputError(f"cannot read metadata {meta_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise AuditInputError(f"invalid metadata JSON {meta_path}: {error}") from error

    if not isinstance(metadata, dict):
        raise AuditInputError("metadata root must be a JSON object")

    tasks: list[TaskRef] = []
    seen_task_ids: set[str] = set()
    for domain, task_ids in metadata.items():
        if not isinstance(domain, str) or not isinstance(task_ids, list):
            raise AuditInputError("metadata must map domain strings to task ID lists")
        for task_id in task_ids:
            if not isinstance(task_id, str) or not task_id:
                raise AuditInputError(f"invalid task ID in domain {domain!r}")
            if task_id in seen_task_ids:
                raise AuditInputError(f"duplicate task ID in metadata: {task_id}")
            seen_task_ids.add(task_id)
            tasks.append(TaskRef(len(tasks), domain, task_id))

    if len(tasks) != expected_total:
        raise AuditInputError(
            f"metadata contains {len(tasks)} tasks; expected {expected_total}"
        )
    return tasks


def parse_score(path: Path) -> tuple[float | None, str | None]:
    try:
        raw_score = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        return None, f"cannot read {path.name}: {error}"

    if not raw_score:
        return None, "result.txt is empty"
    try:
        score = float(raw_score)
    except ValueError:
        return None, f"result.txt is not a float: {raw_score!r}"
    if not math.isfinite(score):
        return None, f"result.txt is not finite: {raw_score!r}"
    if not 0.0 <= score <= 1.0:
        return None, f"score {score!r} is outside [0, 1]"
    return score, None


def read_diagnostics(task_dir: Path) -> tuple[dict[Path, str], list[str]]:
    candidates = {task_dir / "traj.jsonl", task_dir / "response.txt"}
    candidates.update(task_dir.glob("*.log"))

    contents: dict[Path, str] = {}
    read_errors: list[str] = []
    for path in sorted(candidates):
        if not path.is_file():
            continue
        try:
            contents[path] = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            read_errors.append(f"cannot read diagnostic {path.name}: {error}")
    return contents, read_errors


def extract_traj_error(traj_text: str) -> str | None:
    for line in traj_text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "Error" in record:
            return compact(str(record["Error"]))
    return None


def find_transport_failures(contents: dict[Path, str]) -> list[str]:
    matches: set[str] = set()
    for path, text in contents.items():
        for label in transport_failure_labels(text):
            matches.add(f"{label} in {path.name}")
    return sorted(matches)


def transport_failure_labels(text: str) -> list[str]:
    lowered = text.lower()
    if not any(hint in lowered for hint in FAILURE_HINTS):
        return []
    return [
        label
        for label, pattern in TRANSPORT_FAILURE_PATTERNS
        if pattern.search(text)
    ]


def compact(value: str, limit: int = 180) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def audit_tasks(
    tasks: list[TaskRef],
    result_root: Path,
    shard_count: int,
    runtime_logs: list[Path] | None = None,
) -> AuditReport:
    if shard_count < 1:
        raise AuditInputError("shard count must be at least 1")

    report = AuditReport(tasks=tasks, shard_count=shard_count)
    for task in tasks:
        task_dir = result_root / task.domain / task.task_id
        try:
            result_files = sorted(
                path for path in task_dir.rglob("result.txt") if path.is_file()
            )
        except OSError as error:
            result_files = []
            report.corrupt.append(
                Finding(task, f"cannot enumerate result files: {error}")
            )

        has_valid_score = False
        if not result_files:
            if not any(finding.task == task for finding in report.corrupt):
                report.missing.append(Finding(task, "no result.txt"))
        elif len(result_files) != 1:
            relative_paths = ", ".join(
                str(path.relative_to(task_dir)) for path in result_files
            )
            report.corrupt.append(
                Finding(
                    task,
                    f"expected one result.txt, found {len(result_files)}: "
                    f"{relative_paths}",
                )
            )
        else:
            result_path = result_files[0]
            canonical_path = task_dir / "result.txt"
            if result_path != canonical_path:
                report.corrupt.append(
                    Finding(
                        task,
                        f"result.txt is misplaced at "
                        f"{result_path.relative_to(task_dir)}",
                    )
                )
            else:
                score, error = parse_score(result_path)
                if error is not None:
                    report.corrupt.append(Finding(task, error))
                else:
                    assert score is not None
                    has_valid_score = True
                    report.valid_scores.append((task, score))

        diagnostics, diagnostic_read_errors = read_diagnostics(task_dir)
        traj_error = extract_traj_error(
            diagnostics.get(task_dir / "traj.jsonl", "")
        )
        if not has_valid_score and traj_error is not None:
            report.no_score_traj_errors.append(Finding(task, traj_error))

        transport_failures = find_transport_failures(diagnostics)
        transport_failures.extend(diagnostic_read_errors)
        if transport_failures:
            report.suspicious_transport_failures.append(
                Finding(task, "; ".join(sorted(transport_failures)))
            )

    runtime_logs = list(dict.fromkeys(runtime_logs or []))
    if runtime_logs:
        mapped, global_findings = scan_runtime_logs(tasks, runtime_logs)
        merge_task_findings(report.suspicious_transport_failures, mapped, tasks)
        report.global_suspicious_failures.extend(global_findings)
        report.runtime_logs_scanned.extend(runtime_logs)

    return report


def scan_runtime_logs(
    tasks: list[TaskRef], runtime_logs: list[Path]
) -> tuple[list[Finding], list[GlobalFinding]]:
    tasks_by_id = {task.task_id: task for task in tasks}
    uuid_tasks_by_id = {
        task_id.lower(): task
        for task_id, task in tasks_by_id.items()
        if UUID_PATTERN.fullmatch(task_id)
    }
    non_uuid_ids = [
        task_id
        for task_id in tasks_by_id
        if not UUID_PATTERN.fullmatch(task_id)
    ]
    non_uuid_pattern = (
        re.compile(
            "|".join(
                re.escape(task_id)
                for task_id in sorted(non_uuid_ids, key=len, reverse=True)
            )
        )
        if non_uuid_ids
        else None
    )
    worker_pattern = re.compile(r"\bEnvProcess-\d+\b")
    mapped_details: dict[TaskRef, list[str]] = {}
    global_findings: list[GlobalFinding] = []

    for log_path in runtime_logs:
        if not log_path.is_file():
            raise AuditInputError(f"runtime log is not a readable file: {log_path}")
        try:
            lines = log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError as error:
            raise AuditInputError(
                f"cannot read runtime log {log_path}: {error}"
            ) from error

        direct_tasks = [
            tasks_in_log_line(
                line,
                tasks_by_id,
                uuid_tasks_by_id,
                non_uuid_pattern,
            )
            for line in lines
        ]
        workers: list[str | None] = []
        for line in lines:
            worker_match = worker_pattern.search(line)
            workers.append(worker_match.group(0) if worker_match else None)
        worker_tasks: dict[str, TaskRef] = {}
        contextual_tasks: list[TaskRef | None] = []
        for line_tasks, worker in zip(direct_tasks, workers):
            if worker is not None and len(line_tasks) == 1:
                worker_tasks[worker] = next(iter(line_tasks))
            contextual_tasks.append(worker_tasks.get(worker) if worker else None)

        for line_index, line in enumerate(lines):
            labels = transport_failure_labels(line)
            if not labels:
                continue

            mapped_tasks = set(direct_tasks[line_index])
            contextual_task = contextual_tasks[line_index]
            if not mapped_tasks and contextual_task is not None:
                mapped_tasks.add(contextual_task)
            if not mapped_tasks:
                mapped_tasks.update(
                    nearby_tasks(
                        line_index,
                        direct_tasks,
                        workers,
                        workers[line_index],
                    )
                )

            detail = (
                f"{log_path}:{line_index + 1}: {', '.join(labels)}: "
                f"{compact(line)}"
            )
            if mapped_tasks:
                for task in mapped_tasks:
                    mapped_details.setdefault(task, []).append(detail)
            else:
                global_findings.append(
                    GlobalFinding(log_path, line_index + 1, detail)
                )

    mapped_findings = [
        Finding(task, "; ".join(dict.fromkeys(mapped_details[task])))
        for task in tasks
        if task in mapped_details
    ]
    return mapped_findings, global_findings


def tasks_in_log_line(
    line: str,
    tasks_by_id: dict[str, TaskRef],
    uuid_tasks_by_id: dict[str, TaskRef],
    non_uuid_pattern: re.Pattern[str] | None,
) -> set[TaskRef]:
    matches = {
        uuid_tasks_by_id[match.group(0).lower()]
        for match in UUID_PATTERN.finditer(line)
        if match.group(0).lower() in uuid_tasks_by_id
    }
    if non_uuid_pattern is not None:
        matches.update(
            tasks_by_id[task_id] for task_id in non_uuid_pattern.findall(line)
        )
    return matches


def nearby_tasks(
    line_index: int,
    direct_tasks: list[set[TaskRef]],
    workers: list[str | None],
    worker: str | None,
    radius: int = 8,
) -> set[TaskRef]:
    start = max(0, line_index - radius)
    stop = min(len(direct_tasks), line_index + radius + 1)
    matches: set[TaskRef] = set()
    for candidate_index in range(start, stop):
        if worker is not None and workers[candidate_index] != worker:
            continue
        matches.update(direct_tasks[candidate_index])
    return matches if len(matches) == 1 else set()


def merge_task_findings(
    existing: list[Finding], additions: list[Finding], tasks: list[TaskRef]
) -> None:
    details: dict[TaskRef, list[str]] = {}
    for finding in existing + additions:
        details.setdefault(finding.task, []).append(finding.detail)
    existing[:] = [
        Finding(task, "; ".join(dict.fromkeys(details[task])))
        for task in tasks
        if task in details
    ]


def finding_label(finding: Finding, shard_count: int) -> str:
    task = finding.task
    shard = task.index % shard_count
    return (
        f"[index={task.index:03d} shard={shard}] "
        f"{task.domain}/{task.task_id}: {finding.detail}"
    )


def print_findings(
    title: str, findings: list[Finding], shard_count: int
) -> None:
    print(f"\n{title} ({len(findings)}):")
    if not findings:
        print("  none")
        return
    for finding in findings:
        print(f"  {finding_label(finding, shard_count)}")


def print_report(
    report: AuditReport,
    meta_path: Path,
    result_root: Path,
    summary_only: bool = False,
) -> None:
    score_sum = sum(score for _, score in report.valid_scores)
    print("OSWorld strict result audit (read-only)")
    print(f"Metadata: {meta_path}")
    print(f"Result root: {result_root}")
    print(f"Expected tasks: {len(report.tasks)}")
    print(f"Valid scores: {len(report.valid_scores)}")
    print(f"Missing: {len(report.missing)}")
    print(f"Corrupt: {len(report.corrupt)}")
    print(f"No-score trajectory errors: {len(report.no_score_traj_errors)}")
    print(
        "Suspicious client/transport failures: "
        f"{len(report.suspicious_transport_failures)}"
    )
    if report.runtime_logs_scanned:
        print(f"Runtime logs scanned: {len(report.runtime_logs_scanned)}")
        print(
            "Unmapped global runtime-log failures: "
            f"{len(report.global_suspicious_failures)}"
        )
    print(f"Valid score sum: {score_sum:.6f}")

    if not summary_only:
        print_findings("Missing tasks", report.missing, report.shard_count)
        print_findings("Corrupt tasks", report.corrupt, report.shard_count)
        print_findings(
            "No-score tasks with traj Error",
            report.no_score_traj_errors,
            report.shard_count,
        )
        print_findings(
            "Suspicious client/model transport failures",
            report.suspicious_transport_failures,
            report.shard_count,
        )
        if report.runtime_logs_scanned:
            print_global_findings(report.global_suspicious_failures)
    print(f"\nStatus: {'PASS' if report.passed else 'FAIL'}")


def print_global_findings(findings: list[GlobalFinding]) -> None:
    print(f"\nUnmapped global runtime-log failures ({len(findings)}):")
    if not findings:
        print("  none")
        return
    for finding in findings:
        print(f"  {finding.detail}")


def main() -> int:
    args = parse_args()
    if args.expected_total < 1:
        raise AuditInputError("expected total must be at least 1")

    meta_path = Path(args.meta)
    result_root = (
        Path(args.result_dir)
        / args.action_space
        / args.observation_type
        / args.model
    )
    runtime_logs = [Path(path) for group in args.runtime_log for path in group]
    tasks = load_tasks(meta_path, args.expected_total)
    report = audit_tasks(
        tasks,
        result_root,
        args.shard_count,
        runtime_logs=runtime_logs,
    )
    print_report(
        report,
        meta_path,
        result_root,
        summary_only=args.summary_only,
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditInputError as error:
        print(f"audit input error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
