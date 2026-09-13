from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import requests
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts/python"
sys.path.insert(0, str(SCRIPTS))

from osworld_gdrive_proxy import ManagedClashProxy, validate_proxy_url
import refresh_gdrive_oauth
from desktop_env.providers.docker.provider import (
    PORT_BIND_HOST_ENV,
    _host_port_binding,
)
from refresh_gdrive_oauth import (
    _CallbackState,
    _OAuthCallbackHandler,
    _is_additional_verification,
    atomic_install_candidate,
    proxy_environment,
)
from run_gdrive_only import GDRIVE_TASK_IDS, build_overlay


class ProxyHelperTests(unittest.TestCase):
    def test_proxy_url_rejects_credentials(self) -> None:
        with self.assertRaises(ValueError):
            validate_proxy_url("http://user:password@127.0.0.1:1234")

    def test_runtime_config_is_private_and_source_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "config.yaml"
            source = {
                "mixed-port": 7890,
                "allow-lan": False,
                "bind-address": "127.0.0.1",
                "external-controller": "127.0.0.1:9090",
                "proxies": [{"name": "node", "type": "vmess", "server": "example"}],
                "proxy-groups": [],
                "rules": [],
            }
            source_path.write_text(yaml.safe_dump(source), encoding="utf-8")
            before = source_path.read_bytes()
            helper = ManagedClashProxy(
                binary=Path("/bin/true"),
                config=source_path,
                bind_host="127.0.0.1",
                proxy_port=27891,
                controller_port=29091,
            )
            runtime_path = helper._write_runtime_config(helper._load_source_config())
            runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
            self.assertEqual(runtime["mixed-port"], 27891)
            self.assertEqual(runtime["bind-address"], "127.0.0.1")
            self.assertEqual(runtime["external-controller"], "127.0.0.1:29091")
            self.assertEqual(runtime_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(source_path.read_bytes(), before)
            runtime_dir = helper.runtime_dir
            helper.close()
            self.assertIsNotNone(runtime_dir)
            self.assertFalse(runtime_dir.exists())

    def test_proxy_environment_is_restored(self) -> None:
        original = os.environ.get("HTTP_PROXY")
        with proxy_environment("http://127.0.0.1:27891", "10.200.0.1"):
            self.assertEqual(os.environ["HTTPS_PROXY"], "http://127.0.0.1:27891")
            self.assertIn("localhost", os.environ["NO_PROXY"])
        self.assertEqual(os.environ.get("HTTP_PROXY"), original)


class OAuthHelperTests(unittest.TestCase):
    def test_password_page_checks_only_visible_captcha_elements(self) -> None:
        page = mock.Mock()
        page.url = "https://accounts.google.com/v3/signin/challenge/pwd"
        page.locator.return_value.count.return_value = 0

        self.assertFalse(_is_additional_verification(page))
        selector = page.locator.call_args.args[0]
        self.assertIn("iframe[src*='recaptcha']:visible", selector)
        self.assertIn("#captchaimg:visible", selector)

    def test_atomic_install_replaces_only_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "credentials.json"
            candidate = root / ".credentials.candidate.test.json"
            destination.write_text("old", encoding="utf-8")
            candidate.write_text("new", encoding="utf-8")
            atomic_install_candidate(candidate, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), "new")
            self.assertFalse(candidate.exists())
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_atomic_install_refuses_destination_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "actual-credentials.json"
            target.write_text("original", encoding="utf-8")
            destination = root / "credentials.json"
            destination.symlink_to(target)
            candidate = root / ".credentials.candidate.test.json"
            candidate.write_text("candidate", encoding="utf-8")

            # absolute() preserves the final symlink; resolve() would not.
            destination = destination.absolute()
            self.assertTrue(destination.is_symlink())
            with self.assertRaises(Exception):
                atomic_install_candidate(candidate, destination)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertTrue(candidate.exists())

    def test_callback_does_not_log_authorization_code(self) -> None:
        callback_state = _CallbackState(
            "https://accounts.example.invalid/authorize", "expected-state"
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OAuthCallbackHandler)
        server.callback_state = callback_state
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        output = io.StringIO()
        session = requests.Session()
        session.trust_env = False
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                response = session.get(
                    f"http://127.0.0.1:{server.server_port}/"
                    "?state=expected-state&code=sensitive-test-code",
                    timeout=3,
                )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(callback_state.event.is_set())
            self.assertEqual(callback_state.code, "sensitive-test-code")
            self.assertNotIn("sensitive-test-code", output.getvalue())
        finally:
            session.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_manual_verification_rejects_ssh_browser(self) -> None:
        argv = [
            "refresh_gdrive_oauth.py",
            "authorize",
            "--install",
            "--browser",
            "ssh",
            "--manual-verification-timeout",
            "30",
        ]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            result = refresh_gdrive_oauth.main()
        self.assertEqual(result, 2)
        self.assertIn("supported only by the Docker browser", output.getvalue())

    def test_manual_verification_rejects_negative_timeout(self) -> None:
        argv = [
            "refresh_gdrive_oauth.py",
            "authorize",
            "--install",
            "--browser",
            "docker",
            "--manual-verification-timeout",
            "-1",
        ]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            result = refresh_gdrive_oauth.main()
        self.assertEqual(result, 2)
        self.assertIn("finite non-negative", output.getvalue())

    def test_manual_verification_rejects_nonfinite_timeout(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                argv = [
                    "refresh_gdrive_oauth.py",
                    "authorize",
                    "--install",
                    "--browser",
                    "docker",
                    f"--manual-verification-timeout={value}",
                ]
                output = io.StringIO()
                with mock.patch.object(
                    sys, "argv", argv
                ), contextlib.redirect_stdout(output):
                    result = refresh_gdrive_oauth.main()
                self.assertEqual(result, 2)
                self.assertIn("finite non-negative", output.getvalue())

    def test_docker_port_binding_can_be_restricted_to_loopback(self) -> None:
        with mock.patch.dict(
            os.environ, {PORT_BIND_HOST_ENV: "127.0.0.1"}, clear=False
        ):
            self.assertEqual(_host_port_binding(8006), ("127.0.0.1", 8006))

    def test_docker_port_binding_rejects_non_loopback(self) -> None:
        with mock.patch.dict(
            os.environ, {PORT_BIND_HOST_ENV: "0.0.0.0"}, clear=False
        ):
            with self.assertRaises(ValueError):
                _host_port_binding(8006)


class GDriveOverlayTests(unittest.TestCase):
    def test_overlay_contains_only_eight_tasks_and_does_not_touch_sources(self) -> None:
        sources = [
            REPO_ROOT / f"evaluation_examples/examples/multi_apps/{task_id}.json"
            for task_id in GDRIVE_TASK_IDS
        ]
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            login_settings = root / "google-login.json"
            login_settings.write_text(
                json.dumps({"email": "test@example.invalid", "password": "test"}),
                encoding="utf-8",
            )
            overlay, metadata_path = build_overlay(
                overlay_dir=root / "overlay",
                vm_proxy_url="http://10.200.0.1:27891",
                login_settings=login_settings,
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata, {"multi_apps": list(GDRIVE_TASK_IDS)})
            generated = sorted((overlay / "examples/multi_apps").glob("*.json"))
            self.assertEqual(len(generated), 8)
            self.assertEqual({path.stem for path in generated}, set(GDRIVE_TASK_IDS))

            for path in generated:
                task = json.loads(path.read_text(encoding="utf-8"))
                chrome_commands = [
                    step["parameters"]["command"]
                    for step in task["config"]
                    if step.get("type") == "launch"
                    and isinstance(step.get("parameters", {}).get("command"), list)
                    and Path(step["parameters"]["command"][0]).name == "google-chrome"
                ]
                self.assertEqual(len(chrome_commands), 1)
                proxy_flags = [
                    item
                    for item in chrome_commands[0]
                    if item.startswith("--proxy-server=")
                ]
                self.assertEqual(proxy_flags, ["--proxy-server=http://10.200.0.1:27891"])

                login_steps = [
                    step
                    for step in task["config"]
                    if step.get("type") == "login"
                    and step.get("parameters", {}).get("platform") == "googledrive"
                ]
                self.assertEqual(len(login_steps), 1)
                self.assertEqual(
                    login_steps[0]["parameters"]["settings_file"],
                    str(login_settings),
                )

        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
