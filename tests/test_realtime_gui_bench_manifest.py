import hashlib
import json
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEBSITE_ROOT = (
    PROJECT_ROOT / "evaluation_examples/websites/realtime_gui_bench/games"
)
TASK_ROOT = PROJECT_ROOT / "evaluation_examples/examples/realtime_gui_bench"
TASK_LIST = PROJECT_ROOT / "evaluation_examples/test_realtime_gui_bench.json"


def expected_benchmark_ids():
    return (
        "A1 A12 A14 A16 A17 A21 A23 A24 A31 A34 A35 A36 A37 A38 A39 A40 A41 A42 A43 A44 A45 A46 "
        "B1 B4 B7 B14 B15 B20 B23 B32 B36 B37 B38 B39 "
        "C1 C3 C12 C26 C27 C28 C29 C30 C31 C32 C33 C34 C35 C36 C37 C38 C39 "
        "D1 D3 D4 D7 D14 D15 D23 D27 D28 D29 D30 D31 D32 D33 D34 D35 D36 D37"
    ).split()


def load_tasks():
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(TASK_ROOT.glob("*.json"))
    ]


def test_manifest_has_exactly_69_unique_tasks_and_html_files():
    expected = expected_benchmark_ids()
    tasks = load_tasks()
    listed_ids = json.loads(TASK_LIST.read_text(encoding="utf-8"))[
        "realtime_gui_bench"
    ]
    html_files = sorted(WEBSITE_ROOT.glob("*/index.html"))

    assert len(expected) == len(tasks) == len(listed_ids) == len(html_files) == 69
    assert {task["benchmark_id"] for task in tasks} == set(expected)
    assert len({task["id"] for task in tasks}) == 69
    assert {task["id"] for task in tasks} == set(listed_ids)
    assert {path.parent.name.upper() for path in html_files} == set(expected)
    for task in tasks:
        assert str(uuid.UUID(task["id"])) == task["id"]


def test_task_configs_use_the_shared_instruction_and_evaluator():
    tasks = load_tasks()
    instructions = {task["instruction"] for task in tasks}

    assert len(instructions) == 1
    for task in tasks:
        benchmark_id = task["benchmark_id"].lower()
        assert task["snapshot"] == "chrome"
        assert task["source"].endswith(f"/games/{benchmark_id}/index.html")
        assert task["related_apps"] == ["chrome"]
        assert task["proxy"] is False
        assert task["fixed_ip"] is False
        assert task["possibility_of_env_change"] == "low"
        assert task["evaluator"]["func"] == "realtime_gui_bench_result"
        assert task["evaluator"]["result"]["type"] == "realtime_gui_bench_state"
        assert task["evaluator"]["result"]["benchmark_id"] == task["benchmark_id"]
        assert [item["type"] for item in task["config"]] == [
            "upload_file",
            "launch",
            "launch",
            "launch",
            "chrome_open_tabs",
            "activate_window",
            "sleep",
        ]


def test_target_website_tree_contains_only_index_html_files():
    files = [path for path in WEBSITE_ROOT.rglob("*") if path.is_file()]

    assert len(files) == 69
    assert all(path.name == "index.html" for path in files)


def test_all_pages_declare_the_bench_contract():
    for path in WEBSITE_ROOT.glob("*/index.html"):
        html = path.read_text(encoding="utf-8")
        assert "window.BENCH" in html, path
        assert "results:[]" in html or "results: []" in html, path
        assert "status:\"running\"" in html or "status: \"running\"" in html, path
        assert "syncURL" in html, path


def test_d1_preserves_author_original_including_attempt_hint():
    data = (WEBSITE_ROOT / "d1/index.html").read_bytes()
    html = data.decode("utf-8")

    # The user chose the author's unchanged release on 2026-09-09.
    assert hashlib.sha256(data).hexdigest() == (
        "cb86362dfd41e28c8d535e074a24fe6bb1bb6f9cf80491dc0a5bd3e50e096375"
    )
    assert '<div class="hint" id="hint">Attempt 0 / 3</div>' in html
    assert 'hintEl.textContent="Attempt "+BENCH.attempts+" / "+MAX_ATTEMPTS' in html
    assert 'id="startBtn"' in html
    assert 'id="nextBtn"' in html
    assert 'id="nextTitle"' in html
    assert 'showToast("Attempt 1 / " + MAX_ATTEMPTS)' in html
    assert 'showToast("Attempt " + (BENCH.attempts+1) + " / " + MAX_ATTEMPTS)' in html
    assert "const MAX_ATTEMPTS = 3;" in html
    assert "BENCH.attempts += 1;" in html
    assert "BENCH.passed = true;" in html
