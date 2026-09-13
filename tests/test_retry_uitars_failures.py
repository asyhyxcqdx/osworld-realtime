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

from audit_uitars_results import load_tasks
from retry_uitars_failures import (
    RetrySafetyError,
    archive_candidates,
    build_retry_plan,
    build_runner_specs,
    find_active_runners,
    validate_task_refs,
)


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def make_tasks(root: Path, task_ids: list[str]):
    meta = root / "test_all.json"
    meta.write_text(json.dumps({"os": task_ids}), encoding="utf-8")
    return load_tasks(meta, expected_total=len(task_ids))


class RetrySelectionTests(unittest.TestCase):
    def test_rejects_metadata_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tasks = make_tasks(Path(temp_dir), ["../../outside"])
            with self.assertRaisesRegex(RetrySafetyError, "unsafe task ID"):
                validate_task_refs(tasks)

    def test_retries_only_unscored_or_corrupt_and_protects_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir)
            task_ids = [
                "valid-zero",
                "valid-one-with-error",
                "absent",
                "empty",
                "invalid",
                "misplaced",
                "error-only",
            ]
            tasks = make_tasks(root_dir, task_ids)
            result_root = root_dir / "results"
            write(result_root / "os/valid-zero/result.txt", "0\n")
            write(result_root / "os/valid-one-with-error/result.txt", "1\n")
            write(
                result_root / "os/valid-one-with-error/traj.jsonl",
                json.dumps({"Error": "client error"}) + "\n",
            )
            (result_root / "os/empty").mkdir(parents=True)
            write(result_root / "os/invalid/result.txt", "not-a-score\n")
            write(result_root / "os/misplaced/nested/result.txt", "0.5\n")
            write(
                result_root / "os/error-only/traj.jsonl",
                json.dumps({"Error": "VM failed to become ready"}) + "\n",
            )

            plan = build_retry_plan(tasks, result_root, shard_count=4)

            self.assertEqual(
                [candidate.task.task_id for candidate in plan.candidates],
                ["absent", "empty", "invalid", "misplaced", "error-only"],
            )
            self.assertEqual(
                [(task.task_id, score) for task, score in plan.protected_scores],
                [("valid-zero", 0.0), ("valid-one-with-error", 1.0)],
            )
            self.assertEqual(plan.protected_zero_count, 1)
            self.assertEqual(
                [item.task.task_id for item in plan.protected_diagnostic_findings],
                ["valid-one-with-error"],
            )

    def test_archive_rechecks_and_refuses_score_created_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir)
            tasks = make_tasks(root_dir, ["late-zero"])
            result_root = root_dir / "results"
            task_dir = result_root / "os/late-zero"
            task_dir.mkdir(parents=True)
            plan = build_retry_plan(tasks, result_root, shard_count=1)
            write(task_dir / "result.txt", "0\n")

            with self.assertRaisesRegex(RetrySafetyError, "became complete"):
                archive_candidates(
                    plan,
                    root_dir / "archive/run",
                    manifest_path=root_dir / "archive/run/manifest.json",
                    run_id="test-run",
                    model_urls=["http://127.0.0.1:8000/v1"],
                )

            self.assertTrue(task_dir.is_dir())
            self.assertEqual(
                (task_dir / "result.txt").read_text(encoding="utf-8"), "0\n"
            )
            self.assertFalse((root_dir / "archive/run").exists())

    def test_archive_moves_only_existing_candidates_and_records_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir)
            tasks = make_tasks(root_dir, ["partial", "absent"])
            result_root = root_dir / "results"
            write(
                result_root / "os/partial/traj.jsonl",
                json.dumps({"Error": "timeout"}) + "\n",
            )
            plan = build_retry_plan(tasks, result_root, shard_count=2)
            archive_root = root_dir / "archive/run"
            manifest = archive_root / "manifest.json"

            archived = archive_candidates(
                plan,
                archive_root,
                manifest_path=manifest,
                run_id="test-run",
                model_urls=["http://127.0.0.1:8000/v1"],
            )

            self.assertEqual(
                [item.task.task_id for item in archived], ["partial"]
            )
            self.assertFalse((result_root / "os/partial").exists())
            self.assertTrue((archive_root / "os/partial/traj.jsonl").is_file())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "archiving")
            self.assertEqual(payload["archived_task_ids"], ["partial"])
            self.assertEqual(payload["candidate_count"], 2)
            previous = {
                item["task_id"]: item["previous_directory_existed"]
                for item in payload["candidates"]
            }
            self.assertEqual(previous, {"partial": True, "absent": False})

    def test_archive_rolls_back_prior_moves_when_a_later_move_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir)
            tasks = make_tasks(root_dir, ["first", "second"])
            result_root = root_dir / "results"
            write(result_root / "os/first/traj.jsonl", "{}\n")
            write(result_root / "os/second/traj.jsonl", "{}\n")
            plan = build_retry_plan(tasks, result_root, shard_count=1)
            archive_root = root_dir / "archive/run"
            calls = 0

            def fail_second_move(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected move failure")
                os.replace(source, destination)

            with self.assertRaisesRegex(RetrySafetyError, "injected move failure"):
                archive_candidates(
                    plan,
                    archive_root,
                    manifest_path=archive_root / "manifest.json",
                    run_id="test-run",
                    model_urls=["http://127.0.0.1:8000/v1"],
                    replace_path=fail_second_move,
                )

            self.assertTrue((result_root / "os/first/traj.jsonl").is_file())
            self.assertTrue((result_root / "os/second/traj.jsonl").is_file())
            self.assertFalse((archive_root / "os/first").exists())
            payload = json.loads(
                (archive_root / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["status"], "archive_failed")


class RetryRunnerTests(unittest.TestCase):
    def test_find_active_runners_matches_same_result_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            proc = root / "proc"
            proc.mkdir()
            target = root / "formal-results"
            other = root / "other-results"

            def fake_process(pid: int, result: Path) -> None:
                process_dir = proc / str(pid)
                process_dir.mkdir()
                command = [
                    "/usr/bin/python",
                    "/repo/scripts/python/run_multienv_uitars15_v1.py",
                    "--result_dir",
                    str(result),
                ]
                (process_dir / "cmdline").write_bytes(
                    b"\0".join(item.encode() for item in command) + b"\0"
                )
                (process_dir / "cwd").symlink_to(root, target_is_directory=True)

            fake_process(101, target)
            fake_process(102, other)

            active = find_active_runners(
                target, proc_root=proc, current_pid=os.getpid()
            )

            self.assertEqual([item.pid for item in active], [101])

    def test_four_model_specs_share_manifest_but_have_disjoint_shards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            urls = [
                f"http://127.0.0.1:{port}/v1" for port in range(8000, 8004)
            ]
            specs = build_runner_specs(
                model_urls=urls,
                python=Path("/python"),
                runner=Path("/runner.py"),
                path_to_vm=Path("/vm.qcow2"),
                metadata_path=root / "retry.json",
                result_dir=root / "results",
                work_root=root / "work",
                model="uitars15-7b",
                num_envs=4,
                max_steps=100,
                log_level="INFO",
                base_environment={},
            )

            self.assertEqual(len(specs), 4)
            for index, spec in enumerate(specs):
                self.assertEqual(spec.environment["DOUBAO_API_URL"], urls[index])
                shard_count_at = spec.command.index("--shard_count") + 1
                shard_index_at = spec.command.index("--shard_index") + 1
                num_envs_at = spec.command.index("--num_envs") + 1
                meta_at = spec.command.index("--test_all_meta_path") + 1
                self.assertEqual(spec.command[shard_count_at], "4")
                self.assertEqual(spec.command[shard_index_at], str(index))
                self.assertEqual(spec.command[num_envs_at], "4")
                self.assertEqual(spec.command[meta_at], str(root / "retry.json"))
                self.assertIn("--input_swap", spec.command)


if __name__ == "__main__":
    unittest.main()
