"""Collect three Human Benchmark reaction times and prepare its browser connection."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit, urlunsplit

import requests

URL = 'https://humanbenchmark.com/tests/reactiontime'
TRIAL_COUNT = 3
CLOCK_SYNC_SCRIPT = r"""
import json,subprocess,time
started=time.monotonic()
deadline=started+__TIMEOUT_S__
checks=0
last_sync=None
while time.monotonic()<deadline:
    query=subprocess.run(['timedatectl','show','-p','NTPSynchronized','--value'],
                         capture_output=True,text=True,timeout=3)
    checks+=1
    last_sync=query.stdout.strip()
    if query.returncode!=0:
        print(json.dumps({'status':'error','message':query.stderr.strip()}))
        break
    if last_sync!='yes':
        time.sleep(.25)
        continue
    offsets=[]
    for _ in range(21):
        offsets.append(time.time_ns()-time.monotonic_ns())
        if len(offsets)<21:time.sleep(.1)
    spread_s=(max(offsets)-min(offsets))/1e9
    if spread_s<=.01:
        print(json.dumps({'status':'synchronized','ntp_synchronized':True,
                          'waited_s':time.monotonic()-started,'sync_checks':checks,
                          'observation_s':2,'wall_minus_monotonic_spread_s':spread_s}))
        break
else:
    print(json.dumps({'status':'timeout','ntp_synchronized':last_sync=='yes',
                      'waited_s':time.monotonic()-started}))
