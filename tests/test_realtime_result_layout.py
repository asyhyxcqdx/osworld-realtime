"""Keep model/agent results isolated when writing scores and resuming runs."""

import argparse
import ast
import datetime
import fcntl
import json
import os
from pathlib import Path
import sys

import pytest

from lib_results_logger import log_task_completion
from lib_run_single import _evaluate_with_details


@pytest.fixture(autouse=True)
def realtime_keys(monkeypatch, tmp_path):
    # config() only checks that the variable named by api.key_env exists; the value is never used.
    # Redirect the .env loader so a developer's real .env cannot satisfy these placeholders.
    from mm_agents import realtime_env

    monkeypatch.setattr(realtime_env, "DEFAULT_ENV_FILE", tmp_path / "absent.env")
    for name in (
        "PACKY_CLAUDE_SONNET_5_API_KEY",
        "PACKY_GPT_5_6_SOL_API_KEY",
        "PACKY_GEMINI_3_8_FLASH_API_KEY",
        "PACKY_QWEN3_8_MAX_0902_API_KEY",
        "PACKY_KIMI_K3_API_KEY",
        "PACKY_DEEPSEEK_FLASH_API_KEY",
        "PACKY_GLM_5_3_FLASH_API_KEY",
        "PACKY_MINIMAX_M3_API_KEY",
        "PACKY_CLAUDE_FABLE_5_API_KEY",
        "PACKY_GPT_6_ASTRA_API_KEY",
    ):
        monkeypatch.setenv(name, "test-key")


@pytest.fixture
def runner():
    # Importing the CLI parses argv and opens log files. Load its actual path and
    # resume functions without starting that CLI or a VM.
    path = Path(__file__).resolve().parents[1] / "scripts/python/run_multienv.py"
    selected = {"config", "get_result_dir", "write_run_configuration", "get_unfinished", "get_result"}
    source = ast.parse(path.read_text())
    module = ast.Module(
        body=[n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in selected],
        type_ignores=[],
    )
    namespace = {"os": os, "argparse": argparse, "datetime": datetime, "json": json, "fcntl": fcntl}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def configure(runner, monkeypatch, root, model, agent=None, run_id="20260910T173000+0800"):
    argv = ["run_multienv.py", "--result_dir", str(root), "--model", model,
            "--action_space", "computer_13"]
    if agent:
        argv += ["--agent_variant", agent]
        if run_id is not None:
            argv += ["--run_id", run_id]
    monkeypatch.setattr(sys, "argv", argv)
    return runner["config"]()


def test_resume_and_scores_do_not_cross_models_or_agents(tmp_path, monkeypatch, runner):
    runs = []
    for model in ("claude-fable-5", "gpt-6-astra"):
        for i in range(1, 5):
            variant = f"agent{i}"
            args = configure(runner, monkeypatch, tmp_path, model, variant)
            assert args.max_steps == 100
            run = Path(runner["get_result_dir"](
                args.result_dir, args.action_space, args.observation_type, model, variant
            ))
            assert run == tmp_path / model / args.run_id / variant / "computer_13/screenshot"
            task_id = f"finished-{model}-{variant}"
            task = run / "domain" / task_id
            task.mkdir(parents=True)
            (task / "result.txt").write_text(str(i / 4))
            runs.append((args, task_id, i / 4, task))

    all_tasks = [task_id for _, task_id, _, _ in runs]
    for args, task_id, score, task in runs:
        params = (args.action_space, args.model, args.observation_type, args.result_dir)
        unfinished = runner["get_unfinished"](
            *params, {"domain": all_tasks.copy()}, args.agent_variant
        )
        assert unfinished == {"domain": [t for t in all_tasks if t != task_id]}
        assert runner["get_result"](*params, {}, args.agent_variant) == [score]
        assert (task / "result.txt").read_text() == str(score)


def test_legacy_result_layout_and_resume_are_preserved(tmp_path, monkeypatch, runner):
    args = configure(runner, monkeypatch, tmp_path, "legacy-model")
    params = (args.action_space, args.model, args.observation_type, args.result_dir)
    run = Path(runner["get_result_dir"](
        args.result_dir, args.action_space, args.observation_type, args.model
    ))
    assert run == tmp_path / "computer_13/screenshot/legacy-model"
    task = run / "domain/task"
    task.mkdir(parents=True)
    (task / "result.txt").write_text("1")
    assert runner["get_unfinished"](*params, {"domain": ["task", "new"]}) == {"domain": ["new"]}
    assert runner["get_result"](*params, {}) == [1.0]


