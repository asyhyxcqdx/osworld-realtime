#!/usr/bin/env python3
"""Run screenshot-based agents on Human Benchmark and average three reaction times."""
import argparse
import base64
import csv
from datetime import datetime, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from mm_agents.humanbenchmark_reactiontime import (
    TRIAL_COUNT, URL, BrowserProxyBridge, WebsiteScoreReader, browser_task, save_completed_result,
    wait_for_startup_clock,
)
from mm_agents.realtime_agent import RealtimeAgent
from mm_agents.realtime_config import VARIANT_TO_AGENT_ID, agent_kwargs, load_realtime_config
from mm_agents.realtime_env import load_env_file


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def update_summary(root):
    rows = []
    for source in sorted(root.glob('*/*/result.json')):
        result = json.loads(source.read_text())
        rows.append({k:result.get(k) for k in ['run_id','model','agent','kind','measurement','valid_trials',
                    'trial_times_ms',
                    'reaction_time_ms','premature_clicks']}
                    | {'directory':str(source.parent)})
    dump(root/'summary.json',rows)
    trial_columns = [f'trial_{index+1}_ms' for index in range(TRIAL_COUNT)]
    columns = ['run_id','model','agent','kind','measurement','valid_trials',*trial_columns,
               'reaction_time_ms','premature_clicks','directory']
    with (root/'summary.csv').open('w',newline='',encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream,fieldnames=columns)
        writer.writeheader()
        for row in rows:
            csv_row = {key:value for key,value in row.items() if key!='trial_times_ms'}
            csv_row.update(zip(trial_columns,row.get('trial_times_ms') or []))
            writer.writerow(csv_row)


def group_transport(args, group):
    settings = argparse.Namespace(**vars(args))
    settings.api_base_url = args.api_base_url or group.get('api_base_url')
    settings.api_proxy = args.api_proxy or group.get('api_proxy') or 'direct'
    return settings


def prepare_realtime_server(env):
    from scripts.python.install_realtime_server import install
    install(env.controller.http_server, password=getattr(env, 'client_password', ''))


