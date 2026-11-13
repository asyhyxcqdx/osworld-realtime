"""Install realtime/observer extensions, patch the VM entry point, restart service.

The existing VM main.py is preserved with a backup. No model credentials are
uploaded. Run before recording or controlling the VM.
"""
import argparse
import json
import logging
import os
import time
from pathlib import Path

import requests

SERVER_FILES = ("fmp4.py", "screen_capture.py", "realtime.py", "observers.py", "observer_worker.py", "pyxcursor.py")
LEGACY_CURSOR_CAPTURE = '''        cursor_obj = Xcursor()
        imgarray = cursor_obj.getCursorImageArrayFast()
        cursor_img = Image.fromarray(imgarray)
        screenshot = pyautogui.screenshot()
        cursor_x, cursor_y = pyautogui.position()
        screenshot.paste(cursor_img, (cursor_x, cursor_y), cursor_img)
        screenshot.save(file_path)
'''
MANAGED_CURSOR_CAPTURE = (
    "        with Xcursor() as cursor_obj:\n"
    + ''.join('    ' + line for line in LEGACY_CURSOR_CAPTURE.splitlines(keepends=True)[1:])
)


def _retry_setup_request(operation):
    # Uploading Python sources can make Flask's development reloader close an
    # in-flight connection. Only replay idempotent installation requests.
    for attempt, delay in enumerate((0, 1, 2, 4, 8)):
        if delay:
            time.sleep(delay)
        try:
            return operation()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 4:
                raise
            logging.getLogger(__name__).warning('VM installation connection interrupted; retrying setup request')


def install(base_url, server_dir="/home/user/server", password=""):
    base_url = base_url.rstrip("/")

    def execute(code, *, retry=True):
        def send():
            return requests.post(base_url + "/execute", json={"command": ["python3", "-c", code]}, timeout=30)
        response = _retry_setup_request(send) if retry else send()
        response.raise_for_status()
        data = response.json()
        if data.get("returncode") != 0:
            raise RuntimeError(
                "VM extension installation failed: " + data.get("error", "")[:500]
            )
        return data

    source = Path(__file__).resolve().parents[2] / "desktop_env" / "server"
    for name in SERVER_FILES:
        content = (source / name).read_bytes()
        response = _retry_setup_request(lambda: requests.post(
                base_url + "/setup/upload",
                data={"file_path": f"{server_dir}/{name}"},
                files={"file_data": (name, content)},
                timeout=30,
            ))
        response.raise_for_status()
    entry = str(Path(server_dir) / "main.py")
    execute(
        "import pathlib, shutil, py_compile\n"
        f"p=pathlib.Path({entry!r})\n"
        "original=text=p.read_text()\n"
        "if 'register_realtime(app,' not in text:\n"
        " marker=\"if __name__ == '__main__':\"\n"
        " if marker not in text: raise RuntimeError('Unrecognized VM server entry point')\n"
        " insertion='from realtime import register_realtime\\nregister_realtime(app, capture_screen_with_cursor, pyautogui)\\n\\n'\n"
        " text=text.replace(marker,insertion+marker,1)\n"
        f"text=text.replace({LEGACY_CURSOR_CAPTURE!r}, {MANAGED_CURSOR_CAPTURE!r}, 1)\n"
        "if original != text:\n"
        " backup=p.with_name('main.py.before-realtime')\n"
        " if not backup.exists(): shutil.copy2(p, backup)\n"
        " p.write_text(text)\n"
        "py_compile.compile(str(p),doraise=True)\n"
        f"for name in {SERVER_FILES!r}:\n"
        " py_compile.compile(str(p.with_name(name)),doraise=True)\n"
    )
    # The detached child restarts only after the current HTTP response can finish.
    child = (
        "import subprocess,time; time.sleep(1); "
        f"p=subprocess.run(['sudo','-S','systemctl','restart','osworld_server.service'],input={password + chr(10)!r},text=True); "
        "raise SystemExit(p.returncode)"
    )
    try:
        execute(
            "import subprocess\n"
            + f"subprocess.Popen(['python3','-c',{child!r}],start_new_session=True, "
            "stdin=subprocess.DEVNULL,stdout=open('/tmp/osworld-realtime-install.log','w'),stderr=subprocess.STDOUT)",
            retry=False,
        )
    except (requests.ConnectionError, requests.Timeout):
        # Restart may already have been scheduled. Check readiness instead of
        # submitting another restart that could interrupt subsequent recording.
        logging.getLogger(__name__).warning('VM restart connection closed; waiting for service readiness')
    deadline = time.monotonic() + 45
    time.sleep(2)
    while time.monotonic() < deadline:
        try:
            response = requests.get(base_url + "/realtime/capabilities", timeout=3)
            if response.ok:
                capabilities = response.json()
                if (capabilities.get('version') == 1 and capabilities.get('observers') == 1
                        and capabilities.get('observer_capture') == 'per_request_memory'):
                    return capabilities
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError(
        "VM extension did not become ready; inspect /tmp/osworld-realtime-install.log in the VM"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server_url", required=True)
    parser.add_argument("--server_dir", default="/home/user/server")
    args = parser.parse_args()
    print(
        json.dumps(
            install(
                args.server_url, args.server_dir, os.getenv("OSWORLD_VM_PASSWORD", "")
            )
        )
    )
