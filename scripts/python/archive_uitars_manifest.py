#!/usr/bin/env python3
"""Archive OSWorld result directories named by an explicit failure manifest.

The command is read-only unless ``--run`` is supplied.  Unlike the generic
retry helper, this tool intentionally permits canonical scores in ``[0, 1]``
when a supported protocol manifest authorizes invalidating those otherwise
valid-looking results.

Two manifest protocols are supported:

* Legacy clipboard manifests omit ``failure_protocol`` and carry a
  ``pyperclip_failure_count`` for every task.  This preserves the original
  clipboard-repair workflow unchanged.
* UI-TARS transport manifests explicitly set ``failure_protocol`` to
  ``uitars_model_transport_failure_v1``.  Every task must have a valid score,
  ``reason`` set to
  ``valid_score_traj_contains_client_or_model_transport_failure``, and a
  non-empty ``transport_evidence`` list of ``{"label": ..., "count": ...}``
  objects.  Labels come from the strict audit's canonical transport taxonomy
  and counts are exact occurrences.  The top-level ``failure_signature`` is
  ``client/model transport failure in traj.jsonl``.  Declared evidence is
  re-derived from the hashed ``traj.jsonl`` before any archive move.

Both protocols validate official metadata, task identity and summary counts,
bind the declared result root, and use the same atomic move, journal, hash,
rollback, locking, and active-runner protections.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import stat
import sys
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit_uitars_results import TRANSPORT_FAILURE_PATTERNS  # noqa: E402


DEFAULT_MANIFEST = (
    REPO_ROOT
    / "evaluation_examples/uitars15_clipboard_failure_manifest_20260808.json"
)
DEFAULT_META = REPO_ROOT / "evaluation_examples/test_all.json"
DEFAULT_ARCHIVE_DIR = REPO_ROOT / "results_uitars15_clipboard_failure_archive"
EXPECTED_MANIFEST_COUNT = 84
EXPECTED_OFFICIAL_COUNT = 369
SUPPORTED_SCHEMA_VERSION = 1
CLIPBOARD_PROTOCOL = "clipboard_pyperclip_v1"
TRANSPORT_PROTOCOL = "uitars_model_transport_failure_v1"
CLIPBOARD_PROTOCOL_REASON = "clipboard_type_action_returned_pyperclip_exception"
TRANSPORT_PROTOCOL_REASON = (
    "valid_score_traj_contains_client_or_model_transport_failure"
)
TRANSPORT_FAILURE_SIGNATURE = "client/model transport failure in traj.jsonl"
TRANSPORT_EVIDENCE_LABELS = frozenset(
    label for label, _pattern in TRANSPORT_FAILURE_PATTERNS
)

# Backward-compatible name for callers which imported the original constant.
PROTOCOL_REASON = CLIPBOARD_PROTOCOL_REASON


class ArchiveSafetyError(RuntimeError):
    """Raised when a safety precondition or transaction invariant fails."""


@dataclass(frozen=True)
class TransportEvidence:
    label: str
    count: int


@dataclass(frozen=True)
class ManifestTask:
    domain: str
    task_id: str
    pyperclip_failure_count: int | None
    transport_evidence: tuple[TransportEvidence, ...]
    reason: str
    score_at_audit: float | None
    score_status_at_audit: str
    source_shard: int | None


@dataclass(frozen=True)
class ValidatedManifest:
    path: Path
    sha256: str
    manifest_name: str
    failure_protocol: str
    declared_result_root: str
    tasks: tuple[ManifestTask, ...]


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    size: int
    mode: int
    sha256: str


@dataclass(frozen=True)
class DirectorySnapshot:
    path: str
    mode: int


@dataclass(frozen=True)
class TreeSnapshot:
    device: int
    inode: int
    root_mode: int
    directories: tuple[DirectorySnapshot, ...]
    files: tuple[FileSnapshot, ...]
    total_bytes: int
    sha256: str


@dataclass(frozen=True)
class ScoreSnapshot:
    status: str
    value: float | None
    raw: str | None
    sha256: str | None


@dataclass(frozen=True)
class PlannedTask:
    manifest_task: ManifestTask
    source: Path
    tree: TreeSnapshot
    score: ScoreSnapshot


@dataclass(frozen=True)
class ArchivePlan:
    manifest: ValidatedManifest
    meta_path: Path
    meta_sha256: str
    expected_manifest_count: int
    expected_official_count: int
    result_root: Path
    tasks: tuple[PlannedTask, ...]
    fingerprint: str


@dataclass(frozen=True)
class ActiveRunner:
    pid: int
    command: tuple[str, ...]
    reason: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _safe_component(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArchiveSafetyError(f"{label} must be a non-empty string")
    if (
        value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ArchiveSafetyError(f"unsafe {label}: {value!r}")
    return value


def _load_json_object(path: Path, label: str) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ArchiveSafetyError(f"cannot read {label} {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveSafetyError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArchiveSafetyError(f"{label} root must be a JSON object")
    return payload, _sha256_bytes(raw)


def load_official_tasks(
    meta_path: Path, *, expected_count: int
) -> tuple[set[tuple[str, str]], str]:
    payload, digest = _load_json_object(meta_path, "official metadata")
    pairs: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    for raw_domain, raw_ids in payload.items():
        domain = _safe_component(raw_domain, "official domain")
        if not isinstance(raw_ids, list):
            raise ArchiveSafetyError(
                f"official domain {domain!r} must map to a task ID list"
            )
        for raw_id in raw_ids:
            task_id = _safe_component(raw_id, f"official task ID in {domain}")
            if task_id in seen_ids:
                raise ArchiveSafetyError(
                    f"duplicate task ID in official metadata: {task_id}"
                )
            seen_ids.add(task_id)
            pairs.add((domain, task_id))
    if len(pairs) != expected_count:
        raise ArchiveSafetyError(
            f"official metadata contains {len(pairs)} tasks; expected {expected_count}"
        )
    return pairs, digest


def _validated_audit_score(task: dict[str, object], label: str) -> float | None:
    status = task.get("score_status_at_audit")
    raw_score = task.get("score_at_audit")
    if status == "missing":
        if raw_score is not None:
            raise ArchiveSafetyError(f"{label}: missing score must be null")
        return None
    if status != "valid":
        raise ArchiveSafetyError(
            f"{label}: score_status_at_audit must be 'valid' or 'missing'"
        )
    if not _is_number(raw_score):
        raise ArchiveSafetyError(f"{label}: valid audit score must be numeric")
    score = float(raw_score)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ArchiveSafetyError(f"{label}: audit score must be finite and in [0, 1]")
    return score


def _validated_transport_evidence(
    raw_task: dict[str, object], label: str
) -> tuple[TransportEvidence, ...]:
    raw_evidence = raw_task.get("transport_evidence")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise ArchiveSafetyError(
            f"{label}: transport_evidence must be a non-empty list"
        )

    evidence: list[TransportEvidence] = []
    seen_labels: set[str] = set()
    for evidence_index, raw_item in enumerate(raw_evidence):
        item_label = f"{label} transport_evidence[{evidence_index}]"
        if not isinstance(raw_item, dict):
            raise ArchiveSafetyError(f"{item_label} must be an object")
        if set(raw_item) != {"label", "count"}:
            raise ArchiveSafetyError(
                f"{item_label} must contain exactly 'label' and 'count'"
            )
        evidence_label = raw_item.get("label")
        if (
            not isinstance(evidence_label, str)
            or evidence_label not in TRANSPORT_EVIDENCE_LABELS
        ):
            raise ArchiveSafetyError(
                f"{item_label} has unsupported canonical label: {evidence_label!r}"
            )
        if evidence_label in seen_labels:
            raise ArchiveSafetyError(
                f"{label}: duplicate transport evidence label: {evidence_label!r}"
            )
        seen_labels.add(evidence_label)
        count = raw_item.get("count")
        if not _is_integer(count) or count < 1:
            raise ArchiveSafetyError(
                f"{item_label} count must be a positive integer"
            )
        evidence.append(TransportEvidence(evidence_label, count))
    return tuple(sorted(evidence, key=lambda item: item.label))


def _validated_source_shard(
    raw_task: dict[str, object], label: str, *, required: bool
) -> int | None:
    source_shard = raw_task.get("source_shard")
    if source_shard is None and not required:
        return None
    if not _is_integer(source_shard) or not 0 <= source_shard < 4:
        qualifier = "" if required else " when present"
        raise ArchiveSafetyError(
            f"{label}: source_shard must be an integer from 0 through 3{qualifier}"
        )
    return source_shard


def validate_failure_manifest(
    manifest_path: Path,
    meta_path: Path,
    *,
    expected_manifest_count: int = EXPECTED_MANIFEST_COUNT,
    expected_official_count: int = EXPECTED_OFFICIAL_COUNT,
) -> tuple[ValidatedManifest, str]:
    payload, manifest_digest = _load_json_object(manifest_path, "failure manifest")
    official_pairs, meta_digest = load_official_tasks(
        meta_path, expected_count=expected_official_count
    )

    if payload.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ArchiveSafetyError(
            f"unsupported manifest schema_version: {payload.get('schema_version')!r}"
        )
    if payload.get("benchmark") != "OSWorld":
        raise ArchiveSafetyError("failure manifest benchmark must be 'OSWorld'")
    declared_protocol = payload.get("failure_protocol")
    if declared_protocol is None:
        failure_protocol = CLIPBOARD_PROTOCOL
    elif (
        isinstance(declared_protocol, str)
        and declared_protocol in {CLIPBOARD_PROTOCOL, TRANSPORT_PROTOCOL}
    ):
        failure_protocol = declared_protocol
    else:
        raise ArchiveSafetyError(
            f"unsupported failure_protocol: {declared_protocol!r}"
        )

    signature = payload.get("failure_signature")
    if failure_protocol == CLIPBOARD_PROTOCOL:
        if (
            not isinstance(signature, str)
            or "pyperclip.PyperclipException" not in signature
        ):
            raise ArchiveSafetyError(
                "clipboard manifest does not carry the required "
                "PyperclipException signature"
            )
    elif signature != TRANSPORT_FAILURE_SIGNATURE:
        raise ArchiveSafetyError(
            "transport manifest failure_signature must be "
            f"{TRANSPORT_FAILURE_SIGNATURE!r}"
        )
    manifest_name = payload.get("manifest_name")
    if not isinstance(manifest_name, str) or not manifest_name:
        raise ArchiveSafetyError("failure manifest_name must be a non-empty string")
    declared_result_root = payload.get("result_root")
    if not isinstance(declared_result_root, str) or not declared_result_root:
        raise ArchiveSafetyError("failure manifest result_root must be a path string")

    declared_count = payload.get("strict_retry_count")
    if not _is_integer(declared_count) or declared_count != expected_manifest_count:
        raise ArchiveSafetyError(
            f"strict_retry_count is {declared_count!r}; expected {expected_manifest_count}"
        )
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ArchiveSafetyError("failure manifest tasks must be a list")
    if len(raw_tasks) != expected_manifest_count:
        raise ArchiveSafetyError(
            f"failure manifest contains {len(raw_tasks)} tasks; "
            f"expected {expected_manifest_count}"
        )

    tasks: list[ManifestTask] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    for index, raw_task in enumerate(raw_tasks):
        label = f"manifest task[{index}]"
        if not isinstance(raw_task, dict):
            raise ArchiveSafetyError(f"{label} must be an object")
        domain = _safe_component(raw_task.get("domain"), f"{label} domain")
        task_id = _safe_component(raw_task.get("id"), f"{label} ID")
        pair = (domain, task_id)
        if pair in seen_pairs or task_id in seen_ids:
            raise ArchiveSafetyError(f"duplicate manifest task: {domain}/{task_id}")
        seen_pairs.add(pair)
        seen_ids.add(task_id)
        if pair not in official_pairs:
            raise ArchiveSafetyError(
                f"manifest task is not in official metadata: {domain}/{task_id}"
            )

        reason = raw_task.get("reason")
        audit_score = _validated_audit_score(raw_task, label)
        score_status = str(raw_task["score_status_at_audit"])
        if failure_protocol == CLIPBOARD_PROTOCOL:
            failure_count = raw_task.get("pyperclip_failure_count")
            if not _is_integer(failure_count) or failure_count < 1:
                raise ArchiveSafetyError(
                    f"{label}: pyperclip_failure_count must be a positive integer"
                )
            if "transport_evidence" in raw_task:
                raise ArchiveSafetyError(
                    f"{label}: clipboard protocol cannot carry transport_evidence"
                )
            if reason != CLIPBOARD_PROTOCOL_REASON:
                raise ArchiveSafetyError(
                    f"{label}: reason must be {CLIPBOARD_PROTOCOL_REASON!r}"
                )
            transport_evidence: tuple[TransportEvidence, ...] = ()
            source_shard = _validated_source_shard(
                raw_task, label, required=True
            )
        else:
            if "pyperclip_failure_count" in raw_task:
                raise ArchiveSafetyError(
                    f"{label}: transport protocol cannot carry "
                    "pyperclip_failure_count"
                )
            if reason != TRANSPORT_PROTOCOL_REASON:
                raise ArchiveSafetyError(
                    f"{label}: reason must be {TRANSPORT_PROTOCOL_REASON!r}"
                )
            if score_status != "valid" or audit_score is None:
                raise ArchiveSafetyError(
                    f"{label}: transport protocol requires a valid audit score"
                )
            failure_count = None
            transport_evidence = _validated_transport_evidence(raw_task, label)
            source_shard = _validated_source_shard(
                raw_task, label, required=False
            )
        tasks.append(
            ManifestTask(
                domain=domain,
                task_id=task_id,
                pyperclip_failure_count=failure_count,
                transport_evidence=transport_evidence,
                reason=reason,
                score_at_audit=audit_score,
                score_status_at_audit=score_status,
                source_shard=source_shard,
            )
        )

    actual_domain_counts = dict(sorted(Counter(task.domain for task in tasks).items()))
    if payload.get("domain_counts") != actual_domain_counts:
        raise ArchiveSafetyError(
            "manifest domain_counts does not match its task list: "
            f"expected {actual_domain_counts}"
        )
    actual_score_counts = {
        "missing_or_invalid": sum(
            task.score_status_at_audit != "valid" for task in tasks
        ),
        "one": sum(task.score_at_audit == 1.0 for task in tasks),
        "partial": sum(
            task.score_at_audit is not None and 0.0 < task.score_at_audit < 1.0
            for task in tasks
        ),
        "zero": sum(task.score_at_audit == 0.0 for task in tasks),
    }
    if payload.get("score_snapshot_counts") != actual_score_counts:
        raise ArchiveSafetyError(
            "manifest score_snapshot_counts does not match its task list: "
            f"expected {actual_score_counts}"
        )

    return (
        ValidatedManifest(
            path=manifest_path,
            sha256=manifest_digest,
            manifest_name=manifest_name,
            failure_protocol=failure_protocol,
            declared_result_root=declared_result_root,
            tasks=tuple(tasks),
        ),
        meta_digest,
    )


def resolve_result_root(
    manifest: ValidatedManifest, override: Path | None = None
) -> Path:
    declared = Path(manifest.declared_result_root).expanduser()
    if not declared.is_absolute():
        declared = REPO_ROOT / declared
    path = declared if override is None else override.expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    if declared.resolve(strict=False) != path.resolve(strict=False):
        raise ArchiveSafetyError(
            "result root override does not match the path authorized by the manifest"
        )
    if path.is_symlink() or declared.is_symlink():
        raise ArchiveSafetyError(f"result root cannot be a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ArchiveSafetyError(f"cannot resolve result root {path}: {exc}") from exc
    if not resolved.is_dir():
        raise ArchiveSafetyError(f"result root is not a directory: {resolved}")
    return resolved


def safe_external_archive_base(path: Path, result_root: Path) -> Path:
    archive_base = path.expanduser().resolve(strict=False)
    protected = result_root.resolve(strict=True)
    if (
        archive_base == protected
        or archive_base.is_relative_to(protected)
        or protected.is_relative_to(archive_base)
    ):
        raise ArchiveSafetyError(
            "archive directory must be separate from, and not contain, the result "
            f"root: {archive_base}"
        )
    return archive_base


def _hash_regular_file(path: Path, expected: os.stat_result) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArchiveSafetyError(f"cannot open result file {path}: {exc}") from exc
    hasher = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        identity = (expected.st_dev, expected.st_ino, expected.st_size)
        if (opened.st_dev, opened.st_ino, opened.st_size) != identity:
            raise ArchiveSafetyError(f"result file changed while opening: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
        finished = os.fstat(descriptor)
        if (
            finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ino != opened.st_ino
            or finished.st_dev != opened.st_dev
        ):
            raise ArchiveSafetyError(f"result file changed while hashing: {path}")
    finally:
        os.close(descriptor)
    return hasher.hexdigest()


def snapshot_tree(task_dir: Path) -> TreeSnapshot:
    try:
        root_stat = task_dir.lstat()
    except OSError as exc:
        raise ArchiveSafetyError(f"cannot stat task directory {task_dir}: {exc}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ArchiveSafetyError(f"task path is not a real directory: {task_dir}")

    directories: list[DirectorySnapshot] = []
    files: list[FileSnapshot] = []
    try:
        walker = os.walk(task_dir, topdown=True, followlinks=False)
        for raw_root, dir_names, file_names in walker:
            current = Path(raw_root)
            dir_names.sort()
            file_names.sort()
            for name in dir_names:
                path = current / name
                entry_stat = path.lstat()
                if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(
                    entry_stat.st_mode
                ):
                    raise ArchiveSafetyError(
                        f"unsupported entry in task directory: {path}"
                    )
                directories.append(
                    DirectorySnapshot(
                        path=path.relative_to(task_dir).as_posix(),
                        mode=stat.S_IMODE(entry_stat.st_mode),
                    )
                )
            for name in file_names:
                path = current / name
                entry_stat = path.lstat()
                if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(
                    entry_stat.st_mode
                ):
                    raise ArchiveSafetyError(
                        f"unsupported entry in task directory: {path}"
                    )
                files.append(
                    FileSnapshot(
                        path=path.relative_to(task_dir).as_posix(),
                        size=entry_stat.st_size,
                        mode=stat.S_IMODE(entry_stat.st_mode),
                        sha256=_hash_regular_file(path, entry_stat),
                    )
                )
    except ArchiveSafetyError:
        raise
    except OSError as exc:
        raise ArchiveSafetyError(f"cannot scan task directory {task_dir}: {exc}") from exc

    directories.sort(key=lambda item: item.path)
    files.sort(key=lambda item: item.path)
    digest_payload = {
        "root_mode": stat.S_IMODE(root_stat.st_mode),
        "directories": [item.__dict__ for item in directories],
        "files": [item.__dict__ for item in files],
    }
    tree_digest = _sha256_bytes(
        json.dumps(
            digest_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    return TreeSnapshot(
        device=root_stat.st_dev,
        inode=root_stat.st_ino,
        root_mode=stat.S_IMODE(root_stat.st_mode),
        directories=tuple(directories),
        files=tuple(files),
        total_bytes=sum(item.size for item in files),
        sha256=tree_digest,
    )


def snapshot_score(task_dir: Path, tree: TreeSnapshot) -> ScoreSnapshot:
    result_file = next((item for item in tree.files if item.path == "result.txt"), None)
    if result_file is None:
        return ScoreSnapshot("missing", None, None, None)
    path = task_dir / "result.txt"
    if result_file.size > 4096:
        return ScoreSnapshot("invalid", None, "<result.txt exceeds 4096 bytes>", result_file.sha256)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ArchiveSafetyError(f"cannot read score file {path}: {exc}") from exc
    if _sha256_bytes(raw_bytes) != result_file.sha256:
        raise ArchiveSafetyError(f"score file changed after tree snapshot: {path}")
    try:
        raw = raw_bytes.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ScoreSnapshot("invalid", None, "<result.txt is not UTF-8>", result_file.sha256)
    try:
        value = float(raw)
    except ValueError:
        return ScoreSnapshot("invalid", None, raw, result_file.sha256)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return ScoreSnapshot("invalid", None, raw, result_file.sha256)
    return ScoreSnapshot("valid", value, raw, result_file.sha256)


def transport_evidence_counts(text: str) -> tuple[TransportEvidence, ...]:
    """Return exact counts for the canonical transport labels found in text."""

    evidence = []
    for label, pattern in TRANSPORT_FAILURE_PATTERNS:
        count = sum(1 for _match in pattern.finditer(text))
        if count:
            evidence.append(TransportEvidence(label=label, count=count))
    return tuple(sorted(evidence, key=lambda item: item.label))


def _read_snapshotted_file(
    task_dir: Path, tree: TreeSnapshot, relative_path: str
) -> bytes:
    file_snapshot = next(
        (item for item in tree.files if item.path == relative_path), None
    )
    if file_snapshot is None:
        raise ArchiveSafetyError(
            f"required protocol evidence file is missing: {task_dir / relative_path}"
        )
    path = task_dir / relative_path
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ArchiveSafetyError(f"cannot read protocol evidence {path}: {exc}") from exc
    if len(raw) != file_snapshot.size or _sha256_bytes(raw) != file_snapshot.sha256:
        raise ArchiveSafetyError(
            f"protocol evidence changed after tree snapshot: {path}"
        )
    return raw


def _validate_transport_snapshot(
    task: ManifestTask,
    task_dir: Path,
    tree: TreeSnapshot,
    score: ScoreSnapshot,
) -> None:
    if score.status != "valid" or score.value != task.score_at_audit:
        raise ArchiveSafetyError(
            "transport task's current result.txt does not match its valid manifest "
            f"score: {task.domain}/{task.task_id}"
        )
    traj_text = _read_snapshotted_file(task_dir, tree, "traj.jsonl").decode(
        "utf-8", errors="replace"
    )
    actual_evidence = transport_evidence_counts(traj_text)
    if not actual_evidence:
        raise ArchiveSafetyError(
            "transport task traj.jsonl contains no canonical client/model failure: "
            f"{task.domain}/{task.task_id}"
        )
    if actual_evidence != task.transport_evidence:
        declared = [item.__dict__ for item in task.transport_evidence]
        actual = [item.__dict__ for item in actual_evidence]
        raise ArchiveSafetyError(
            "transport_evidence does not exactly match traj.jsonl for "
            f"{task.domain}/{task.task_id}: declared {declared}, actual {actual}"
        )


def _snapshot_task(task: ManifestTask, result_root: Path) -> PlannedTask:
    domain_dir = result_root / task.domain
    source = domain_dir / task.task_id
    if domain_dir.is_symlink():
        raise ArchiveSafetyError(f"domain directory cannot be a symlink: {domain_dir}")
    try:
        source.relative_to(result_root)
    except ValueError as exc:  # defensive; components were validated earlier
        raise ArchiveSafetyError(f"task path escaped result root: {source}") from exc
    tree = snapshot_tree(source)
    score = snapshot_score(source, tree)
    planned = PlannedTask(
        manifest_task=task,
        source=source,
        tree=tree,
        score=score,
    )
    if task.transport_evidence:
        _validate_transport_snapshot(task, source, tree, score)
    return planned


def _plan_fingerprint(
    manifest_digest: str,
    meta_digest: str,
    result_root: Path,
    tasks: Sequence[PlannedTask],
) -> str:
    payload = {
        "manifest_sha256": manifest_digest,
        "meta_sha256": meta_digest,
        "result_root": str(result_root),
        "tasks": [
            {
                "domain": item.manifest_task.domain,
                "id": item.manifest_task.task_id,
                "device": item.tree.device,
                "inode": item.tree.inode,
                "tree_sha256": item.tree.sha256,
                "score": item.score.__dict__,
            }
            for item in tasks
        ],
    }
    return _sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def build_archive_plan(
    manifest_path: Path,
    meta_path: Path,
    *,
    result_root: Path | None = None,
    expected_manifest_count: int = EXPECTED_MANIFEST_COUNT,
    expected_official_count: int = EXPECTED_OFFICIAL_COUNT,
) -> ArchivePlan:
    manifest_path = manifest_path.expanduser().resolve(strict=True)
    meta_path = meta_path.expanduser().resolve(strict=True)
    manifest, meta_digest = validate_failure_manifest(
        manifest_path,
        meta_path,
        expected_manifest_count=expected_manifest_count,
        expected_official_count=expected_official_count,
    )
    resolved_result_root = resolve_result_root(manifest, result_root)
    planned_tasks = tuple(
        _snapshot_task(task, resolved_result_root) for task in manifest.tasks
    )
    fingerprint = _plan_fingerprint(
        manifest.sha256, meta_digest, resolved_result_root, planned_tasks
    )
    return ArchivePlan(
        manifest=manifest,
        meta_path=meta_path,
        meta_sha256=meta_digest,
        expected_manifest_count=expected_manifest_count,
        expected_official_count=expected_official_count,
        result_root=resolved_result_root,
        tasks=planned_tasks,
        fingerprint=fingerprint,
    )


def _resolved_process_path(value: str, cwd: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve(strict=False)


def _looks_like_runner(command: Sequence[str]) -> bool:
    for item in command:
        name = Path(item).name
        if name.startswith("run_multienv_") and name.endswith(".py"):
            return True
        if name in {
            "retry_uitars_failures.py",
            "run_gdrive_only.py",
            "run_multienv_uitars.py",
        }:
            return True
    return False


def find_active_runners(
    result_root: Path,
    *,
    proc_root: Path = Path("/proc"),
    current_pid: int | None = None,
) -> list[ActiveRunner]:
    """Find known OSWorld runners or processes writing into this result root."""

    current_pid = os.getpid() if current_pid is None else current_pid
    target = result_root.resolve(strict=True)
    try:
        process_dirs = list(proc_root.iterdir())
    except OSError as exc:
        raise ArchiveSafetyError(f"cannot inspect active processes: {exc}") from exc

    active: list[ActiveRunner] = []
    for process_dir in process_dirs:
        if not process_dir.name.isdigit() or int(process_dir.name) == current_pid:
            continue
        pid = int(process_dir.name)
        try:
            raw = (process_dir / "cmdline").read_bytes()
            command = tuple(
                part.decode("utf-8", errors="replace")
                for part in raw.split(b"\0")
                if part
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if not command:
            continue
        known_runner = _looks_like_runner(command)
        try:
            cwd = (process_dir / "cwd").resolve(strict=True)
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            cwd = Path("/")
        configured_results: list[Path] = []
        for index, item in enumerate(command):
            if item in {"--result_dir", "--result-dir"} and index + 1 < len(command):
                configured_results.append(_resolved_process_path(command[index + 1], cwd))
            elif item.startswith("--result_dir=") or item.startswith("--result-dir="):
                configured_results.append(
                    _resolved_process_path(item.partition("=")[2], cwd)
                )
        writes_target = any(
            configured == target or target.is_relative_to(configured)
            for configured in configured_results
        )
        if known_runner or writes_target:
            reason = "known OSWorld runner" if known_runner else "matching result directory"
            active.append(ActiveRunner(pid=pid, command=command, reason=reason))
    return sorted(active, key=lambda item: item.pid)


@contextlib.contextmanager
def exclusive_archive_lock(result_root: Path) -> Iterator[None]:
    digest = hashlib.sha256(str(result_root).encode("utf-8")).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"osworld-manifest-archive-{digest}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveSafetyError(
                "another manifest archive transaction is already active"
            ) from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_json_write(path: Path, payload: object) -> None:
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
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _tree_payload(tree: TreeSnapshot) -> dict[str, object]:
    return {
        "source_device": tree.device,
        "source_inode": tree.inode,
        "root_mode": oct(tree.root_mode),
        "directory_count": len(tree.directories),
        "file_count": len(tree.files),
        "total_bytes": tree.total_bytes,
        "tree_sha256": tree.sha256,
        "directories": [
            {"path": item.path, "mode": oct(item.mode)} for item in tree.directories
        ],
        "files": [
            {
                "path": item.path,
                "size": item.size,
                "mode": oct(item.mode),
                "sha256": item.sha256,
            }
            for item in tree.files
        ],
    }


def _protocol_evidence_payload(
    failure_protocol: str, task: ManifestTask
) -> dict[str, object]:
    if failure_protocol == CLIPBOARD_PROTOCOL:
        return {
            "reason": task.reason,
            "pyperclip_failure_count": task.pyperclip_failure_count,
            "source_shard": task.source_shard,
        }
    evidence: dict[str, object] = {
        "reason": task.reason,
        "transport_evidence": [item.__dict__ for item in task.transport_evidence],
    }
    if task.source_shard is not None:
        evidence["source_shard"] = task.source_shard
    return evidence


def _transaction_payload(
    plan: ArchivePlan,
    archive_root: Path,
    *,
    run_id: str,
    created_at: str,
    status: str,
    states: dict[tuple[str, str], str],
    error: str | None = None,
    rollback_errors: Sequence[str] = (),
) -> dict[str, object]:
    tasks: list[dict[str, object]] = []
    for item in plan.tasks:
        task = item.manifest_task
        destination = archive_root / "tasks" / task.domain / task.task_id
        tasks.append(
            {
                "domain": task.domain,
                "id": task.task_id,
                "state": states[(task.domain, task.task_id)],
                "source": str(item.source),
                "destination": str(destination),
                "protocol_evidence": _protocol_evidence_payload(
                    plan.manifest.failure_protocol, task
                ),
                "score_at_manifest_audit": {
                    "status": task.score_status_at_audit,
                    "value": task.score_at_audit,
                },
                "score_at_archive_plan": item.score.__dict__,
                "source_tree": _tree_payload(item.tree),
            }
        )
    payload: dict[str, object] = {
        "schema_version": 1,
        "operation": "archive_osworld_protocol_failure_manifest",
        "run_id": run_id,
        "status": status,
        "created_at": created_at,
        "updated_at": _utc_now(),
        "input_manifest": str(plan.manifest.path),
        "input_manifest_sha256": plan.manifest.sha256,
        "input_manifest_name": plan.manifest.manifest_name,
        "failure_protocol": plan.manifest.failure_protocol,
        "official_metadata": str(plan.meta_path),
        "official_metadata_sha256": plan.meta_sha256,
        "result_root": str(plan.result_root),
        "archive_root": str(archive_root),
        "task_count": len(tasks),
        "plan_fingerprint": plan.fingerprint,
        "tasks": tasks,
    }
    if error is not None:
        payload["error"] = error
    if rollback_errors:
        payload["rollback_errors"] = list(rollback_errors)
    return payload


def _nearest_existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise ArchiveSafetyError(f"no existing ancestor for archive path: {path}")
        candidate = parent
    return candidate


def _same_snapshot(left: PlannedTask, right: PlannedTask) -> bool:
    return (
        left.manifest_task == right.manifest_task
        and left.tree == right.tree
        and left.score == right.score
    )


def execute_archive(
    initial_plan: ArchivePlan,
    archive_base: Path,
    *,
    run_id: str | None = None,
    proc_root: Path = Path("/proc"),
    current_pid: int | None = None,
    replace_path: Callable[[Path, Path], None] = os.replace,
) -> Path:
    """Execute a validated plan and return its committed transaction manifest."""

    archive_base = safe_external_archive_base(archive_base, initial_plan.result_root)
    active = find_active_runners(
        initial_plan.result_root, proc_root=proc_root, current_pid=current_pid
    )
    if active:
        raise ArchiveSafetyError(
            "refusing to archive while OSWorld runners are active: "
            + ", ".join(str(item.pid) for item in active)
        )

    # Re-read both manifests and re-hash every source after --run authorization.
    current_plan = build_archive_plan(
        initial_plan.manifest.path,
        initial_plan.meta_path,
        result_root=initial_plan.result_root,
        expected_manifest_count=initial_plan.expected_manifest_count,
        expected_official_count=initial_plan.expected_official_count,
    )
    if current_plan.fingerprint != initial_plan.fingerprint:
        raise ArchiveSafetyError(
            "archive inputs changed between initial planning and --run revalidation"
        )

    existing_archive_parent = _nearest_existing_ancestor(archive_base)
    result_device = initial_plan.result_root.stat().st_dev
    if existing_archive_parent.stat().st_dev != result_device:
        raise ArchiveSafetyError(
            "archive and result directories must be on the same filesystem for "
            "atomic os.replace moves"
        )
    if any(item.tree.device != result_device for item in initial_plan.tasks):
        raise ArchiveSafetyError(
            "every task directory must be on the result filesystem for atomic moves"
        )

    with exclusive_archive_lock(initial_plan.result_root):
        locked_plan = build_archive_plan(
            initial_plan.manifest.path,
            initial_plan.meta_path,
            result_root=initial_plan.result_root,
            expected_manifest_count=initial_plan.expected_manifest_count,
            expected_official_count=initial_plan.expected_official_count,
        )
        if locked_plan.fingerprint != initial_plan.fingerprint:
            raise ArchiveSafetyError(
                "archive inputs changed before the locked validation completed"
            )
        active = find_active_runners(
            initial_plan.result_root, proc_root=proc_root, current_pid=current_pid
        )
        if active:
            raise ArchiveSafetyError("an OSWorld runner became active before archiving")

        archive_base.mkdir(parents=True, exist_ok=True)
        if archive_base.stat().st_dev != initial_plan.result_root.stat().st_dev:
            raise ArchiveSafetyError(
                "archive directory is not on the result filesystem"
            )
        if run_id is None:
            run_id = (
                f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
                f"{uuid.uuid4().hex[:10]}"
            )
        _safe_component(run_id, "archive run ID")
        archive_root = archive_base / run_id
        archive_root.mkdir(parents=False, exist_ok=False)
        transaction_path = archive_root / "transaction.json"
        created_at = _utc_now()
        states = {
            (item.manifest_task.domain, item.manifest_task.task_id): "planned"
            for item in locked_plan.tasks
        }
        moved: list[PlannedTask] = []
        _atomic_json_write(
            transaction_path,
            _transaction_payload(
                locked_plan,
                archive_root,
                run_id=run_id,
                created_at=created_at,
                status="prepared",
                states=states,
            ),
        )

        try:
            # This check is deliberately adjacent to the first source mutation.
            if find_active_runners(
                initial_plan.result_root,
                proc_root=proc_root,
                current_pid=current_pid,
            ):
                raise ArchiveSafetyError(
                    "an OSWorld runner became active immediately before archiving"
                )

            for expected in locked_plan.tasks:
                # Close the gap between the locked whole-plan hash and this move.
                immediate = _snapshot_task(
                    expected.manifest_task, locked_plan.result_root
                )
                if not _same_snapshot(expected, immediate):
                    raise ArchiveSafetyError(
                        "task changed immediately before move: "
                        f"{expected.manifest_task.domain}/"
                        f"{expected.manifest_task.task_id}"
                    )
                destination = (
                    archive_root
                    / "tasks"
                    / expected.manifest_task.domain
                    / expected.manifest_task.task_id
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    raise ArchiveSafetyError(
                        f"archive destination already exists: {destination}"
                    )
                replace_path(expected.source, destination)
                moved.append(expected)
                key = (expected.manifest_task.domain, expected.manifest_task.task_id)
                states[key] = "archived"
                _atomic_json_write(
                    transaction_path,
                    _transaction_payload(
                        locked_plan,
                        archive_root,
                        run_id=run_id,
                        created_at=created_at,
                        status="moving",
                        states=states,
                    ),
                )

            revalidated_manifest, revalidated_meta_digest = validate_failure_manifest(
                locked_plan.manifest.path,
                locked_plan.meta_path,
                expected_manifest_count=locked_plan.expected_manifest_count,
                expected_official_count=locked_plan.expected_official_count,
            )
            if (
                revalidated_manifest.sha256 != locked_plan.manifest.sha256
                or revalidated_meta_digest != locked_plan.meta_sha256
            ):
                raise ArchiveSafetyError("input metadata changed during archiving")
            if find_active_runners(
                initial_plan.result_root,
                proc_root=proc_root,
                current_pid=current_pid,
            ):
                raise ArchiveSafetyError("an OSWorld runner became active during archiving")

            for expected in locked_plan.tasks:
                if expected.source.exists() or expected.source.is_symlink():
                    raise ArchiveSafetyError(
                        f"source unexpectedly exists after move: {expected.source}"
                    )
                destination = (
                    archive_root
                    / "tasks"
                    / expected.manifest_task.domain
                    / expected.manifest_task.task_id
                )
                archived_tree = snapshot_tree(destination)
                archived_score = snapshot_score(destination, archived_tree)
                if archived_tree != expected.tree or archived_score != expected.score:
                    raise ArchiveSafetyError(
                        "post-move hash validation failed: "
                        f"{expected.manifest_task.domain}/"
                        f"{expected.manifest_task.task_id}"
                    )

            _atomic_json_write(
                transaction_path,
                _transaction_payload(
                    locked_plan,
                    archive_root,
                    run_id=run_id,
                    created_at=created_at,
                    status="committed",
                    states=states,
                ),
            )
            return transaction_path
        except BaseException as exc:
            rollback_errors: list[str] = []
            for item in reversed(moved):
                key = (item.manifest_task.domain, item.manifest_task.task_id)
                destination = (
                    archive_root
                    / "tasks"
                    / item.manifest_task.domain
                    / item.manifest_task.task_id
                )
                try:
                    if item.source.exists() or item.source.is_symlink():
                        raise ArchiveSafetyError(
                            f"rollback source already exists: {item.source}"
                        )
                    item.source.parent.mkdir(parents=True, exist_ok=True)
                    replace_path(destination, item.source)
                    restored_tree = snapshot_tree(item.source)
                    restored_score = snapshot_score(item.source, restored_tree)
                    if restored_tree != item.tree or restored_score != item.score:
                        raise ArchiveSafetyError(
                            f"restored task failed hash validation: {item.source}"
                        )
                    states[key] = "rolled_back"
                except BaseException as rollback_exc:
                    states[key] = "rollback_failed"
                    rollback_errors.append(str(rollback_exc))
            for item in locked_plan.tasks:
                key = (item.manifest_task.domain, item.manifest_task.task_id)
                if states[key] == "planned":
                    states[key] = "unchanged"
            status = "rollback_failed" if rollback_errors else "rolled_back"
            try:
                _atomic_json_write(
                    transaction_path,
                    _transaction_payload(
                        locked_plan,
                        archive_root,
                        run_id=run_id,
                        created_at=created_at,
                        status=status,
                        states=states,
                        error=str(exc),
                        rollback_errors=rollback_errors,
                    ),
                )
            except BaseException as journal_exc:
                rollback_errors.append(f"cannot update transaction manifest: {journal_exc}")
            detail = f"archive transaction failed and status is {status}: {exc}"
            if rollback_errors:
                detail += "; " + "; ".join(rollback_errors)
            raise ArchiveSafetyError(detail) from exc


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Perform the archive transaction; default is a read-only dry run",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=(
            "Clipboard legacy manifest or explicit "
            "uitars_model_transport_failure_v1 manifest"
        ),
    )
    parser.add_argument(
        "--meta",
        type=Path,
        default=DEFAULT_META,
        help="Authoritative OSWorld domain-to-task-ID metadata",
    )
    parser.add_argument(
        "--result-root",
        type=Path,
        default=None,
        help="Override the result_root declared by the failure manifest",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=DEFAULT_ARCHIVE_DIR,
        help="External archive base (must share the result filesystem)",
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=EXPECTED_MANIFEST_COUNT,
        help="Exact number of tasks authorized by the manifest",
    )
    parser.add_argument(
        "--expected-official-count",
        type=int,
        default=EXPECTED_OFFICIAL_COUNT,
        help="Exact number of unique tasks required in official metadata",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Omit per-task dry-run details",
    )
    return parser.parse_args(argv)


def _print_plan(
    plan: ArchivePlan,
    archive_base: Path,
    active: Sequence[ActiveRunner],
    *,
    summary_only: bool,
) -> None:
    score_counts = Counter(item.score.status for item in plan.tasks)
    file_count = sum(len(item.tree.files) for item in plan.tasks)
    total_bytes = sum(item.tree.total_bytes for item in plan.tasks)
    print("OSWorld UI-TARS protocol-failure archive plan")
    print(f"Failure protocol: {plan.manifest.failure_protocol}")
    print(f"Authorized manifest tasks: {len(plan.tasks)}")
    print(f"Current scores: {dict(sorted(score_counts.items()))}")
    print(f"Audited files: {file_count} ({total_bytes} bytes)")
    print(f"Result root: {plan.result_root}")
    print(f"External archive base: {archive_base}")
    print(f"Active OSWorld runners: {len(active)}")
    print(f"Plan fingerprint: {plan.fingerprint}")
    if not summary_only:
        for item in plan.tasks:
            score = item.score.value if item.score.status == "valid" else item.score.status
            print(
                f"  {item.manifest_task.domain}/{item.manifest_task.task_id} "
                f"score={score} files={len(item.tree.files)} "
                f"sha256={item.tree.sha256}"
            )
    if active:
        print(
            "RUN BLOCKED: active runner PIDs "
            + ", ".join(str(item.pid) for item in active)
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.expected_count < 1 or args.expected_official_count < 1:
        raise ArchiveSafetyError("expected counts must be positive")
    plan = build_archive_plan(
        args.manifest,
        args.meta,
        result_root=args.result_root,
        expected_manifest_count=args.expected_count,
        expected_official_count=args.expected_official_count,
    )
    archive_base = safe_external_archive_base(args.archive_dir, plan.result_root)
    active = find_active_runners(plan.result_root)
    _print_plan(plan, archive_base, active, summary_only=args.summary_only)
    if not args.run:
        print("DRY RUN: no files or directories were changed")
        return 0
    if active:
        raise ArchiveSafetyError("formal OSWorld runner is still active")
    transaction_path = execute_archive(plan, archive_base)
    print(f"COMMITTED: transaction manifest is {transaction_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ArchiveSafetyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
