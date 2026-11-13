import ctypes
import gc
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_env.server import pyxcursor


@pytest.fixture
def xlibs(monkeypatch):
    monkeypatch.setenv('DISPLAY', ':0')
    pixels = (ctypes.c_ulong * 1)(0xFF123456)
    data = pyxcursor.XFixesCursorImage(x=3, y=4, width=1, height=1, pixels=pixels)
    display = ctypes.pointer(pyxcursor.Display())
    xlib = SimpleNamespace(XOpenDisplay=Mock(return_value=display),
                           XCloseDisplay=Mock(return_value=0), XFree=Mock(return_value=0))
    fixes = SimpleNamespace(XFixesGetCursorImage=Mock(return_value=ctypes.pointer(data)))
    monkeypatch.setattr(pyxcursor.ctypes.util, 'find_library', lambda name: name)
    monkeypatch.setattr(pyxcursor.ctypes.cdll, 'LoadLibrary', lambda name: xlib if name == 'X11' else fixes)
    return xlib, fixes, pixels


def test_cursor_releases_each_image_and_display_exactly_once(xlibs):
    xlib, _, pixels = xlibs
    with pyxcursor.Xcursor() as cursor:
        image = cursor.getCursorImageArrayFast()
        assert image.shape == (1, 1, 4)
        assert cursor.position == (3, 4)
        cursor.getCursorImageArrayFast()
        assert xlib.XFree.call_count == 1
    pixels[0] = 0
    assert image.any()  # Returned array owns its pixels after XFree.
    cursor.close()
    assert xlib.XFree.call_count == 2
    xlib.XCloseDisplay.assert_called_once()
    with pytest.raises(RuntimeError, match='closed'):
        cursor.getCursorImageData()


def test_cursor_cleanup_also_runs_after_capture_exception(xlibs):
    xlib, _, _ = xlibs
    with pytest.raises(ValueError, match='capture failed'):
        with pyxcursor.Xcursor() as cursor:
            cursor.getCursorImageData()
            raise ValueError('capture failed')
    xlib.XFree.assert_called_once()
    xlib.XCloseDisplay.assert_called_once()


def test_cursor_rejects_failed_display_open_without_calling_xfixes(xlibs):
    xlib, fixes, _ = xlibs
    xlib.XOpenDisplay.return_value = ctypes.POINTER(pyxcursor.Display)()
    with pytest.raises(RuntimeError, match='Cannot open'):
        pyxcursor.Xcursor()
    fixes.XFixesGetCursorImage.assert_not_called()
    xlib.XFree.assert_not_called()
    xlib.XCloseDisplay.assert_not_called()


def test_legacy_caller_without_context_manager_still_releases_resources(xlibs):
    xlib, _, _ = xlibs
    cursor = pyxcursor.Xcursor()
    cursor.getCursorImageArrayFast()
    del cursor
    gc.collect()
    xlib.XFree.assert_called_once()
    xlib.XCloseDisplay.assert_called_once()
