"""Real DeepSeek tool-use checks with real VM observers; no D-class tasks."""
import argparse
import base64
from datetime import datetime, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FIXTURE = '''import json,os,time,tkinter as tk
from pathlib import Path
root=tk.Tk(); root.attributes('-fullscreen',True); root.attributes('-topmost',True)
canvas=tk.Canvas(root,width=1920,height=1080,bg='#202c40',highlightthickness=0);canvas.pack(fill='both',expand=True)
canvas.create_text(960,70,text='OBSERVER INTEGRATION CHECK',fill='white',font=('Arial',32))
canvas.create_text(960,145,text='Click inside the signal panel only when GREEN. Do nothing when RED.',fill='white',font=('Arial',23))
canvas.create_text(960,190,text='After 3 green clicks, the panel says COMPLETE. Stop your observer.',fill='white',font=('Arial',23))
rect=canvas.create_rectangle(560,300,1360,800,fill='#df3a3a',outline='white',width=4)
label=canvas.create_text(960,540,text='RED',fill='white',font=('Arial',52))
counter=canvas.create_text(960,900,text='',fill='white',font=('Arial',25))
state={'count':0,'wrong_clicks':0,'phase':'RED','events':[]}
output=Path(STATE_PATH)
Path(str(output)+'.pid').write_text(str(os.getpid()))
def save(event):
 state['events'].append({'event':event,'monotonic_s':time.monotonic()})
 output.write_text(json.dumps(state))
 canvas.itemconfigure(counter,text='Green clicks: %d / 3    Red clicks: %d'%(state['count'],state['wrong_clicks']))
def green():
 if state['count']<3:
  state['phase']='GREEN';canvas.itemconfigure(rect,fill='#36cb62');canvas.itemconfigure(label,text='GREEN');save('green')
def click(e):
 if not(560<=e.x<=1360 and 300<=e.y<=800):return
 if state['phase']=='GREEN':
  state['count']+=1
  if state['count']==3:
   state['phase']='COMPLETE';canvas.itemconfigure(rect,fill='#2860b5');canvas.itemconfigure(label,text='COMPLETE')
  else:
   state['phase']='RED';canvas.itemconfigure(rect,fill='#df3a3a');canvas.itemconfigure(label,text='RED');root.after(2700,green)
  save('green_click')
 elif state['phase']=='RED':state['wrong_clicks']+=1;save('red_click')
canvas.bind('<Button-1>',click)
save('start');root.after(2000,green);root.mainloop()
'''


