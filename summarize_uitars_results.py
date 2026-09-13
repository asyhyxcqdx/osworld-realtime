from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize an OSWorld UI-TARS run")
    parser.add_argument("--meta", default="evaluation_examples/test_all.json")
    parser.add_argument("--result-dir", default="results_uitars15_full_official")
    parser.add_argument("--model", default="uitars15-7b")
    return parser.parse_args()


def read_score(path: Path) -> float | None:
    try:
        return float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def has_recorded_error(path: Path) -> bool:
    try:
        return any('"Error"' in line for line in path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return False


def main() -> None:
    args = parse_args()
    metadata = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    result_root = (
        Path(args.result_dir) / "pyautogui" / "screenshot" / args.model
    )

    total = 0
    completed = 0
    recorded_errors = 0
    score_sum = 0.0
    domain_rows = []

    for domain, task_ids in metadata.items():
        domain_completed = 0
        domain_score = 0.0
        for task_id in task_ids:
            total += 1
            task_dir = result_root / domain / task_id
            score = read_score(task_dir / "result.txt")
            if score is None:
                if has_recorded_error(task_dir / "traj.jsonl"):
                    recorded_errors += 1
                continue
            completed += 1
            domain_completed += 1
            score_sum += score
            domain_score += score
        domain_rows.append(
            (domain, len(task_ids), domain_completed, domain_score)
        )

    missing = total - completed
    print(f"Total tasks: {total}")
    print(f"Completed: {completed}")
    print(f"Missing or still running: {missing}")
    print(f"Recorded errors without score: {recorded_errors}")
    print(f"Score sum: {score_sum:.4f}")
    print(f"Overall score (missing=0): {score_sum / total * 100:.2f}%")
    completed_average = score_sum / completed * 100 if completed else 0.0
    print(f"Completed-only average: {completed_average:.2f}%")
    print("\nBy domain:")
    for domain, domain_total, domain_completed, domain_score in domain_rows:
        overall = domain_score / domain_total * 100
        print(
            f"{domain:24} {domain_completed:3}/{domain_total:<3} "
            f"score(missing=0): {overall:6.2f}%"
        )


if __name__ == "__main__":
    main()
