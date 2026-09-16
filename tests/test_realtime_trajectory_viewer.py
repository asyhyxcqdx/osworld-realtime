import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from PIL import Image

from lib_realtime_trajectory import build_trajectory_data, render_trajectory


@pytest.fixture
def png():
    out = io.BytesIO()
    Image.new('RGB', (1920, 1080), '#315ee9').save(out, format='PNG')
    return out.getvalue()


def write_log(root, events):
    path = root / 'trajectory.jsonl'
    path.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events))
    return path


@pytest.fixture
def trajectory(tmp_path, png):
    (tmp_path / 'initial_state.png').write_bytes(png)
    (tmp_path / 'step_1.png').write_bytes(png)
    (tmp_path / 'query_1_0.png').write_bytes(png)
    encoded = base64.b64encode(png).decode()
    task = {'role': 'user', 'content': [{'type': 'text', 'text': 'Task: duplicated on purpose'}]}
    image = {'role': 'user', 'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data_sha256': hashlib.sha256(encoded.encode()).hexdigest(), 'data_length': len(encoded)}}]}
    call = {'id': 'c1', 'name': 'computer_click', 'arguments': {'x': 995, 'y': 724}}
    obs = {'screenshot_file': 'initial_state.png', 'task_time_s': 0.5}
    events = [
        {'event': 'initial_observation', 'decision_id': 0, 'observation': obs},
        {'event': 'model_request', 'decision_id': 1, 'request_id': 1, 'observation': obs, 'request_messages': [task, task, image]},
        {'event': 'model_response', 'decision_id': 1, 'request_id': 1, 'calls': [call], 'reasoning': ['Observe the button.'], 'reasoning_status': 'returned', 'latency_s': 3, 'text': 'Click Start'},
        {'event': 'action_submitted', 'decision_id': 1, 'actions': [{'action_type': 'CLICK', 'parameters': call['arguments']}]},
        {'event': 'action_executed', 'decision_id': 1, 'actions': [{'action_type': 'CLICK', 'parameters': call['arguments']}], 'observation': {'screenshot_file': 'step_1.png', 'task_time_s': 4}, 'info': {'sequence_actions': [{'action': {'action_type': 'WAIT', 'parameters': {'duration_s': 0.25}}, 'started_s': 3, 'finished_s': 3.251, 'duration_s': 0.251}]}},
        {'event': 'action_tool_result', 'decision_id': 2, 'calls': [[call, {'executed': True}]]},
        {'event': 'model_request', 'decision_id': 2, 'request_id': 2, 'observation': {'screenshot_file': 'step_1.png', 'task_time_s': 4}, 'request_messages': [task, task, image]},
        {'event': 'model_response', 'decision_id': 2, 'request_id': 2, 'calls': [{'id': 'q1', 'name': 'get_frames', 'arguments': '{"times_s":[1,2]}'}]},
        {'event': 'frame_query_artifacts', 'decision_id': 2, 'images': [{'file': 'query_1_0.png', 'actual_time_s': 1.01, 'requested_time_s': 1}]},
        {'event': 'tool_result', 'decision_id': 2, 'request_id': 2, 'call_id': 'q1', 'result': {'task_time_s': 5, 'available_until_s': 4.9, 'frames': [{'status': 'ok', 'actual_time_s': 1.01, 'requested_time_s': 1}, {'status': 'not_ready', 'requested_time_s': 2}]}},
        {'event': 'evaluation', 'decision_id': 2, 'result': 1, 'details': {'benchmark_id': 'C1', 'status': 'passed', 'pass_at_1': 0, 'pass_at_3': 1, 'attempts_completed': 2}},
    ]
    (tmp_path / 'experiment.json').write_text(json.dumps({'model': 'test-model', 'variant': 'agent4', 'instruction': 'A test task'}))
    (tmp_path / 'system_prompt.txt').write_text('Use native screen pixels.')
    return write_log(tmp_path, events)


def test_message_storage_dedup_preserves_history_order_and_repetitions(trajectory):
    original = trajectory.read_bytes()
    data = build_trajectory_data(trajectory)
    requests = [e for e in data['events'] if e['event'] == 'model_request']
    assert len(data['messages']) == 2
    assert requests[0]['_message_refs'] == requests[1]['_message_refs'] == [0, 0, 1]
    restored = [data['messages'][i] for i in requests[0]['_message_refs']]
    assert restored[0] == restored[1]
    assert restored[2]['content'][0]['source']['asset_id'] in data['assets']
    assert len(data['assets']) == 1  # Three PNG filenames, one identical image.
    assert trajectory.read_bytes() == original


