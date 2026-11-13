"""TRACE observer tool schemas, Jev loop and VM Python lifecycle bridge."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid

import requests
from PIL import Image

from mm_agents.realtime_protocol import ACTION_TOOL_TYPES, validate_action_sequence

OBSERVER_ACTIONS = {'create_observer': 'OBSERVER_CREATE', 'stop_observer': 'OBSERVER_STOP'}

JEV_DESCRIPTION = '''Start a named background observer driven by Jev.
Provide name and cases: visual conditions mapped to sequences of existing GUI actions.
An empty sequence means no action. The runtime repeatedly captures a fresh screenshot,
asks Jev to select a condition, and executes its actions. NONE performs no action.
Selecting the same condition on successive iterations executes its actions each time.
Use your registered GUI tools' action parameters and coordinate convention; the runtime
converts coordinates to native screen pixels. Jev selects cases, not coordinates.
This tool returns after startup. The observer runs while you think or use other tools
until stopped or the task ends. Status and errors are reported in subsequent feedback.
Stop an active observer before creating a replacement with the same name.'''

SCRIPT_DESCRIPTION = '''Run a named Python observer in the background inside the game VM.
Provide name and code, the complete Python script. Use pyautogui.screenshot() to obtain
a Pillow Image and PyAutoGUI mouse/keyboard functions to operate the game.
PyAutoGUI coordinates are native 1920x1080 screen pixels, with origin at the top-left.
Python computation/timing, Pillow and NumPy are available; import the libraries you use.
Write the script's conditions, actions, loops and exit behavior yourself.
Syntax errors are returned without starting the script. After startup this tool returns
immediately; the script continues while you think or use other tools. Uncaught exceptions
and exit status are reported in subsequent feedback. The observer ends when its script
exits, stop_observer is called, or the task ends. Scripts are not automatically rewritten
or restarted. Observe the game through screenshots and use mouse/keyboard GUI interaction.
Stop an active observer before creating a replacement with the same name.'''


def observer_tools(mode, action_tools):
    if mode == 'none':
        return []
    props = {'name': {'type': 'string', 'minLength': 1, 'maxLength': 128}}
    if mode == 'jev':
        choices = []
        for tool in action_tools:
            choices.append({'type': 'object', 'properties': {
                'action_type': {'type': 'string', 'enum': [ACTION_TOOL_TYPES[tool['name']]]},
                'parameters': copy.deepcopy(tool['parameters'])},
                'required': ['action_type', 'parameters'], 'additionalProperties': False})
        props['cases'] = {'type': 'object', 'minProperties': 1, 'additionalProperties': {
            'type': 'array', 'maxItems': 100, 'items': {'anyOf': choices}}}
    elif mode == 'script':
        props['code'] = {'type': 'string', 'minLength': 1}
    else:
        raise ValueError('Unknown observer mode')
    return [
        {'name': 'create_observer', 'description': JEV_DESCRIPTION if mode == 'jev' else SCRIPT_DESCRIPTION,
         'parameters': {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}},
        {'name': 'stop_observer', 'description': 'Stop the named observer. No later results may trigger actions. Release its held input. Return stopped or its existing final status.',
         'parameters': {'type': 'object', 'properties': {'name': props['name']}, 'required': ['name'], 'additionalProperties': False}},
    ]


def decode_observer_call(call, mode, coordinates):
    value = call.get('arguments', {})
    value = json.loads(value) if isinstance(value, str) else copy.deepcopy(value)
    field = 'cases' if mode == 'jev' else 'code'
    expected = {'name'} if call['name'] == 'stop_observer' else {'name', field}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('Observer parameters must be exactly ' + ', '.join(sorted(expected)))
    if not isinstance(value['name'], str) or not 1 <= len(value['name'].strip()) <= 128:
        raise ValueError('Observer name must contain 1-128 characters')
    if 'cases' in value:
        cases = value['cases']
        if not isinstance(cases, dict) or not cases:
            raise ValueError('cases must map visual descriptions to action lists')
        for condition, actions in cases.items():
            if not isinstance(condition, str) or not condition.strip() or not isinstance(actions, list) or len(actions) > 100:
                raise ValueError('Each case needs a nonempty description and at most 100 actions')
            if any(not isinstance(a, dict) or set(a) != {'action_type', 'parameters'} for a in actions):
                raise ValueError('Each action requires action_type and parameters')
            native = [coordinates.to_native_action(a) for a in actions]
            validate_action_sequence(native)
            if any(a['action_type'] == 'DONE' for a in native[:-1]):
                raise ValueError('DONE must be the last action')
            cases[condition] = native
    if 'code' in value and (not isinstance(value['code'], str) or not value['code'].strip()):
        raise ValueError('code must be a nonempty Python script')
    return {'action_type': OBSERVER_ACTIONS[call['name']], 'parameters': value}


class ObserverManager:
    def __init__(self, env, mode, output, emit, jev_config=None, classifier=None):
        self.env, self.mode, self.out, self.emit = env, mode, Path(output), emit
        self.out.mkdir(parents=True, exist_ok=True)
        self.base = env.controller.http_server
        self.session_id = env.controller.realtime_session
        self.cfg = dict(jev_config or {})
        self.classifier = classifier
        self.feedback = queue.Queue()
        self.threads, self.stops, self.names = {}, {}, {}
        self.cursor, self.closed, self.fatal = 0, False, None
        self.poll_stop = threading.Event()
        self.terminal = threading.Event()
        self.poll_lock = threading.Lock()
        self.event_lock = threading.Lock()
        self.local = threading.local()
        if mode == 'jev' and classifier is None:
            key_env = self.cfg.get('key_env', 'JEV_API_KEY')
            self.key = os.environ.get(key_env)
            if not self.key:
                raise ValueError(key_env + ' is required for the Jev observer')
        self.rpc('POST', '/observers/configure', {'mode': mode})
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()

    def rpc(self, method, path, data=None):
        payload = {'session_id': self.session_id, **(data or {})}
        # VM traffic must never inherit a model API proxy; do not share Sessions across threads.
        if not hasattr(self.local, 'session'):
            self.local.session = requests.Session()
            self.local.session.trust_env = False
        kwargs = {'params' if method == 'GET' else 'json': payload}
        response = self.local.session.request(method, self.base + '/realtime' + path, timeout=130, **kwargs)
        response.raise_for_status()
        return response.json()

    def record(self, event):
        with self.event_lock:
            self.emit(event)

    def download(self, relative):
        target = self.out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(self.base + '/realtime/observers/artifact',
                                params={'session_id': self.session_id, 'path': relative},
                                proxies={'http': '', 'https': ''}, timeout=30)
        response.raise_for_status()
        target.write_bytes(response.content)

    def poll(self):
        with self.poll_lock:
            body = self.rpc('GET', '/observers/events', {'cursor': self.cursor})
            for event in body['events']:
                if event.get('screenshot_file'):
                    self.download(event['screenshot_file'])
                self.record(event)
                if event['event'] in {'observer_failed', 'observer_completed'}:
                    self.feedback.put({k: v for k, v in event.items() if k not in {'task_time_s', 'vm_monotonic_s'}})
            self.cursor = body['cursor']
            return body

    def _poll_loop(self):
        while not self.poll_stop.wait(.2):
            try:
                self.poll()
            except Exception as exc:
                self.fatal = type(exc).__name__ + ': ' + str(exc)
                self.feedback.put({'event': 'observer_infrastructure_error', 'message': self.fatal})
                return

    def drain_feedback(self):
        if self.fatal:
            raise RuntimeError('Observer VM/log connection failed: ' + self.fatal)
        values = []
        while True:
            try:
                values.append(self.feedback.get_nowait())
            except queue.Empty:
                return values

    def create(self, parameters):
        result = self.rpc('POST', '/observers/create', parameters if self.mode == 'script' else {'name': parameters['name']})
        if result.get('id'):
            ident = result['id']
            root = self.out / 'observers' / ident
            root.mkdir(parents=True, exist_ok=True)
            (root / ('code.py' if self.mode == 'script' else 'cases.json')).write_text(
                parameters['code'] if self.mode == 'script' else json.dumps(parameters['cases'], ensure_ascii=False, indent=2))
            self.names[parameters['name']] = ident
            if self.mode == 'jev' and result['status'] == 'running':
                stop = self.stops[ident] = threading.Event()
                thread = threading.Thread(target=self._jev_loop, args=(ident, parameters, stop), daemon=True)
                self.threads[ident] = thread
                thread.start()
        return result

    def stop(self, name):
        ident = self.names.get(name)
        if ident in self.stops:
            self.stops[ident].set()
        return self.rpc('POST', '/observers/stop', {'name': name})

    def classify(self, screenshot, criteria):
        if self.classifier:
            return self.classifier(screenshot, criteria)
        maximum = self.cfg.get('image_max_side', 1568)
        im = Image.open(io.BytesIO(screenshot)).convert('RGB')
        original = im.size
        im.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=85)
        ref = str(uuid.uuid4())
        body = {'model': self.cfg.get('model', 'jev-1.14.0'),
                'state': {'screenshot': ref, 'original_size': list(original), 'sent_size': list(im.size),
                          'condition_coordinate_system': self.cfg.get('condition_coordinate_system', 'native_pixels')},
                'assets': {ref: {'type': 'image', 'format': 'jpeg', 'data_base64': base64.b64encode(buf.getvalue()).decode()}},
                'questions': {'case': {'type': 'choice', 'instructions':
                    'Examine the current screenshot and select the matching visual condition. Judge only this screenshot. '
                    'Use NONE if no condition matches or the image is insufficient. Coordinate references in conditions '
                    'use condition_coordinate_system on the original screen; sent_size describes image resizing.',
                    'criteria': {**criteria, 'NONE': 'No supplied condition matches, or the screenshot is insufficient to choose.'}}}}
        if not hasattr(self.local, 'jev_session'):
            self.local.jev_session = requests.Session()
            self.local.jev_session.trust_env = False
            proxy = os.environ.get('JEV_PROXY')
            if proxy:
                self.local.jev_session.proxies.update(http=proxy, https=proxy)
        base = os.environ.get('JEV_BASE_URL', self.cfg.get('base_url', 'https://www.packyapi.ai')).rstrip('/')
        url = base + ('/systemone' if base.endswith('/v1') else '/v1/systemone')
        response = self.local.jev_session.post(url,
                                    headers={'Authorization': 'Bearer ' + self.key, 'TypeSafe-Danger-Zone-Images': 'true'},
                                    json=body, timeout=self.cfg.get('timeout_s', 30))
        response.raise_for_status()
        return response.json()

    def _jev_loop(self, ident, parameters, stop):
        from desktop_env.controllers.python import PythonController, PYAUTOGUI_PKGS_PREFIX
        cases = list(parameters['cases'].items())
        criteria = {f'C{i}': condition for i, (condition, _) in enumerate(cases)}
        index = 0
        try:
            while not stop.is_set() and not self.terminal.is_set():
                shot = self.rpc('GET', '/observation')
                if stop.is_set():
                    break
                image = base64.b64decode(shot.pop('image'))
                index += 1
                relative = f'observers/{ident}/jev_{index:06d}.png'
                (self.out / relative).write_bytes(image)
                began = time.monotonic()
                self.record({'event': 'observer_jev_request', 'observer_id': ident, 'name': parameters['name'],
                             'screenshot_file': relative, 'observation': shot, 'criteria': criteria,
                             'request_monotonic_s': began})
                raw = self.classify(image, criteria)
                answer = raw.get('answers', {}).get('case', {})
                choice = answer.get('choice')
                self.record({'event': 'observer_jev_response', 'observer_id': ident, 'name': parameters['name'],
                             'choice': choice, 'response': raw, 'latency_s': time.monotonic() - began,
                             'ignored_after_stop': stop.is_set()})
                if stop.is_set():
                    break
                if choice == 'NONE':
                    continue
                if choice not in criteria:
                    raise ValueError('Jev returned an unknown case')
                actions = cases[int(choice[1:])][1]
                if not actions:
                    continue
                collector = object.__new__(PythonController)
                groups = []
                for action in actions:
                    commands = []
                    collector.execute_python_command = lambda code: commands.append(PYAUTOGUI_PKGS_PREFIX.format(command=code))
                    collector.execute_action(action)
                    groups.append({'action': action, 'commands': commands})
                result = self.rpc('POST', '/sequence', {'groups': groups, 'observer_id': ident})
                if result.get('done'):
                    self.terminal.set()
        except Exception as exc:
            if not stop.is_set():
                event = {'event': 'observer_failed', 'name': parameters['name'], 'observer_id': ident,
                         'error': {'error_type': type(exc).__name__, 'message': str(exc)}}
                try:
                    self.rpc('POST', '/observers/stop', {'observer_id': ident, 'error': event['error']})
                except Exception as cleanup_error:
                    self.record(event)
                    self.feedback.put(event)
                    self.fatal = 'Cannot stop failed Jev observer: ' + str(cleanup_error)
        finally:
            if hasattr(self.local, 'jev_session'):
                self.local.jev_session.close()

    def execute(self, actions):
        results, sequence_actions = {}, []
        done = self.terminal.is_set()
        index = 0
        while index < len(actions) and not done:
            action = actions[index]
            if action['action_type'] in OBSERVER_ACTIONS.values():
                params = action['parameters']
                result = self.create(params) if action['action_type'] == 'OBSERVER_CREATE' else self.stop(params['name'])
                results[index] = result
                self.record({'event': 'observer_tool_result', 'action': action, 'result': result})
                index += 1
            else:
                end = index + 1
                while end < len(actions) and actions[end]['action_type'] not in OBSERVER_ACTIONS.values():
                    end += 1
                _, _, done, info = self.env.step_sequence(actions[index:end])
                sequence_actions.extend(info.get('sequence_actions', []))
                index = end
            done = done or self.terminal.is_set()
        for skipped in range(index, len(actions)):
            results[skipped] = {'status': 'not_executed', 'reason': 'task_done'}
        self.poll()
        obs = self.env._get_obs()
        return obs, 0, done, {'observer_results': results, 'sequence_actions': sequence_actions}

    def close(self):
        if self.closed:
            return
        for stop in self.stops.values():
            stop.set()
        try:
            self.rpc('POST', '/observers/stop_all')
            for thread in self.threads.values():
                thread.join(timeout=self.cfg.get('timeout_s', 30) + 5)
                if thread.is_alive():
                    raise RuntimeError('Jev worker did not finish after cancellation')
        finally:
            self.poll_stop.set()
            self.poll_thread.join(timeout=140)
        body = self.poll()
        for job in body['observers']:
            if job['mode'] == 'script':
                for name in ('stdout.log', 'stderr.log'):
                    self.download(f"observers/{job['id']}/{name}")
        (self.out / 'observer_summary.json').write_text(json.dumps(body['observers'], ensure_ascii=False, indent=2))
        self.closed = True