"""


def wait_for_startup_clock(env, timeout_s=60):
    """Let the guest finish its initial NTP correction before starting measurements."""
    response=env.controller.execute_python_command(CLOCK_SYNC_SCRIPT.replace('__TIMEOUT_S__',str(float(timeout_s))))
    if not response or response.get('returncode')!=0:
        raise RuntimeError('Could not inspect the VM startup clock: '+str(response))
    check=json.loads(response['output'])
    if check.get('status')!='synchronized' or not check.get('ntp_synchronized'):
        raise RuntimeError('VM startup clock is not ready: '+json.dumps(check))
    return check


READ_SCORE_JS = r"""
(() => {
  if (location.hostname !== 'humanbenchmark.com' || location.pathname !== '/tests/reactiontime') {
    throw new Error('Unexpected reaction-time page');
  }
  const panel = document.querySelector('.view-splash,.view-waiting,.view-go,.view-result,.view-scold,.view-score');
  if (!panel) throw new Error('Reaction-time test panel is missing');
  let results = null;
  const key = Object.keys(panel).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
  let fiber = key ? panel[key] : null;
  for (let depth=0; fiber && depth<40; depth++, fiber=fiber.return) {
    for (const candidate of [fiber,fiber.alternate].filter(Boolean)) {
      let hook = candidate.memoizedState;
      for (let i=0; hook && i<40; i++,hook=hook.next) {
        const state = hook.memoizedState;
        if (state && Array.isArray(state.trials)) {
          const values = state.trials.map(t => t.result);
          if (values.every(v => typeof v==='number' && Number.isFinite(v) && v>=0)) {
            if (results===null || values.length>results.length) results=values;
            break;
          }
        }
      }
    }
  }
  if (results===null) throw new Error('Frontend completed-trial records are unavailable');
  const text = panel.innerText;
  if (panel.classList.contains('view-result')) {
    const match=text.match(/([0-9]+)\s*ms\b/);
    if (!match || Number(match[1])!==results[results.length-1]) {
      throw new Error('Displayed trial time disagrees with the frontend record');
    }
  }
  const trials = results.slice(0,__TRIAL_COUNT__);
  const completed = trials.length===__TRIAL_COUNT__;
  return {completed,url:location.href,page_score_text:text,
          too_soon:panel.classList.contains('view-scold'),
          reaction_time_ms:completed ? trials.reduce((sum,value)=>sum+value,0)/__TRIAL_COUNT__ : null,
          trial_times_ms:trials,website_completed_trials_at_collection:results.length,
          score_source:'Human Benchmark completed frontend trials'};
})()
""".replace('__TRIAL_COUNT__',str(TRIAL_COUNT))


def _evaluate_cdp_expression(address, expression, timeout):
    from desktop_env.evaluators.getters.realtime_gui import _evaluate_cdp_expression as evaluate
    return evaluate(address,expression,timeout)


class WebsiteScoreReader:
    """Read completed trials only; the runner never adds evaluator data to model inputs."""
    def __init__(self, vm_ip, chromium_port):
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(f'http://{vm_ip}:{chromium_port}/json/list',timeout=10)
            response.raise_for_status()
            pages = [p for p in response.json() if p.get('type')=='page'
                     and p.get('url','').split('?')[0].rstrip('/')==URL]
        if len(pages)!=1:
            raise RuntimeError(f'Expected one reaction-time page, found {len(pages)}')
        target = urlsplit(pages[0]['webSocketDebuggerUrl'])
        self.address = urlunsplit((target.scheme,f'{vm_ip}:{chromium_port}',target.path,target.query,target.fragment))

    def ready(self):
        return _evaluate_cdp_expression(self.address,
            "location.href.startsWith('https://humanbenchmark.com/tests/reactiontime') && !!document.querySelector('.view-splash')",10)

    def read(self):
        return _evaluate_cdp_expression(self.address,READ_SCORE_JS,10)


def read_completed_scores(vm_ip, chromium_port):
    return WebsiteScoreReader(vm_ip,chromium_port).read()


def save_completed_result(output, snapshot, *, model, agent, run_id, kind, premature_clicks=None):
    if not snapshot.get('completed'):
        raise ValueError('The website has not recorded three valid reaction times')
    values = snapshot['trial_times_ms']
    if len(values)!=TRIAL_COUNT or any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in values):
        raise ValueError('Expected three valid frontend reaction times')
    mean = sum(values)/TRIAL_COUNT
    if type(snapshot['reaction_time_ms']) not in (int,float) or snapshot['reaction_time_ms']!=mean:
        raise ValueError('Reaction time disagrees with the mean of the three frontend trials')
    out = Path(output)
    out.mkdir(parents=True,exist_ok=True)
    result = {'schema':'humanbenchmark-reactiontime/2.0','kind':kind,'run_id':run_id,
              'model':model,'agent':agent,'completed_at':datetime.now(timezone.utc).isoformat(),
              **snapshot,'measurement':'mean_of_three_valid_trials','valid_trials':TRIAL_COUNT,
              'premature_clicks':premature_clicks}
    target = out/'result.json'
    if target.exists():
        raise FileExistsError(target)
    temporary = out/'result.json.tmp'
    temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(target)
    return result


def browser_task(instruction, proxy=None):
    command = ['google-chrome','--remote-debugging-port=1337','--disable-features=OutdatedBuildDetector']
    if proxy:
        command.append('--proxy-server='+proxy)
    return {'id':'humanbenchmark_reactiontime','snapshot':'chrome','instruction':instruction,
            'config':[
                {'type':'launch','parameters':{'command':command}},
                {'type':'launch','parameters':{'command':['socat','tcp-listen:9222,fork','tcp:localhost:1337']}},
                {'type':'chrome_open_tabs','parameters':{'urls_to_open':[URL]}},
                {'type':'activate_window','parameters':{'window_name':'Google Chrome'}},
                {'type':'sleep','parameters':{'seconds':3}}],
            'evaluator':{'func':'exact_match','result':{}},'proxy':False}


class BrowserProxyBridge:
    """Make a host loopback proxy reachable from this disposable Docker VM."""
    def __init__(self, env, proxy):
        self.server = self.thread = None
        self.url = proxy
        if not proxy:
            return
        parsed = urlsplit(proxy)
        if parsed.hostname not in {'localhost','127.0.0.1'}:
            return
        if parsed.username or parsed.password or parsed.scheme not in {'http','socks5'} or not parsed.port:
            raise ValueError('Use an HTTP or SOCKS5 proxy URL without embedded credentials')
        env.provider.container.reload()
        networks = env.provider.container.attrs['NetworkSettings']['Networks']
        gateway = next(n['Gateway'] for n in networks.values() if n.get('Gateway'))
        upstream = (parsed.hostname,parsed.port)

        class Relay(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    with socket.create_connection(upstream,timeout=10) as remote:
                        remote.settimeout(None)
                        peers = [self.request,remote]
                        while True:
                            ready,_,_ = select.select(peers,[],[],60)
                            if not ready:
                                return
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                (remote if source is self.request else self.request).sendall(data)
                except OSError:
                    pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

        self.server = Server((gateway,0),Relay)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url = f'{parsed.scheme}://{gateway}:{self.server.server_address[1]}'

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
