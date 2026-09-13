#!/usr/bin/env python3
"""Safely verify or replace OSWorld Google Drive OAuth credentials.

The existing OAuth client is a Desktop/installed client with a localhost
redirect URI. The supported low-friction flow is therefore a localhost
callback forwarded over SSH. Google's device authorization flow requires a
different OAuth client type and is not used here.

No client secret, authorization code, access token, refresh token, email, or
password is printed. A new credential is written to a private candidate file,
forced through a refresh, checked with a minimal Drive API request, and only
then atomically installed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive

from osworld_gdrive_proxy import ManagedClashProxy, ProxyStartupError


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_SETTINGS = REPO_ROOT / "evaluation_examples/settings/googledrive/settings.yml"
DEFAULT_CREDENTIALS = (
    REPO_ROOT / "evaluation_examples/settings/googledrive/credentials.json"
)
DEFAULT_LOGIN_SETTINGS = (
    Path("/mnt/zhaorunsong/repo/CUA/Env/OSWorld")
    / "evaluation_examples/settings/google/settings.json"
)
DEFAULT_CLASH_BINARY = Path("/mnt/zhaorunsong/lx/clash/clash-linux-amd64-latest")
DEFAULT_CLASH_CONFIG = Path("/mnt/zhaorunsong/lx/clash/config.yaml")
DEFAULT_VM_PATH = REPO_ROOT / "docker_vm_data/Ubuntu.qcow2"


class CredentialValidationError(RuntimeError):
    pass


class AuthorizationFlowError(RuntimeError):
    pass


def _prepare_authorization(
    auth: GoogleAuth, callback_port: int
) -> tuple[str, str]:
    auth.GetFlow()
    auth.flow.redirect_uri = f"http://localhost:{callback_port}/"
    oauth_state = secrets.token_urlsafe(32)
    auth_url = auth.flow.step1_get_authorize_url(state=oauth_state)
    return auth_url, oauth_state


@contextlib.contextmanager
def proxy_environment(proxy_url: str, bridge_host: str) -> Iterator[None]:
    keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
    )
    previous = {key: os.environ.get(key) for key in keys}
    bypass = f"127.0.0.1,localhost,{bridge_host}"
    os.environ.update(
        {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "NO_PROXY": bypass,
            "no_proxy": bypass,
        }
    )
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _expected_email(login_settings: Path | None) -> str | None:
    if login_settings is None:
        return None
    try:
        data = json.loads(login_settings.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CredentialValidationError("Google login settings are not readable") from exc
    email = data.get("email")
    password = data.get("password")
    if not isinstance(email, str) or not email or not isinstance(password, str) or not password:
        raise CredentialValidationError("Google login settings are incomplete")
    return email.casefold()


def _load_google_auth(settings_path: Path, credential_path: Path) -> GoogleAuth:
    auth = GoogleAuth(settings_file=str(settings_path))
    try:
        auth.LoadCredentialsFile(str(credential_path))
    except Exception as exc:
        raise CredentialValidationError("Candidate credential could not be loaded") from exc
    if auth.credentials is None or not auth.credentials.refresh_token:
        raise CredentialValidationError("Candidate credential has no refresh token")
    return auth


def validate_candidate_credentials(
    *,
    settings_path: Path,
    candidate_path: Path,
    login_settings: Path | None = None,
) -> None:
    """Force refresh, minimally query Drive, and persist the refreshed candidate."""

    expected_email = _expected_email(login_settings)
    auth = _load_google_auth(settings_path, candidate_path)
    try:
        # Force this even when the newly-issued access token is still fresh. It
        # proves that the refresh token needed by unattended evaluation works.
        auth.Refresh()
        auth.Authorize()
        drive = GoogleDrive(auth)
        drive.ListFile(
            {"q": "'root' in parents and trashed = false", "maxResults": 1}
        ).GetList()
        if expected_email is not None:
            about = auth.service.about().get(fields="user(emailAddress)").execute()
            actual_email = ((about.get("user") or {}).get("emailAddress") or "").casefold()
            if actual_email != expected_email:
                raise CredentialValidationError(
                    "OAuth account does not match the OSWorld browser-login account"
                )
        auth.SaveCredentialsFile(str(candidate_path))
    except CredentialValidationError:
        raise
    except Exception as exc:
        raise CredentialValidationError(
            "OAuth refresh or the minimal Drive API check failed"
        ) from exc

    candidate_path.chmod(0o600)
    with candidate_path.open("rb") as handle:
        os.fsync(handle.fileno())


def verify_installed_credentials(
    *,
    settings_path: Path,
    credentials_path: Path,
    login_settings: Path | None = None,
) -> None:
    """Validate a private copy so the installed credential is never mutated."""

    if credentials_path.is_symlink():
        raise CredentialValidationError("Refusing to verify a credential symlink")
    if not credentials_path.is_file():
        raise CredentialValidationError("Installed credential file is missing")
    with tempfile.TemporaryDirectory(prefix="osworld-gdrive-verify-") as temp_dir:
        candidate = Path(temp_dir) / "credentials.json"
        shutil.copyfile(credentials_path, candidate)
        candidate.chmod(0o600)
        validate_candidate_credentials(
            settings_path=settings_path,
            candidate_path=candidate,
            login_settings=login_settings,
        )


def _candidate_path(credentials_path: Path) -> Path:
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".credentials.candidate.",
        suffix=".json",
        dir=credentials_path.parent,
    )
    os.close(descriptor)
    candidate = Path(name)
    candidate.chmod(0o600)
    return candidate


def atomic_install_candidate(candidate: Path, credentials_path: Path) -> None:
    if candidate.parent.resolve() != credentials_path.parent.resolve():
        raise CredentialValidationError(
            "Candidate must be on the same filesystem as the installed credential"
        )
    if credentials_path.is_symlink():
        raise CredentialValidationError("Refusing to replace a credential symlink")
    if candidate.is_symlink() or not candidate.is_file():
        raise CredentialValidationError("Candidate credential is not a regular file")
    candidate.chmod(0o600)
    os.replace(candidate, credentials_path)
    credentials_path.chmod(0o600)
    directory_fd = os.open(credentials_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class _CallbackState:
    def __init__(self, auth_url: str, expected_state: str) -> None:
        self.auth_url = auth_url
        self.expected_state = expected_state
        self.event = threading.Event()
        self.code: str | None = None
        self.error = False


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    server_version = "OSWorldOAuth/1.0"

    def log_message(self, format: str, *args) -> None:
        # Request URLs contain authorization codes and must never reach logs.
        return

    def _plain_response(self, status: HTTPStatus, message: str) -> None:
        body = (message + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        state: _CallbackState = self.server.callback_state  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        if parsed.path == "/start":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", state.auth_url)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if parsed.path != "/":
            self._plain_response(HTTPStatus.NOT_FOUND, "Not found")
            return

        query = parse_qs(parsed.query)
        callback_state = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        if callback_state != state.expected_state or not code or query.get("error"):
            state.error = True
            state.event.set()
            self._plain_response(
                HTTPStatus.BAD_REQUEST,
                "Authorization was not accepted. Return to the server terminal.",
            )
            return

        state.code = code
        state.event.set()
        self._plain_response(
            HTTPStatus.OK,
            "Authorization received. You may close this browser tab.",
        )


def wait_for_authorization_code(
    *,
    auth: GoogleAuth,
    callback_port: int,
    timeout: float,
) -> str:
    auth_url, oauth_state = _prepare_authorization(auth, callback_port)
    callback_state = _CallbackState(auth_url, oauth_state)

    server = ThreadingHTTPServer(("127.0.0.1", callback_port), _OAuthCallbackHandler)
    server.callback_state = callback_state  # type: ignore[attr-defined]
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="osworld-oauth-callback",
        daemon=True,
    )
    server_thread.start()
    try:
        print("ACTION REQUIRED: complete one Google authorization in your own browser.")
        print(
            f"1. On your computer, open an SSH tunnel: "
            f"ssh -N -L {callback_port}:127.0.0.1:{callback_port} <this-server>"
        )
        print(f"2. Open http://localhost:{callback_port}/start and approve Drive access.")
        print("Waiting for the localhost callback; no credential values will be logged.")
        if not callback_state.event.wait(timeout):
            raise AuthorizationFlowError("Timed out waiting for browser authorization")
        if callback_state.error or callback_state.code is None:
            raise AuthorizationFlowError("Browser authorization was rejected")
        return callback_state.code
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def _login_values(login_settings: Path) -> tuple[str, str]:
    try:
        data = json.loads(login_settings.read_text(encoding="utf-8"))
        email = data["email"]
        password = data["password"]
    except Exception as exc:
        raise AuthorizationFlowError("Google login settings are not readable") from exc
    if not isinstance(email, str) or not email or not isinstance(password, str) or not password:
        raise AuthorizationFlowError("Google login settings are incomplete")
    return email, password


def _is_additional_verification(page) -> bool:
    path = urlsplit(page.url).path.casefold()
    if "signin/rejected" in path:
        return True
    if page.locator(
        "iframe[src*='recaptcha']:visible, #captchaimg:visible, "
        "input[name='ca']:visible"
    ).count():
        return True
    if "/challenge/" in path and not path.rstrip("/").endswith("/pwd"):
        return not bool(page.locator("input[name='Passwd']:visible").count())
    return False


def _click_standard_consent(page) -> bool:
    path = urlsplit(page.url).path.casefold()
    if "oauth" in path:
        checkboxes = page.locator("input[type='checkbox']:visible")
        for index in range(checkboxes.count()):
            checkbox = checkboxes.nth(index)
            try:
                if checkbox.is_enabled() and not checkbox.is_checked():
                    checkbox.check(timeout=3000)
            except Exception:
                continue

    for label in ("Continue", "Allow", "继续", "允许", "同意"):
        pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.IGNORECASE)
        button = page.get_by_role("button", name=pattern)
        for index in range(button.count()):
            candidate = button.nth(index)
            try:
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click(timeout=5000)
                    return True
            except Exception:
                continue
    return False


def docker_browser_authorization_code(
    *,
    auth: GoogleAuth,
    callback_port: int,
    login_settings: Path,
    vm_proxy_url: str,
    path_to_vm: Path,
    manual_verification_timeout: float = 0,
) -> str:
    """Use a clean OSWorld VM's real Chrome for normal login and consent only."""

    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    from desktop_env.controllers.setup import _connect_chrome_over_cdp
    from desktop_env.desktop_env import DesktopEnv

    email, password = _login_values(login_settings)
    auth_url, expected_state = _prepare_authorization(auth, callback_port)
    captured: dict[str, str | bool | None] = {
        "code": None,
        "error": False,
        "state_mismatch": False,
    }

    def capture_callback(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.hostname not in {"localhost", "127.0.0.1"}:
            return
        try:
            parsed_port = parsed.port
        except ValueError:
            return
        if parsed_port != callback_port or parsed.path != "/":
            return
        query = parse_qs(parsed.query)
        callback_state = (query.get("state") or [""])[0]
        if callback_state != expected_state:
            captured["state_mismatch"] = True
            return
        if query.get("error"):
            captured["error"] = True
            return
        code = (query.get("code") or [""])[0]
        if code and callback_state == expected_state:
            captured["code"] = code

    env = None
    browser = None
    try:
        from desktop_env.providers.docker.provider import PORT_BIND_HOST_ENV

        previous_bind_host = os.environ.get(PORT_BIND_HOST_ENV)
        os.environ[PORT_BIND_HOST_ENV] = "127.0.0.1"
        try:
            env = DesktopEnv(
                provider_name="docker",
                path_to_vm=str(path_to_vm),
                headless=True,
                action_space="pyautogui",
                require_a11y_tree=False,
            )
        finally:
            if previous_bind_host is None:
                os.environ.pop(PORT_BIND_HOST_ENV, None)
            else:
                os.environ[PORT_BIND_HOST_ENV] = previous_bind_host
        env.setup_controller.setup(
            [
                {
                    "type": "launch",
                    "parameters": {
                        "command": [
                            "google-chrome",
                            "--remote-debugging-port=1337",
                            f"--proxy-server={vm_proxy_url}",
                        ]
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
            ]
        )

        with sync_playwright() as playwright:
            browser = _connect_chrome_over_cdp(
                playwright,
                f"http://{env.vm_ip}:{env.chromium_port}",
                env.setup_controller.http_server,
            )
            page = browser.contexts[0].new_page()
            page.on("request", lambda request: capture_callback(request.url))

            manual_deadline: float | None = None
            manual_notice_printed = False

            def wait_for_additional_verification() -> str | None:
                nonlocal manual_deadline, manual_notice_printed
                if manual_verification_timeout <= 0:
                    raise AuthorizationFlowError(
                        "Google requested CAPTCHA, 2FA, recovery, or a security check; stopped"
                    )
                if manual_deadline is None:
                    manual_deadline = time.monotonic() + manual_verification_timeout
                if not manual_notice_printed:
                    vnc_port = getattr(env, "vnc_port", None)
                    if (
                        not isinstance(vnc_port, int)
                        or isinstance(vnc_port, bool)
                        or not 1 <= vnc_port <= 65535
                    ):
                        raise AuthorizationFlowError(
                            "Manual verification desktop is unavailable"
                        )
                    print(
                        "MANUAL VERIFICATION REQUIRED: use the isolated VNC desktop; "
                        "no account credentials will be displayed here.",
                        flush=True,
                    )
                    print(f"VNC_PORT={vnc_port}", flush=True)
                    manual_notice_printed = True

                while _is_additional_verification(page):
                    capture_callback(page.url)
                    if captured["state_mismatch"] or captured["error"]:
                        raise AuthorizationFlowError("Google authorization was rejected")
                    if captured["code"]:
                        return str(captured["code"])
                    if time.monotonic() >= manual_deadline:
                        raise AuthorizationFlowError(
                            "Timed out waiting for manual Google verification"
                        )
                    page.wait_for_timeout(1000)
                return None

            try:
                page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
            except PlaywrightTimeout as exc:
                raise AuthorizationFlowError(
                    "Google sign-in page did not load in the isolated VM"
                ) from exc

            identifier = page.locator("#identifierId:visible")
            try:
                identifier.wait_for(state="visible", timeout=15000)
                identifier.fill(email)
                page.locator("#identifierNext").click(timeout=5000)
            except Exception as exc:
                raise AuthorizationFlowError(
                    "Normal Google email sign-in step was not available"
                ) from exc

            password_input = page.locator("input[name='Passwd']:visible")
            for _ in range(30):
                capture_callback(page.url)
                if captured["code"]:
                    return str(captured["code"])
                if _is_additional_verification(page):
                    code = wait_for_additional_verification()
                    if code is not None:
                        return code
                    continue
                if password_input.count() and password_input.is_visible():
                    break
                page.wait_for_timeout(1000)
            else:
                raise AuthorizationFlowError(
                    "Google did not present the normal password step; stopped"
                )

            try:
                password_input.fill(password)
                page.locator("#passwordNext").click(timeout=5000)
            except Exception as exc:
                raise AuthorizationFlowError(
                    "Normal Google password submission failed"
                ) from exc

            consent_clicks = 0
            automatic_deadline = time.monotonic() + 60
            while True:
                capture_callback(page.url)
                if captured["state_mismatch"] or captured["error"]:
                    raise AuthorizationFlowError("Google authorization was rejected")
                if captured["code"]:
                    return str(captured["code"])
                if _is_additional_verification(page):
                    code = wait_for_additional_verification()
                    if code is not None:
                        return code
                    continue
                if page.locator("#identifierId:visible").count():
                    raise AuthorizationFlowError(
                        "Google returned to sign-in instead of standard consent; stopped"
                    )
                if _click_standard_consent(page):
                    consent_clicks += 1
                    if consent_clicks > 4:
                        raise AuthorizationFlowError(
                            "Google did not complete after standard consent; stopped"
                        )
                    page.wait_for_timeout(2000)
                    continue
                active_deadline = manual_deadline or automatic_deadline
                if time.monotonic() >= active_deadline:
                    break
                page.wait_for_timeout(1000)
            raise AuthorizationFlowError(
                "Google did not reach a standard Drive consent callback; stopped"
            )
    except AuthorizationFlowError:
        raise
    except Exception as exc:
        raise AuthorizationFlowError(
            "Isolated Google authorization failed before a valid callback"
        ) from exc
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if env is not None:
            try:
                env.close()
            except Exception:
                pass


def authorize_candidate(
    *,
    settings_path: Path,
    credentials_path: Path,
    login_settings: Path | None,
    callback_port: int,
    callback_timeout: float,
    browser_mode: str = "ssh",
    vm_proxy_url: str | None = None,
    path_to_vm: Path = DEFAULT_VM_PATH,
    manual_verification_timeout: float = 0,
) -> Path:
    auth = GoogleAuth(settings_file=str(settings_path))
    if browser_mode == "ssh":
        code = wait_for_authorization_code(
            auth=auth,
            callback_port=callback_port,
            timeout=callback_timeout,
        )
    elif browser_mode == "docker":
        if vm_proxy_url is None:
            raise AuthorizationFlowError("Docker browser proxy is missing")
        code = docker_browser_authorization_code(
            auth=auth,
            callback_port=callback_port,
            login_settings=login_settings,
            vm_proxy_url=vm_proxy_url,
            path_to_vm=path_to_vm,
            manual_verification_timeout=manual_verification_timeout,
        )
    else:
        raise AuthorizationFlowError("Unknown authorization browser mode")
    try:
        auth.credentials = auth.flow.step2_exchange(code)
        auth.Authorize()
    except Exception as exc:
        raise AuthorizationFlowError("OAuth code exchange failed") from exc

    candidate = _candidate_path(credentials_path)
    try:
        auth.SaveCredentialsFile(str(candidate))
        validate_candidate_credentials(
            settings_path=settings_path,
            candidate_path=candidate,
            login_settings=login_settings,
        )
        return candidate
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--login-settings", type=Path, default=DEFAULT_LOGIN_SETTINGS)
    parser.add_argument("--clash-binary", type=Path, default=DEFAULT_CLASH_BINARY)
    parser.add_argument("--clash-config", type=Path, default=DEFAULT_CLASH_CONFIG)
    parser.add_argument("--bind-host")
    parser.add_argument("--proxy-port", type=int, default=27891)
    parser.add_argument("--controller-port", type=int, default=29091)
    return parser


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "verify",
        parents=[_common_parser()],
        help="Verify a private copy of the currently installed credential",
    )
    authorize = subparsers.add_parser(
        "authorize",
        parents=[_common_parser()],
        help="Authorize, validate, and atomically install a new credential",
    )
    authorize.add_argument("--callback-port", type=int, default=18080)
    authorize.add_argument("--callback-timeout", type=float, default=900)
    authorize.add_argument(
        "--browser",
        choices=["ssh", "docker"],
        default="ssh",
        help="Use a personal browser over SSH, or an isolated OSWorld Chrome",
    )
    authorize.add_argument("--path-to-vm", type=Path, default=DEFAULT_VM_PATH)
    authorize.add_argument(
        "--manual-verification-timeout",
        type=float,
        default=0,
        help=(
            "When using the Docker browser, keep its VNC desktop open for this "
            "many seconds if Google requests an additional verification step"
        ),
    )
    authorize.add_argument(
        "--install",
        action="store_true",
        help="Required acknowledgement before replacing credentials.json",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    settings_path = args.settings.expanduser().resolve()
    credentials_path = args.credentials.expanduser().absolute()
    login_settings = args.login_settings.expanduser().resolve()

    if not settings_path.is_file():
        print("ERROR: Google Drive settings file is missing")
        return 1
    if not login_settings.is_file():
        print("ERROR: Google browser-login settings file is missing")
        return 1
    if args.command == "authorize" and not args.install:
        print("ERROR: authorize requires --install acknowledgement")
        return 2
    if (
        args.command == "authorize"
        and (
            not math.isfinite(args.manual_verification_timeout)
            or args.manual_verification_timeout < 0
        )
    ):
        print("ERROR: --manual-verification-timeout must be a finite non-negative number")
        return 2
    if (
        args.command == "authorize"
        and args.manual_verification_timeout > 0
        and args.browser != "docker"
    ):
        print("ERROR: manual verification is supported only by the Docker browser")
        return 2

    original_cwd = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        with ManagedClashProxy(
            binary=args.clash_binary,
            config=args.clash_config,
            bind_host=args.bind_host,
            proxy_port=args.proxy_port,
            controller_port=args.controller_port,
        ) as proxy:
            with proxy_environment(proxy.host_proxy_url, proxy.bind_host):
                if args.command == "verify":
                    verify_installed_credentials(
                        settings_path=settings_path,
                        credentials_path=credentials_path,
                        login_settings=login_settings,
                    )
                    print("VALID: OAuth refresh and minimal Drive API check succeeded")
                    return 0

                candidate = authorize_candidate(
                    settings_path=settings_path,
                    credentials_path=credentials_path,
                    login_settings=login_settings,
                    callback_port=args.callback_port,
                    callback_timeout=args.callback_timeout,
                    browser_mode=args.browser,
                    vm_proxy_url=proxy.vm_proxy_url,
                    path_to_vm=args.path_to_vm.expanduser().resolve(),
                    manual_verification_timeout=args.manual_verification_timeout,
                )
                try:
                    atomic_install_candidate(candidate, credentials_path)
                finally:
                    candidate.unlink(missing_ok=True)
                print("INSTALLED: validated OAuth credential replaced credentials.json atomically")
                return 0
    except (CredentialValidationError, AuthorizationFlowError, ProxyStartupError) as exc:
        # These messages are deliberately generic and cannot contain OAuth data.
        print(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        print("CANCELLED: no candidate credential was installed")
        return 130
    except Exception as exc:
        print(
            "ERROR: unexpected failure "
            f"({type(exc).__name__}); no OAuth data was logged"
        )
        return 1
    finally:
        os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
