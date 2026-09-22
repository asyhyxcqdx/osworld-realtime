import json

import pytest

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


def _script_identity_probe(monkeypatch, outcomes):
    """把单次探针替换成按剧本出牌：元素是异常就抛，否则作为结果返回。"""
    calls = []
    pending = list(outcomes)

    def fake_read(env, config):
        calls.append(len(calls) + 1)
        outcome = pending.pop(0) if len(pending) > 1 else pending[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    sleeps = []
    monkeypatch.setattr(realtime_gui, "_read_page_identity", fake_read)
    monkeypatch.setattr(realtime_gui.time, "sleep", lambda seconds: sleeps.append(seconds))
    return calls, sleeps


_IDENTITY = {"target_id": "page-1", "page_ids": ["page-1"], "url": "http://127.0.0.1:8765/", "time_origin": 1.5}


def test_page_identity_retries_a_slow_vm_until_it_answers(monkeypatch):
    calls, sleeps = _script_identity_probe(
        monkeypatch, [TimeoutError("timed out in 10.0s"), _IDENTITY]
    )

    assert realtime_gui.realtime_page_identity(_FakeEnv(), {}) == _IDENTITY
    assert calls == [1, 2]
    assert sleeps == [2.0]


def test_page_identity_gives_up_after_the_retry_budget(monkeypatch):
    calls, sleeps = _script_identity_probe(monkeypatch, [OSError("connection refused")])

    with pytest.raises(TimeoutError, match="failed after 4 attempts"):
        realtime_gui.realtime_page_identity(_FakeEnv(), {})

    assert calls == [1, 2, 3, 4]
    assert sleeps == [2.0, 5.0, 10.0]


def test_page_identity_does_not_retry_a_missing_or_duplicated_page(monkeypatch):
    calls, sleeps = _script_identity_probe(
        monkeypatch,
        [RuntimeError("Realtime task page is missing or duplicated; run is invalid"), _IDENTITY],
    )

    with pytest.raises(RuntimeError, match="missing or duplicated"):
        realtime_gui.realtime_page_identity(_FakeEnv(), {})

    assert calls == [1]
    assert sleeps == []
