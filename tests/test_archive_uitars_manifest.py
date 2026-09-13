from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts/python"
sys.path.insert(0, str(SCRIPTS))

from archive_uitars_manifest import (  # noqa: E402
    ArchiveSafetyError,
    TRANSPORT_PROTOCOL,
    TRANSPORT_PROTOCOL_REASON,
    build_archive_plan,
    execute_archive,
    main,
    safe_external_archive_base,
)


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def task_id(index: int) -> str:
    return f"00000000-0000-0000-0000-{index:012d}"


def make_fixture(root: Path, scores: list[str | None]):
    result_root = root / "formal/pyautogui/screenshot/uitars15-7b"
    ids = [task_id(index + 1) for index in range(len(scores))]
    meta_path = root / "test_all.json"
    meta_path.write_text(json.dumps({"os": ids}), encoding="utf-8")
    tasks = []
    counts = {"missing_or_invalid": 0, "one": 0, "partial": 0, "zero": 0}
    for index, (current_id, score) in enumerate(zip(ids, scores)):
        task_dir = result_root / "os" / current_id
        write(task_dir / "traj.jsonl", json.dumps({"step": index}) + "\n")
        if score is None:
            status = "missing"
            audit_score = None
            counts["missing_or_invalid"] += 1
        else:
            write(task_dir / "result.txt", score + "\n")
            status = "valid"
            audit_score = float(score)
            if audit_score == 0:
                counts["zero"] += 1
            elif audit_score == 1:
                counts["one"] += 1
            else:
                counts["partial"] += 1
        tasks.append(
            {
                "domain": "os",
                "id": current_id,
                "pyperclip_failure_count": index + 1,
                "reason": "clipboard_type_action_returned_pyperclip_exception",
                "score_at_audit": audit_score,
                "score_status_at_audit": status,
                "source_shard": index % 4,
            }
        )
    manifest = {
        "schema_version": 1,
        "benchmark": "OSWorld",
        "manifest_name": "test-clipboard-failures",
        "failure_signature": (
            "pyperclip.PyperclipException: Pyperclip could not find a mechanism"
        ),
        "result_root": str(result_root),
        "strict_retry_count": len(tasks),
        "domain_counts": {"os": len(tasks)},
        "score_snapshot_counts": counts,
        "tasks": tasks,
    }
    manifest_path = root / "failures.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, meta_path, result_root, ids


def plan_for(root: Path, scores: list[str | None]):
    manifest, meta, result_root, ids = make_fixture(root, scores)
    plan = build_archive_plan(
        manifest,
        meta,
        result_root=result_root,
        expected_manifest_count=len(scores),
        expected_official_count=len(scores),
    )
    return plan, manifest, meta, result_root, ids


def make_transport_fixture(root: Path, scores: list[str]):
    result_root = root / "formal/pyautogui/screenshot/uitars15-7b"
    ids = [task_id(index + 1) for index in range(len(scores))]
    meta_path = root / "test_all.json"
    meta_path.write_text(json.dumps({"os": ids}), encoding="utf-8")
    tasks = []
    counts = {"missing_or_invalid": 0, "one": 0, "partial": 0, "zero": 0}
    for index, (current_id, score) in enumerate(zip(ids, scores)):
        task_dir = result_root / "os" / current_id
        write(task_dir / "traj.jsonl", json.dumps({"response": "client error"}) + "\n")
        write(task_dir / "result.txt", score + "\n")
        audit_score = float(score)
        if audit_score == 0:
            counts["zero"] += 1
        elif audit_score == 1:
            counts["one"] += 1
        else:
            counts["partial"] += 1
        tasks.append(
            {
                "domain": "os",
                "id": current_id,
                "reason": TRANSPORT_PROTOCOL_REASON,
                "transport_evidence": [{"label": "client error", "count": 1}],
                "score_at_audit": audit_score,
                "score_status_at_audit": "valid",
            }
        )
    manifest = {
        "schema_version": 1,
        "benchmark": "OSWorld",
        "manifest_name": "test-model-transport-failures",
        "failure_protocol": TRANSPORT_PROTOCOL,
        "failure_signature": "client/model transport failure in traj.jsonl",
        "result_root": str(result_root),
        "strict_retry_count": len(tasks),
        "domain_counts": {"os": len(tasks)},
        "score_snapshot_counts": counts,
        "tasks": tasks,
    }
    manifest_path = root / "transport-failures.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, meta_path, result_root, ids


