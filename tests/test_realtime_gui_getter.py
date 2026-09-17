import json


from desktop_env.evaluators.getters import realtime_gui


class _FakeEnv:
    vm_ip = "127.0.0.1"
    chromium_port = 9222


class _TargetsResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return [
            {
                "type": "page",
                "id": "page-1",
                "url": "http://127.0.0.1:8765/c1/index.html",
                "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/c1",
            }
        ]


def test_bench_state_getter_uses_the_bench_expression(monkeypatch):
    """Scoring breaks if the module-level BENCH expression is missing.

    The getter swallows exceptions and retries, so a deleted constant would only
    show up as "no result.txt" for every task in a real run.
    """
    seen = {}

    def fake_evaluate(websocket_url, expression, timeout):
        seen["websocket_url"] = websocket_url
        seen["expression"] = expression
        return {
            "protocol_version": "realtime-gui-bench/1.1",
            "task": "double_jump",
            "max_attempts": 3,
            "attempts_completed": 2,
            "passed": True,
            "status": "passed",
            "pass_at_1": 0,
            "pass_at_3": 1,
        }

    monkeypatch.setattr(realtime_gui.requests, "get", lambda *args, **kwargs: _TargetsResponse())
    monkeypatch.setattr(realtime_gui, "_evaluate_cdp_expression", fake_evaluate)

    details = realtime_gui.get_realtime_gui_bench_state(
        _FakeEnv(), {"benchmark_id": "C1", "poll_attempts": 1}
    )

    assert "window.BENCH" in seen["expression"]
    assert seen["websocket_url"] == "ws://127.0.0.1:9222/devtools/page/c1"
    assert details["benchmark_id"] == "C1"
    assert details["pass_at_3"] == 1 and details["passed"] is True
    assert details["raw_bench"]["status"] == "passed"


def test_evaluate_cdp_expression_ignores_events(monkeypatch):
    messages = iter(
        [
            json.dumps({"method": "Runtime.consoleAPICalled"}),
            json.dumps(
                {
                    "id": 1,
                    "result": {
                        "result": {
                            "type": "object",
                            "value": {"checkpoint": "CheckPoint1"},
                        }
                    },
                }
            ),
        ]
    )

    class FakeWebSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def send(self, message):
            self.sent = json.loads(message)

        def recv(self, timeout):
            return next(messages)

    websocket = FakeWebSocket()
    monkeypatch.setattr(realtime_gui, "connect", lambda *args, **kwargs: websocket)

    result = realtime_gui._evaluate_cdp_expression(
        "ws://127.0.0.1:9222/devtools/page/game", "1 + 1", 5
    )

    assert result == {"checkpoint": "CheckPoint1"}
    assert websocket.sent["method"] == "Runtime.evaluate"
    assert websocket.sent["params"]["awaitPromise"] is True
