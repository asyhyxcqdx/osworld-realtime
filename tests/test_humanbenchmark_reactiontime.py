import csv
import json
from argparse import Namespace
import shutil
import subprocess
import sys

import pytest

from mm_agents.humanbenchmark_reactiontime import READ_SCORE_JS, browser_task, save_completed_result, wait_for_startup_clock
from mm_agents.realtime_config import default_config_path, load_realtime_config
from scripts.python import run_humanbenchmark_reactiontime as runner
from scripts.python.run_humanbenchmark_reactiontime import group_transport


def snapshot(values=None, *, too_soon=False):
    values = [500,501,503] if values is None else values
    return {'completed':len(values)==3,'trial_times_ms':values,'too_soon':too_soon,
            'reaction_time_ms':sum(values)/3 if len(values)==3 else None,
            'page_score_text':f'{values[-1]} ms\nClick to keep going' if values else ''}


def save(path, data):
    return save_completed_result(path,data,model='example',agent='agent4',
                                 run_id='test',kind='smoke',premature_clicks=2)


def test_preserve_three_website_scores_and_unrounded_mean_without_duplicate_csv(tmp_path):
    result = save(tmp_path,snapshot())
    assert result['reaction_time_ms']==1504/3
    assert result['valid_trials']==3
    assert result['trial_times_ms']==[500,501,503]
    assert result['measurement']=='mean_of_three_valid_trials'
    assert json.loads((tmp_path/'result.json').read_text())==result
    assert not list(tmp_path.glob('*.csv'))
    original = (tmp_path/'result.json').read_bytes()
    with pytest.raises(FileExistsError):
        save(tmp_path,snapshot())
    assert (tmp_path/'result.json').read_bytes()==original


@pytest.mark.parametrize('changes',[
    {'completed':False},
    {'trial_times_ms':[]},
    {'trial_times_ms':[100]},
    {'trial_times_ms':[100,200]},
    {'trial_times_ms':[100,200,300,400]},
    {'trial_times_ms':[100,200,float('nan')]},
    {'trial_times_ms':[100,200,-1]},
    {'trial_times_ms':[100,200,True]},
    {'reaction_time_ms':1},
])
def test_incomplete_or_inconsistent_frontend_values_do_not_publish_a_score(tmp_path,changes):
    data = snapshot()|changes
    with pytest.raises(ValueError):
        save(tmp_path,data)
    assert not (tmp_path/'result.json').exists()


@pytest.mark.skipif(shutil.which('node') is None,reason='Node.js is needed to execute the frontend reader')
@pytest.mark.parametrize('values,phase',[
    ([], 'view-splash'),
    ([500], 'view-result'),
    ([500,501], 'view-waiting'),
    ([500,501], 'view-scold'),
    ([500,501,503], 'view-result'),
    ([500,501,503,1], 'view-result'),
])
def test_frontend_reader_waits_for_three_and_keeps_the_first_three(values,phase):
    # Execute the actual browser expression against a completed-trial React state.
    harness = r"""
    const data = JSON.parse(require('fs').readFileSync(0,'utf8'));
    global.location = {hostname:'humanbenchmark.com',pathname:'/tests/reactiontime',
                       href:'https://humanbenchmark.com/tests/reactiontime'};
    const panel = {innerText:data.values.length ? `${data.values.at(-1)} ms\nClick to keep going` : '',
                   classList:{contains:cls=>cls===data.phase}};
    panel['__reactFiber$test'] = {memoizedState:{memoizedState:{
        trials:data.values.map(result=>({result}))},next:null},return:null};
    global.document = {querySelector:()=>panel};
    process.stdout.write(JSON.stringify(eval(data.expression)));
    """
    output = subprocess.run([shutil.which('node'),'-e',harness],
                            input=json.dumps({'expression':READ_SCORE_JS,'values':values,'phase':phase}),
                            capture_output=True,text=True,check=True)
    result = json.loads(output.stdout)
    assert result['completed']==(len(values)>=3)
    assert result['trial_times_ms']==values[:3]
    assert result['website_completed_trials_at_collection']==len(values)
    assert result['too_soon']==(phase=='view-scold')
    assert result['reaction_time_ms']==(1504/3 if len(values)>=3 else None)