def transport_plan_for(root: Path, scores: list[str]):
    manifest, meta, result_root, ids = make_transport_fixture(root, scores)
    plan = build_archive_plan(
        manifest,
        meta,
        result_root=result_root,
        expected_manifest_count=len(scores),
        expected_official_count=len(scores),
    )
    return plan, manifest, meta, result_root, ids


class ManifestValidationTests(unittest.TestCase):
    def test_accepts_explicitly_listed_zero_partial_and_one_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plan, _, _, _, _ = plan_for(
                Path(temp_dir), ["0", "0.25", "1", None]
            )

            self.assertEqual(len(plan.tasks), 4)
            self.assertEqual(
                [item.score.value for item in plan.tasks], [0.0, 0.25, 1.0, None]
            )
            self.assertTrue(all(item.tree.files for item in plan.tasks))

    def test_rejects_duplicate_manifest_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, manifest_path, meta_path, result_root, _ = plan_for(
                root, ["0", "1"]
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["tasks"][1]["id"] = payload["tasks"][0]["id"]
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ArchiveSafetyError, "duplicate manifest"):
                build_archive_plan(
                    manifest_path,
                    meta_path,
                    result_root=result_root,
                    expected_manifest_count=2,
                    expected_official_count=2,
                )

    def test_rejects_domain_id_not_in_official_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, manifest_path, meta_path, result_root, _ = plan_for(root, ["0"])
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["tasks"][0]["domain"] = "chrome"
            payload["domain_counts"] = {"chrome": 1}
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ArchiveSafetyError, "not in official"):
                build_archive_plan(
                    manifest_path,
                    meta_path,
                    result_root=result_root,
                    expected_manifest_count=1,
                    expected_official_count=1,
                )

    def test_rejects_archive_below_result_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plan, _, _, result_root, _ = plan_for(Path(temp_dir), ["0"])
            with self.assertRaisesRegex(ArchiveSafetyError, "separate"):
                safe_external_archive_base(result_root / "archive", plan.result_root)

    def test_rejects_archive_ancestor_of_result_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan, _, _, _, _ = plan_for(root, ["0"])
            with self.assertRaisesRegex(ArchiveSafetyError, "not contain"):
                safe_external_archive_base(root, plan.result_root)


