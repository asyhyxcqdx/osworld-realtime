import copy
import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from websockets.sync.client import connect


logger = logging.getLogger("desktopenv.getter.realtime_gui")


_REALTIME_GUI_BENCH_EXPRESSION = r"""
(() => {
  if (!window.BENCH || typeof window.BENCH !== 'object') return null;
  return {
    ...window.BENCH,
    protocol_version: window.BENCH.protocol_version,
    task: window.BENCH.task,
    max_attempts: window.BENCH.max_attempts,
    attempts_completed: window.BENCH.attempts_completed,
    passed: window.BENCH.passed,
    status: window.BENCH.status,
    pass_at_1: window.BENCH.pass_at_1,
    pass_at_3: window.BENCH.pass_at_3
  };
})()
"""


def _rewrite_websocket_url(env, websocket_url: str) -> str:
    parsed = urlsplit(websocket_url)
    return urlunsplit(
        (
            parsed.scheme,
            f"{env.vm_ip}:{env.chromium_port}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _evaluate_cdp_expression(websocket_url: str, expression: str, timeout: float) -> Any:
    request_id = 1
    deadline = time.monotonic() + timeout
    with connect(websocket_url, open_timeout=timeout, close_timeout=1) as websocket:
        websocket.send(
            json.dumps(
                {
                    "id": request_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }
            )
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for Chrome Runtime.evaluate")
            message = json.loads(websocket.recv(timeout=remaining))
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Chrome DevTools error: {message['error']}")
            evaluation = message.get("result", {})
            if evaluation.get("exceptionDetails"):
                raise RuntimeError(
                    f"Chrome JavaScript error: {evaluation['exceptionDetails']}"
                )
            return evaluation.get("result", {}).get("value")


def _normalize_realtime_gui_bench_state(
    state: Any, benchmark_id: str
) -> Dict[str, Any]:
    """Validate and normalize the protocol-defined game state.

    The game owns ``pass_at_1`` and ``pass_at_3``. They are never inferred from
    attempt counters or diagnostic event logs.
    """
    if not isinstance(state, dict):
        raise ValueError("window.BENCH is missing or is not an object")

    required = {
        "protocol_version",
        "task",
        "max_attempts",
        "attempts_completed",
        "passed",
        "status",
        "pass_at_1",
        "pass_at_3",
    }
    missing = sorted(required.difference(state))
    if missing:
        raise ValueError(f"window.BENCH is missing fields: {', '.join(missing)}")

    protocol_version = state["protocol_version"]
    task = state["task"]
    max_attempts = state["max_attempts"]
    attempts_completed = state["attempts_completed"]
    passed = state["passed"]
    status = state["status"]
    pass_at_1 = state["pass_at_1"]
    pass_at_3 = state["pass_at_3"]

    if protocol_version != "realtime-gui-bench/1.1":
        raise ValueError(
            "window.BENCH.protocol_version must be realtime-gui-bench/1.1"
        )
    if not isinstance(task, str) or not task.strip():
        raise ValueError("window.BENCH.task must be a non-empty string")
    if type(max_attempts) is not int or max_attempts != 3:
        raise ValueError("window.BENCH.max_attempts must equal 3")
    if type(attempts_completed) is not int or not 0 <= attempts_completed <= max_attempts:
        raise ValueError(
            "window.BENCH.attempts_completed must be an integer from 0 to max_attempts"
        )
    if type(passed) is not bool:
        raise ValueError("window.BENCH.passed must be boolean")
    if type(pass_at_1) not in {int, float} or isinstance(pass_at_1, bool) or pass_at_1 not in {0, 1}:
        raise ValueError("window.BENCH.pass_at_1 must be numeric 0 or 1")
    if type(pass_at_3) not in {int, float} or isinstance(pass_at_3, bool) or pass_at_3 not in {0, 1}:
        raise ValueError("window.BENCH.pass_at_3 must be numeric 0 or 1")
    if not isinstance(status, str) or status not in {"ready", "running", "passed", "failed"}:
        raise ValueError("window.BENCH.status must be ready, running, passed, or failed")
    if passed != (pass_at_3 == 1):
        raise ValueError("window.BENCH.passed must equal pass_at_3 == 1")
    if pass_at_1 == 1 and attempts_completed != 1:
        raise ValueError("pass_at_1=1 requires first-attempt completion")
    if status == "ready":
        if attempts_completed != 0 or passed or pass_at_1 != 0 or pass_at_3 != 0:
            raise ValueError("ready BENCH state has inconsistent completion fields")
    elif status == "running":
        if attempts_completed >= max_attempts or passed or pass_at_1 != 0 or pass_at_3 != 0:
            raise ValueError("running BENCH state has inconsistent completion fields")
    elif status == "passed":
        if not passed or pass_at_3 != 1 or attempts_completed < 1:
            raise ValueError("passed BENCH state has inconsistent completion fields")
    elif status == "failed":
        if passed or pass_at_1 != 0 or pass_at_3 != 0 or attempts_completed != max_attempts:
            raise ValueError("failed BENCH state has inconsistent completion fields")

    return {
        "benchmark_id": benchmark_id,
        "protocol_version": protocol_version,
        "task": task,
        "max_attempts": max_attempts,
        "attempts_completed": attempts_completed,
        "passed": passed,
        "result": float(pass_at_3),
        "pass_at_1": float(pass_at_1),
        "pass_at_3": float(pass_at_3),
        "status": status,
        "raw_bench": copy.deepcopy(state),
    }


def get_realtime_gui_bench_state(env, config: Dict[str, Any]) -> Dict[str, Any]:
    """Read and validate the active RealtimeGUI-Bench page state over CDP."""

    benchmark_id = config.get("benchmark_id")
    if not isinstance(benchmark_id, str) or not benchmark_id:
        raise ValueError("realtime_gui_bench_state requires benchmark_id")

    target_url_contains = config.get(
        "target_url_contains", "127.0.0.1:8765/"
    )
    attempts = int(config.get("poll_attempts", 3))
    retry_interval = float(config.get("retry_interval", 0.5))
    timeout = float(config.get("timeout", 10.0))
    if attempts < 1 or retry_interval < 0 or timeout <= 0:
        raise ValueError("invalid realtime_gui_bench_state retry configuration")

    targets_url = f"http://{env.vm_ip}:{env.chromium_port}/json/list"
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(targets_url, timeout=timeout)
            response.raise_for_status()
            targets = response.json()
            matching_targets = [
                target
                for target in targets
                if target.get("type") == "page"
                and target_url_contains in target.get("url", "")
                and target.get("webSocketDebuggerUrl")
            ]
            if not matching_targets:
                raise RuntimeError(
                    f"no Chrome page contains URL fragment {target_url_contains!r}"
                )

            target = matching_targets[-1]
            websocket_url = _rewrite_websocket_url(
                env, target["webSocketDebuggerUrl"]
            )
            raw_state = _evaluate_cdp_expression(
                websocket_url, _REALTIME_GUI_BENCH_EXPRESSION, timeout
            )
            details = _normalize_realtime_gui_bench_state(
                raw_state, benchmark_id.upper()
            )
            env._evaluation_details = details
            return details
        except Exception as error:
            last_error = error
            logger.warning(
                "Failed to read RealtimeGUI-Bench state for %s (attempt %d/%d): %s",
                benchmark_id,
                attempt,
                attempts,
                error,
            )
            if attempt < attempts:
                time.sleep(retry_interval)

    raise RuntimeError(
        f"Unable to read valid RealtimeGUI-Bench state for {benchmark_id}"
    ) from last_error


def realtime_page_identity(env, config):
    """Read document identity without exposing DOM or BENCH to the model."""
    response = requests.get(f"http://{env.vm_ip}:{env.chromium_port}/json/list", timeout=10)
    response.raise_for_status()
    pages = [p for p in response.json() if p.get("type") == "page"]
    fragment = config.get("target_url_contains", "127.0.0.1:8765/")
    matches = [p for p in pages if fragment in p.get("url", "") and p.get("webSocketDebuggerUrl")]
    if len(matches) != 1:
        raise RuntimeError("Realtime task page is missing or duplicated; run is invalid")
    target = matches[0]
    document = _evaluate_cdp_expression(
        _rewrite_websocket_url(env, target["webSocketDebuggerUrl"]),
        "({url: location.href.split('#')[0], time_origin: performance.timeOrigin})", 10,
    )
    if not isinstance(document, dict) or not isinstance(document.get("time_origin"), (int, float)):
        raise RuntimeError("Cannot verify realtime document identity")
    return {"target_id": target["id"], "page_ids": sorted(p["id"] for p in pages), **document}


def verify_realtime_page_identity(env, config, expected):
    current = realtime_page_identity(env, config)
    if current != expected:
        raise RuntimeError("Realtime page was reloaded, navigated, or replaced; run is invalid")
    return current
