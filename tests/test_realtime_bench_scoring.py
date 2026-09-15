import pytest

from desktop_env.evaluators.getters.realtime_gui import _normalize_realtime_gui_bench_state
from desktop_env.evaluators.metrics.realtime_gui import realtime_gui_bench_result
from scripts.python.check_realtime_gui_contract import check_bench


def state(status, attempts, p1, p3):
    return dict(protocol_version='realtime-gui-bench/1.1', task='double_jump', max_attempts=3,
                attempts_completed=attempts, passed=p3 == 1, status=status, pass_at_1=p1, pass_at_3=p3,
                results=['diagnostic only'])


@pytest.mark.parametrize('status,attempts,p1,p3', [
    ('ready', 0, 0, 0), ('running', 0, 0, 0), ('running', 2, 0, 0),
    ('passed', 1, 1, 1), ('passed', 2, 0, 1), ('passed', 3, 0, 1), ('failed', 3, 0, 0),
])
def test_checker_getter_metric_agree_without_using_diagnostic_results(status, attempts, p1, p3):
    raw = state(status, attempts, p1, p3)
    check = check_bench(raw, 'C1')
    assert check['errors'] == []
    assert check['scored'] == (status in {'passed', 'failed'})
    details = _normalize_realtime_gui_bench_state(raw, 'C1')
    assert details['raw_bench'] == raw
    assert details['raw_bench'] is not raw
    assert realtime_gui_bench_result(details) == p3
    assert details['pass_at_1'] == p1


@pytest.mark.parametrize('status,attempts,p1,p3', [
    ('passed', 2, 1, 1), ('passed', 0, 0, 1), ('failed', 2, 0, 0),
    ('ready', 1, 0, 0), ('running', 3, 0, 0), ('running', 1, 1, 1),
])
def test_inconsistent_game_state_is_not_a_normal_score(status, attempts, p1, p3):
    raw = state(status, attempts, p1, p3)
    assert check_bench(raw, 'C1')['errors']
    with pytest.raises(ValueError):
        _normalize_realtime_gui_bench_state(raw, 'C1')


def test_reloaded_document_is_invalid_even_with_same_url_and_bench(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from desktop_env.evaluators.getters import realtime_gui as module
    pages = [{'type': 'page', 'id': 'game', 'url': 'http://127.0.0.1:8765/c1/index.html#status=ready', 'webSocketDebuggerUrl': 'ws://vm:9222/devtools/page/game'}]
    monkeypatch.setattr(module.requests, 'get', Mock(return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: pages)))
    monkeypatch.setattr(module, '_rewrite_websocket_url', lambda env, url: url)
    read = Mock(side_effect=[
        {'url': 'http://127.0.0.1:8765/c1/index.html', 'time_origin': 100},
        {'url': 'http://127.0.0.1:8765/c1/index.html', 'time_origin': 100},
        {'url': 'http://127.0.0.1:8765/c1/index.html', 'time_origin': 200},
    ])
    monkeypatch.setattr(module, '_evaluate_cdp_expression', read)
    env = SimpleNamespace(vm_ip='vm', chromium_port=9222)
    config = {'target_url_contains': '/c1/index.html'}
    expected = module.realtime_page_identity(env, config)
    module.verify_realtime_page_identity(env, config, expected)
    with pytest.raises(RuntimeError, match='reloaded'):
        module.verify_realtime_page_identity(env, config, expected)
