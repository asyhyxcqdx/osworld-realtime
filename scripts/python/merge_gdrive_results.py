#!/usr/bin/env python3
"""Safely merge isolated Google Drive recovery results into a full OSWorld run.

The command is read-only by default. Passing ``--apply`` copies only the eight
known Google Drive task directories. Existing clean scores are never replaced;
an existing directory is replaceable only when its score is missing or its
diagnostics contain a recognized infrastructure failure.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit_uitars_results import (  # noqa: E402
    extract_traj_error,
    parse_score,
    read_diagnostics,
    transport_failure_labels,
)
from run_gdrive_only import GDRIVE_TASK_IDS  # noqa: E402


DEFAULT_RECOVERY_DIR = REPO_ROOT / "results_uitars15_gdrive_recovery"
DEFAULT_FORMAL_DIR = REPO_ROOT / "results_uitars15_full_official"
DEFAULT_ACTION_SPACE = "pyautogui"
DEFAULT_OBSERVATION_TYPE = "screenshot"
DEFAULT_MODEL = "uitars15-7b"
DOMAIN = "multi_apps"

ALLOWED_ROOT_FILES = (
    re.compile(r"args\.json"),
    re.compile(r"args-shard-\d+-of-\d+\.json"),
)

# These patterns are deliberately narrow. Agent/action failures are benchmark
# outcomes and must not be relabeled as infrastructure failures.
INFRASTRUCTURE_PATTERNS = (
    (
        "Google OAuth failure",
        re.compile(
            r"\b(?:invalid_grant|RefreshError|invalid[_ ]credentials?|"
            r"token (?:has )?(?:expired|been revoked)|oauth.{0,40}"
            r"(?:denied|expired|invalid))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Google Drive authentication failure",
        re.compile(
            r"(?:google(?:apis)?|google drive|drive API).{0,80}"
            r"(?:401|403|unauthorized|forbidden|authentication failed)",
            re.IGNORECASE,
        ),
    ),
    (
        "Docker/VM startup failure",
        re.compile(
            r"(?:VM failed to become ready|No such container|DockerException|"
            r"error (?:starting|stopping) container|KVM.{0,40}(?:failed|error))",
            re.IGNORECASE,
        ),
    ),
)


class MergeValidationError(RuntimeError):
    pass


class MergeApplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskInspection:
    path: Path
    exists: bool
    digest: str | None
    result_files: tuple[Path, ...] = ()
    score: float | None = None
    score_error: str | None = None
    trajectory_error: str | None = None
    infrastructure_evidence: tuple[str, ...] = ()
    diagnostic_read_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class MergeAction:
    task_id: str
    kind: str
    reason: str
    source: Path
    destination: Path
    score: float
    source_digest: str
    destination_digest: str | None
    destination_existed: bool


@dataclass
class MergePlan:
    recovery_dir: Path
    formal_dir: Path
    recovery_root: Path
    formal_root: Path
    actions: list[MergeAction] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.errors


def result_root(
    base: Path, action_space: str, observation_type: str, model: str
) -> Path:
    return base / action_space / observation_type / model


def _relative_name(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise MergeValidationError(f"path escaped task directory: {path}") from exc


def tree_digest(path: Path) -> str | None:
    """Hash file names, types, modes, and contents without following symlinks."""

    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_dir():
        raise MergeValidationError(f"task path is not a real directory: {path}")

    digest = hashlib.sha256()
    for candidate in [path, *sorted(path.rglob("*"), key=lambda item: item.as_posix())]:
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise MergeValidationError(f"cannot stat {candidate}: {exc}") from exc
        relative = "." if candidate == path else _relative_name(candidate, path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(metadata.st_mode):04o}".encode("ascii"))
        digest.update(b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            raise MergeValidationError(f"symlink is not allowed in a task result: {candidate}")
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(b"D\0")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise MergeValidationError(f"non-regular result artifact is not allowed: {candidate}")
        digest.update(b"F\0")
        try:
            with candidate.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise MergeValidationError(f"cannot read {candidate}: {exc}") from exc
        digest.update(b"\0")
    return digest.hexdigest()


def _infrastructure_evidence(contents: dict[Path, str]) -> list[str]:
    evidence: set[str] = set()
    for path, text in contents.items():
        for label in transport_failure_labels(text):
            evidence.add(f"{label} in {path.name}")
        for label, pattern in INFRASTRUCTURE_PATTERNS:
            if pattern.search(text):
                evidence.add(f"{label} in {path.name}")
    return sorted(evidence)


def inspect_task(path: Path) -> TaskInspection:
    digest = tree_digest(path)
    if digest is None:
        return TaskInspection(path=path, exists=False, digest=None)

    try:
        result_files = tuple(
            sorted(candidate for candidate in path.rglob("result.txt") if candidate.is_file())
        )
    except OSError as exc:
        raise MergeValidationError(f"cannot enumerate result files in {path}: {exc}") from exc

    score = None
    score_error = None
    if len(result_files) == 1 and result_files[0] == path / "result.txt":
        score, score_error = parse_score(result_files[0])
    elif not result_files:
        score_error = "no result.txt"
    elif len(result_files) == 1:
        score_error = f"result.txt is misplaced at {_relative_name(result_files[0], path)}"
    else:
        score_error = f"found {len(result_files)} result.txt files"

    contents, read_errors = read_diagnostics(path)
    trajectory_error = extract_traj_error(contents.get(path / "traj.jsonl", ""))
    return TaskInspection(
        path=path,
        exists=True,
        digest=digest,
        result_files=result_files,
        score=score,
        score_error=score_error,
        trajectory_error=trajectory_error,
        infrastructure_evidence=tuple(_infrastructure_evidence(contents)),
        diagnostic_read_errors=tuple(read_errors),
    )


def validate_recovery_scope(recovery_root: Path) -> list[str]:
    errors: list[str] = []
    if recovery_root.is_symlink() or not recovery_root.is_dir():
        return [f"recovery result root is missing or not a real directory: {recovery_root}"]

    try:
        entries = list(recovery_root.iterdir())
    except OSError as exc:
        return [f"cannot enumerate recovery result root {recovery_root}: {exc}"]

    for entry in entries:
        if entry.is_symlink():
            errors.append(f"recovery root contains a symlink: {entry.name}")
        elif entry.is_dir() and entry.name != DOMAIN:
            errors.append(f"unexpected recovery domain directory: {entry.name}")
        elif entry.is_file() and not any(
            pattern.fullmatch(entry.name) for pattern in ALLOWED_ROOT_FILES
        ):
            errors.append(f"unexpected file in recovery result root: {entry.name}")
        elif not entry.is_dir() and not entry.is_file():
            errors.append(f"unexpected recovery artifact: {entry.name}")

    domain_dir = recovery_root / DOMAIN
    if domain_dir.is_symlink() or not domain_dir.is_dir():
        errors.append(f"recovery domain directory is missing: {domain_dir}")
        return errors

    try:
        task_entries = list(domain_dir.iterdir())
    except OSError as exc:
        errors.append(f"cannot enumerate {domain_dir}: {exc}")
        return errors

    known_ids = set(GDRIVE_TASK_IDS)
    for entry in task_entries:
        if entry.name not in known_ids:
            errors.append(f"unknown task in recovery directory: {entry.name}")
        elif entry.is_symlink() or not entry.is_dir():
            errors.append(f"recovery task is not a real directory: {entry.name}")
    return errors


def _validate_recovery_task(task_id: str, inspection: TaskInspection) -> list[str]:
    prefix = f"recovery {task_id}"
    if not inspection.exists:
        return [f"{prefix}: task directory is missing"]
    errors: list[str] = []
    if inspection.score_error:
        errors.append(f"{prefix}: {inspection.score_error}")
    if inspection.trajectory_error is not None:
        errors.append(f"{prefix}: traj.jsonl contains Error: {inspection.trajectory_error}")
    if inspection.infrastructure_evidence:
        errors.append(
            f"{prefix}: infrastructure failure remains: "
            + "; ".join(inspection.infrastructure_evidence)
        )
    if inspection.diagnostic_read_errors:
        errors.extend(f"{prefix}: {error}" for error in inspection.diagnostic_read_errors)
    return errors


def _destination_state(inspection: TaskInspection) -> tuple[str, str]:
    if not inspection.exists or not inspection.result_files:
        return "missing", "no existing result.txt"
    if inspection.diagnostic_read_errors:
        return "corrupt", "; ".join(inspection.diagnostic_read_errors)
    if inspection.infrastructure_evidence:
        return (
            "infrastructure_error",
            "; ".join(inspection.infrastructure_evidence),
        )
    if inspection.score is not None and inspection.score_error is None:
        return "valid", f"existing clean score={inspection.score:g}"
    return "corrupt", inspection.score_error or "invalid destination result"


def build_merge_plan(
    *,
    recovery_dir: Path,
    formal_dir: Path,
    action_space: str = DEFAULT_ACTION_SPACE,
    observation_type: str = DEFAULT_OBSERVATION_TYPE,
    model: str = DEFAULT_MODEL,
) -> MergePlan:
    recovery_dir = recovery_dir.expanduser().absolute()
    formal_dir = formal_dir.expanduser().absolute()
    recovery_root = result_root(recovery_dir, action_space, observation_type, model)
    formal_root = result_root(formal_dir, action_space, observation_type, model)
    plan = MergePlan(recovery_dir, formal_dir, recovery_root, formal_root)

    if recovery_dir.resolve() == formal_dir.resolve():
        plan.errors.append("recovery and formal result directories must be different")
        return plan
    if len(GDRIVE_TASK_IDS) != 8 or len(set(GDRIVE_TASK_IDS)) != 8:
        plan.errors.append("the built-in Google Drive task allowlist is not exactly 8 unique IDs")
        return plan

    plan.errors.extend(validate_recovery_scope(recovery_root))

    for task_id in GDRIVE_TASK_IDS:
        source_path = recovery_root / DOMAIN / task_id
        destination_path = formal_root / DOMAIN / task_id
        try:
            source = inspect_task(source_path)
            destination = inspect_task(destination_path)
        except MergeValidationError as exc:
            plan.errors.append(f"{task_id}: {exc}")
            continue

        destination_state, destination_reason = _destination_state(destination)
        source_errors = _validate_recovery_task(task_id, source)

        if destination_state == "valid":
            plan.skipped.append((task_id, destination_reason))
            # A present recovery directory still has to be trustworthy, even
            # when idempotence means it will not be copied.
            if source.exists:
                plan.errors.extend(source_errors)
            continue

        if source_errors:
            plan.errors.extend(source_errors)
            continue
        assert source.score is not None and source.digest is not None

        if destination_state == "corrupt":
            plan.errors.append(
                f"formal {task_id}: refusing to replace corrupt result without "
                f"explicit infrastructure evidence ({destination_reason})"
            )
            continue

        kind = (
            "install_missing"
            if destination_state == "missing"
            else "replace_infrastructure_error"
        )
        plan.actions.append(
            MergeAction(
                task_id=task_id,
                kind=kind,
                reason=destination_reason,
                source=source_path,
                destination=destination_path,
                score=source.score,
                source_digest=source.digest,
                destination_digest=destination.digest,
                destination_existed=destination.exists,
            )
        )
    return plan


def _runner_active_for(result_dir: Path, script_name: str) -> bool:
    result_marker = str(result_dir).encode()
    name_marker = result_dir.name.encode()
    script_marker = script_name.encode()
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit() or int(process_dir.name) == os.getpid():
            continue
        try:
            command = (process_dir / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if script_marker in command and (result_marker in command or name_marker in command):
            return True
    return False


@contextlib.contextmanager
def _exclusive_apply_locks() -> Iterator[None]:
    lock_paths = (
        Path(tempfile.gettempdir()) / "osworld-gdrive-merge.lock",
        Path(tempfile.gettempdir()) / "osworld-gdrive-recovery.lock",
    )
    descriptors: list[int] = []
    try:
        for lock_path in lock_paths:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                raise MergeApplyError(f"another process holds {lock_path.name}")
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
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
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for path in [root, *root.rglob("*")]:
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for path in reversed(directories):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _manifest_payload(
    plan: MergePlan,
    transaction_id: str,
    status: str,
    applied: list[str],
    error: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "transaction_id": transaction_id,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "recovery_root": str(plan.recovery_root),
        "formal_root": str(plan.formal_root),
        "allowed_task_ids": list(GDRIVE_TASK_IDS),
        "applied_task_ids": list(applied),
        "actions": [
            {
                "task_id": action.task_id,
                "kind": action.kind,
                "reason": action.reason,
                "score": action.score,
                "source_digest": action.source_digest,
                "destination_digest_before": action.destination_digest,
                "destination_existed": action.destination_existed,
            }
            for action in plan.actions
        ],
    }
    if error is not None:
        payload["error"] = error
    return payload


def apply_merge_plan(
    plan: MergePlan,
    *,
    check_active_processes: bool = True,
    replace_path: Callable[[Path, Path], None] = os.replace,
) -> Path | None:
    if not plan.ready:
        raise MergeApplyError("cannot apply a merge plan with validation errors")
    if not plan.actions:
        return None

    with _exclusive_apply_locks():
        if check_active_processes:
            if _runner_active_for(plan.formal_dir, "run_multienv_uitars15_v1.py"):
                raise MergeApplyError("formal OSWorld runner is still active")
            if _runner_active_for(plan.recovery_dir, "run_gdrive_only.py"):
                raise MergeApplyError("Google Drive recovery runner is still active")

        # Recheck every input immediately before the first write.
        for action in plan.actions:
            if tree_digest(action.source) != action.source_digest:
                raise MergeApplyError(f"recovery task changed after planning: {action.task_id}")
            if tree_digest(action.destination) != action.destination_digest:
                raise MergeApplyError(f"formal task changed after planning: {action.task_id}")

        plan.formal_dir.mkdir(parents=True, exist_ok=True)
        staging_parent = plan.formal_dir / ".gdrive_merge_staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(tempfile.mkdtemp(prefix="transaction-", dir=staging_parent))
        transaction_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
        transaction_dir = plan.formal_dir / "_gdrive_merge_backups" / transaction_id
        manifest_path = transaction_dir / "manifest.json"
        staged_paths: dict[str, Path] = {}
        started: list[tuple[MergeAction, Path | None]] = []
        installed_task_ids: list[str] = []

        try:
            for action in plan.actions:
                staged_path = staging_root / action.task_id
                shutil.copytree(action.source, staged_path, copy_function=shutil.copy2)
                _fsync_tree(staged_path)
                staged = inspect_task(staged_path)
                staged_errors = _validate_recovery_task(action.task_id, staged)
                if staged_errors or staged.digest != action.source_digest:
                    detail = "; ".join(staged_errors) or "staged digest mismatch"
                    raise MergeApplyError(f"staged recovery task failed validation: {detail}")
                staged_paths[action.task_id] = staged_path

            transaction_dir.mkdir(parents=True, exist_ok=False)
            _atomic_json_write(
                manifest_path,
                _manifest_payload(plan, transaction_id, "prepared", []),
            )

            for action in plan.actions:
                if tree_digest(action.destination) != action.destination_digest:
                    raise MergeApplyError(f"formal task changed during staging: {action.task_id}")
                action.destination.parent.mkdir(parents=True, exist_ok=True)
                backup_path: Path | None = None
                if action.destination_existed:
                    backup_path = transaction_dir / "previous" / action.task_id
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    replace_path(action.destination, backup_path)
                # Record the exchange before installing the staged directory so
                # the common rollback path also covers an install failure after
                # the old directory has already moved to its backup.
                started.append((action, backup_path))
                replace_path(staged_paths[action.task_id], action.destination)
                installed_task_ids.append(action.task_id)
                _atomic_json_write(
                    manifest_path,
                    _manifest_payload(
                        plan,
                        transaction_id,
                        "applying",
                        installed_task_ids,
                    ),
                )

            _atomic_json_write(
                manifest_path,
                _manifest_payload(
                    plan,
                    transaction_id,
                    "complete",
                    installed_task_ids,
                ),
            )
            return manifest_path
        except Exception as exc:
            rollback_errors: list[str] = []
            rollback_root = staging_root / "rollback"
            rollback_root.mkdir(parents=True, exist_ok=True)
            for action, backup_path in reversed(started):
                try:
                    if action.destination.exists():
                        replace_path(action.destination, rollback_root / action.task_id)
                    if backup_path is not None and backup_path.exists():
                        replace_path(backup_path, action.destination)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{action.task_id}: {rollback_exc}")
            if manifest_path.parent.exists():
                status = "rollback_failed" if rollback_errors else "rolled_back"
                error = str(exc)
                if rollback_errors:
                    error += "; rollback errors: " + "; ".join(rollback_errors)
                _atomic_json_write(
                    manifest_path,
                    _manifest_payload(plan, transaction_id, status, [], error),
                )
            if rollback_errors:
                raise MergeApplyError(
                    f"merge failed and rollback was incomplete: {exc}; "
                    + "; ".join(rollback_errors)
                ) from exc
            raise MergeApplyError(f"merge failed and was rolled back: {exc}") from exc
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
            try:
                staging_parent.rmdir()
            except OSError:
                pass


def print_plan(plan: MergePlan, *, applying: bool) -> None:
    mode = "APPLY" if applying else "DRY RUN"
    print(f"Google Drive result merge ({mode})")
    print(f"Recovery root: {plan.recovery_root}")
    print(f"Formal root: {plan.formal_root}")
    print(f"Allowed task IDs: {len(GDRIVE_TASK_IDS)}")
    print(f"Planned writes: {len(plan.actions)}")
    print(f"Protected existing scores: {len(plan.skipped)}")
    print(f"Validation errors: {len(plan.errors)}")
    for action in plan.actions:
        print(
            f"  {action.kind}: {DOMAIN}/{action.task_id} "
            f"score={action.score:g} ({action.reason})"
        )
    for task_id, reason in plan.skipped:
        print(f"  preserve: {DOMAIN}/{task_id} ({reason})")
    for error in plan.errors:
        print(f"  ERROR: {error}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery-dir", type=Path, default=DEFAULT_RECOVERY_DIR)
    parser.add_argument("--formal-dir", type=Path, default=DEFAULT_FORMAL_DIR)
    parser.add_argument("--action-space", default=DEFAULT_ACTION_SPACE)
    parser.add_argument("--observation-type", default=DEFAULT_OBSERVATION_TYPE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the validated merge; without this flag no files are written",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan = build_merge_plan(
        recovery_dir=args.recovery_dir,
        formal_dir=args.formal_dir,
        action_space=args.action_space,
        observation_type=args.observation_type,
        model=args.model,
    )
    print_plan(plan, applying=args.apply)
    if not plan.ready:
        print("Status: REFUSED")
        return 1
    if not args.apply:
        print("Status: DRY RUN OK; rerun with --apply to write")
        return 0
    try:
        manifest = apply_merge_plan(plan)
    except MergeApplyError as exc:
        print(f"Status: REFUSED: {exc}")
        return 1
    if manifest is None:
        print("Status: NO-OP; all existing clean scores were preserved")
    else:
        print(f"Status: APPLIED; transaction manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