def test_frames_actual_times_wait_duration_and_old_decision_labels(trajectory):
    d = build_trajectory_data(trajectory)
    old_result = next(e for e in d['events'] if e['event'] == 'action_tool_result')
    assert old_result['decision_id'] == 2
    assert old_result['_view']['decision'] == 1
    frame = next(e for e in d['events'] if e['event'] == 'tool_result')
    assert frame['_view']['images'][0]['time'] == 1.01
    assert frame['result']['frames'][1]['status'] == 'not_ready'
    assert next(e for e in d['events'] if e['event'] == 'frame_query_artifacts')['_view']['hidden']
    execution = next(e for e in d['events'] if e['event'] == 'action_executed')
    assert [f['label'] for f in execution['_view']['images']] == ['决策输入', '执行后']
    assert execution['info']['sequence_actions'][0]['duration_s'] == .251
    assert d['evaluation']['details']['pass_at_1'] == 0


def test_relative_response_markers_use_recorded_protocol_without_rewriting_calls(tmp_path, png):
    (tmp_path / 'input.png').write_bytes(png)
    raw_calls = [
        {'name': 'computer_click', 'arguments': {'x': 518, 'y': 670}},
        {'name': 'computer_click', 'arguments': {'x': 1600, 'y': 880}},
    ]
    path = write_log(tmp_path, [
        {'event': 'model_request', 'request_id': 1, 'coordinate_system': 'normalized_0_999',
         'observation': {'screenshot_file': 'input.png', 'task_time_s': 0}},
        {'event': 'model_response', 'request_id': 1, 'calls': raw_calls},
        {'event': 'model_request', 'request_id': 2, 'coordinate_system': 'unknown-contract'},
        {'event': 'model_response', 'request_id': 2, 'calls': raw_calls},
    ])
    before = path.read_bytes()
    data = build_trajectory_data(path)
    response = data['events'][1]
    assert response['calls'] == raw_calls
    assert response['_view']['coordinate_system'] == 'normalized_0_999'
    assert response['_view']['marks'] == [
        {'x': 994, 'y': 723, 'label': '1', 'action': 'computer_click'},
    ]
    assert not data['events'][3]['_view']['marks']
    assert any('未知坐标协议' in warning for warning in data['warnings'])
    assert path.read_bytes() == before


