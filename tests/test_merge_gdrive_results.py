from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts/python"
sys.path.insert(0, str(SCRIPTS))

from merge_gdrive_results import (  # noqa: E402
    DOMAIN,
    GDRIVE_TASK_IDS,
    MergeApplyError,
    apply_merge_plan,
    build_merge_plan,
    main,
    result_root,
)


def task_path(base: Path, task_id: str) -> Path:
    return result_root(base, "pyautogui", "screenshot", "uitars15-7b") / DOMAIN / task_id


def write_result(base: Path, task_id: str, score: str = "1\n") -> Path:
    path = task_path(base, task_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "result.txt").write_text(score, encoding="utf-8")
    (path / "traj.jsonl").write_text(
        json.dumps({"response": "finished normally"}) + "\n",
        encoding="utf-8",
    )
    return path


def make_other_destinations_valid(formal: Path, excluded: set[str]) -> None:
    for task_id in GDRIVE_TASK_IDS:
        if task_id not in excluded:
            write_result(formal, task_id, "0.25\n")


class MergeGDriveResultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.recovery = self.root / "recovery"
        self.formal = self.root / "formal"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_default_cli_is_a_read_only_dry_run(self) -> None:
        for task_id in GDRIVE_TASK_IDS:
            write_result(self.recovery, task_id)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            return_code = main(
                [
                    "--recovery-dir",
                    str(self.recovery),
                    "--formal-dir",
                    str(self.formal),
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertFalse(self.formal.exists())
        self.assertIn("DRY RUN", output.getvalue())
        self.assertIn("Planned writes: 8", output.getvalue())
        self.assertIn("rerun with --apply", output.getvalue())

    def test_apply_is_staged_and_idempotent(self) -> None:
        for task_id in GDRIVE_TASK_IDS:
            write_result(self.recovery, task_id, "0.5\n")

        plan = build_merge_plan(recovery_dir=self.recovery, formal_dir=self.formal)
        manifest = apply_merge_plan(plan, check_active_processes=False)

        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(
            json.loads(manifest.read_text(encoding="utf-8"))["status"],
            "complete",
        )
        for task_id in GDRIVE_TASK_IDS:
            self.assertEqual(
                (task_path(self.formal, task_id) / "result.txt").read_text(),
                "0.5\n",
            )

        second_plan = build_merge_plan(
            recovery_dir=self.recovery, formal_dir=self.formal
        )
        self.assertTrue(second_plan.ready)
        self.assertFalse(second_plan.actions)
        self.assertEqual(len(second_plan.skipped), 8)
        self.assertIsNone(
            apply_merge_plan(second_plan, check_active_processes=False)
        )

    def test_existing_clean_score_is_never_overwritten(self) -> None:
        task_id = GDRIVE_TASK_IDS[0]
        write_result(self.recovery, task_id, "1\n")
        write_result(self.formal, task_id, "0.25\n")
        make_other_destinations_valid(self.formal, {task_id})

        plan = build_merge_plan(recovery_dir=self.recovery, formal_dir=self.formal)

        self.assertTrue(plan.ready)
        self.assertFalse(plan.actions)
        self.assertEqual(len(plan.skipped), 8)
        self.assertIsNone(apply_merge_plan(plan, check_active_processes=False))
        self.assertEqual(
            (task_path(self.formal, task_id) / "result.txt").read_text(),
            "0.25\n",
        )

    def test_missing_partial_directory_is_replaced_and_backed_up(self) -> None:
        task_id = GDRIVE_TASK_IDS[0]
        write_result(self.recovery, task_id, "0.75\n")
        partial = task_path(self.formal, task_id)
        partial.mkdir(parents=True)
        (partial / "traj.jsonl").write_text('{"Error":"setup stopped"}\n')
        make_other_destinations_valid(self.formal, {task_id})

        plan = build_merge_plan(recovery_dir=self.recovery, formal_dir=self.formal)
        self.assertTrue(plan.ready)
        self.assertEqual(
            [(item.task_id, item.kind) for item in plan.actions],
            [(task_id, "install_missing")],
        )

        manifest = apply_merge_plan(plan, check_active_processes=False)

        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(
            (task_path(self.formal, task_id) / "result.txt").read_text(),
            "0.75\n",
        )
        backup = manifest.parent / "previous" / task_id / "traj.jsonl"
        self.assertEqual(backup.read_text(), '{"Error":"setup stopped"}\n')

    def test_explicit_oauth_failure_allows_replacing_existing_score(self) -> None:
        task_id = GDRIVE_TASK_IDS[0]
        write_result(self.recovery, task_id, "1\n")
        destination = write_result(self.formal, task_id, "0\n")
        (destination / "traj.jsonl").write_text(
            '{"Error":"oauth2client RefreshError: invalid_grant"}\n',
            encoding="utf-8",
        )
        make_other_destinations_valid(self.formal, {task_id})

        plan = build_merge_plan(recovery_dir=self.recovery, formal_dir=self.formal)

        self.assertTrue(plan.ready)
        self.assertEqual(
            [(item.task_id, item.kind) for item in plan.actions],
            [(task_id, "replace_infrastructure_error")],
        )
        manifest = apply_merge_plan(plan, check_active_processes=False)
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(
            (task_path(self.formal, task_id) / "result.txt").read_text(), "1\n"
        )
        self.assertEqual(
            (manifest.parent / "previous" / task_id / "result.txt").read_text(),
            "0\n",
        )

    def test_corrupt_destination_without_infrastructure_is_refused(self) -> None:
        task_id = GDRIVE_TASK_IDS[0]
        write_result(self.recovery, task_id, "1\n")
        write_result(self.formal, task_id, "nan\n")
        make_other_destinations_valid(self.formal, {task_id})

        plan = build_merge_plan(recovery_dir=self.recovery, formal_dir=self.formal)

        self.assertFalse(plan.ready)
        self.assertFalse(plan.actions)
        self.assertTrue(
            any("refusing to replace corrupt result" in error for error in plan.errors)
        )
        with self.assertRaises(MergeApplyError):
            apply_merge_plan(plan, check_active_processes=False)
        self.assertEqual(
            (task_path(self.formal, task_id) / "result.txt").read_text(),
            "nan\n",
        )

    def test_recovery_rejects_unknown_task_and_failed_source(self) -> None:
        task_id = GDRIVE_TASK_IDS[0]
        source = write_result(self.recovery, task_id)
        (source / "traj.jsonl").write_text(
            '{"Error":"model action parse failed"}\n'
        )
        unknown = task_path(self.recovery, "not-a-drive-task")
        unknown.mkdir(parents=True)
        (unknown / "result.txt").write_text("1\n")
        make_other_destinations_valid(self.formal, set())
        for artifact in task_path(self.formal, task_id).iterdir():
            artifact.unlink()
        task_path(self.formal, task_id).rmdir()

        plan = build_merge_plan(recovery_dir=self.recovery, formal_dir=self.formal)

        self.assertFalse(plan.ready)
        self.assertTrue(
            any("unknown task in recovery directory" in error for error in plan.errors)
        )
        self.assertTrue(
            any("traj.jsonl contains Error" in error for error in plan.errors)
        )

    def test_recovery_rejects_nonfinite_score(self) -> None:
        task_id = GDRIVE_TASK_IDS[0]
        write_result(self.recovery, task_id, "nan\n")
        make_other_destinations_valid(self.formal, {task_id})

        plan = build_merge_plan(recovery_dir=self.recovery, formal_dir=self.formal)

        self.assertFalse(plan.ready)
        self.assertTrue(any("not finite" in error for error in plan.errors))
        self.assertFalse(plan.actions)

    def test_apply_refuses_while_formal_runner_is_active(self) -> None:
        for task_id in GDRIVE_TASK_IDS:
            write_result(self.recovery, task_id)
        plan = build_merge_plan(recovery_dir=self.recovery, formal_dir=self.formal)
        self.assertTrue(plan.ready)

        with mock.patch(
            "merge_gdrive_results._runner_active_for", return_value=True
        ), self.assertRaisesRegex(MergeApplyError, "runner is still active"):
            apply_merge_plan(plan)

        self.assertFalse(self.formal.exists())

    def test_commit_failure_rolls_back_previous_installs(self) -> None:
        first, second = GDRIVE_TASK_IDS[:2]
        write_result(self.recovery, first)
        write_result(self.recovery, second)
        make_other_destinations_valid(self.formal, {first, second})
        plan = build_merge_plan(recovery_dir=self.recovery, formal_dir=self.formal)
        self.assertTrue(plan.ready)
        self.assertEqual(len(plan.actions), 2)
        failed = False

        def fail_second_install(source: Path, destination: Path) -> None:
            nonlocal failed
            if destination == task_path(self.formal, second) and not failed:
                failed = True
                raise OSError("injected second install failure")
            os.replace(source, destination)

        with self.assertRaisesRegex(MergeApplyError, "rolled back"):
            apply_merge_plan(
                plan,
                check_active_processes=False,
                replace_path=fail_second_install,
            )

        self.assertFalse(task_path(self.formal, first).exists())
        self.assertFalse(task_path(self.formal, second).exists())
        manifests = list(
            (self.formal / "_gdrive_merge_backups").glob("*/manifest.json")
        )
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
