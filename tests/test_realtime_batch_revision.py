"""Git metadata must not be required to run a downloaded source archive."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.python import run_realtime_batch as batch


def initialize_git(path, *, commit=True):
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    if commit:
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                        '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'fixture'],
                       cwd=path, check=True)


def test_checkout_records_its_commit(tmp_path, monkeypatch):
    initialize_git(tmp_path)
    monkeypatch.setattr(batch, 'REPO', tmp_path)
    expected = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=tmp_path, text=True).strip()
    assert batch.git_commit() == expected


def test_archive_does_not_inherit_an_unrelated_parent_commit(tmp_path, monkeypatch):
    initialize_git(tmp_path)
    archive = tmp_path / 'extracted-source'
    archive.mkdir()
    monkeypatch.setattr(batch, 'REPO', archive)
    assert batch.git_commit() is None


def test_unborn_checkout_has_no_commit(tmp_path, monkeypatch):
    initialize_git(tmp_path, commit=False)
    monkeypatch.setattr(batch, 'REPO', tmp_path)
    assert batch.git_commit() is None


def test_missing_git_executable_is_optional(tmp_path, monkeypatch):
    initialize_git(tmp_path)
    monkeypatch.setattr(batch, 'REPO', tmp_path)
    monkeypatch.setenv('PATH', str(tmp_path / 'no-executables'))
    assert batch.git_commit() is None


def test_batch_from_archive_writes_manifest_and_reaches_worker_start(tmp_path, monkeypatch):
    from mm_agents import realtime_auditor, realtime_env

    archive = tmp_path / 'extracted-source'
    archive.mkdir()
    monkeypatch.setattr(batch, 'REPO', archive)
    monkeypatch.setattr(realtime_env, 'load_env_file', lambda: {})
    monkeypatch.setattr(realtime_auditor, 'require_judge_key', lambda: 'test-key')
    monkeypatch.setattr(batch, 'load_tasks', lambda args: {'realtime_gui_bench': ['task']})
    monkeypatch.setattr(batch, 'model_key_envs', lambda *args: {'deepseek-flash': 'TEST_API_KEY'})
    monkeypatch.setattr(batch, 'resolve_keys', lambda *args: (
        {'deepseek-flash': 'test-key'}, {'deepseek-flash': 'environment'}))
    argv = ['run_realtime_batch.py', '--agent_variant', 'agent4', '--models', 'deepseek-flash',
            '--run_id', 'archive-test', '--result_dir', 'results', '--exclusive-keys-confirmed']
    if hasattr(batch, 'DEFAULT_PACKY_BASE_URL'):
        argv.append('--skip-billing')
    monkeypatch.setattr(sys, 'argv', argv)

    class WorkerStartReached(Exception):
        pass

    def start_worker(command, **kwargs):
        assert 'scripts/python/run_multienv.py' in command
        assert Path(kwargs['cwd']) == archive
        raise WorkerStartReached

    monkeypatch.setattr(batch.subprocess, 'Popen', start_worker)
    with pytest.raises(WorkerStartReached):
        batch.main()
    manifest = json.loads((archive / 'results/_cost/archive-test/manifest.json').read_text())
    assert manifest['commit'] is None
    assert manifest['models'] == ['deepseek-flash']
    assert manifest['task_count'] == 1