class TransportManifestValidationTests(unittest.TestCase):
    def test_accepts_explicit_transport_protocol_without_pyperclip_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plan, _, _, _, _ = transport_plan_for(
                Path(temp_dir), ["0", "0.25", "1"]
            )

            self.assertEqual(plan.manifest.failure_protocol, TRANSPORT_PROTOCOL)
            self.assertEqual([item.score.value for item in plan.tasks], [0.0, 0.25, 1.0])
            for item in plan.tasks:
                self.assertIsNone(item.manifest_task.pyperclip_failure_count)
                self.assertEqual(
                    [entry.__dict__ for entry in item.manifest_task.transport_evidence],
                    [{"label": "client error", "count": 1}],
                )

    def test_rejects_transport_manifest_that_carries_pyperclip_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, manifest_path, meta_path, result_root, _ = transport_plan_for(root, ["0"])
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["tasks"][0]["pyperclip_failure_count"] = 1
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                ArchiveSafetyError, "cannot carry pyperclip_failure_count"
            ):
                build_archive_plan(
                    manifest_path,
                    meta_path,
                    result_root=result_root,
                    expected_manifest_count=1,
                    expected_official_count=1,
                )

    def test_transport_protocol_must_be_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, manifest_path, meta_path, result_root, _ = transport_plan_for(root, ["0"])
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            del payload["failure_protocol"]
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                ArchiveSafetyError, "required PyperclipException signature"
            ):
                build_archive_plan(
                    manifest_path,
                    meta_path,
                    result_root=result_root,
                    expected_manifest_count=1,
                    expected_official_count=1,
                )

    def test_rejects_empty_or_nonpositive_transport_evidence(self) -> None:
        mutations = (
            (lambda task: task.update(transport_evidence=[]), "non-empty list"),
            (
                lambda task: task["transport_evidence"][0].update(count=0),
                "positive integer",
            ),
        )
        for mutate, error in mutations:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _, manifest_path, meta_path, result_root, _ = transport_plan_for(
                    root, ["0"]
                )
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutate(payload["tasks"][0])
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(ArchiveSafetyError, error):
                    build_archive_plan(
                        manifest_path,
                        meta_path,
                        result_root=result_root,
                        expected_manifest_count=1,
                        expected_official_count=1,
                    )

    def test_transport_tasks_must_be_unique_and_official(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, manifest_path, meta_path, result_root, _ = transport_plan_for(
                root, ["0", "1"]
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["tasks"][1]["id"] = payload["tasks"][0]["id"]
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ArchiveSafetyError, "duplicate manifest"):
                build_archive_plan(
                    manifest_path,
                    meta_path,
                    result_root=result_root,
                    expected_manifest_count=2,
                    expected_official_count=2,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, manifest_path, meta_path, result_root, _ = transport_plan_for(root, ["0"])
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["tasks"][0]["id"] = task_id(999)
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ArchiveSafetyError, "not in official"):
                build_archive_plan(
                    manifest_path,
                    meta_path,
                    result_root=result_root,
                    expected_manifest_count=1,
                    expected_official_count=1,
                )

    def test_rejects_wrong_transport_reason_or_missing_valid_score(self) -> None:
        mutations = (
            (
                lambda payload: payload["tasks"][0].update(reason="clipboard_failure"),
                "reason must be",
            ),
            (
                lambda payload: (
                    payload["tasks"][0].update(
                        score_status_at_audit="missing", score_at_audit=None
                    ),
                    payload.update(
                        score_snapshot_counts={
                            "missing_or_invalid": 1,
                            "one": 0,
                            "partial": 0,
                            "zero": 0,
                        }
                    ),
                ),
                "requires a valid audit score",
            ),
        )
        for mutate, error in mutations:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _, manifest_path, meta_path, result_root, _ = transport_plan_for(
                    root, ["0"]
                )
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutate(payload)
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(ArchiveSafetyError, error):
                    build_archive_plan(
                        manifest_path,
                        meta_path,
                        result_root=result_root,
                        expected_manifest_count=1,
                        expected_official_count=1,
                    )

    def test_rejects_transport_evidence_not_exactly_present_in_traj(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, manifest_path, meta_path, result_root, _ = transport_plan_for(root, ["0"])
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["tasks"][0]["transport_evidence"][0]["count"] = 2
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                ArchiveSafetyError, "does not exactly match traj.jsonl"
            ):
                build_archive_plan(
                    manifest_path,
                    meta_path,
                    result_root=result_root,
                    expected_manifest_count=1,
                    expected_official_count=1,
                )

    def test_rejects_manifest_score_not_matching_current_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, meta, result_root, ids = make_transport_fixture(root, ["0"])
            write(result_root / "os" / ids[0] / "result.txt", "1\n")

            with self.assertRaisesRegex(
                ArchiveSafetyError, "does not match its valid manifest score"
            ):
                build_archive_plan(
                    manifest,
                    meta,
                    result_root=result_root,
                    expected_manifest_count=1,
                    expected_official_count=1,
                )

    def test_rejects_transport_manifest_summary_and_root_tampering(self) -> None:
        mutations = (
            (
                lambda payload, _root: payload.update(strict_retry_count=2),
                "strict_retry_count",
            ),
            (
                lambda payload, _root: payload.update(domain_counts={"chrome": 1}),
                "domain_counts",
            ),
            (
                lambda payload, _root: payload.update(
                    score_snapshot_counts={
                        "missing_or_invalid": 0,
                        "one": 1,
                        "partial": 0,
                        "zero": 0,
                    }
                ),
                "score_snapshot_counts",
            ),
            (
                lambda payload, root: payload.update(result_root=str(root / "other")),
                "result root override",
            ),
        )
        for mutate, error in mutations:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _, manifest_path, meta_path, result_root, _ = transport_plan_for(
                    root, ["0"]
                )
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutate(payload, root)
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(ArchiveSafetyError, error):
                    build_archive_plan(
                        manifest_path,
                        meta_path,
                        result_root=result_root,
                        expected_manifest_count=1,
                        expected_official_count=1,
                    )

    def test_transport_transaction_records_only_transport_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan, _, _, _, _ = transport_plan_for(root, ["1"])
            proc = root / "proc"
            proc.mkdir()

            transaction = execute_archive(
                plan,
                root / "archive",
                run_id="test-transport-run",
                proc_root=proc,
            )

            payload = json.loads(transaction.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "committed")
            self.assertEqual(payload["failure_protocol"], TRANSPORT_PROTOCOL)
            evidence = payload["tasks"][0]["protocol_evidence"]
            self.assertNotIn("pyperclip_failure_count", evidence)
            self.assertEqual(
                evidence,
                {
                    "reason": TRANSPORT_PROTOCOL_REASON,
                    "transport_evidence": [{"label": "client error", "count": 1}],
                },
            )


class ArchiveTransactionTests(unittest.TestCase):
    def test_cli_defaults_to_dry_run_without_creating_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, manifest, meta, result_root, ids = plan_for(root, ["0"])
            archive = root / "archive"

            return_code = main(
                [
                    "--manifest",
                    str(manifest),
                    "--meta",
                    str(meta),
                    "--result-root",
                    str(result_root),
                    "--archive-dir",
                    str(archive),
                    "--expected-count",
                    "1",
                    "--expected-official-count",
                    "1",
                    "--summary-only",
                ]
            )

            self.assertEqual(return_code, 0)
            self.assertTrue((result_root / "os" / ids[0]).is_dir())
            self.assertFalse(archive.exists())

    def test_successful_transaction_moves_and_records_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan, _, _, result_root, ids = plan_for(root, ["0", "1"])
            proc = root / "proc"
            proc.mkdir()

            transaction = execute_archive(
                plan,
                root / "archive",
                run_id="test-run",
                proc_root=proc,
            )

            payload = json.loads(transaction.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "committed")
            self.assertEqual(payload["task_count"], 2)
            self.assertEqual(
                [item["score_at_archive_plan"]["value"] for item in payload["tasks"]],
                [0.0, 1.0],
            )
            self.assertTrue(payload["tasks"][0]["source_tree"]["files"][0]["sha256"])
            for current_id in ids:
                self.assertFalse((result_root / "os" / current_id).exists())
                self.assertTrue(
                    (root / "archive/test-run/tasks/os" / current_id).is_dir()
                )

    def test_changed_source_is_rejected_before_archive_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan, _, _, result_root, ids = plan_for(root, ["0"])
            write(result_root / "os" / ids[0] / "late.txt", "changed\n")
            proc = root / "proc"
            proc.mkdir()

            with self.assertRaisesRegex(ArchiveSafetyError, "inputs changed"):
                execute_archive(
                    plan,
                    root / "archive",
                    run_id="test-run",
                    proc_root=proc,
                )

            self.assertTrue((result_root / "os" / ids[0]).is_dir())
            self.assertFalse((root / "archive").exists())

    def test_active_runner_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan, _, _, result_root, ids = plan_for(root, ["0"])
            proc = root / "proc"
            process = proc / "101"
            process.mkdir(parents=True)
            command = [
                "/usr/bin/python",
                "/repo/scripts/python/run_multienv_uitars15_v1.py",
                "--result_dir",
                str(result_root.parents[2]),
            ]
            (process / "cmdline").write_bytes(
                b"\0".join(item.encode() for item in command) + b"\0"
            )
            (process / "cwd").symlink_to(root, target_is_directory=True)

            with self.assertRaisesRegex(ArchiveSafetyError, "runners are active"):
                execute_archive(
                    plan,
                    root / "archive",
                    run_id="test-run",
                    proc_root=proc,
                    current_pid=os.getpid(),
                )

            self.assertTrue((result_root / "os" / ids[0]).is_dir())
            self.assertFalse((root / "archive").exists())

    def test_midway_failure_rolls_every_prior_move_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan, _, _, result_root, ids = plan_for(root, ["0", None])
            proc = root / "proc"
            proc.mkdir()
            calls = 0

            def fail_second_forward_move(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected move failure")
                os.replace(source, destination)

            with self.assertRaisesRegex(ArchiveSafetyError, "status is rolled_back"):
                execute_archive(
                    plan,
                    root / "archive",
                    run_id="test-run",
                    proc_root=proc,
                    replace_path=fail_second_forward_move,
                )

            for current_id in ids:
                self.assertTrue((result_root / "os" / current_id).is_dir())
                self.assertFalse(
                    (root / "archive/test-run/tasks/os" / current_id).exists()
                )
            payload = json.loads(
                (root / "archive/test-run/transaction.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(payload["status"], "rolled_back")
            self.assertEqual(
                [item["state"] for item in payload["tasks"]],
                ["rolled_back", "unchanged"],
            )


if __name__ == "__main__":
    unittest.main()