def test_missing_model_config_cannot_silently_fall_back(tmp_path, monkeypatch, runner):
    with pytest.raises(SystemExit):
        configure(runner, monkeypatch, tmp_path, "model-that-has-no-config", "agent1")


def test_cli_cannot_override_config_with_another_provider_model(monkeypatch, runner):
    path = Path(__file__).resolve().parents[1] / "configs/realtime_agents/combine-claude-fable-5.yaml"
    monkeypatch.setattr(sys, "argv", ["run_multienv.py", "--agent_variant", "agent4",
        "--action_space", "computer_13", "--agent_config", str(path), "--model", "gpt-6-astra"])
    with pytest.raises(SystemExit):
        runner["config"]()


@pytest.mark.parametrize('width,height', [(1600, 900), (1920, 1200), (0, 1080)])
def test_realtime_cli_rejects_screen_size_inconsistent_with_coordinate_protocol(monkeypatch, runner, width, height):
    monkeypatch.setattr(sys, 'argv', ['run_multienv.py', '--agent_variant', 'agent1',
        '--action_space', 'computer_13', '--screen_width', str(width), '--screen_height', str(height)])
    with pytest.raises(SystemExit):
        runner['config']()


@pytest.mark.parametrize("budget", [0, 4, 100])
def test_query_limit_is_optional_and_defaults_to_unlimited(monkeypatch, runner, budget):
    argv = ["run_multienv.py", "--agent_variant", "agent3", "--action_space", "computer_13"]
    monkeypatch.setattr(sys, "argv", argv)
    assert runner["config"]().max_frame_queries == 0
    monkeypatch.setattr(sys, "argv", argv + ["--max_frame_queries", str(budget)])
    assert runner["config"]().max_frame_queries == budget
    monkeypatch.setattr(sys, "argv", argv + ["--max_frame_queries", "-1"])
    with pytest.raises(SystemExit):
        runner["config"]()


def test_task_results_stay_with_each_model_and_agent_without_category_summary(tmp_path, monkeypatch, runner):
    runs = []
    for model in ("claude-fable-5", "gpt-6-astra"):
        for i in range(1, 5):
            args = configure(runner, monkeypatch, tmp_path, model, f"agent{i}")
            root = Path(args.result_dir)
            task = root / "computer_13/screenshot/realtime_gui_bench/task-a1"
            task.mkdir(parents=True)
            score = float(i % 2)

            class Env:
                def evaluate(self):
                    self._evaluation_details = {
                        "benchmark_id": "A1", "pass_at_1": score, "pass_at_3": score,
                        "status": "passed" if score else "failed",
                    }
                    return score

            result = _evaluate_with_details(Env(), str(task))
            log_task_completion({"id": "task-a1"}, result, str(task), args)
            runs.append((root, score))

    for root, score in runs:
        results = json.loads((root / "summary/results.json").read_text())
        details = json.loads((root / "computer_13/screenshot/realtime_gui_bench/task-a1/result.json").read_text())
        assert details["pass_at_1"] == score
        assert {p.name for p in (root / "summary").iterdir()} == {"results.json"}
        assert len(results) == 1 and results[0]["score"] == score
    assert not (tmp_path / "summary").exists()
    assert not (tmp_path / "claude-fable-5/summary").exists()
    assert not (tmp_path / "gpt-6-astra/summary").exists()


def test_new_experiment_does_not_resume_an_older_batch(tmp_path, monkeypatch, runner):
    old = configure(runner, monkeypatch, tmp_path, "claude-fable-5", "agent1", "previous-batch")
    old_task = Path(old.result_dir) / "computer_13/screenshot/domain/task"
    old_task.mkdir(parents=True)
    (old_task / "result.txt").write_text("1")
    fresh = configure(runner, monkeypatch, tmp_path, "claude-fable-5", "agent1", None)
    assert fresh.run_id != old.run_id
    timestamp = datetime.datetime.strptime(fresh.run_id, "%Y%m%dT%H%M%S.%f%z")
    assert timestamp == datetime.datetime.fromisoformat(fresh.agent_started_at)
    assert runner["get_unfinished"](
        fresh.action_space, fresh.model, fresh.observation_type, fresh.result_dir,
        {"domain": ["task"]}, fresh.agent_variant,
    ) == {"domain": ["task"]}
    resumed = configure(runner, monkeypatch, tmp_path, "claude-fable-5", "agent1", old.run_id)
    assert runner["get_unfinished"](
        resumed.action_space, resumed.model, resumed.observation_type, resumed.result_dir,
        {"domain": ["task"]}, resumed.agent_variant,
    ) == {"domain": []}
    assert (old_task / "result.txt").read_text() == "1"


