#!/usr/bin/env python3
"""Import RealtimeGUI-Bench HTML files and generate OSWorld task configs."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEBSITE_ROOT = (
    PROJECT_ROOT / "evaluation_examples/websites/realtime_gui_bench/games"
)
TASK_ROOT = PROJECT_ROOT / "evaluation_examples/examples/realtime_gui_bench"
TASK_LIST = PROJECT_ROOT / "evaluation_examples/test_realtime_gui_bench.json"

INSTRUCTION = (
    "阅读当前页面中的游戏规则，并按照规则完成游戏。如果当前尝试失败且页面允许继续尝试，"
    "请继续完成后续尝试。游戏显示成功，或者所有尝试机会均已用完后，请立即结束任务并停留在当前页面。"
)

def benchmark_ids() -> list[str]:
    """Read the installed, possibly non-contiguous game IDs."""
    return sorted(
        (path.parent.name for path in WEBSITE_ROOT.glob("*/index.html")),
        key=lambda item: (item[0], int(item[1:])),
    )


def discover_sources(source_root: Path) -> dict[str, Path]:
    """Import RealtimeGame/<category>/<ID>_<name>/index.html without renumbering."""
    sources = {}
    for source in sorted(source_root.rglob("index.html")):
        parts = source.relative_to(source_root).parts
        if len(parts) != 3 or parts[0] not in "abcd" or len(parts[0]) != 1:
            raise ValueError(f"Unexpected source layout: {source}")
        category, name, _ = parts
        # This historical filename is the random-enemy task already called D1.
        if (category, name) == ("d", "C2_Random_Enemy"):
            benchmark_id = "d1"
        else:
            match = re.fullmatch(r"([abcd])([1-9]\d*)_.+", name, re.IGNORECASE)
            if not match or match[1].lower() != category:
                raise ValueError(f"Task ID/category mismatch: {source}")
            benchmark_id = match[1].lower() + match[2]
        if benchmark_id in sources:
            raise ValueError(f"Duplicate benchmark ID: {benchmark_id}")
        sources[benchmark_id] = source
    if not sources:
        raise ValueError(f"No game HTML found in {source_root}")
    payloads = {path for path in source_root.rglob("*") if path.is_file()}
    if payloads != set(sources.values()):
        raise ValueError("Package contains additional assets; review upload config before importing")
    return dict(sorted(sources.items(), key=lambda item: (item[0][0], int(item[0][1:]))))


def task_uuid(benchmark_id: str) -> str:
    task_url = f"https://os-world.github.io/realtime-gui-bench/{benchmark_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, task_url))


def task_config(benchmark_id: str) -> dict:
    task_id = task_uuid(benchmark_id)
    local_path = (
        f"evaluation_examples/websites/realtime_gui_bench/games/"
        f"{benchmark_id}/index.html"
    )
    vm_path = f"/tmp/realtime_gui_bench/{benchmark_id}/index.html"
    page_url = f"http://127.0.0.1:8765/{benchmark_id}/index.html"
    return {
        "id": task_id,
        "benchmark_id": benchmark_id.upper(),
        "snapshot": "chrome",
        "instruction": INSTRUCTION,
        "source": local_path,
        "config": [
            {
                "type": "upload_file",
                "parameters": {
                    "files": [{"local_path": local_path, "path": vm_path}]
                },
            },
            {
                "type": "launch",
                "parameters": {
                    "command": [
                        "python3",
                        "-m",
                        "http.server",
                        "8765",
                        "--directory",
                        "/tmp/realtime_gui_bench",
                    ]
                },
            },
            {
                "type": "launch",
                "parameters": {
                    "command": ["google-chrome", "--remote-debugging-port=1337"]
                },
            },
            {
                "type": "launch",
                "parameters": {
                    "command": [
                        "socat",
                        "tcp-listen:9222,fork",
                        "tcp:localhost:1337",
                    ]
                },
            },
            {
                "type": "chrome_open_tabs",
                "parameters": {"urls_to_open": [page_url]},
            },
            {
                "type": "activate_window",
                "parameters": {"window_name": "Google Chrome"},
            },
            {"type": "sleep", "parameters": {"seconds": 3}},
        ],
        "trajectory": "trajectories/",
        "related_apps": ["chrome"],
        "evaluator": {
            "func": "realtime_gui_bench_result",
            "result": {
                "type": "realtime_gui_bench_state",
                "benchmark_id": benchmark_id.upper(),
                "target_url_contains": page_url.removeprefix("http://"),
                "attempts": 3,
                "retry_interval": 0.5,
                "timeout": 10.0,
            },
        },
        "proxy": False,
        "fixed_ip": False,
        "possibility_of_env_change": "low",
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "source_root",
        type=Path,
        help="Extracted RealtimeGame directory containing a/, b/, c/, d/",
    )
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    source_files = discover_sources(source_root)
    ids = list(source_files)

    task_ids = [task_uuid(item) for item in ids]
    if len(set(task_ids)) != len(ids):
        raise SystemExit("Generated task UUIDs are not unique")

    stale_games = set(benchmark_ids()).difference(ids)
    stale_configs = {path.stem for path in TASK_ROOT.glob("*.json")}.difference(task_ids)
    if stale_games or stale_configs:
        raise SystemExit(
            "Previous suite contains obsolete games/configs. Back up and move the old "
            "games/ and task JSON directories out of the active tree before replacement."
        )

    for benchmark_id, source in source_files.items():
        target = WEBSITE_ROOT / benchmark_id / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

        config = task_config(benchmark_id)
        write_json(TASK_ROOT / f"{config['id']}.json", config)

    write_json(TASK_LIST, {"realtime_gui_bench": task_ids})
    print(f"Imported {len(ids)} HTML files and generated {len(task_ids)} tasks")


if __name__ == "__main__":
    main()