def test_summary_includes_each_trial_and_the_same_mean(tmp_path):
    save(tmp_path/'example'/'agent4',snapshot())
    runner.update_summary(tmp_path)
    row = json.loads((tmp_path/'summary.json').read_text())[0]
    assert row['trial_times_ms']==[500,501,503]
    assert row['reaction_time_ms']==1504/3
    with (tmp_path/'summary.csv').open(encoding='utf-8-sig') as stream:
        csv_row = next(csv.DictReader(stream))
    assert [csv_row[f'trial_{index}_ms'] for index in [1,2,3]]==['500','501','503']
    assert float(csv_row['reaction_time_ms'])==1504/3


@pytest.mark.parametrize('variant',['agent1','agent4'])
def test_session_continues_after_two_scores_and_stops_at_three(tmp_path,monkeypatch,variant):
    import yaml
    config = load_realtime_config(default_config_path(variant,'deepseek-flash'),variant=variant)
    config['system_prompt'] = 'Complete three valid reaction-time trials.'
    config['user_prompt'] = 'This is a simple reaction-time test.'
    config_path = tmp_path/'agent.yaml'
    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv(config['api']['key_env'],'unit-test-placeholder')
    state = {'predictions':0,'closed':False,'sequence_steps':0,'atomic_steps':0,'clock_checks':0}
    readings = iter([snapshot([500]),snapshot([500],too_soon=True),snapshot([500,501]),snapshot()])

    class Agent:
        def __init__(self, **kwargs):
            self.system,self.user_prompt = kwargs['system_prompt_text'],kwargs['user_prompt_text']
            self.sequence,self.frames = kwargs['sequence'],kwargs['frames']
            self.coordinate_system = kwargs['coordinate_system']
            self.wire = Namespace(protocol=kwargs['api_format'],base_url=kwargs['api_base_url'],
                                  session=Namespace(close=lambda:None))
            self.counters = {'model_requests':0}

        def bind_event_sink(self, callback): pass
        def bind_frame_query(self, callback): pass
        def record_action_result(self,*args,**kwargs): pass
        def predict(self,*args):
            state['predictions']+=1
            self.counters['model_requests']+=1
            return '',[{'action_type':'CLICK','parameters':{'x':960,'y':432}}]

    class Env:
        def __init__(self,**kwargs):
            self.vm_ip,self.chromium_port = '127.0.0.1',9222
            self.controller = Namespace(last_observation_time=0,last_capture_interval=[0,0.01],
                start_realtime_recording=self.start_recording,end_realtime_recording=lambda out:None,
                execute_python_command=self.clock_check)
        def clock_check(self,code):
            assert 'timedatectl' in code
            state['clock_checks']+=1
            return {'returncode':0,'output':json.dumps({'status':'synchronized','ntp_synchronized':True})}
        def start_recording(self,interval):
            assert state['clock_checks']==1
            assert state['server_prepared']
            return {}
        def reset(self,**kwargs): pass
        def _get_obs(self):
            self.controller.last_observation_time+=0.1
            return {'screenshot':b'test screenshot'}
        def step(self,*args):
            state['atomic_steps']+=1
            return self._get_obs(),0,False,{}
        def step_sequence(self,*args):
            state['sequence_steps']+=1
            return self._get_obs(),0,False,{}
        def close(self): state['closed']=True

    monkeypatch.setitem(sys.modules,'desktop_env.desktop_env',Namespace(DesktopEnv=Env))
    monkeypatch.setitem(sys.modules,'lib_realtime_trajectory',Namespace(render_trajectory=lambda out:None))
    monkeypatch.setattr(runner,'RealtimeAgent',Agent)
    monkeypatch.setattr(runner,'prepare_realtime_server',lambda env:state.update(server_prepared=True))
    monkeypatch.setattr(runner,'BrowserProxyBridge',lambda env,proxy:Namespace(url=None,close=lambda:None))
    monkeypatch.setattr(runner,'WebsiteScoreReader',lambda *args:Namespace(ready=lambda:True,read=lambda:next(readings)))
    args = Namespace(prompt_key=False,api_base_url='https://example.test',api_proxy='direct',
                     run_id='three_trials',kind='smoke',browser_proxy=None,max_steps=10,
                     max_duration_s=60,path_to_vm=tmp_path/'unused.qcow2')
    root = tmp_path/'results'
    runner.run_group(config_path,variant,root,args)
    out = root/'deepseek-flash'/variant
    result = json.loads((out/'result.json').read_text())
    assert state['predictions']==4
    assert state['sequence_steps' if variant=='agent4' else 'atomic_steps']==4
    assert state['closed']
    assert state['clock_checks']==1
    assert state['server_prepared']
    assert result['trial_times_ms']==[500,501,503]
    assert result['reaction_time_ms']==1504/3
    assert result['premature_clicks']==1
    assert json.loads((out/'experiment.json').read_text())['valid_trials']==3
    # Resuming a completed group spends no new model requests.
    runner.run_group(config_path,variant,root,args)
    assert state['predictions']==4
    # The old single-trial run must never be treated as a completed three-trial run.
    (out/'result.json').write_text(json.dumps({'measurement':'first_valid_trial','valid_trials':1}))
    with pytest.raises(RuntimeError,match='new run_id'):
        runner.run_group(config_path,variant,root,args)
    assert state['predictions']==4


