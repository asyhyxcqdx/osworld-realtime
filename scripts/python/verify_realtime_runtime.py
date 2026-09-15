#!/usr/bin/env python3
"""Serial VM integration checks with scripted replies (no model API or scores)."""
import argparse
import base64
import io
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from PIL import Image
from desktop_env.desktop_env import DesktopEnv
from mm_agents.realtime_agent import RealtimeAgent
from mm_agents.realtime_config import agent_kwargs, default_config_path, load_realtime_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, default=Path('docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2'))
    parser.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {'method': 'Scripted model replies, real VM/controller/Agent; no paid API and no model score', 'image': str(args.image.resolve()), 'runs': []}
    env = DesktopEnv(provider_name='docker', path_to_vm=str(args.image.resolve()), action_space='computer_13', headless=True, screen_size=(1920, 1080), require_a11y_tree=False)
    try:
        task = json.loads(Path('evaluation_examples/examples/realtime_gui_bench/5169e1b0-1a7d-538b-8e59-8785c39460ce.json').read_text())
        env.reset(task_config=task)
        from desktop_env.evaluators.getters.realtime_gui import (
            realtime_page_identity, verify_realtime_page_identity,
        )
        page_config = task['evaluator']['result']
        identity = realtime_page_identity(env, page_config)
        for model in ('claude-fable-5', 'gpt-6-astra'):
            for variant in ('agent1', 'agent2', 'agent3', 'agent4'):
                out = args.artifacts / model / variant
                out.mkdir(parents=True, exist_ok=True)
                cfg = load_realtime_config(default_config_path(variant, model), variant=variant)
                agent = RealtimeAgent(variant=variant, **agent_kwargs(cfg))
                recording = env.controller.start_realtime_recording()
                row = {'model': model, 'variant': variant, 'recording': recording, 'actions': [], 'frame_statuses': []}
                call_count = 0

                def request(system, messages, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    assert ('get_frames' in {t['name'] for t in kwargs['tools']}) == agent.frames
                    for message in messages:
                        parts = message.get('content', [])
                        if not isinstance(parts, list):
                            continue
                        for block in parts:
                            if block['type'] == 'image':
                                data = base64.b64decode(block['source']['data'])
                            elif block['type'] == 'input_image':
                                data = base64.b64decode(block['image_url'].split(',', 1)[1])
                            else:
                                continue
                            assert Image.open(io.BytesIO(data)).size == (1920, 1080)
                    if agent.frames and call_count == 1:
                        calls = [('get_frames', {'times_s': [0]})]
                    elif call_count == (2 if agent.frames else 1):
                        calls = [('computer_wait', {'duration_s': 0.12})]
                        if agent.sequence:
                            calls += [('computer_key_down', {'key': 'shift'}), ('computer_key_up', {'key': 'shift'}), ('computer_wait', {'duration_s': 0.13})]
                    else:
                        calls = [('computer_done', {})]
                    if agent.wire.protocol == 'anthropic_messages':
                        return {'content': [{'type': 'tool_use', 'id': f'{call_count}-{i}', 'name': n, 'input': p} for i, (n, p) in enumerate(calls)]}
                    return {'output': [{'type': 'function_call', 'call_id': f'{call_count}-{i}', 'name': n, 'arguments': json.dumps(p)} for i, (n, p) in enumerate(calls)]}

                def query(times):
                    result = env.controller.get_frames(times)
                    row['frame_statuses'].extend(f['status'] for f in result['frames'])
                    for f in result['frames']:
                        assert f['status'] == 'ok'
                        assert Image.open(io.BytesIO(base64.b64decode(f['image']['data']))).size == (1920, 1080)
                    return result

                agent.wire.request = request
                agent.bind_frame_query(query if agent.frames else None)
                try:
                    obs = env._get_obs()
                    done = False
                    while not done:
                        obs['task_time_s'] = env.controller.last_observation_time
                        _, actions = agent.predict('Runtime verification only.', obs)
                        obs, reward, done, info = env.step_sequence(actions) if agent.sequence else env.step(actions[0], 0)
                        agent.record_action_result(actions, reward=reward, done=done, info=info)
                        row['actions'].extend(info['sequence_actions'])
                    for action in row['actions']:
                        if action['action']['action_type'] == 'WAIT':
                            expected = action['action']['parameters']['duration_s']
                            assert expected - .01 <= action['duration_s'] <= expected + .25
                        elif action['action']['action_type'] in {'KEY_DOWN', 'KEY_UP'}:
                            assert action['duration_s'] < .08, 'Unexpected implicit key pause'
                    row['passed'] = True
                    row['counters'] = agent.counters
                    print(model, variant, 'PASS', flush=True)
                finally:
                    env.controller.end_realtime_recording(str(out))
                    verify_realtime_page_identity(env, page_config, identity)
                    report['runs'].append(row)
                    (args.artifacts / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        report['passed'] = len(report['runs']) == 8 and all(r.get('passed') for r in report['runs'])
        # Diagnostic-only reload after the eight checks, never part of a model run.
        env.controller.execute_python_command("pyautogui.hotkey('ctrl', 'r')")
        time.sleep(1)
        try:
            verify_realtime_page_identity(env, page_config, identity)
        except RuntimeError:
            report['reload_detected'] = True
        else:
            raise AssertionError('Reloaded document was accepted')
    finally:
        env.close()
        (args.artifacts / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    if not report.get('passed'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
