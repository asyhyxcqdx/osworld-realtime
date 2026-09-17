import json


from desktop_env.evaluators.getters import realtime_gui


class _FakeEnv:
    vm_ip = "127.0.0.1"
    chromium_port = 9222


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return [
            {
                "type": "page",
                "url": "https://www.bilibili.com/toy/example",
                "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/outer",
            },
            {
                "type": "iframe",
                "url": "https://www.bilibilitoy.com/phoebe/index.html",
                "webSocketDebuggerUrl": "ws://localhost:1337/devtools/page/game",
            },
        ]


def test_get_phoebe_checkpoint_uses_matching_game_target(monkeypatch):
    monkeypatch.setattr(realtime_gui.requests, "get", lambda *args, **kwargs: _FakeResponse())
    seen = {}

    def fake_evaluate(websocket_url, expression, timeout):
        seen["websocket_url"] = websocket_url
        seen["expression"] = expression
        seen["timeout"] = timeout
        return {"checkpoint": "CheckPoint1"}

    monkeypatch.setattr(realtime_gui, "_evaluate_cdp_expression", fake_evaluate)

    result = realtime_gui.get_phoebe_checkpoint(_FakeEnv(), {"attempts": 1})

    assert result == "CheckPoint1"
    assert seen["websocket_url"] == "ws://127.0.0.1:9222/devtools/page/game"
    assert "FILE_DATA" in seen["expression"]


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