@pytest.mark.parametrize('response',[
    None,
    {'returncode':1,'output':'','error':'clock service unavailable'},
    {'returncode':0,'output':json.dumps({'status':'timeout','ntp_synchronized':False})},
])
def test_startup_clock_failure_does_not_allow_measurement(response):
    env=Namespace(controller=Namespace(execute_python_command=lambda code:response))
    with pytest.raises(RuntimeError,match='clock'):
        wait_for_startup_clock(env)


def test_browser_uses_original_site_with_requested_proxy_and_exact_task_prompt():
    prompt = 'This is a simple reaction-time test.'
    task = browser_task(prompt,'http://10.200.0.1:17898')
    assert task['instruction']==prompt
    assert task['config'][0]['parameters']['command'][-1]=='--proxy-server=http://10.200.0.1:17898'
    assert task['config'][2]['parameters']['urls_to_open']==['https://humanbenchmark.com/tests/reactiontime']
    assert not any(step['type'] in {'upload_file','execute'} for step in task['config'])


def test_company_route_is_preserved_unless_explicitly_overridden():
    company = {'api_base_url':'https://company.example/v1','api_proxy':'socks5h://127.0.0.1:18888'}
    resolved = group_transport(Namespace(api_base_url=None,api_proxy=None),company)
    assert resolved.api_base_url=='https://company.example/v1'
    assert resolved.api_proxy=='socks5h://127.0.0.1:18888'
    resolved = group_transport(Namespace(api_base_url='https://other.example',api_proxy='direct'),company)
    assert resolved.api_base_url=='https://other.example'
    assert resolved.api_proxy=='direct'

def test_shipped_reactiontime_configs_share_prompt_and_plan_is_portable():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    directory = root / 'configs/humanbenchmark_reactiontime'
    plan = json.loads((directory / 'trace_models.json').read_text())
    assert plan['valid_trials_per_group'] == 3 and len(plan['groups']) == 8
    prompts = set()
    for group in plan['groups']:
        config_path = Path(group['config'])
        assert not config_path.is_absolute()
        assert set(group) == {'model', 'agent', 'config'}
        config = load_realtime_config(root / config_path, variant=group['agent'])
        assert config['api']['model'] == group['model']
        prompts.add(config['user_prompt'])
    assert len(prompts) == 1
    for variant, agent_id in [('agent1', 'vanilla'), ('agent2', 'anticipatory'), ('agent3', 'video')]:
        config = load_realtime_config(directory / (agent_id + '-deepseek-flash.yaml'), variant=variant)
        assert config['user_prompt'] in prompts
