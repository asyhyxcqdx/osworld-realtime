"""Check the deployed realtime extension before a model experiment starts."""
import hashlib
import json
from pathlib import Path

import requests


def verify_server_source(base_url):
    names = ("realtime.py", "fmp4.py")
    source = Path(__file__).resolve().parent / "server"
    expected = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names}
    # Read only extension source hashes. No game state or credentials are read.
    code = (
        "import hashlib,json,pathlib; "
        "root=pathlib.Path('/home/user/server'); "
        f"print(json.dumps({{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in {names!r}}}))"
    )
    response = requests.post(
        base_url.rstrip("/") + "/execute",
        json={"command": ["python3", "-c", code]}, timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("returncode") != 0:
        raise RuntimeError("Cannot read VM realtime extension; install the current realtime server")
    observed = json.loads(result["output"])
    if observed != expected:
        raise RuntimeError(
            "VM realtime source does not match this checkout. Use the final VM image "
            "or --install_realtime_server before running model experiments. "
            f"Expected {expected}; observed {observed}"
        )
    return observed
