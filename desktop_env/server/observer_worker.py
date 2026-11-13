"""Run one ordinary Python/PyAutoGUI observer inside the game VM.

PyAutoGUI's public screen/input functions are recorded by the VM service.
The script still imports pyautogui normally; its Python control flow is untouched.
"""
import io
import json
import os
from pathlib import Path
import runpy
import sys
import traceback
import base64
from contextlib import contextmanager

import requests
import pyautogui
from PIL import Image


INPUT_FUNCTIONS = (
    'moveTo', 'moveRel', 'dragTo', 'dragRel', 'click', 'doubleClick',
    'tripleClick', 'rightClick', 'middleClick', 'leftClick', 'mouseDown',
    'mouseUp', 'scroll', 'hscroll', 'vscroll', 'press', 'keyDown', 'keyUp',
    'hotkey', 'write', 'typewrite', 'position', 'size',
)


def main():
    root = Path(sys.argv[1])
    config = json.loads((root / 'runtime.json').read_text())
    session = requests.Session()
    session.trust_env = False
    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = False

    def encode(value):
        # NumPy scalars/coordinate arrays are accepted by ordinary PyAutoGUI.
        if hasattr(value, 'tolist'):
            return value.tolist()
        raise TypeError('Cannot serialize PyAutoGUI argument: ' + type(value).__name__)

    def call(method, args, kwargs):
        payload = {
            'session_id': config['session_id'], 'observer_id': config['observer_id'],
            'method': method, 'args': args, 'kwargs': kwargs,
            'pause': pyautogui.PAUSE, 'failsafe': pyautogui.FAILSAFE,
        }
        response = session.post(config['url'] + '/realtime/observers/call',
                                json=json.loads(json.dumps(payload, default=encode)), timeout=120)
        body = response.json()
        if not response.ok:
            raise RuntimeError(body.get('message', 'Observer GUI call failed'))
        if method == 'screenshot':
            return Image.open(io.BytesIO(base64.b64decode(body['image']))).convert('RGB')
        value = body.get('value')
        if method == 'position':
            return pyautogui.Point(*value)
        if method == 'size':
            return pyautogui.Size(*value)
        return value

    def wrap(method):
        def invoke(*args, **kwargs):
            return call(method, args, kwargs)
        return invoke

    for method in (*INPUT_FUNCTIONS, 'screenshot'):
        setattr(pyautogui, method, wrap(method))
    # PyAutoGUI's locateOnScreen/pixel helpers use PyScreeze internally.
    # Keep their observations on the same recorded screenshot path.
    import pyscreeze
    pyscreeze.screenshot = pyautogui.screenshot
    @contextmanager
    def hold(keys, logScreenshot=None, _pause=True):
        keys = [keys] if isinstance(keys, str) else list(keys)
        pressed = []
        try:
            for key in keys:
                pyautogui.keyDown(key, _pause=_pause)
                pressed.append(key)
            yield
        finally:
            for key in reversed(pressed):
                pyautogui.keyUp(key, _pause=_pause)
    pyautogui.hold = hold
    try:
        runpy.run_path(str(root / 'code.py'), run_name='__main__')
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code in (None, 0):
            return
        trace = traceback.format_exc()
        frames = traceback.extract_tb(exc.__traceback__)
        user_frames = [f for f in frames if f.filename == str(root / 'code.py')]
        (root / 'error.json').write_text(json.dumps({
            'error_type': type(exc).__name__, 'message': str(exc),
            'line': user_frames[-1].lineno if user_frames else None,
            'traceback': trace,
        }, ensure_ascii=False))
        sys.stderr.write(trace)
        sys.exit(1)
    finally:
        session.close()


if __name__ == '__main__':
    main()
