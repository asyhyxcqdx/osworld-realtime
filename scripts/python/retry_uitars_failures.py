#!/usr/bin/env python3
"""Safely retry only incomplete OSWorld UI-TARS tasks.

The command is read-only by default. Passing ``--run`` is intentionally
strict: every canonical finite score in ``[0, 1]`` is protected (including
zero), active runners for the same result directory are rejected, and old
incomplete task directories are atomically moved into a timestamped archive
before the retry runners start.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator, Sequence
from urllib.parse import urlsplit, urlunsplit


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit_uitars_results import (  # noqa: E402
    EXPECTED_TASK_COUNT,
    AuditInputError,
    Finding,
    TaskRef,
    audit_tasks,
    load_tasks,
    parse_score,
)


RUNNER = REPO_ROOT / "scripts/python/run_multienv_uitars15_v1.py"
DEFAULT_META = REPO_ROOT / "evaluation_examples/test_all.json"
DEFAULT_RESULT_DIR = REPO_ROOT / "results_uitars15_full_official"
DEFAULT_ARCHIVE_DIR = REPO_ROOT / "results_uitars15_full_official_retry_archive"
DEFAULT_WORK_DIR = REPO_ROOT / "results_uitars15_full_official_retry_runs"
DEFAULT_VM_PATH = REPO_ROOT / "docker_vm_data/Ubuntu.qcow2"
DEFAULT_PYTHON = Path(
    "/mnt/zhaorunsong/anaconda3/envs/osworld-yhyx/bin/python"
)
DEFAULT_MODEL_URLS = tuple(
    f"http://127.0.0.1:{port}/v1" for port in range(8000, 8004)
)


class RetrySafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetryCandidate:
    task: TaskRef
    task_dir: Path
    reasons: tuple[str, ...]
    previous_directory_existed: bool


@dataclass
class RetryPlan:
    tasks: list[TaskRef]
    result_root: Path
    candidates: list[RetryCandidate] = field(default_factory=list)
    protected_scores: list[tuple[TaskRef, float]] = field(default_factory=list)
    protected_diagnostic_findings: list[Finding] = field(default_factory=list)

    @property
    def protected_zero_count(self) -> int:
        return sum(score == 0.0 for _, score in self.protected_scores)


@dataclass(frozen=True)
class ActiveRunner:
    pid: int
    command: tuple[str, ...]


@dataclass(frozen=True)
class RunnerSpec:
    shard_index: int
    model_url: str
    command: tuple[str, ...]
    environment: dict[str, str]
    log_path: Path


def result_root(
    result_dir: Path, action_space: str, observation_type: str, model: str
) -> Path:
    return result_dir / action_space / observation_type / model


def validate_task_refs(tasks: Sequence[TaskRef]) -> None:
    for task in tasks:
        for label, value in (("domain", task.domain), ("task ID", task.task_id)):
            if (
                value in {"", ".", ".."}
                or Path(value).name != value
                or "/" in value
                or "\\" in value
            ):
                raise RetrySafetyError(
                    f"unsafe {label} path component in metadata: {value!r}"
                )


def build_retry_plan(
    tasks: list[TaskRef],
    task_result_root: Path,
    *,
    shard_count: int,
    runtime_logs: list[Path] | None = None,
) -> RetryPlan:
    """Build a retry plan while treating every valid score as immutable."""

    validate_task_refs(tasks)
    report = audit_tasks(
        tasks,
        task_result_root,
        shard_count=shard_count,
        runtime_logs=runtime_logs,
    )
    valid_tasks = {task for task, _ in report.valid_scores}
    reasons: dict[TaskRef, list[str]] = {}

    def add(findings: Iterable[Finding], label: str) -> None:
        for finding in findings:
            reasons.setdefault(finding.task, []).append(
                f"{label}: {finding.detail}"
            )

    add(report.missing, "missing")
    add(report.corrupt, "corrupt")
    add(report.no_score_traj_errors, "trajectory Error")
    add(report.suspicious_transport_failures, "infrastructure evidence")

    candidates: list[RetryCandidate] = []
    for task in tasks:
        if task in valid_tasks:
            continue
        task_reasons = tuple(dict.fromkeys(reasons.get(task, [])))
        if not task_reasons:
            raise RetrySafetyError(
                f"task has neither a valid score nor a retry reason: "
                f"{task.domain}/{task.task_id}"
            )
        candidates.append(
            RetryCandidate(
                task=task,
                task_dir=task_result_root / task.domain / task.task_id,
                reasons=task_reasons,
                previous_directory_existed=(
                    task_result_root / task.domain / task.task_id
                ).exists(),
            )
        )

    protected_diagnostics = [
        finding
        for finding in report.suspicious_transport_failures
        if finding.task in valid_tasks
    ]
    return RetryPlan(
        tasks=tasks,
        result_root=task_result_root,
        candidates=candidates,
        protected_scores=list(report.valid_scores),
        protected_diagnostic_findings=protected_diagnostics,
    )


def _canonical_score(task_dir: Path) -> tuple[float | None, str | None]:
    """Return a score only when exactly one canonical result.txt exists."""

    try:
        result_files = sorted(
            path for path in task_dir.rglob("result.txt") if path.is_file()
        )
    except OSError as exc:
        return None, f"cannot enumerate result files: {exc}"
    if len(result_files) != 1:
        return None, f"found {len(result_files)} result.txt files"
    if result_files[0] != task_dir / "result.txt":
        return None, "result.txt is not canonical"
    return parse_score(result_files[0])


def _resolved_argument_path(value: str, cwd: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve(strict=False)


def find_active_runners(
    target_result_dir: Path,
    *,
    proc_root: Path = Path("/proc"),
    current_pid: int | None = None,
) -> list[ActiveRunner]:
    """Find runner processes that write to ``target_result_dir``."""

    target = target_result_dir.resolve(strict=False)
    current_pid = os.getpid() if current_pid is None else current_pid
    matches: list[ActiveRunner] = []
    try:
        process_dirs = list(proc_root.iterdir())
    except OSError as exc:
        raise RetrySafetyError(f"cannot inspect active processes: {exc}") from exc

    for process_dir in process_dirs:
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        if pid == current_pid:
            continue
        try:
            raw = (process_dir / "cmdline").read_bytes()
            command = tuple(
                item.decode("utf-8", errors="replace")
                for item in raw.split(b"\0")
                if item
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if not any(Path(item).name == RUNNER.name for item in command):
            continue
        try:
            cwd = (process_dir / "cwd").resolve(strict=True)
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            cwd = Path("/")

        configured_result: str | None = None
        for index, item in enumerate(command):
            if item == "--result_dir" and index + 1 < len(command):
                configured_result = command[index + 1]
                break
            if item.startswith("--result_dir="):
                configured_result = item.partition("=")[2]
                break
        if configured_result is not None:
            if _resolved_argument_path(configured_result, cwd) == target:
                matches.append(ActiveRunner(pid, command))
        elif target.name and target.name in "\0".join(command):
            # Fail closed for an unusual invocation whose argument format is
            # not understood but which still names the protected directory.
            matches.append(ActiveRunner(pid, command))
    return sorted(matches, key=lambda process: process.pid)


@contextlib.contextmanager
def exclusive_retry_lock(result_dir: Path) -> Iterator[None]:
    digest = hashlib.sha256(
        str(result_dir.resolve(strict=False)).encode("utf-8")
    ).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"osworld-uitars-retry-{digest}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RetrySafetyError(
                "another retry coordinator is already active"
            ) from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def retry_metadata(candidates: Sequence[RetryCandidate]) -> dict[str, list[str]]:
    metadata: dict[str, list[str]] = {}
    for candidate in candidates:
        metadata.setdefault(candidate.task.domain, []).append(candidate.task.task_id)
    return metadata


def _transaction_payload(
    *,
    run_id: str,
    plan: RetryPlan,
    archive_root: Path,
    status: str,
    archived: Sequence[RetryCandidate],
    model_urls: Sequence[str],
    error: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": run_id,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "result_root": str(plan.result_root),
        "archive_root": str(archive_root),
        "model_urls": list(model_urls),
        "protected_valid_scores": len(plan.protected_scores),
        "protected_zero_scores": plan.protected_zero_count,
        "candidate_count": len(plan.candidates),
        "candidates": [
            {
                "index": candidate.task.index,
                "domain": candidate.task.domain,
                "task_id": candidate.task.task_id,
                "reasons": list(candidate.reasons),
                "previous_directory_existed": candidate.previous_directory_existed,
            }
            for candidate in plan.candidates
        ],
        "archived_task_ids": [item.task.task_id for item in archived],
    }
    if error is not None:
        payload["error"] = error
    return payload


def archive_candidates(
    plan: RetryPlan,
    archive_root: Path,
    *,
    manifest_path: Path,
    run_id: str,
    model_urls: Sequence[str],
    replace_path: Callable[[Path, Path], None] = os.replace,
) -> list[RetryCandidate]:
    """Move existing incomplete directories aside, rolling back on failure."""

    # A second score check closes the gap between planning and the first write.
    for candidate in plan.candidates:
        score, _ = _canonical_score(candidate.task_dir)
        if score is not None:
            raise RetrySafetyError(
                "refusing to archive a task that became complete after planning: "
                f"{candidate.task.domain}/{candidate.task.task_id} score={score}"
            )

    archived: list[RetryCandidate] = []
    archive_root.mkdir(parents=True, exist_ok=False)
    _atomic_json_write(
        manifest_path,
        _transaction_payload(
            run_id=run_id,
            plan=plan,
            archive_root=archive_root,
            status="prepared",
            archived=archived,
            model_urls=model_urls,
        ),
    )
    try:
        for candidate in plan.candidates:
            source = candidate.task_dir
            if not source.exists() and not source.is_symlink():
                continue
            score, _ = _canonical_score(source)
            if score is not None:
                raise RetrySafetyError(
                    "refusing to archive a task that became complete during "
                    f"archiving: {candidate.task.domain}/{candidate.task.task_id} "
                    f"score={score}"
                )
            destination = archive_root / candidate.task.domain / candidate.task.task_id
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise RetrySafetyError(f"archive destination already exists: {destination}")
            replace_path(source, destination)
            archived.append(candidate)
            _atomic_json_write(
                manifest_path,
                _transaction_payload(
                    run_id=run_id,
                    plan=plan,
                    archive_root=archive_root,
                    status="archiving",
                    archived=archived,
                    model_urls=model_urls,
                ),
            )
    except Exception as exc:
        rollback_errors: list[str] = []
        still_archived = list(archived)
        for candidate in reversed(archived):
            source = archive_root / candidate.task.domain / candidate.task.task_id
            destination = candidate.task_dir
            try:
                if destination.exists() or destination.is_symlink():
                    raise RetrySafetyError(
                        f"rollback destination unexpectedly exists: {destination}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                replace_path(source, destination)
                still_archived.remove(candidate)
            except Exception as rollback_exc:  # pragma: no cover - rare I/O failure
                rollback_errors.append(str(rollback_exc))
        detail = str(exc)
        if rollback_errors:
            detail += "; rollback errors: " + "; ".join(rollback_errors)
        _atomic_json_write(
            manifest_path,
            _transaction_payload(
                run_id=run_id,
                plan=plan,
                archive_root=archive_root,
                status="archive_failed",
                archived=still_archived,
                model_urls=model_urls,
                error=detail,
            ),
        )
        raise RetrySafetyError(detail) from exc
    return archived


def normalize_model_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RetrySafetyError(f"invalid model URL: {value!r}")
    path = parsed.path.rstrip("/")
    if path != "/v1":
        raise RetrySafetyError(f"model URL must end in /v1: {value!r}")
    if parsed.query or parsed.fragment:
        raise RetrySafetyError(f"model URL cannot contain query or fragment: {value!r}")
    return urlunsplit((parsed.scheme, parsed.netloc, "/v1", "", ""))


def check_model(model_url: str, timeout: float = 5.0) -> None:
    parsed = urlsplit(normalize_model_url(model_url))
    health_url = urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(health_url, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise RetrySafetyError(
                    f"model health check returned HTTP {response.status}: {health_url}"
                )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RetrySafetyError(
            f"model health check failed for {model_url}: {exc}"
        ) from exc


def runner_command(
    *,
    python: Path,
    runner: Path,
    path_to_vm: Path,
    metadata_path: Path,
    result_dir: Path,
    model: str,
    num_envs: int,
    shard_count: int,
    shard_index: int,
    max_steps: int,
    log_level: str,
) -> tuple[str, ...]:
    return (
        str(python),
        str(runner),
        "--provider_name",
        "docker",
        "--path_to_vm",
        str(path_to_vm),
        "--headless",
        "--action_space",
        "pyautogui",
        "--observation_type",
        "screenshot",
        "--model",
        model,
        "--model_type",
        "qwen25vl",
        "--infer_mode",
        "qwen25vl_normal",
        "--input_swap",
        "--domain",
        "all",
        "--test_all_meta_path",
        str(metadata_path),
        "--test_config_base_dir",
        str(REPO_ROOT / "evaluation_examples"),
        "--max_steps",
        str(max_steps),
        "--num_envs",
        str(num_envs),
        "--shard_count",
        str(shard_count),
        "--shard_index",
        str(shard_index),
        "--client_password",
        "password",
        "--result_dir",
        str(result_dir),
        "--log_level",
        log_level,
    )


def build_runner_specs(
    *,
    model_urls: Sequence[str],
    python: Path,
    runner: Path,
    path_to_vm: Path,
    metadata_path: Path,
    result_dir: Path,
    work_root: Path,
    model: str,
    num_envs: int,
    max_steps: int,
    log_level: str,
    base_environment: dict[str, str] | None = None,
) -> list[RunnerSpec]:
    normalized_urls = [normalize_model_url(url) for url in model_urls]
    environment = (
        os.environ.copy() if base_environment is None else base_environment.copy()
    )
    specs: list[RunnerSpec] = []
    for shard_index, model_url in enumerate(normalized_urls):
        shard_environment = environment.copy()
        shard_environment.update(
            {
                "DOUBAO_API_URL": model_url,
                "DOUBAO_API_KEY": "EMPTY",
                "HF_ENDPOINT": "https://hf-mirror.com",
                "TOKENIZERS_PARALLELISM": "false",
            }
        )
        specs.append(
            RunnerSpec(
                shard_index=shard_index,
                model_url=model_url,
                command=runner_command(
                    python=python,
                    runner=runner,
                    path_to_vm=path_to_vm,
                    metadata_path=metadata_path,
                    result_dir=result_dir,
                    model=model,
                    num_envs=num_envs,
                    shard_count=len(normalized_urls),
                    shard_index=shard_index,
                    max_steps=max_steps,
                    log_level=log_level,
                ),
                environment=shard_environment,
                log_path=work_root / f"runner-shard-{shard_index}.log",
            )
        )
    return specs


def _terminate_processes(
    processes: Sequence[tuple[RunnerSpec, subprocess.Popen[bytes], BinaryIO]],
    *,
    grace_seconds: float = 90.0,
) -> None:
    for _, process, _ in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    for _, process, _ in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_specs(specs: Sequence[RunnerSpec]) -> int:
    processes: list[tuple[RunnerSpec, subprocess.Popen[bytes], BinaryIO]] = []
    try:
        for spec in specs:
            log_handle = spec.log_path.open("ab", buffering=0)
            try:
                process = subprocess.Popen(
                    spec.command,
                    cwd=REPO_ROOT,
                    env=spec.environment,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
            except Exception:
                log_handle.close()
                raise
            processes.append((spec, process, log_handle))

        first_failure: int | None = None
        while processes:
            remaining: list[tuple[RunnerSpec, subprocess.Popen[bytes], BinaryIO]] = []
            for spec, process, log_handle in processes:
                return_code = process.poll()
                if return_code is None:
                    remaining.append((spec, process, log_handle))
                    continue
                log_handle.close()
                if return_code != 0 and first_failure is None:
                    first_failure = return_code
            processes = remaining
            if first_failure is not None:
                _terminate_processes(processes)
                return first_failure
            if processes:
                time.sleep(1)
        return 0
    except KeyboardInterrupt:
        _terminate_processes(processes)
        return 130
    finally:
        _terminate_processes(processes)
        for _, process, log_handle in processes:
            log_handle.close()


def validate_task_configs(candidates: Sequence[RetryCandidate]) -> None:
    errors: list[str] = []
    for candidate in candidates:
        path = (
            REPO_ROOT
            / "evaluation_examples/examples"
            / candidate.task.domain
            / f"{candidate.task.task_id}.json"
        )
        try:
            task_config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{candidate.task.domain}/{candidate.task.task_id}: {exc}")
            continue
        if (
            not isinstance(task_config, dict)
            or task_config.get("id") != candidate.task.task_id
        ):
            errors.append(
                f"{candidate.task.domain}/{candidate.task.task_id}: task ID mismatch"
            )
    if errors:
        raise RetrySafetyError(
            "retry task configuration validation failed: " + "; ".join(errors)
        )


def _safe_external_directory(path: Path, task_result_root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    protected = task_result_root.resolve(strict=False)
    if resolved == protected or resolved.is_relative_to(protected):
        raise RetrySafetyError(f"{label} must be outside the task result root")
    return resolved


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Archive incomplete artifacts and launch retries; default is read-only",
    )
    parser.add_argument("--meta", type=Path, default=DEFAULT_META)
    parser.add_argument("--expected-total", type=int, default=EXPECTED_TASK_COUNT)
    parser.add_argument("--result-dir", type=Path, default=DEFAULT_RESULT_DIR)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--path-to-vm", type=Path, default=DEFAULT_VM_PATH)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--runner", type=Path, default=RUNNER)
    parser.add_argument("--model", default="uitars15-7b")
    parser.add_argument("--action-space", default="pyautogui")
    parser.add_argument("--observation-type", default="screenshot")
    parser.add_argument(
        "--model-url",
        action="append",
        default=None,
        help="OpenAI-compatible /v1 endpoint; repeat up to four times",
    )
    parser.add_argument("--num-envs-per-url", type=int, default=4)
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--runtime-log",
        action="append",
        default=[],
        type=Path,
        help="Optional prior runner log used only to label infrastructure evidence",
    )
    parser.add_argument("--summary-only", action="store_true")
    return parser.parse_args(argv)


def _print_plan(plan: RetryPlan, active: Sequence[ActiveRunner], summary_only: bool) -> None:
    print("OSWorld UI-TARS incomplete-task retry plan (read-only)")
    print(f"Official tasks: {len(plan.tasks)}")
    print(f"Protected valid scores: {len(plan.protected_scores)}")
    print(f"Protected valid zero scores: {plan.protected_zero_count}")
    print(f"Retry candidates: {len(plan.candidates)}")
    print(f"Active runners for result directory: {len(active)}")
    if plan.protected_diagnostic_findings:
        print(
            "Protected scored tasks with diagnostic evidence: "
            f"{len(plan.protected_diagnostic_findings)} (not retried)"
        )
    if not summary_only:
        for candidate in plan.candidates:
            print(
                f"  [index={candidate.task.index:03d}] "
                f"{candidate.task.domain}/{candidate.task.task_id}: "
                + "; ".join(candidate.reasons)
            )
    if active:
        print(
            "RUN BLOCKED: the formal runners must finish before --run "
            f"(PIDs: {', '.join(str(item.pid) for item in active)})"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.expected_total < 1:
        raise RetrySafetyError("expected total must be at least 1")
    if not 1 <= args.num_envs_per_url <= 32:
        raise RetrySafetyError("num environments per URL must be between 1 and 32")
    if args.max_steps < 1:
        raise RetrySafetyError("max steps must be at least 1")

    model_urls = tuple(
        normalize_model_url(value)
        for value in (args.model_url or DEFAULT_MODEL_URLS)
    )
    if not 1 <= len(model_urls) <= 4:
        raise RetrySafetyError("provide between one and four model URLs")
    if len(set(model_urls)) != len(model_urls):
        raise RetrySafetyError("model URLs must be unique")

    meta_path = args.meta.expanduser().resolve()
    result_dir = args.result_dir.expanduser().resolve(strict=False)
    task_result_root = result_root(
        result_dir, args.action_space, args.observation_type, args.model
    )
    archive_dir = _safe_external_directory(
        args.archive_dir, task_result_root, "archive directory"
    )
    work_dir = _safe_external_directory(
        args.work_dir, task_result_root, "work directory"
    )
    if archive_dir == work_dir:
        raise RetrySafetyError("archive and work directories must differ")

    tasks = load_tasks(meta_path, args.expected_total)
    validate_task_refs(tasks)
    plan = build_retry_plan(
        tasks,
        task_result_root,
        shard_count=len(model_urls),
        runtime_logs=[path.expanduser().resolve() for path in args.runtime_log],
    )
    validate_task_configs(plan.candidates)
    active = find_active_runners(result_dir)
    _print_plan(plan, active, args.summary_only)
    if not args.run:
        print("DRY RUN: no files moved and no runner launched")
        return 0
    if active:
        raise RetrySafetyError("formal OSWorld runner is still active")
    if not plan.candidates:
        print("Nothing to retry: every official task has a valid score")
        return 0

    python = args.python.expanduser().resolve()
    runner = args.runner.expanduser().resolve()
    path_to_vm = args.path_to_vm.expanduser().resolve()
    for path, label in (
        (python, "Python interpreter"),
        (runner, "runner"),
        (path_to_vm, "Docker VM image"),
    ):
        if not path.is_file():
            raise RetrySafetyError(f"{label} is missing: {path}")

    with exclusive_retry_lock(result_dir):
        # Rebuild under the lock because the dry-run snapshot may have changed.
        active = find_active_runners(result_dir)
        if active:
            raise RetrySafetyError("formal OSWorld runner became active")
        plan = build_retry_plan(
            tasks,
            task_result_root,
            shard_count=len(model_urls),
            runtime_logs=[path.expanduser().resolve() for path in args.runtime_log],
        )
        if not plan.candidates:
            print("Nothing to retry: every official task now has a valid score")
            return 0
        validate_task_configs(plan.candidates)
        active_urls = model_urls[: min(len(model_urls), len(plan.candidates))]
        for model_url in active_urls:
            check_model(model_url)

        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:10]}"
        run_root = work_dir / run_id
        archive_root = archive_dir / run_id
        run_root.mkdir(parents=True, exist_ok=False)
        metadata_path = run_root / "retry_tasks.json"
        _atomic_json_write(metadata_path, retry_metadata(plan.candidates))
        manifest_path = archive_root / "manifest.json"

        # The final process check is immediately adjacent to the first move.
        if find_active_runners(result_dir):
            raise RetrySafetyError("formal OSWorld runner became active before archive")
        archived = archive_candidates(
            plan,
            archive_root,
            manifest_path=manifest_path,
            run_id=run_id,
            model_urls=active_urls,
        )
        specs = build_runner_specs(
            model_urls=active_urls,
            python=python,
            runner=runner,
            path_to_vm=path_to_vm,
            metadata_path=metadata_path,
            result_dir=result_dir,
            work_root=run_root,
            model=args.model,
            num_envs=args.num_envs_per_url,
            max_steps=args.max_steps,
            log_level=args.log_level,
        )
        _atomic_json_write(
            manifest_path,
            _transaction_payload(
                run_id=run_id,
                plan=plan,
                archive_root=archive_root,
                status="running",
                archived=archived,
                model_urls=active_urls,
            ),
        )
        print(
            f"STARTING: {len(plan.candidates)} tasks across {len(specs)} runners; "
            f"archive={archive_root}; logs={run_root}"
        )
        try:
            return_code = run_specs(specs)
            final_plan = build_retry_plan(
                tasks,
                task_result_root,
                shard_count=len(active_urls),
            )
            status = (
                "complete"
                if return_code == 0 and not final_plan.candidates
                else "failed"
            )
            error = None
            if return_code != 0:
                error = f"runner exit status {return_code}"
            elif final_plan.candidates:
                error = f"{len(final_plan.candidates)} tasks still lack a valid score"
                return_code = 1
        except Exception as exc:
            _atomic_json_write(
                manifest_path,
                _transaction_payload(
                    run_id=run_id,
                    plan=plan,
                    archive_root=archive_root,
                    status="failed",
                    archived=archived,
                    model_urls=active_urls,
                    error=f"retry coordinator failure: {exc}",
                ),
            )
            raise RetrySafetyError(f"retry coordinator failed: {exc}") from exc
        _atomic_json_write(
            manifest_path,
            _transaction_payload(
                run_id=run_id,
                plan=plan,
                archive_root=archive_root,
                status=status,
                archived=archived,
                model_urls=active_urls,
                error=error,
            ),
        )
        return return_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AuditInputError, RetrySafetyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