@pytest.mark.parametrize("run_id", ["", ".", "..", "../escape", "a/b", "a\\b"])
def test_run_id_cannot_escape_its_experiment_directory(tmp_path, monkeypatch, runner, run_id):
    with pytest.raises(SystemExit):
        configure(runner, monkeypatch, tmp_path, "claude-fable-5", "agent1", run_id)


def test_shared_batch_keeps_distinct_agent_and_resume_start_times(tmp_path, monkeypatch, runner):
    launches = [
        ("agent1", "2026-09-10T17:30:00+08:00"),
        ("agent2", "2026-09-10T18:10:00+08:00"),
        ("agent1", "2026-09-11T09:00:00+08:00"),
    ]
    batch = "20260910T173000+0800"
    for variant, actual_start in launches:
        args = configure(runner, monkeypatch, tmp_path, "claude-fable-5", variant, batch)
        args.agent_started_at = actual_start
        runner["write_run_configuration"](args)
    root = tmp_path / "claude-fable-5" / batch
    records = [json.loads(line) for line in (root / "agent1/launches.jsonl").read_text().splitlines()]
    assert [record["agent_started_at"] for record in records] == [launches[0][1], launches[2][1]]
    other = json.loads((root / "agent2/launches.jsonl").read_text())
    assert other["agent_started_at"] == launches[1][1]
    assert {record["run_id"] for record in [*records, other]} == {batch}
    batch_launches = [json.loads(line) for line in (root / "launches.jsonl").read_text().splitlines()]
    assert [record["agent_variant"] for record in batch_launches] == ["agent1", "agent2", "agent1"]
    manifest = json.loads((root / "experiment_manifest.json").read_text())
    assert manifest["created_at"] == launches[0][1]
    assert manifest["agents"] == ["agent1", "agent2", "agent3", "agent4"]
    args_json = json.loads((root / "agent1/computer_13/screenshot/args.json").read_text())
    assert args_json["agent_started_at"] == launches[2][1]


@pytest.mark.parametrize('model,key_env,limit', [
    ('claude-sonnet-5', 'PACKY_CLAUDE_SONNET_5_API_KEY', 128000),
    ('gpt-5.6-sol', 'PACKY_GPT_5_6_SOL_API_KEY', 128000),
    ('gemini-3.8-flash', 'PACKY_GEMINI_3_8_FLASH_API_KEY', 65536),
    ('qwen3.8-max-0902', 'PACKY_QWEN3_8_MAX_0902_API_KEY', 128000),
    ('kimi-k3', 'PACKY_KIMI_K3_API_KEY', 128000),
    ('deepseek-flash', 'PACKY_DEEPSEEK_FLASH_API_KEY', 128000),
    ('glm-5.3-flash', 'PACKY_GLM_5_3_FLASH_API_KEY', 128000),
    ('MiniMax-M3', 'PACKY_MINIMAX_M3_API_KEY', 128000),
    # Every model names its own variable, so no two accounts share one credential.
    ('claude-fable-5', 'PACKY_CLAUDE_FABLE_5_API_KEY', 128000),
    ('gpt-6-astra', 'PACKY_GPT_6_ASTRA_API_KEY', 128000),
])
def test_packy_model_cli_uses_config_and_requires_the_selected_key(tmp_path, monkeypatch, runner, model, key_env, limit):
    monkeypatch.delenv(key_env, raising=False)
    monkeypatch.setenv('PACKY_API_KEY', 'unrelated-provider-key')
    with pytest.raises(SystemExit):
        configure(runner, monkeypatch, tmp_path, model, 'agent3')
    monkeypatch.setenv(key_env, 'selected-test-key')
    args = configure(runner, monkeypatch, tmp_path, model, 'agent3')
    assert args.max_tokens == limit
    assert args.realtime_config['api']['key_env'] == key_env
    assert args.thinking_effort == (None if model == 'MiniMax-M3' else 'high')
    api_format = {'gemini-3.8-flash': 'openai_chat', 'gpt-5.6-sol': 'openai_responses',
                  'gpt-6-astra': 'openai_responses'}.get(model, 'anthropic_messages')
    assert args.api_format == api_format
