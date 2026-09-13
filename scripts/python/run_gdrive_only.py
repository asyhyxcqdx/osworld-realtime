#!/usr/bin/env python3
"""Prepare or run the eight OSWorld Google Drive tasks in isolation.

The source task JSON files are never modified. An overlay injects a private
Docker-bridge proxy only into Google Chrome launch commands and points browser
login at an existing private settings file. Host-side PyDrive uses the same
managed Clash instance. Results always go to a separate recovery directory.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import requests
import yaml

from osworld_gdrive_proxy import ManagedClashProxy, ProxyStartupError
from refresh_gdrive_oauth import (
    CredentialValidationError,
    DEFAULT_CLASH_BINARY,
    DEFAULT_CLASH_CONFIG,
    DEFAULT_CREDENTIALS,
    DEFAULT_LOGIN_SETTINGS,
    DEFAULT_SETTINGS,
    proxy_environment,
    verify_installed_credentials,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts/python/run_multienv_uitars15_v1.py"
SOURCE_CONFIG_BASE = REPO_ROOT / "evaluation_examples"
PROTECTED_RESULT_DIR = REPO_ROOT / "results_uitars15_full_official"
DEFAULT_RESULT_DIR = REPO_ROOT / "results_uitars15_gdrive_recovery"
DEFAULT_VM_PATH = REPO_ROOT / "docker_vm_data/Ubuntu.qcow2"

GDRIVE_TASK_IDS = (
    "0c825995-5b70-4526-b663-113f4c999dd2",
    "22a4636f-8179-4357-8e87-d1743ece1f81",
    "46407397-a7d5-4c6b-92c6-dbe038b1457b",
    "4e9f0faf-2ecc-4ae8-a804-28c9a75d1ddc",
    "78aed49a-a710-4321-a793-b611a7c5b56b",
    "897e3b53-5d4d-444b-85cb-2cdc8a97d903",
    "a0b9dc9c-fc07-4a88-8c5d-5e3ecad91bcb",
    "b52b40a5-ad70-4c53-b5b0-5650a8387052",
)


class OverlayError(RuntimeError):
    pass


def _configured_credentials_path(settings_path: Path) -> Path:
    try:
        settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
        configured = Path(settings["save_credentials_file"]).expanduser()
    except Exception as exc:
        raise OverlayError("Google Drive settings do not name a credential file") from exc
    if not configured.is_absolute():
        configured = REPO_ROOT / configured
    return configured.absolute()


def _formal_runner_active() -> bool:
    marker = b"results_uitars15_full_official"
    runner = b"run_multienv_uitars15_v1.py"
    proc_root = Path("/proc")
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit() or int(process_dir.name) == os.getpid():
            continue
        try:
            command = (process_dir / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if runner in command and marker in command:
            return True
    return False


@contextlib.contextmanager
def _exclusive_recovery_lock():
    lock_path = Path(tempfile.gettempdir()) / "osworld-gdrive-recovery.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Google Drive recovery process is active") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _inject_vm_proxy(
    task: dict[str, Any], *, vm_proxy_url: str, login_settings: Path
) -> dict[str, Any]:
    copied = copy.deepcopy(task)
    chrome_launch_count = 0
    login_count = 0
    for step in copied.get("config", []):
        parameters = step.get("parameters") or {}
        if step.get("type") == "launch":
            command = parameters.get("command")
            if (
                isinstance(command, list)
                and command
                and Path(str(command[0])).name == "google-chrome"
            ):
                command = [
                    item
                    for item in command
                    if not str(item).startswith("--proxy-server=")
                ]
                command.append(f"--proxy-server={vm_proxy_url}")
                parameters["command"] = command
                chrome_launch_count += 1
        elif step.get("type") == "login" and parameters.get("platform") == "googledrive":
            parameters["settings_file"] = str(login_settings)
            login_count += 1

    if chrome_launch_count != 1:
        raise OverlayError(
            f"Task {copied.get('id', '<unknown>')} has {chrome_launch_count} Chrome launches"
        )
    if login_count != 1:
        raise OverlayError(
            f"Task {copied.get('id', '<unknown>')} has {login_count} Google login steps"
        )
    return copied


def build_overlay(
    *,
    overlay_dir: Path,
    vm_proxy_url: str,
    login_settings: Path,
    source_config_base: Path = SOURCE_CONFIG_BASE,
) -> tuple[Path, Path]:
    if not login_settings.is_file():
        raise OverlayError("Google browser-login settings file is missing")
    examples_dir = overlay_dir / "examples/multi_apps"
    examples_dir.mkdir(parents=True, exist_ok=True)

    for task_id in GDRIVE_TASK_IDS:
        source = source_config_base / f"examples/multi_apps/{task_id}.json"
        try:
            task = json.loads(source.read_text(encoding="utf-8"))
        except Exception as exc:
            raise OverlayError(f"Could not load source task {task_id}") from exc
        if task.get("id") != task_id:
            raise OverlayError(f"Task ID mismatch for {task_id}")
        modified = _inject_vm_proxy(
            task,
            vm_proxy_url=vm_proxy_url,
            login_settings=login_settings,
        )
        _atomic_json_write(examples_dir / f"{task_id}.json", modified)

    metadata_path = overlay_dir / "test_gdrive_only.json"
    _atomic_json_write(metadata_path, {"multi_apps": list(GDRIVE_TASK_IDS)})
    manifest_path = overlay_dir / "manifest.json"
    _atomic_json_write(
        manifest_path,
        {
            "task_count": len(GDRIVE_TASK_IDS),
            "task_ids": list(GDRIVE_TASK_IDS),
            "source_config_base": str(source_config_base.resolve()),
            "login_settings_path": str(login_settings.resolve()),
            "vm_proxy_url": vm_proxy_url,
            "source_tasks_modified": False,
        },
    )
    return overlay_dir, metadata_path


def _runner_command(args: argparse.Namespace, overlay: Path, metadata: Path) -> list[str]:
    return [
        str(args.python),
        str(RUNNER),
        "--provider_name",
        "docker",
        "--path_to_vm",
        str(args.path_to_vm),
        "--headless",
        "--action_space",
        "pyautogui",
        "--observation_type",
        "screenshot",
        "--model",
        args.model,
        "--model_type",
        "qwen25vl",
        "--infer_mode",
        "qwen25vl_normal",
        "--input_swap",
        "--domain",
        "all",
        "--test_all_meta_path",
        str(metadata),
        "--test_config_base_dir",
        str(overlay),
        "--max_steps",
        "100",
        "--num_envs",
        "1",
        "--client_password",
        "password",
        "--result_dir",
        str(args.result_dir),
        "--log_level",
        args.log_level,
    ]


def _check_model(model_url: str) -> None:
    health_url = model_url.removesuffix("/v1").rstrip("/") + "/health"
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(health_url, timeout=5)
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError("UI-TARS vLLM health check failed") from exc
    finally:
        session.close()


def _run_child(command: list[str], environment: dict[str, str]) -> int:
    process = subprocess.Popen(command, cwd=REPO_ROOT, env=environment)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.send_signal(signal.SIGTERM)
        try:
            return process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Actually launch the eight tasks; without this flag only validate the overlay",
    )
    parser.add_argument("--result-dir", type=Path, default=DEFAULT_RESULT_DIR)
    parser.add_argument("--path-to-vm", type=Path, default=DEFAULT_VM_PATH)
    parser.add_argument(
        "--python",
        type=Path,
        default=Path("/mnt/zhaorunsong/anaconda3/envs/osworld-yhyx/bin/python"),
    )
    parser.add_argument("--model", default="uitars15-7b")
    parser.add_argument("--model-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--login-settings", type=Path, default=DEFAULT_LOGIN_SETTINGS)
    parser.add_argument("--clash-binary", type=Path, default=DEFAULT_CLASH_BINARY)
    parser.add_argument("--clash-config", type=Path, default=DEFAULT_CLASH_CONFIG)
    parser.add_argument("--bind-host")
    parser.add_argument("--proxy-port", type=int, default=27891)
    parser.add_argument("--controller-port", type=int, default=29091)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.result_dir = args.result_dir.expanduser().absolute()
    args.path_to_vm = args.path_to_vm.expanduser().resolve()
    args.python = args.python.expanduser().resolve()
    settings = args.settings.expanduser().resolve()
    credentials = args.credentials.expanduser().absolute()
    login_settings = args.login_settings.expanduser().resolve()

    if args.result_dir.resolve() == PROTECTED_RESULT_DIR.resolve():
        print("ERROR: this helper refuses to write the formal full-run result directory")
        return 2
    if not args.path_to_vm.is_file() or not args.python.is_file():
        print("ERROR: Python environment or Docker VM image is missing")
        return 1

    try:
        if _configured_credentials_path(settings).resolve() != credentials.resolve():
            raise OverlayError(
                "--credentials must match save_credentials_file in the Drive settings"
            )
        if not args.run:
            with tempfile.TemporaryDirectory(prefix="osworld-gdrive-overlay-check-") as temp_dir:
                overlay, metadata = build_overlay(
                    overlay_dir=Path(temp_dir),
                    vm_proxy_url="http://10.200.0.1:27891",
                    login_settings=login_settings,
                )
                command = _runner_command(args, overlay, metadata)
                if "--enable_proxy" in command:
                    raise OverlayError("Dry-run command unexpectedly enables task proxy setup")
                print(
                    "DRY RUN OK: exactly 8 tasks; source JSON unchanged; "
                    "one Chrome bridge proxy per task; one environment; separate results"
                )
                print("Run again with --run after OAuth verification succeeds.")
                return 0

        if _formal_runner_active():
            raise RuntimeError(
                "Formal full-run workers are still active; recovery must wait"
            )

        with _exclusive_recovery_lock():
            args.result_dir.mkdir(parents=True, exist_ok=True)
            overlay_dir = args.result_dir / "_gdrive_overlay"
            with ManagedClashProxy(
                binary=args.clash_binary,
                config=args.clash_config,
                bind_host=args.bind_host,
                proxy_port=args.proxy_port,
                controller_port=args.controller_port,
            ) as proxy:
                overlay, metadata = build_overlay(
                    overlay_dir=overlay_dir,
                    vm_proxy_url=proxy.vm_proxy_url,
                    login_settings=login_settings,
                )
                with proxy_environment(proxy.host_proxy_url, proxy.bind_host):
                    verify_installed_credentials(
                        settings_path=settings,
                        credentials_path=credentials,
                        login_settings=login_settings,
                    )
                    _check_model(args.model_url)
                    environment = os.environ.copy()
                    environment.update(
                        {
                            "DOUBAO_API_URL": args.model_url,
                            "DOUBAO_API_KEY": "EMPTY",
                            "HF_ENDPOINT": "https://hf-mirror.com",
                            "TOKENIZERS_PARALLELISM": "false",
                        }
                    )
                    command = _runner_command(args, overlay, metadata)
                    print(
                        "STARTING: 8 Google Drive tasks, sequentially, "
                        f"results={args.result_dir}"
                    )
                    return _run_child(command, environment)
    except (OverlayError, ProxyStartupError, CredentialValidationError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        print("CANCELLED: recovery runner stopped")
        return 130
    except Exception:
        print("ERROR: unexpected recovery-runner failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
