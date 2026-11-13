"""Task-scoped observer processes, recorded PyAutoGUI calls and lifecycle APIs."""
import base64
from contextlib import contextmanager
import inspect
import io
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from flask import jsonify, request, send_file

INPUT_METHODS = {
    'moveTo', 'moveRel', 'dragTo', 'dragRel', 'click', 'doubleClick',
    'tripleClick', 'rightClick', 'middleClick', 'leftClick', 'mouseDown',
    'mouseUp', 'scroll', 'hscroll', 'vscroll', 'press', 'keyDown', 'keyUp',
    'hotkey', 'write', 'typewrite', 'position', 'size',
}
BLOCKED_KEYS = {'f5', 'f12', 'tab', 'enter', 'return', 'esc', 'escape',
                'browserrefresh', 'browserback', 'browserforward', 'browserhome'}
BLOCKED_CHORDS = [set(x.split('+')) for x in (
    'ctrl+r', 'ctrl+shift+r', 'ctrl+shift+i', 'ctrl+shift+j', 'ctrl+shift+c',
    'ctrl+u', 'ctrl+l', 'ctrl+s', 'ctrl+p', 'alt+left', 'alt+right',
)]


class ObserverRuntime:
    version = 1

    def __init__(self, recorder, pyautogui, input_lock, main_keys, main_buttons):
        self.recorder, self.gui, self.input_lock = recorder, pyautogui, input_lock
        self.main_keys, self.main_buttons = main_keys, main_buttons
        self.mode = 'none'
        self.jobs = {}
        self.events = []
        self.lock = threading.RLock()

    @property
    def enabled(self):
        return self.mode != 'none'

    def event(self, kind, job=None, **fields):
        with self.lock:
            record = {'event': kind, 'task_time_s': self.recorder.elapsed(),
                      'vm_monotonic_s': time.monotonic(), **fields}
            if job:
                record.update(name=job['name'], observer_id=job['id'], mode=job['mode'])
            record['observer_event_index'] = len(self.events) + 1
            self.events.append(record)
            with (self.recorder.path / 'observer_events.jsonl').open('a') as out:
                out.write(json.dumps(record, ensure_ascii=False) + '\n')
            return record

    def configure(self, mode):
        if mode not in {'jev', 'script', 'none'}:
            raise ValueError('Unknown observer mode')
        self.stop_all()
        with self.lock:
            self.jobs, self.events, self.mode = {}, [], mode
        return {'status': 'ok', 'mode': mode, 'version': self.version}

    def guard(self, instance):
        with self.lock:
            job = self.jobs.get(instance)
            if not job or job['status'] != 'running':
                raise ValueError('Observer is not running')
            return job

    def snapshot(self, job):
        return {**{k: job[k] for k in ('name', 'id', 'mode', 'status', 'error', 'returncode') if k in job},
                'held_keys': sorted(job['keys']), 'held_buttons': sorted(job['buttons'], key=str)}

    def create(self, name, code=None, url=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError('name must contain 1-128 characters')
        self.refresh()
        with self.lock:
            if any(j['name'] == name and j['status'] == 'running' for j in self.jobs.values()):
                return {'status': 'error', 'name': name, 'message': 'Stop the active observer before replacing it.'}
            if self.mode == 'none' or (self.mode == 'script') != (code is not None):
                raise ValueError('Observer implementation does not match the configured mode')
            ident = uuid.uuid4().hex
            root = self.recorder.path / 'observers' / ident
            root.mkdir(parents=True)
            job = {'name': name, 'id': ident, 'mode': self.mode, 'status': 'running',
                   'root': root, 'process': None, 'keys': set(), 'buttons': set()}
            if code is not None:
                if not isinstance(code, str) or not code.strip():
                    raise ValueError('code must be a nonempty Python script')
                (root / 'code.py').write_text(code)
                (root / 'stdout.log').touch()
                (root / 'stderr.log').touch()
                try:
                    compile(code, str(root / 'code.py'), 'exec')
                except SyntaxError as exc:
                    job.update(status='failed', error={'error_type': type(exc).__name__,
                               'message': exc.msg, 'line': exc.lineno})
                    self.jobs[ident] = job
                    self.event('observer_failed', job, error=job['error'], phase='syntax')
                    return self.snapshot(job)
                # Credentials for model APIs are never passed to the VM worker.
                (root / 'runtime.json').write_text(json.dumps({
                    'url': url, 'session_id': self.recorder.session_id, 'observer_id': ident}))
                with (root / 'stdout.log').open('w') as stdout, (root / 'stderr.log').open('w') as stderr:
                    job['process'] = subprocess.Popen(
                        [sys.executable, str(Path(__file__).with_name('observer_worker.py')), str(root)],
                        stdout=stdout, stderr=stderr, start_new_session=True)
            self.jobs[ident] = job
            self.event('observer_started', job)
            return self.snapshot(job)

    def _release(self, job):
        # Call with the input lock, so stopping cannot race an input injection.
        others = [j for j in self.jobs.values() if j is not job and j['status'] == 'running']
        keys = self.main_keys | set().union(*(j['keys'] for j in others))
        buttons = self.main_buttons | set().union(*(j['buttons'] for j in others))
        for key in job['keys'] - keys:
            self.gui.keyUp(key)
        for button in job['buttons'] - buttons:
            self.gui.mouseUp(button=button)
        job['keys'].clear()
        job['buttons'].clear()

    def refresh(self):
        # Polling must not wait behind a long GUI call from a still-running job.
        with self.lock:
            finished = any(j['status'] == 'running' and j['process'] is not None
                           and j['process'].poll() is not None for j in self.jobs.values())
        if not finished:
            return
        with self.input_lock, self.lock:
            for job in self.jobs.values():
                proc = job['process']
                if job['status'] != 'running' or proc is None or proc.poll() is None:
                    continue
                job['returncode'] = proc.returncode
                job['status'] = 'completed' if proc.returncode == 0 else 'failed'
                error_path = job['root'] / 'error.json'
                if job['status'] == 'failed':
                    if error_path.exists():
                        job['error'] = json.loads(error_path.read_text())
                    else:
                        job['error'] = {'error_type': 'ProcessExit', 'message': (job['root'] / 'stderr.log').read_text()[-8000:]}
                self._release(job)
                self.event('observer_' + job['status'], job, returncode=proc.returncode, error=job.get('error'))

    def stop(self, name=None, instance=None, error=None):
        with self.lock:
            matches = [j for j in self.jobs.values() if j['id'] == instance] if instance else [j for j in self.jobs.values() if j['name'] == name]
            if not matches:
                return {'name': name, 'status': 'not_found'}
            job = matches[-1]
            if job['status'] != 'running':
                return self.snapshot(job)
            job['status'] = 'failed' if error else 'stopped'
            if error:
                job['error'] = error
            proc = job['process']
        # Invalidate before waiting for input_lock: an in-flight GUI sleep checks
        # this state, and a late classifier response must not inject an action.
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
        with self.input_lock, self.lock:
            self._release(job)
            self.event('observer_' + job['status'], job, error=job.get('error'))
            return self.snapshot(job)

    def stop_all(self):
        for job in list(self.jobs.values()):
            self.stop(instance=job['id'])

    def _keys(self, method, args, kwargs):
        values = []
        if method in {'keyDown', 'keyUp', 'press'}:
            value = args[0] if args else kwargs.get('key', kwargs.get('keys', []))
            values = [value] if isinstance(value, str) else list(value)
        elif method == 'hotkey':
            values = list(args)
        elif method in {'write', 'typewrite'}:
            value = args[0] if args else kwargs.get('message', '')
            values = list(value)
        return {str(v).lower() for v in values}

    @contextmanager
    def gui_settings(self, job, pause=0, failsafe=False):
        """Called under input_lock; cancel PyAutoGUI's own pauses on stop."""
        old_pause, old_failsafe = self.gui.PAUSE, self.gui.FAILSAFE
        original_time = getattr(self.gui, 'time', None)
        runtime = self
        class ObserverClock:
            def __getattr__(self, key):
                return getattr(original_time, key)
            def sleep(self, duration):
                deadline = time.monotonic() + max(0, duration)
                while True:
                    runtime.guard(job['id'])
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    time.sleep(min(.05, remaining))
        self.gui.PAUSE, self.gui.FAILSAFE = pause, failsafe
        if job and original_time is not None:
            self.gui.time = ObserverClock()
        try:
            yield
        finally:
            self.gui.PAUSE, self.gui.FAILSAFE = old_pause, old_failsafe
            if original_time is not None:
                self.gui.time = original_time

    def call(self, data):
        ident, method = data['observer_id'], data['method']
        args, kwargs = data.get('args', []), data.get('kwargs', {})
        if method not in INPUT_METHODS | {'screenshot'}:
            raise ValueError('Unsupported PyAutoGUI method')
        if method == 'screenshot':
            job = self.guard(ident)
            if args or kwargs.get('imageFilename') is not None or set(kwargs) - {'region'}:
                raise ValueError('Use screenshot() or screenshot(region=...); images are saved by the runtime')
            started = self.recorder.elapsed()
            im = self.gui.screenshot(**kwargs)
            buf = io.BytesIO()
            im.save(buf, format='PNG')
            rel = 'observers/' + ident + '/frame_' + uuid.uuid4().hex + '.png'
            (self.recorder.path / rel).write_bytes(buf.getvalue())
            self.event('observer_screenshot', job, screenshot_file=rel,
                       capture_started_s=started, capture_finished_s=self.recorder.elapsed(),
                       region=kwargs.get('region'))
            return {'image': base64.b64encode(buf.getvalue()).decode()}
        with self.input_lock:
            job = self.guard(ident)
            keys = self._keys(method, args, kwargs)
            held = self.main_keys | set().union(*(j['keys'] for j in self.jobs.values()))
            aliases = {'ctrlleft': 'ctrl', 'ctrlright': 'ctrl', 'shiftleft': 'shift',
                       'shiftright': 'shift', 'altleft': 'alt', 'altright': 'alt'}
            combined = {aliases.get(k, k) for k in keys | held}
            if method != 'keyUp' and (keys & BLOCKED_KEYS or keys & {'\t', '\n', '\r'} or any(c <= combined for c in BLOCKED_CHORDS)):
                self.event('observer_action_rejected', job, method=method, args=args, kwargs=kwargs)
                raise ValueError('Refreshing, browser shortcuts, Tab, Enter and Escape are not game controls')
            pause = data.get('pause', 0)
            if not isinstance(pause, (int, float)) or not math.isfinite(pause) or pause < 0:
                raise ValueError('Invalid PyAutoGUI pause')
            started = self.recorder.elapsed()
            prior_keys, prior_buttons = job['keys'].copy(), job['buttons'].copy()
            # Track keys before injection so cancellation halfway through hotkey
            # or press can release them as well as explicit keyDown calls.
            if method in {'keyDown', 'press', 'hotkey', 'write', 'typewrite'}:
                job['keys'].update(keys)
            if method in {'mouseDown', 'click', 'doubleClick', 'tripleClick',
                          'leftClick', 'rightClick', 'middleClick', 'dragTo', 'dragRel'}:
                bound = inspect.signature(getattr(self.gui, method)).bind_partial(*args, **kwargs)
                default_button = {'rightClick': 'right', 'middleClick': 'middle'}.get(method, 'left')
                job['buttons'].add(bound.arguments.get('button', kwargs.get('button', default_button)))
            try:
                with self.gui_settings(job, pause, bool(data.get('failsafe', False))):
                    value = getattr(self.gui, method)(*args, **kwargs)
                if method == 'keyUp':
                    job['keys'].difference_update(keys)
                elif method == 'mouseUp':
                    bound = inspect.signature(self.gui.mouseUp).bind_partial(*args, **kwargs)
                    job['buttons'].discard(bound.arguments.get('button', 'left'))
                if method in {'press', 'hotkey', 'write', 'typewrite'}:
                    job['keys'] = prior_keys
                if method in {'click', 'doubleClick', 'tripleClick', 'leftClick',
                              'rightClick', 'middleClick', 'dragTo', 'dragRel'}:
                    job['buttons'] = prior_buttons
                self.event('observer_action', job, method=method, args=args, kwargs=kwargs,
                           started_s=started, finished_s=self.recorder.elapsed())
                return {'value': value}
            except Exception as exc:
                self.event('observer_action_error', job, method=method, args=args, kwargs=kwargs,
                           error_type=type(exc).__name__, message=str(exc))
                raise

    def routes(self, bp, check_session):
        @bp.post('/observers/configure', endpoint='observer_configure')
        def configure():
            data = check_session()
            return jsonify(self.configure(data['mode']))

        @bp.post('/observers/create', endpoint='observer_create')
        def create():
            data = check_session()
            url = 'http://127.0.0.1:' + str(request.environ['SERVER_PORT'])
            return jsonify(self.create(data['name'], data.get('code'), url))

        @bp.post('/observers/stop', endpoint='observer_stop')
        def stop():
            data = check_session()
            return jsonify(self.stop(data.get('name'), data.get('observer_id'), data.get('error')))

        @bp.post('/observers/stop_all', endpoint='observer_stop_all')
        def stop_all():
            check_session()
            self.stop_all()
            return jsonify(status='ok')

        @bp.post('/observers/call', endpoint='observer_call')
        def call():
            data = check_session()
            try:
                return jsonify(self.call(data))
            except Exception as exc:
                return jsonify(status='error', message=str(exc), error_type=type(exc).__name__), 400

        @bp.get('/observers/events', endpoint='observer_events')
        def events():
            data = check_session()
            self.refresh()
            with self.lock:
                cursor = int(data.get('cursor', 0))
                return jsonify(events=self.events[cursor:], cursor=len(self.events),
                               observers=[self.snapshot(j) for j in self.jobs.values()])

        @bp.get('/observers/artifact', endpoint='observer_artifact')
        def artifact():
            data = check_session()
            root = (self.recorder.path / 'observers').resolve()
            path = (self.recorder.path / data['path']).resolve()
            if root not in path.parents or path.name == 'runtime.json':
                raise ValueError('Invalid observer artifact')
            return send_file(path)
