import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask, request
import numpy as np
from PIL import Image
import pytest

from desktop_env.server import screen_capture
from desktop_env.server.realtime import register_realtime


@pytest.fixture
def cursor(monkeypatch):
    closed = Mock()
    pixels = np.zeros((2, 2, 4), dtype=np.uint8)
    pixels[0, 0] = [255, 0, 0, 255]
    @contextmanager
    def factory():
        try:
            yield SimpleNamespace(getCursorImageArrayFast=lambda: pixels.copy(), position=(3, 4))
        finally:
            closed()
    monkeypatch.setattr(screen_capture, 'Xcursor', factory)
    monkeypatch.setenv('DISPLAY', ':42')
    return closed


def test_png_is_fresh_memory_with_cursor_and_no_shared_file(tmp_path, monkeypatch, cursor):
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / 'screenshot.png'
    sentinel.write_bytes(b'existing capture')
    grab = Mock(side_effect=[Image.new('RGB', (16, 12), 'green'), Image.new('RGB', (16, 12), 'blue')])
    monkeypatch.setattr(screen_capture.ImageGrab, 'grab', grab)
    images = [screen_capture.capture_png(), screen_capture.capture_png()]
    for payload, color in zip(images, [(0, 128, 0), (0, 0, 255)]):
        with Image.open(io.BytesIO(payload)) as image:
            assert image.size == (16, 12)
            assert image.getpixel((0, 0)) == color
            assert image.getpixel((3, 4)) == (255, 0, 0)
            assert image.getpixel((4, 4)) == color
    assert grab.call_count == 2
    grab.assert_called_with(xdisplay=':42')
    assert cursor.call_count == 2
    assert sentinel.read_bytes() == b'existing capture'
    assert list(tmp_path.iterdir()) == [sentinel]


def test_memory_capture_releases_cursor_when_grab_fails(monkeypatch, cursor):
    monkeypatch.setattr(screen_capture.ImageGrab, 'grab', Mock(side_effect=OSError('X11 unavailable')))
    with pytest.raises(OSError, match='X11 unavailable'):
        screen_capture.capture_png()
    cursor.assert_called_once()


@pytest.mark.parametrize('mode', ['jev', 'script'])
def test_parallel_observations_are_independent_and_not_serialized(monkeypatch, cursor, mode):
    app = Flask(__name__)
    legacy = Mock(side_effect=AssertionError('Observer screenshot called legacy file capture'))
    gui = SimpleNamespace(position=Mock(side_effect=AssertionError('Shared GUI connection used for screenshot')),
                          keyUp=Mock(), mouseUp=Mock())
    register_realtime(app, legacy, gui)
    recorder = app.extensions['osworld_realtime']
    recorder.session_id = 'parallel-memory'
    recorder.elapsed = time.monotonic
    with app.test_client() as client:
        assert client.post('/realtime/observers/configure', json={
            'session_id': recorder.session_id, 'mode': mode}).status_code == 200
    simultaneous_grabs = threading.Barrier(4)
    def grab(**kwargs):
        # All four requests must reach capture before any one can finish. A
        # global screenshot lock would break the barrier and fail this test.
        simultaneous_grabs.wait(timeout=3)
        return Image.new('RGB', (16, 12), (int(request.headers['X-Test-Color']), 0, 0))
    monkeypatch.setattr(screen_capture.ImageGrab, 'grab', grab)
    def observe(color):
        with app.test_client() as client:
            response = client.get('/realtime/observation?session_id=parallel-memory',
                                  headers={'X-Test-Color': str(color)})
            assert response.status_code == 200
            body = response.json
            with Image.open(io.BytesIO(base64.b64decode(body['image']))) as image:
                assert image.getpixel((8, 8)) == (color, 0, 0)
            return body['capture_started_s'], body['capture_finished_s']
    with ThreadPoolExecutor(max_workers=4) as pool:
        intervals = list(pool.map(observe, [40, 80, 120, 160]))
    assert max(start for start, _ in intervals) < min(end for _, end in intervals)
    assert cursor.call_count == 4
    gui.position.assert_not_called()
    legacy.assert_not_called()