def run_group(config_path, variant, root, args):
    from desktop_env.desktop_env import DesktopEnv
    from lib_realtime_trajectory import render_trajectory
    config = load_realtime_config(config_path,variant=variant)
    model = config['api']['model']
    out = root/model/variant
    if (out/'result.json').exists():
        previous = json.loads((out/'result.json').read_text())
        if previous.get('measurement')!='mean_of_three_valid_trials' or previous.get('valid_trials')!=TRIAL_COUNT:
            raise RuntimeError(f'Existing results use a different trial count; use a new run_id: {out}')
        print('ALREADY_COMPLETED',model,variant,flush=True)
        return
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f'Existing unfinished session is preserved at {out}; use a new run_id for another session')
    key_env = config['api']['key_env']
    if args.prompt_key:
        os.environ[key_env] = getpass.getpass(f'API key for {model} (hidden): ')
    if not os.environ.get(key_env):
        for name in ('REALTIME_API_KEY','PACKY_API_KEY'):
            if os.environ.get(name):
                os.environ[key_env] = os.environ[name]
                break
    if not os.environ.get(key_env):
        raise RuntimeError(f'Set {key_env} in the environment or use --prompt-key')
    agent = RealtimeAgent(variant=variant,**agent_kwargs(config),api_base_url=args.api_base_url)
    agent.wire.session.trust_env = False
    if args.api_proxy != 'direct':
        agent.wire.session.proxies = {'http':args.api_proxy,'https':args.api_proxy}
    out.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(config_path,out/'agent_config.yaml')
    (out/'system_prompt.txt').write_text(agent.system)
    (out/'user_prompt.txt').write_text(agent.user_prompt+'\n')
    env = bridge = reader = None
    recording = False
    decisions = events = queries = observed_premature = 0
    was_premature = False
    snapshot = {'completed':False}
    started = time.monotonic()
    metadata = {'run_id':args.run_id,'kind':args.kind,'model':model,'variant':variant,
                'url':URL,'started_at':now(),'config':str(config_path),
                'api_format':agent.wire.protocol,'api_base_url':agent.wire.base_url,
                'api_proxy':args.api_proxy,'browser_proxy':args.browser_proxy,
                'thinking':config['api']['thinking'],'coordinate_system':agent.coordinate_system,
                'max_output_tokens':config['api']['max_output_tokens'],'screen_size':[1920,1080],
                'sequence':agent.sequence,'frame_query':agent.frames,'max_decision_rounds':args.max_steps,
                'measurement':'mean_of_three_valid_trials','target_valid_trials':TRIAL_COUNT,
                'observation_policy':'capture a current screenshot immediately before each decision',
                'max_duration_s':args.max_duration_s,'model_log_version':3,
                'score_collection':'completed frontend trial times; arithmetic mean of the first three; evaluator only',
                'task_config_file':'task_config.json',
                'premature_count_method':'observed distinct Too soon screens after action batches',
                'status':'starting'}
    dump(out/'experiment.json',metadata)

    def emit(event):
        nonlocal events
        events += 1
        decision = decisions if event.get('event')=='action_tool_result' else decisions+1
        row = {'event_index':events,'wall_time':now(),'decision_id':decision,**event}
        with (out/'trajectory.jsonl').open('a') as stream:
            stream.write(json.dumps(row,ensure_ascii=False)+'\n')

    def observe(obs, filename):
        obs['task_time_s'] = env.controller.last_observation_time
        obs['screenshot_file'] = filename
        (out/filename).write_bytes(obs['screenshot'])
        return obs

    def query_frames(times):
        nonlocal queries
        queries += 1
        result = env.controller.get_frames(times)
        images = []
        for index,frame in enumerate(result.get('frames',[])):
            if frame.get('image'):
                name = f'query_{queries}_{index}.png'
                (out/name).write_bytes(base64.b64decode(frame['image']['data']))
                images.append({'file':name,'requested_time_s':frame.get('requested_time_s'),
                               'actual_time_s':frame.get('actual_time_s')})
        emit({'event':'frame_query_artifacts','times_s':times,'images':images})
        return result

    try:
        env = DesktopEnv(provider_name='docker',path_to_vm=str(args.path_to_vm),headless=True,
                         action_space='computer_13',screen_size=(1920,1080),require_a11y_tree=False,
                         cache_dir=str(out/'cache'))
        bridge = BrowserProxyBridge(env,args.browser_proxy)
        task = browser_task(agent.user_prompt,bridge.url)
        dump(out/'task_config.json',task)
        env.reset(task_config=task)
        prepare_realtime_server(env)
        reader = WebsiteScoreReader(env.vm_ip,env.chromium_port)
        ready_deadline = time.monotonic()+30
        while not reader.ready():
            if time.monotonic()>=ready_deadline:
                raise RuntimeError('Original reaction-time page did not become ready')
            time.sleep(0.5)
        print('WAITING_FOR_STARTUP_CLOCK',model,variant,flush=True)
        metadata['clock_check']=wait_for_startup_clock(env)
        dump(out/'experiment.json',metadata)
        print('STARTUP_CLOCK_READY',model,variant,flush=True)
        record = env.controller.start_realtime_recording(100)
        recording = True
        metadata.update(status='running',recording=record,browser_proxy_in_vm=bridge.url)
        dump(out/'experiment.json',metadata)
        agent.bind_event_sink(emit)
        agent.bind_frame_query(query_frames if agent.frames else None)
        obs = observe(env._get_obs(),'initial_state.png')
        emit({'event':'initial_observation','decision_id':0,'observation':{
            'screenshot_file':'initial_state.png','task_time_s':obs['task_time_s']}})
        print('MODEL_SESSION_STARTED',model,variant,str(out),flush=True)
        for _ in range(args.max_steps):
            if time.monotonic()-started>args.max_duration_s:
                raise RuntimeError('Session duration limit reached')
            if decisions:
                obs = observe(env._get_obs(),f'input_{decisions+1}.png')
            text,actions = agent.predict(agent.user_prompt,obs)
            decisions += 1
            emit({'event':'action_submitted','decision_id':decisions,'actions':actions,'response':text})
            dispatched = time.monotonic()
            try:
                if agent.sequence:
                    obs,reward,done,info = env.step_sequence(actions)
                else:
                    obs,reward,done,info = env.step(actions[0],0)
            except Exception as error:
                emit({'event':'action_execution_error','decision_id':decisions,'actions':actions,
                      'execution_error':getattr(error,'details',{'message':str(error)})})
                raise
            obs = observe(obs,f'step_{decisions}.png')
            emit({'event':'action_executed','decision_id':decisions,'actions':actions,
                  'reward':reward,'done':done,'info':info,
                  'dispatch_to_observation_s':time.monotonic()-dispatched,
                  'observation':{'screenshot_file':obs['screenshot_file'],'task_time_s':obs['task_time_s'],
                                 'capture_interval_s':env.controller.last_capture_interval}})
            agent.record_action_result(actions,reward=reward,done=done,info=info)
            grade_started = time.monotonic()
            snapshot = reader.read()
            # A score panel can mount just after the immediate post-click screenshot.
            if done and not snapshot.get('completed'):
                time.sleep(0.1)
                snapshot = reader.read()
            premature = snapshot.get('too_soon',False)
            observed_premature += int(premature and not was_premature)
            was_premature = premature
            emit({'event':'website_score_observation','decision_id':decisions,'snapshot':snapshot,
                  'collection_s':time.monotonic()-grade_started})
            if metadata.get('trial_times_ms')!=snapshot.get('trial_times_ms',[]):
                metadata.update(trial_times_ms=snapshot.get('trial_times_ms',[]),
                                valid_trials=len(snapshot.get('trial_times_ms',[])))
                dump(out/'experiment.json',metadata)
            print('STEP',decisions,'ACTIONS',','.join(a['action_type'] for a in actions),
                  'TRIALS',len(snapshot.get('trial_times_ms',[])),'OF',TRIAL_COUNT,flush=True)
            if snapshot.get('completed'):
                save_completed_result(out,snapshot,model=model,agent=variant,run_id=args.run_id,
                                      kind=args.kind,premature_clicks=observed_premature)
                observe(env._get_obs(),'final_state.png')
                metadata.update(status='completed',reaction_time_ms=snapshot['reaction_time_ms'])
                print('REACTIONTIME_COMPLETED',json.dumps({'model':model,'agent':variant,
                    'reaction_time_ms':snapshot['reaction_time_ms']}),flush=True)
                break
            if done:
                raise RuntimeError('Agent submitted DONE before three reaction times were recorded')
        else:
            raise RuntimeError('Decision limit reached before three reaction times were recorded')
    except BaseException as error:
        metadata.update(status='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',
                        error={'type':type(error).__name__,'message':str(error)})
        emit({'event':'run_error',**metadata['error']})
        raise
    finally:
        metadata.update(finished_at=now(),elapsed_s=time.monotonic()-started,
                        decisions=decisions,agent_counters=agent.counters,
                        premature_clicks_observed=observed_premature)
        agent.bind_event_sink(None)
        cleanup_errors = []
        if env is not None:
            if recording:
                try:
                    env.controller.end_realtime_recording(str(out))
                except Exception as error:
                    cleanup_errors.append('recording: '+str(error))
            try:
                env.close()
            except Exception as error:
                cleanup_errors.append('VM: '+str(error))
        if bridge is not None:
            bridge.close()
        agent.wire.session.close()
        if (out/'trajectory.jsonl').exists():
            try:
                render_trajectory(out)
            except Exception as error:
                cleanup_errors.append('trajectory viewer: '+str(error))
        metadata['artifact_errors'] = cleanup_errors
        dump(out/'experiment.json',metadata)
        update_summary(root)


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--config',type=Path)
    source.add_argument('--plan',type=Path)
    parser.add_argument('--model')
    parser.add_argument('--agent_variant',choices=sorted(VARIANT_TO_AGENT_ID))
    parser.add_argument('--run_id')
    parser.add_argument('--result_dir',type=Path,default=REPO/'results_humanbenchmark_reactiontime')
    parser.add_argument('--path_to_vm',type=Path,default=REPO/'docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2')
    parser.add_argument('--kind',choices=['smoke','formal'],default='formal')
    parser.add_argument('--browser-proxy',default=os.getenv('HUMANBENCHMARK_BROWSER_PROXY'))
    parser.add_argument('--api_base_url',default=os.getenv('REALTIME_API_BASE_URL'))
    parser.add_argument('--api-proxy',help='model proxy URL, or direct; otherwise use the group route')
    parser.add_argument('--prompt-key',action='store_true')
    parser.add_argument('--max_steps',type=int,default=100)
    parser.add_argument('--max-duration-s',type=float,default=1800)
    parser.add_argument('--dry-run',action='store_true')
    args = parser.parse_args()
    args.path_to_vm = args.path_to_vm.resolve()
    if args.plan:
        plan = json.loads(args.plan.read_text())
        if plan.get('valid_trials_per_group')!=TRIAL_COUNT:
            parser.error('This runner collects three valid trials; use a plan with valid_trials_per_group=3')
        groups = [g for g in plan['groups'] if (not args.model or g['model']==args.model)
                  and (not args.agent_variant or g['agent']==args.agent_variant)]
        args.run_id = args.run_id or plan['run_id']
    else:
        config = load_realtime_config(args.config)
        variant = args.agent_variant or next(k for k,v in VARIANT_TO_AGENT_ID.items() if v==config['agent_id'])
        groups = [{'config':str(args.config),'model':config['api']['model'],'agent':variant}]
    if not groups or not args.run_id:
        parser.error('Select at least one configured group and provide --run_id when using --config')
    if Path(args.run_id).name!=args.run_id or args.run_id in {'.','..'}:
        parser.error('run_id must be a single directory name')
    if args.max_steps<1 or args.max_duration_s<=0:
        parser.error('Step and duration budgets must be positive')
    for group in groups:
        load_realtime_config(group['config'],variant=group['agent'])
    root = args.result_dir.resolve()/args.run_id
    if args.dry_run:
        print(json.dumps({'run_id':args.run_id,'groups':groups,'valid_trials_per_group':TRIAL_COUNT,
                          'measurement':'mean_of_three_valid_trials','output':str(root)},ensure_ascii=False,indent=2))
        return
    root.mkdir(parents=True,exist_ok=True)
    no_proxy = 'localhost,127.0.0.1,::1'
    os.environ.update(NO_PROXY=no_proxy,no_proxy=no_proxy,OSWORLD_DOCKER_PORT_BIND_HOST='127.0.0.1')
    logging.basicConfig(level=logging.INFO)
    for group in groups:
        run_group(Path(group['config']),group['agent'],root,group_transport(args,group))
    update_summary(root)


if __name__ == '__main__':
    main()
