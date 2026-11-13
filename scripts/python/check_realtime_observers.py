"""Technical observer checks on a disposable VM; no benchmark tasks are loaded."""
import argparse
import base64
from datetime import datetime
import getpass
import io
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FIXTURE = '''import tkinter as tk
import json,time
r=tk.Tk();r.attributes('-fullscreen',True);r.configure(bg='#4bdb6a')
count=0
label=tk.Label(r,text='OBSERVER TEST: GREEN',font=('Arial',48),bg='#4bdb6a')
label.pack(expand=True)
def clicked(event):
 global count
 count+=1
 label.config(text='OBSERVER TEST: GREEN\\nClicks: '+str(count))
 with open('/tmp/observer_fixture_clicks.json','w') as f:
  json.dump({'count':count,'last_click_monotonic':time.monotonic()},f)
r.bind('<Button-1>',clicked)
r.mainloop()
'''

SCRIPT = '''import pyautogui
import time
pyautogui.keyDown('a')
while True:
    image = pyautogui.screenshot()
    r, g, b = image.getpixel((100, 100))[:3]
    if g > r and g > b:
        pyautogui.click(x=960, y=540)
    time.sleep(0.5)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jev', action='store_true', help='Include real Jev API calls')
    parser.add_argument('--path_to_vm', default=str(ROOT / 'docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2'))
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    from mm_agents.realtime_env import load_env_file
    load_env_file()
    os.environ['NO_PROXY'] = 'localhost,127.0.0.1,::1'
    os.environ['OSWORLD_DOCKER_PORT_BIND_HOST'] = '127.0.0.1'
    if args.jev and not os.environ.get('JEV_API_KEY'):
        os.environ['JEV_API_KEY'] = getpass.getpass('Jev API key: ')
    from desktop_env.desktop_env import DesktopEnv
    from mm_agents.realtime_agent import ModelWire, RealtimeAgent
    from mm_agents.realtime_config import agent_kwargs, default_config_path, load_realtime_config
    from mm_agents.realtime_observers import ObserverManager
    from mm_agents.humanbenchmark_reactiontime import wait_for_startup_clock
    from scripts.python.install_realtime_server import install
    from lib_realtime_trajectory import render_trajectory
    import requests
    report = {'technical_check': True, 'formal_D_tasks': 0, 'main_model': 'scripted tool calls', 'checks': {}}
    env = runtime = None
    recording = False
    event_lock = threading.Lock()
    active_out = out
    def emit(event):
        with event_lock:
            with (active_out / 'trajectory.jsonl').open('a') as f:
                f.write(json.dumps({'wall_time': datetime.now().isoformat(), **event}, ensure_ascii=False) + '\n')
    def save():
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    def check(name, detail):
        report['checks'][name] = detail
        save()
        print('CHECK', name, json.dumps(detail, ensure_ascii=False), flush=True)
    def until(predicate, timeout=15):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.1)
        raise AssertionError('Timed out waiting for ' + str(predicate))
    def call(agent, wire, name, params):
        wire.request.return_value = {'content': [{'type': 'tool_use', 'id': 'call_'+str(time.time_ns()),
                                                 'name': name, 'input': params}]}
        obs = env._get_obs()
        obs['task_time_s'] = env.controller.last_observation_time
        _, actions = agent.predict('', obs)
        began = time.monotonic()
        _, _, done, info = runtime.execute(actions)
        agent.record_action_result(actions, done=done, info=info)
        return info.get('observer_results', {}).get(0, info), time.monotonic()-began
    try:
        print('BOOT_VM', flush=True)
        env = DesktopEnv(provider_name='docker', path_to_vm=args.path_to_vm, headless=True,
                         action_space='computer_13', screen_size=(1920,1080), require_a11y_tree=False,
                         cache_dir=str(out/'cache'))
        env.reset()
        deps = env.controller.execute_python_command("import json,sys,tkinter,numpy,PIL,pyautogui;print(json.dumps({'python':sys.version,'screen':list(pyautogui.size()),'numpy':numpy.__version__,'pillow':PIL.__version__}))")
        assert deps and deps['returncode'] == 0, deps
        check('vm_dependencies', json.loads(deps['output']))
        install(env.controller.http_server, password=env.client_password)
        from desktop_env.realtime_contract import verify_server_source
        check('server_source', verify_server_source(env.controller.http_server,
              names=('realtime.py','fmp4.py','observers.py','observer_worker.py','pyxcursor.py','screen_capture.py')))
        upload = requests.post(env.controller.http_server+'/setup/upload',
                 data={'file_path':'/tmp/observer_fixture.py'},
                 files={'file_data':('observer_fixture.py',FIXTURE.encode())}, timeout=15)
        upload.raise_for_status()
        requests.post(env.controller.http_server+'/setup/launch',
                      json={'command':['/usr/bin/python3','/tmp/observer_fixture.py'],'shell':False}, timeout=15).raise_for_status()
        time.sleep(2)
        check('clock', wait_for_startup_clock(env))
        for mode in (['script', 'jev'] if args.jev else ['script']):
            active_out = out/mode
            active_out.mkdir()
            config = load_realtime_config(default_config_path('agent4','deepseek-flash',observer_mode=mode))
            wire = ModelWire('mock', 'anthropic_messages')
            wire.request = Mock()
            agent = RealtimeAgent(**agent_kwargs(config), variant='agent4', wire=wire)
            record = env.controller.start_realtime_recording(100)
            recording = True
            (active_out/'experiment.json').write_text(json.dumps({'observer_mode':mode,'technical_check':True,'recording':record},indent=2))
            (active_out/'system_prompt.txt').write_text(agent.system)
            runtime = ObserverManager(env,mode,active_out,emit,agent.observer_config)
            agent.bind_event_sink(emit)
            agent.bind_observer_feedback(runtime.drain_feedback)
            if mode == 'script':
                bad, _ = call(agent,wire,'create_observer',{'name':'syntax','code':'while True\n    pass'})
                assert bad['status']=='failed' and bad['error']['error_type']=='SyntaxError',bad
                check('script_syntax_error',bad)
                bad, _ = call(agent,wire,'create_observer',{'name':'runtime_error','code':'import time\ntime.sleep(0.4)\nprint(target_x)\n'})
                assert bad['status']=='running',bad
                until(lambda: any(j['name']=='runtime_error' and j['status']=='failed' for j in runtime.poll()['observers']))
                call(agent,wire,'stop_observer',{'name':'runtime_error'})
                assert 'NameError' in json.dumps(wire.request.call_args.args[1])
                check('script_error_reaches_next_model_request',{'error_type':'NameError','received':True})
                started, duration = call(agent,wire,'create_observer',{'name':'live','code':SCRIPT})
                assert started['status']=='running' and duration<5,(started,duration)
                ident=started['id']
                until(lambda: any(e['event']=='observer_action' and e.get('observer_id')==ident and e.get('method')=='click'
                                  for e in [json.loads(s) for s in (active_out/'trajectory.jsonl').read_text().splitlines()]))
                check('script_background_started',{'startup_tool_s':duration})
                # WAIT deliberately exceeds the old /run_python 30-second limit.
                _, wait_duration = call(agent,wire,'computer_wait',{'duration_s':32})
                body=runtime.poll()
                job=next(j for j in body['observers'] if j['id']==ident)
                assert job['status']=='running',job
                before=[json.loads(s) for s in (active_out/'trajectory.jsonl').read_text().splitlines()]
                clicks=[e for e in before if e['event']=='observer_action' and e.get('method')=='click' and e.get('observer_id')==ident]
                assert len(clicks)>=3,len(clicks)
                check('script_survives_30s_and_operates_during_wait',{'wait_s':wait_duration,'click_count':len(clicks),'status':job['status']})
                stopped, _=call(agent,wire,'stop_observer',{'name':'live'})
                assert stopped['status']=='stopped' and not stopped['held_keys'],stopped
                runtime.poll(); cursor=runtime.cursor
                time.sleep(1)
                new=runtime.rpc('GET','/observers/events',{'cursor':cursor})['events']
                assert not any(e['event']=='observer_action' and e.get('observer_id')==ident for e in new)
                check('script_stopped_no_late_input',stopped)
                finished,_=call(agent,wire,'create_observer',{'name':'natural_exit','code':
                    "import pyautogui\nimport numpy as np\nassert pyautogui.screenshot().size == (1920,1080)\n"
                    "with pyautogui.hold('b'):\n    pyautogui.moveTo(np.int64(900),np.int64(500))\nprint('finished')\n"})
                until(lambda: any(j['id']==finished['id'] and j['status']=='completed' for j in runtime.poll()['observers']))
                check('script_natural_completion',{'status':'completed'})
                long_input, _ = call(agent,wire,'create_observer',{'name':'long_input', 'code':
                    "import pyautogui\npyautogui.press('a', presses=100, interval=1)\n"})
                time.sleep(1)
                stopped, stop_s = call(agent,wire,'stop_observer',{'name':'long_input'})
                assert stopped['status']=='stopped' and stop_s < 3 and not stopped['held_keys'], (stopped,stop_s)
                check('script_stop_interrupts_long_input',{'stop_s':stop_s,'status':stopped['status']})
            else:
                cases={'The main test panel is green and displays OBSERVER TEST: GREEN.':[
                    {'action_type':'CLICK','parameters':{'x':960,'y':540}}],
                    'The main test panel is red.':[]}
                started,duration=call(agent,wire,'create_observer',{'name':'jev_live','cases':cases})
                assert started['status']=='running',started
                ident=started['id']
                def clicked_twice():
                    runtime.poll()
                    rows=[json.loads(s) for s in (active_out/'trajectory.jsonl').read_text().splitlines()]
                    return len([e for e in rows if e['event']=='observer_action' and e.get('observer_id')==ident])>=2
                until(clicked_twice,timeout=90)
                call(agent,wire,'computer_wait',{'duration_s':2})
                stopped,_=call(agent,wire,'stop_observer',{'name':'jev_live'})
                assert stopped['status']=='stopped',stopped
                check('jev_live_start_classify_click_stop',{'startup_tool_s':duration,'status':stopped['status']})
            runtime.close(); runtime=None
            env.controller.end_realtime_recording(str(active_out)); recording=False
            render_trajectory(active_out/'trajectory.jsonl')
        report['status']='passed'
    except BaseException as exc:
        report['status']='failed'
        report['error']={'type':type(exc).__name__,'message':str(exc)}
        logging.exception('Observer technical check failed')
        raise
    finally:
        try:
            if runtime: runtime.close()
        finally:
            try:
                if recording: env.controller.end_realtime_recording(str(active_out))
            finally:
                if env: env.close()
                save()
                print('FINISHED',report['status'],str(out),flush=True)


if __name__=='__main__':
    main()