def inspect_run(folder, fixture, terminal):
    rows = [json.loads(line) for line in (folder/'trajectory.jsonl').read_text().splitlines()]
    requests = [r for r in rows if r['event']=='model_request']
    responses = [r for r in rows if r['event']=='model_response']
    tools = [r for r in rows if r['event']=='observer_tool_result']
    failures = [r for r in rows if r['event']=='observer_failed']
    stopped = [r for r in tools if r['action']['action_type']=='OBSERVER_STOP' and r['result'].get('status')=='stopped']
    feedback_requests = [r['request_id'] for r in requests if 'Background observer updates:' in json.dumps(r.get('request_messages',[]))]
    direct_inputs = [a for r in rows if r['event']=='action_submitted' for a in r['actions']
                     if a['action_type'] not in {'OBSERVER_CREATE','OBSERVER_STOP','WAIT','DONE'}]
    observer_actions = [r for r in rows if r['event']=='observer_action']
    overlap = []
    for request in requests:
        reply = next((r for r in responses if r['request_id']==request['request_id']),None)
        if reply:
            count = sum(request['host_monotonic_s'] < r['host_monotonic_s'] < reply['host_monotonic_s'] for r in observer_actions)
            if count: overlap.append({'request_id':request['request_id'],'observer_actions':count})
    checks = {'real_main_model_responses':len(responses), 'done_called_by_model':terminal,
              'successful_green_clicks':fixture['count'], 'red_clicks':fixture['wrong_clicks'],
              'observer_creations':len([r for r in tools if r['action']['action_type']=='OBSERVER_CREATE']),
              'model_stopped_running_observer':bool(stopped), 'background_feedback_in_requests':feedback_requests,
              'background_failures':[{'name':r['name'],'error':r.get('error')} for r in failures],
              'main_direct_input_actions':direct_inputs, 'observer_actions':len(observer_actions),
              'observer_actions_during_model_requests':overlap,
              'jev_responses':len([r for r in rows if r['event']=='observer_jev_response']),
              'injected_faults':len([r for r in rows if r['event']=='technical_fault_injection']),
              'usage':[r.get('usage',{}) for r in responses]}
    checks['passed'] = bool(terminal and responses and fixture['count']==3 and fixture['wrong_clicks']==0
                            and stopped and feedback_requests and failures and not direct_inputs and observer_actions)
    if folder.name == 'script':
        checks['passed'] &= any(r.get('phase')=='syntax' for r in failures)
        checks['passed'] &= any(r.get('error',{}).get('error_type')=='RuntimeError' for r in failures)
    else:
        checks['passed'] &= checks['jev_responses'] > 0 and checks['injected_faults']==1
    return checks


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--modes',nargs='+',choices=['script','jev'],default=['script','jev'])
    parser.add_argument('--prompt-keys',action='store_true')
    parser.add_argument('--max-decisions',type=int,default=15)
    parser.add_argument('--base-url',default='https://www.packyapi.ai')
    parser.add_argument('--path-to-vm',default=str(ROOT/'docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2'))
    args=parser.parse_args()
    from mm_agents.realtime_env import load_env_file
    load_env_file()
    for name in ['PACKY_DEEPSEEK_FLASH_API_KEY'] + (['JEV_API_KEY'] if 'jev' in args.modes else []):
        if args.prompt_keys or not os.environ.get(name):
            os.environ[name]=getpass.getpass(name+': ')
    os.environ['NO_PROXY']='localhost,127.0.0.1,::1'
    os.environ['OSWORLD_DOCKER_PORT_BIND_HOST']='127.0.0.1'
    import requests
    import yaml
    from desktop_env.desktop_env import DesktopEnv
    from desktop_env.realtime_contract import verify_server_source
    from mm_agents.realtime_agent import RealtimeAgent
    from mm_agents.realtime_config import agent_kwargs,default_config_path,load_realtime_config
    from mm_agents.realtime_observers import ObserverManager
    from mm_agents.humanbenchmark_reactiontime import wait_for_startup_clock
    from scripts.python.install_realtime_server import install
    from lib_realtime_trajectory import render_trajectory
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    report={'main_model':'deepseek-flash','main_model_calls':'real_api','formal_D_tasks':0,
            'test':'live_tool_use_and_intentional_error_feedback','status':'running','modes':{}}
    def save():
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    save()
    env=runtime=None;recording=False
    session=requests.Session();session.trust_env=False
    try:
        print('BOOT_VM',flush=True)
        env=DesktopEnv(provider_name='docker',path_to_vm=args.path_to_vm,headless=True,
                       action_space='computer_13',screen_size=(1920,1080),require_a11y_tree=False,cache_dir=str(out/'cache'))
        env.reset()
        install(env.controller.http_server,password=env.client_password)
        report['server_source']=verify_server_source(env.controller.http_server,names=('realtime.py','fmp4.py','observers.py','observer_worker.py','pyxcursor.py','screen_capture.py'))
        report['clock']=wait_for_startup_clock(env);save()
        prompts=yaml.safe_load((ROOT/'configs/realtime_agents/observers/live_checks.yaml').read_text())
        for mode in args.modes:
            folder=out/mode;folder.mkdir()
            fixture_path=f'/tmp/observer_live_{mode}.py';state_path=f'/tmp/observer_live_{mode}.json'
            source=FIXTURE.replace('STATE_PATH',repr(state_path))
            (folder/'fixture.py').write_text(source)
            response=session.post(env.controller.http_server+'/setup/upload',data={'file_path':fixture_path},
                                  files={'file_data':('fixture.py',source.encode())},timeout=15);response.raise_for_status()
            session.post(env.controller.http_server+'/setup/launch',json={'command':['python3',fixture_path],'shell':False},timeout=15).raise_for_status()
            time.sleep(2)
            config=load_realtime_config(default_config_path('agent4','deepseek-flash',observer_mode=mode))
            config['user_prompt'] += '\n\n'+prompts[mode]
            config_path=folder/'agent_config.yaml';config_path.write_text(yaml.safe_dump(config,sort_keys=False,allow_unicode=True))
            config=load_realtime_config(config_path,variant='agent4')
            agent=RealtimeAgent(**agent_kwargs(config),variant='agent4',api_base_url=args.base_url)
            agent.wire.session.trust_env=False
            (folder/'system_prompt.txt').write_text(agent.system)
            record=env.controller.start_realtime_recording(100);recording=True
            (folder/'experiment.json').write_text(json.dumps({'observer_mode':mode,'main_model':'deepseek-flash',
                'main_model_calls':'real_api','formal_D_tasks':0,'recording':record,'config':str(config_path),
                'fault_test':'model_authored_syntax_and_runtime_errors' if mode=='script' else 'one_exception_after_real_jev_response'},indent=2))
            lock=threading.RLock();event_index=0
            def emit(event):
                nonlocal event_index
                with lock:
                    event_index+=1
                    row={'event_index':event_index,'wall_time':datetime.now(timezone.utc).isoformat(),
                         'host_monotonic_s':time.monotonic(),**event}
                    with (folder/'trajectory.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
                if event['event'] in {'model_request','model_response','observer_failed','observer_started','observer_stopped'}:
                    small={k:event[k] for k in ('request_id','name','latency_s','error') if k in event}
                    print(mode,event['event'],json.dumps(small,ensure_ascii=False),flush=True)
            class FaultCheckManager(ObserverManager):
                injected=False
                def classify(self,screenshot,criteria):
                    raw=super().classify(screenshot,criteria)
                    if not self.injected:
                        self.injected=True
                        self.record({'event':'technical_fault_injection','kind':'after_real_jev_response',
                                     'real_jev_response':raw,'message':'Intentional one-time Jev error-feedback check'})
                        raise RuntimeError('Intentional one-time Jev error-feedback check; recreate the observer to continue.')
                    return raw
            runtime=(FaultCheckManager if mode=='jev' else ObserverManager)(env,mode,folder,emit,agent.observer_config)
            agent.bind_event_sink(emit);agent.bind_observer_feedback(runtime.drain_feedback)
            def frame_query(times):
                result=env.controller.get_frames(times)
                images=[]
                for i,frame in enumerate(result.get('frames',[])):
                    if frame.get('image'):
                        filename=f'query_{time.time_ns()}_{i}.png'
                        (folder/filename).write_bytes(base64.b64decode(frame['image']['data']))
                        images.append({'file':filename,'actual_time_s':frame.get('actual_time_s')})
                emit({'event':'frame_query_artifacts','images':images,'times_s':times})
                return result
            agent.bind_frame_query(frame_query)
            obs=env._get_obs();obs['task_time_s']=env.controller.last_observation_time
            obs['screenshot_file']='initial_state.png';(folder/obs['screenshot_file']).write_bytes(obs['screenshot'])
            emit({'event':'initial_observation','observation':{'task_time_s':obs['task_time_s'],'screenshot_file':obs['screenshot_file']}})
            done=False
            for decision in range(1,args.max_decisions+1):
                _,actions=agent.predict('',obs)
                emit({'event':'action_submitted','decision_id':decision,'actions':actions})
                print(mode,'ACTIONS',decision,[a['action_type'] for a in actions],flush=True)
                obs,reward,done,info=runtime.execute(actions)
                obs['task_time_s']=env.controller.last_observation_time
                obs['screenshot_file']=f'step_{decision}.png';(folder/obs['screenshot_file']).write_bytes(obs['screenshot'])
                emit({'event':'action_executed','decision_id':decision,'actions':actions,'info':info,
                      'observation':{'task_time_s':obs['task_time_s'],'screenshot_file':obs['screenshot_file']}})
                agent.record_action_result(actions,reward=reward,done=done,info=info)
                if done:break
            runtime.close();runtime=None
            env.controller.end_realtime_recording(str(folder));recording=False
            state_result=env.controller.execute_python_command(f"from pathlib import Path; print(Path({state_path!r}).read_text())")
            assert state_result['returncode']==0,state_result
            fixture=json.loads(state_result['output']);(folder/'fixture_result.json').write_text(json.dumps(fixture,indent=2))
            checks=inspect_run(folder,fixture,done)
            (folder/'agent_metrics.json').write_text(json.dumps(agent.counters,indent=2))
            (folder/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
            report['modes'][mode]=checks;save()
            render_trajectory(folder/'trajectory.jsonl')
            print('CHECKS',mode,json.dumps({k:v for k,v in checks.items() if k not in {'usage','background_failures'}},ensure_ascii=False),flush=True)
            env.controller.execute_python_command(f"import os,signal;from pathlib import Path;os.kill(int(Path({(state_path+'.pid')!r}).read_text()),signal.SIGTERM)")
            agent.wire.session.close()
        report['status']='passed' if all(x['passed'] for x in report['modes'].values()) else 'failed'
    except BaseException as exc:
        report['status']='error';report['error']={'type':type(exc).__name__,'message':str(exc)}
        logging.exception('Live observer check failed')
        raise
    finally:
        try:
            if runtime:runtime.close()
        finally:
            try:
                if recording:env.controller.end_realtime_recording(str(folder))
            finally:
                if env:env.close()
                session.close();save()
                print('FINISHED',report['status'],str(out),flush=True)
    if report['status']!='passed':raise SystemExit(1)


if __name__=='__main__':
    main()
