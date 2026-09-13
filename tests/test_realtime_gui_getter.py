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


@pytest.mark.parametrize(
    ("state", "pass_at_1", "pass_at_3"),
    [
        (
            {
                "task": "example",
                "attempts": 0,
                "maxAttempts": 3,
                "passed": False,
                "results": [],
                "status": "running",
            },
            0.0,
            0.0,
        ),
        (
            {
                "task": "example",
                "attempts": 0,
                "maxAttempts": 3,
                "passed": True,
                "results": [True],
                "status": "passed",
            },
            1.0,
            1.0,
        ),
        (
            {
                "task": "example",
                "attempts": 1,
                "maxAttempts": 3,
                "passed": True,
                "results": [False, True],
                "status": "passed",
            },
            0.0,
            1.0,
        ),
        (
            {
                "task": "example",
                "attempts": 3,
                "maxAttempts": 3,
                "passed": False,
                "results": [False, False, False],
                "status": "failed",
            },
            0.0,
            0.0,
        ),
    ],
)
def test_normalize_realtime_gui_bench_state(state, pass_at_1, pass_at_3):
    result = realtime_gui._normalize_realtime_gui_bench_state(state, "A2")

    assert result["benchmark_id"] == "A2"
    assert result["pass_at_1"] == pass_at_1
    assert result["pass_at_3"] == pass_at_3
    assert result["result"] == pass_at_3
    assert result["attempt_results"] == state["results"]


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {
            "task": "example",
            "attempts": 0,
            "maxAttempts": 4,
            "passed": False,
            "results": [],
            "status": "running",
        },
        {
            "task": "example",
            "attempts": 0,
            "maxAttempts": 3,
            "passed": True,
            "results": [],
            "status": "running",
        },
        {
            "task": "example",
            "attempts": 3,
            "maxAttempts": 3,
            "passed": False,
            "results": [False, False, False],
            "status": "running",
        },
    ],
)
def test_normalize_realtime_gui_bench_state_rejects_invalid_state(state):
    with pytest.raises(ValueError):
        realtime_gui._normalize_realtime_gui_bench_state(state, "A2")


def test_get_realtime_gui_bench_state_reads_matching_page(monkeypatch):
    class BenchResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "type": "page",
                    "url": "http://127.0.0.1:8765/a2/index.html",
                    "webSocketDebuggerUrl": "ws://localhost:1337/devtools/page/a2",
                }
            ]

    state = {
        "task": "digit_flash",
        "attempts": 1,
        "maxAttempts": 3,
        "passed": True,
        "results": [False, True],
        "status": "passed",
    }
    monkeypatch.setattr(
        realtime_gui.requests, "get", lambda *args, **kwargs: BenchResponse()
    )
    monkeypatch.setattr(
        realtime_gui, "_evaluate_cdp_expression", lambda *args, **kwargs: state
    )
    env = _FakeEnv()

    result = realtime_gui.get_realtime_gui_bench_state(
        env,
        {
            "benchmark_id": "A2",
            "target_url_contains": "127.0.0.1:8765/a2/index.html",
            "attempts": 1,
        },
    )

    assert result["attempt_results"] == [False, True]
    assert result["pass_at_1"] == 0.0
    assert result["pass_at_3"] == 1.0
    assert env._evaluation_details == result


@pytest.mark.parametrize("raw", [[], ["hit"], ["miss", "hit"], ["miss", "miss", "hit"], ["miss"] * 3, [False, "hit"]])
def test_final_package_hit_miss_results(raw):
    normalized = [item is True or item == "hit" for item in raw]
    passed = any(normalized)
    state = {"task": "subitize_count", "attempts": normalized.count(False),
             "maxAttempts": 3, "passed": passed, "results": raw,
             "status": "passed" if passed else "failed" if len(raw) == 3 else "running"}
    result = realtime_gui._normalize_realtime_gui_bench_state(state, "A31")
    assert result["attempt_results"] == normalized
    assert result["pass_at_1"] == float(bool(normalized and normalized[0]))
    assert result["pass_at_3"] == float(passed)
    assert state["results"] == raw


@pytest.mark.parametrize("raw", [[], [True], ["hit"], ["miss", "hit"], ["hit"] * 9, ["unknown", {"event": "delivery"}], None])
@pytest.mark.parametrize("passed,failed", [(False, 0), (False, 1), (False, 2), (False, 3), (True, 0), (True, 1), (True, 2)])
def test_scores_depend_on_completed_game_not_raw_result_events(raw, passed, failed):
    state = {"task": "example", "attempts": failed, "maxAttempts": 3,
             "passed": passed, "results": raw,
             "status": "passed" if passed else "failed" if failed == 3 else "running"}
    result = realtime_gui._normalize_realtime_gui_bench_state(state, "A41")
    assert result["result"] == result["pass_at_3"] == float(passed)
    assert result["pass_at_1"] == float(passed and failed == 0)
    assert result["attempt_results"] == [False] * failed + ([True] if passed else [])
    assert result["raw_bench"] == state


def test_a38_missing_events_and_a41_partial_order_are_not_misgraded():
    base = {"task": "enemy_layout_recall", "attempts": 0, "maxAttempts": 3,
            "passed": True, "status": "passed", "results": []}
    a38 = realtime_gui._normalize_realtime_gui_bench_state(base, "A38")
    assert a38["pass_at_1"] == a38["pass_at_3"] == 1.0
    assert a38["attempt_results"] == [True]

    base.update(task="kitchen_order_match", passed=False, status="running", results=["hit", "hit"])
    partial = realtime_gui._normalize_realtime_gui_bench_state(base, "A41")
    assert partial["pass_at_1"] == partial["pass_at_3"] == 0.0
    assert partial["attempt_results"] == []
    base.update(passed=True, status="passed", results=["hit"] * 3)
    complete = realtime_gui._normalize_realtime_gui_bench_state(base, "A41")
    assert complete["attempt_results"] == [True]
    assert complete["raw_bench"]["results"] == ["hit"] * 3
    base["results"].append("changed_after_read")
    assert complete["raw_bench"]["results"] == ["hit"] * 3


@pytest.mark.parametrize("change", [
    {"passed": "true"}, {"passed": 1}, {"status": []}, {"status": "passed"},
    {"attempts": True}, {"attempts": -1}, {"attempts": 4}, {"attempts": 0.0},
    {"attempts": 3, "passed": True, "status": "passed"},
    {"attempts": 1, "status": "failed"}, {"maxAttempts": True},
])
def test_score_source_fields_still_require_valid_types_and_consistent_state(change):
    state = {"task": "example", "attempts": 0, "maxAttempts": 3,
             "passed": False, "status": "running"}
    state.update(change)
    with pytest.raises(ValueError):
        realtime_gui._normalize_realtime_gui_bench_state(state, "A38")


def test_results_field_is_optional_for_scoring_and_not_synthesized_as_raw_data():
    state = {"task": "example", "attempts": 1, "maxAttempts": 3,
             "passed": True, "status": "passed"}
    result = realtime_gui._normalize_realtime_gui_bench_state(state, "A38")
    assert result["pass_at_3"] == 1.0
    assert result["pass_at_1"] == 0.0
    assert "results" not in result["raw_bench"]
