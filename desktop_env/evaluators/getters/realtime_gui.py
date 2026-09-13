import copy
import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from websockets.sync.client import connect


logger = logging.getLogger("desktopenv.getter.realtime_gui")


_PHOEBE_SAVE_EXPRESSION = r"""
(async () => {
  const databaseNames = new Set(['/userfs']);
  if (typeof indexedDB.databases === 'function') {
    for (const info of await indexedDB.databases()) {
      if (info && info.name) databaseNames.add(info.name);
    }
  }

  const openDatabase = (name) => new Promise((resolve, reject) => {
    const request = indexedDB.open(name);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error(`Cannot open ${name}`));
  });

  const readEntries = (database) => new Promise((resolve, reject) => {
    const transaction = database.transaction('FILE_DATA', 'readonly');
    const request = transaction.objectStore('FILE_DATA').openCursor();
    const entries = [];
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve(entries);
        return;
      }
      entries.push({key: String(cursor.key), value: cursor.value});
      cursor.continue();
    };
    request.onerror = () => reject(request.error || new Error('Cannot read FILE_DATA'));
  });

  for (const databaseName of databaseNames) {
    let database = null;
    try {
      database = await openDatabase(databaseName);
      if (!database.objectStoreNames.contains('FILE_DATA')) continue;
      const entries = await readEntries(database);
      for (const entry of entries) {
        if (!/\/savegame_web_[^/]+\.json$/.test(entry.key)) continue;
        const rawContents = entry.value && entry.value.contents;
        if (!rawContents) continue;
        const bytes = rawContents instanceof Uint8Array
          ? rawContents
          : new Uint8Array(rawContents);
        const saveData = JSON.parse(new TextDecoder('utf-8').decode(bytes));
        if (saveData && typeof saveData.checkpoint === 'string') {
          return {
            checkpoint: saveData.checkpoint,
            saveKey: entry.key,
            database: databaseName
          };
        }
      }
    } catch (error) {
      // Try the next database. The final Python-side log reports a miss.
    } finally {
      if (database) database.close();
    }
  }
  return {checkpoint: null};
})()
"""

_REALTIME_GUI_BENCH_EXPRESSION = r"""
(() => {
  if (!window.BENCH || typeof window.BENCH !== 'object') return null;
  return {
    task: window.BENCH.task,
    attempts: window.BENCH.attempts,
    maxAttempts: window.BENCH.maxAttempts,
    passed: window.BENCH.passed,
    results: window.BENCH.results,
    status: window.BENCH.status
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


def get_phoebe_checkpoint(env, config: Dict[str, Any]) -> Optional[str]:
    """Read the current I Wanna Be Phoebe checkpoint from Godot's IDBFS save."""

    target_url_contains = config.get("target_url_contains", "bilibilitoy.com")
    attempts = int(config.get("attempts", 5))
    retry_interval = float(config.get("retry_interval", 1.0))
    timeout = float(config.get("timeout", 15.0))
    targets_url = f"http://{env.vm_ip}:{env.chromium_port}/json/list"

    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(targets_url, timeout=timeout)
            response.raise_for_status()
            targets = response.json()
            matching_targets = [
                target
                for target in targets
                if target_url_contains in target.get("url", "")
                and target.get("webSocketDebuggerUrl")
            ]
            for target in matching_targets:
                websocket_url = _rewrite_websocket_url(
                    env, target["webSocketDebuggerUrl"]
                )
                result = _evaluate_cdp_expression(
                    websocket_url, _PHOEBE_SAVE_EXPRESSION, timeout
                )
                checkpoint = result.get("checkpoint") if isinstance(result, dict) else None
                if isinstance(checkpoint, str) and checkpoint:
                    return checkpoint
            logger.warning(
                "Phoebe checkpoint not found (attempt %d/%d; matching targets: %d)",
                attempt,
                attempts,
                len(matching_targets),
            )
        except Exception as error:
            logger.warning(
                "Failed to read Phoebe checkpoint (attempt %d/%d): %s",
                attempt,
                attempts,
                error,
            )
        if attempt < attempts:
            time.sleep(retry_interval)

    return None


def _normalize_realtime_gui_bench_state(
    state: Any, benchmark_id: str
) -> Dict[str, Any]:
    """Score whole-game completion, not individual events in BENCH.results.

    In the 2026-09-08 RealtimeGame package, attempts counts *failed* attempts:
    each failure increments it, while winning leaves it unchanged. Therefore
    passed with attempts == 0 means first-attempt success. The compact history
    below is derived from these fields, not copied from the raw event log (A41
    logs individual orders, and A38 does not append any results at all).
    """
    if not isinstance(state, dict):
        raise ValueError("window.BENCH is missing or is not an object")

    required = {"task", "attempts", "maxAttempts", "passed", "status"}
    missing = sorted(required.difference(state))
    if missing:
        raise ValueError(f"window.BENCH is missing fields: {', '.join(missing)}")

    task = state["task"]
    attempts = state["attempts"]
    max_attempts = state["maxAttempts"]
    passed = state["passed"]
    status = state["status"]

    if not isinstance(task, str) or not task.strip():
        raise ValueError("window.BENCH.task must be a non-empty string")
    if type(attempts) is not int or not 0 <= attempts <= 3:
        raise ValueError("window.BENCH.attempts must be an integer from 0 to 3")
    if type(max_attempts) is not int or max_attempts != 3:
        raise ValueError("window.BENCH.maxAttempts must equal 3")
    if type(passed) is not bool:
        raise ValueError("window.BENCH.passed must be boolean")
    if not isinstance(status, str) or status not in {"running", "passed", "failed"}:
        raise ValueError("window.BENCH.status must be running, passed, or failed")

    if passed and attempts >= max_attempts:
        raise ValueError("a win must occur before all three failed attempts are consumed")
    expected_status = "passed" if passed else "failed" if attempts == max_attempts else "running"
    if status != expected_status:
        raise ValueError("window.BENCH.status disagrees with passed/attempts")

    pass_at_1 = float(passed and attempts == 0)
    pass_at_3 = float(passed)
    attempt_results = [False] * attempts + ([True] if passed else [])

    return {
        "benchmark_id": benchmark_id,
        "result": pass_at_3,
        "pass_at_1": pass_at_1,
        "pass_at_3": pass_at_3,
        "attempt_results": attempt_results,
        "status": status,
        # Preserve the actual page fields separately, including raw results.
        # They may contain zero, many, or differently encoded sub-action events.
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
    attempts = int(config.get("attempts", 3))
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
