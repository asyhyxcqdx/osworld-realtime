from unittest.mock import Mock

import pytest
import requests

from scripts.python import install_realtime_server as installer


def test_reloader_disconnect_retries_upload_and_patch_but_not_restart(monkeypatch):
    monkeypatch.setattr(installer.time, 'sleep', lambda _: None)
    response = Mock(ok=True)
    response.json.return_value = {'returncode': 0, 'version': 1, 'observers': 1, 'observer_capture': 'per_request_memory'}
    calls = []
    upload_payloads = []
    def post(url, **kwargs):
        if url.endswith('/setup/upload'):
            name, content = kwargs['files']['file_data']
            calls.append(name)
            if name == 'fmp4.py':
                upload_payloads.append(content)
                if len(upload_payloads) == 1:
                    raise requests.ConnectionError('reload while uploading')
        else:
            code = kwargs['json']['command'][2]
            label = 'restart' if 'subprocess.Popen' in code else 'patch'
            calls.append(label)
            if label == 'patch' and calls.count('patch') == 1:
                raise requests.ConnectionError('reload while patching')
            if label == 'restart':
                raise requests.ConnectionError('response lost after restart was scheduled')
        return response
    monkeypatch.setattr(installer.requests, 'post', post)
    get = Mock(return_value=response)
    monkeypatch.setattr(installer.requests, 'get', get)
    assert installer.install('http://vm')['observers'] == 1
    assert upload_payloads[0] == upload_payloads[1] and upload_payloads[0]
    assert calls.count('patch') == 2
    assert calls.count('restart') == 1
    get.assert_called_once_with('http://vm/realtime/capabilities', timeout=3)


def test_install_transport_retry_is_bounded_and_preserves_error(monkeypatch):
    monkeypatch.setattr(installer.time, 'sleep', lambda _: None)
    operation = Mock(side_effect=requests.Timeout('VM remains unavailable'))
    with pytest.raises(requests.Timeout):
        installer._retry_setup_request(operation)
    assert operation.call_count == 5


def test_install_does_not_retry_python_compile_failure(monkeypatch):
    monkeypatch.setattr(installer.time, 'sleep', lambda _: None)
    response = Mock(ok=True)
    response.json.return_value = {'returncode': 1, 'error': 'SyntaxError'}
    post = Mock(return_value=response)
    monkeypatch.setattr(installer.requests, 'post', post)
    with pytest.raises(RuntimeError, match='SyntaxError'):
        installer.install('http://vm')
    assert post.call_count == 7  # Six uploads, then one compile request.


def test_install_updates_cursor_dependency_and_patches_legacy_capture_idempotently(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.time, 'sleep', lambda _: None)
    original = (
        'def capture_screen_with_cursor():\n'
        '    if user_platform == "Windows":\n        pass\n'
        '    elif user_platform == "Linux":\n' + installer.LEGACY_CURSOR_CAPTURE
        + "\nif __name__ == '__main__':\n    pass\n"
    )
    entry = tmp_path / 'main.py'
    entry.write_text(original)
    response = Mock(ok=True)
    response.json.return_value = {'returncode': 0, 'version': 1, 'observers': 1, 'observer_capture': 'per_request_memory'}
    def post(url, **kwargs):
        if url.endswith('/setup/upload'):
            name, content = kwargs['files']['file_data']
            (tmp_path / name).write_bytes(content)
        else:
            code = kwargs['json']['command'][2]
            if 'py_compile' in code:
                exec(code, {})  # Execute only the idempotent patch/compile request.
        return response
    monkeypatch.setattr(installer.requests, 'post', post)
    monkeypatch.setattr(installer.requests, 'get', Mock(return_value=response))
    installer.install('http://vm', server_dir=str(tmp_path))
    first = entry.read_text()
    assert installer.MANAGED_CURSOR_CAPTURE in first
    assert 'XCloseDisplay' in (tmp_path / 'pyxcursor.py').read_text()
    assert 'XFree' in (tmp_path / 'pyxcursor.py').read_text()
    installer.install('http://vm', server_dir=str(tmp_path))
    assert entry.read_text() == first
    assert (tmp_path / 'main.py.before-realtime').read_text() == original