def test_nested_inline_images_are_embedded_once(tmp_path, png):
    source = {'type': 'base64', 'media_type': 'image/png', 'data': base64.b64encode(png).decode()}
    path = write_log(tmp_path, [{'event': 'model_request', 'request_messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'q', 'content': [{'type': 'image', 'source': source}, {'type': 'image', 'source': source}]}]}]}])
    d = build_trajectory_data(path)
    assert len(d['assets']) == 1
    sources = d['messages'][0]['content'][0]['content']
    assert sources[0]['source']['asset_id'] == sources[1]['source']['asset_id']
    assert source['data'] not in json.dumps(d['messages'])


def test_overwritten_screenshot_is_not_shown_as_the_original(tmp_path, png):
    (tmp_path / 'initial_state.png').write_bytes(png)
    obs = {'screenshot_file': 'initial_state.png', 'task_time_s': .1}
    path = write_log(tmp_path, [
        {'event': 'initial_observation', 'observation': obs},
        {'event': 'model_request', 'request_id': 1, 'observation': {**obs, 'sha256': '0'*64}},
    ])
    d = build_trajectory_data(path)
    assert all(e['_view']['images'][0]['asset'] is None for e in d['events'])
    assert any('哈希不一致' in w for w in d['warnings'])


def test_partial_jsonl_and_missing_images_still_render(tmp_path):
    path = write_log(tmp_path, [{'event': 'initial_observation', 'observation': {'screenshot_file': 'missing.png'}}])
    with path.open('a') as f:
        f.write('[]\n{"event":')
    d = build_trajectory_data(path)
    assert len(d['events']) == 1
    assert len(d['warnings']) == 3
    assert render_trajectory(path).exists()


def test_do_not_embed_paths_or_symlinks_outside_task_folder(tmp_path, png):
    task = tmp_path / 'task';task.mkdir()
    outside = tmp_path / 'outside.png';outside.write_bytes(png)
    (task / 'linked.png').symlink_to(outside)
    path = write_log(task, [
        {'event': 'initial_observation', 'observation': {'screenshot_file': '../outside.png'}},
        {'event': 'initial_observation', 'observation': {'screenshot_file': 'linked.png'}},
    ])
    d = build_trajectory_data(path)
    assert not d['assets']
    assert len(d['warnings']) == 2


def test_model_text_cannot_close_embedded_json_script(tmp_path):
    attack = '</script><script>window.viewerPwned=1</script><img src=x onerror=alert(1)>'
    path = write_log(tmp_path, [{'event': 'model_response', 'text': attack, 'reasoning': [attack]}])
    before = path.read_bytes()
    html = render_trajectory(path).read_text()
    assert attack not in html
    assert '\\u003c/script\\u003e' in html
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        render_trajectory(path, path)


def test_cli_renders_existing_task_and_recursive_root(trajectory):
    script = Path(__file__).resolve().parents[1] / 'scripts/python/render_realtime_trajectory.py'
    result = subprocess.run([sys.executable, str(script), str(trajectory.parent.parent), '--recursive'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (trajectory.parent / 'trajectory.html').exists()


def test_browser_navigation_frames_history_and_safe_text(trajectory):
    playwright = pytest.importorskip('playwright.sync_api')
    attack = '</script><script>window.viewerPwned=1</script>'
    rows = [json.loads(line) for line in trajectory.read_text().splitlines()]
    timing_result = json.dumps({'duration_s': 0.003609571000001921,
                               'started_s': 7.128817319869995,
                               'finished_s': 7.132425546646118,
                               'times_s': [1.234567, 2], 'x': 960.123456})
    request = next(row for row in rows if row['event'] == 'model_request')
    request['wall_time'] = '2026-09-16T11:00:00.123456+00:00'
    request['request_messages'].append({'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'timing', 'content': [{'type': 'text', 'text': timing_result}]},
    ]})
    write_log(trajectory.parent, rows)
    with trajectory.open('a') as stream:
        stream.write(json.dumps({'event': 'model_response', 'decision_id': 2, 'text': attack}) + '\n')
    html = render_trajectory(trajectory)
    with playwright.sync_playwright() as pw:
        executable = os.environ.get('REALTIME_VIEWER_CHROMIUM', pw.chromium.executable_path)
        if not Path(executable).exists():
            pytest.skip('Playwright Chromium is not installed')
        browser = pw.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width':1440,'height':1000})
        errors = [];page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(html.as_uri());page.wait_for_function('window.__trajectoryViewerReady === true')
        assert page.title() == 'test-model · 轨迹回放'
        page.locator('#timeline button').filter(has_text='模型回复').first.click()
        page.wait_for_function('document.querySelectorAll(".marker").length === 1')
        assert page.locator('.stage img').evaluate('(im)=>im.naturalWidth') == 1920
        page.select_option('#filter','frame')
        assert page.locator('#event-title').inner_text() == '历史帧返回'
        assert page.get_by_text('not_ready',exact=True).count() == 1
        page.select_option('#filter','model')
        page.locator('#timeline button').filter(has_text='模型请求').first.click()
        page.get_by_text('展开完整请求历史',exact=True).click()
        page.wait_for_function('document.querySelectorAll(".message").length === 4')
        assert '任务说明 2 份' in page.locator('#content').inner_text()
        assert '11:00:00.123+00:00' in page.locator('#event-meta').inner_text()
        assert '123456' not in page.locator('#event-meta').inner_text()
        page.locator('.message>summary').nth(2).click()
        page.wait_for_function('document.querySelectorAll(".message img").length === 1')
        page.locator('.message>summary').nth(3).click()
        timing_message = page.locator('.message').nth(3)
        # The details toggle lazily renders its body after the click completes.
        playwright.expect(timing_message).to_contain_text('"duration_s": 0.004')
        displayed = timing_message.inner_text()
        assert '"started_s": 7.129' in displayed
        assert '"finished_s": 7.132' in displayed
        assert '1.235' in displayed and '2.000' in displayed
        assert '960.123456' in displayed
        assert '0.003609571' not in displayed
        assert page.evaluate('D.messages[D.events.find(e=>e.event==="model_request")._message_refs[3]].content[0].content[0].text') == timing_result
        page.fill('#search','viewerPwned')
        assert attack in page.locator('#content').inner_text()
        assert page.evaluate('window.viewerPwned') is None
        assert not errors
        browser.close()
