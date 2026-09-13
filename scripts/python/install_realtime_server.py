"""Install two extension files, patch the VM entry point, then restart its service.

The existing VM main.py is preserved with a backup. No model credentials are
uploaded. Run before recording or controlling the VM.
"""
import argparse
import json
import os
import time
from pathlib import Path

import requests


def install(base_url, server_dir="/home/user/server", password=""):
    base_url = base_url.rstrip("/")

    def execute(code):
        response = requests.post(
            base_url + "/execute", json={"command": ["python3", "-c", code]}, timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if data.get("returncode") != 0:
            raise RuntimeError(
                "VM extension installation failed: " + data.get("error", "")[:500]
            )
        return data

    source = Path(__file__).resolve().parents[2] / "desktop_env" / "server"
    for name in ("fmp4.py", "realtime.py"):
        with (source / name).open("rb") as file:
            response = requests.post(
                base_url + "/setup/upload",
                data={"file_path": f"{server_dir}/{name}"},
                files={"file_data": (name, file)},
                timeout=30,
            )
            response.raise_for_status()
    entry = str(Path(server_dir) / "main.py")
    execute(
        "import pathlib, shutil, py_compile\n"
        f"p=pathlib.Path({entry!r})\n"
        "text=p.read_text()\n"
        "if 'register_realtime(app,' not in text:\n"
        " backup=p.with_name('main.py.before-realtime')\n"
        " if not backup.exists(): shutil.copy2(p, backup)\n"
        " marker=\"if __name__ == '__main__':\"\n"
        " if marker not in text: raise RuntimeError('Unrecognized VM server entry point')\n"
        " insertion='from realtime import register_realtime\\nregister_realtime(app, capture_screen_with_cursor, pyautogui)\\n\\n'\n"
        " p.write_text(text.replace(marker,insertion+marker,1))\n"
        "py_compile.compile(str(p),doraise=True)\n"
    )
    # The detached child restarts only after the current HTTP response can finish.
    child = (
        "import subprocess,time; time.sleep(1); "
        f"p=subprocess.run(['sudo','-S','systemctl','restart','osworld_server.service'],input={password + chr(10)!r},text=True); "
        "raise SystemExit(p.returncode)"
    )
    execute(
        "import subprocess\n"
        + f"subprocess.Popen(['python3','-c',{child!r}],start_new_session=True, "
        "stdin=subprocess.DEVNULL,stdout=open('/tmp/osworld-realtime-install.log','w'),stderr=subprocess.STDOUT)"
    )
    deadline = time.monotonic() + 45
    time.sleep(2)
    while time.monotonic() < deadline:
        try:
            response = requests.get(base_url + "/realtime/capabilities", timeout=3)
            if response.ok and response.json().get("version") == 1:
                return response.json()
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
