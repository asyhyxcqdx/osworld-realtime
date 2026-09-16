"""Export a read-only, offline HTML view of a realtime trajectory.

Only files inside the trajectory directory may become image assets. Model text is
stored as escaped JSON and rendered as text, never interpreted as HTML.
"""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import quote


_IMAGE_LIMIT = 32 * 1024 * 1024


def _image_mime(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


class _Assets:
    def __init__(self, root, warnings):
        self.root, self.warnings = root.resolve(), warnings
        self.assets, self.files, self.hashes = {}, {}, {}

    def file(self, name):
        if not isinstance(name, str) or not name:
            return None
        if name in self.files:
            return self.files[name]
        self.files[name] = None
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root):
            self.warnings.append(f'未读取目录外图片：{name}')
            return None
        try:
            if path.stat().st_size > _IMAGE_LIMIT:
                raise ValueError('图片超过 32 MiB')
            data = path.read_bytes()
            asset = self.bytes(data)
            if asset is None:
                raise ValueError('不是支持的图片格式')
        except (OSError, ValueError) as exc:
            self.warnings.append(f'图片不可用：{name}（{exc}）')
            return None
        self.files[name] = asset
        self.assets[asset]['files'].append(name)
        return asset

    def bytes(self, data):
        mime = _image_mime(data)
        if mime is None or len(data) > _IMAGE_LIMIT:
            return None
        key = hashlib.sha256(data).hexdigest()
        if key not in self.assets:
            encoded = base64.b64encode(data).decode('ascii')
            url = f'data:{mime};base64,' + encoded
            self.assets[key] = {'src': url, 'files': []}
            for value in (encoded, url):
                self.hashes[hashlib.sha256(value.encode('ascii')).hexdigest()] = key
        return key

    def inline(self, encoded):
        if not isinstance(encoded, str) or len(encoded) > _IMAGE_LIMIT * 4 // 3 + 8:
            return None
        try:
            return self.bytes(base64.b64decode(encoded, validate=True))
        except (ValueError, TypeError):
            return None


def _sanitize(value, assets):
    if isinstance(value, list):
        return [_sanitize(v, assets) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if not isinstance(value, dict):
        return value
    result = {}
    image_source = value.get('type') == 'base64' and str(value.get('media_type', '')).startswith('image/')
    for key, item in value.items():
        if image_source and key == 'data' and isinstance(item, str):
            result['asset_id'] = assets.inline(item)
            result['data_length'] = len(item)
            result['data'] = '[图片数据单独展示]'
        elif key in {'image_url', 'url'} and isinstance(item, str) and item.startswith('data:image/'):
            result['asset_id'] = assets.inline(item.split(',', 1)[1]) if ',' in item else None
            result[key] = '[图片数据单独展示]'
        elif key in {'encrypted_content', 'signature', 'thought_signature', 'thoughtSignature'} and isinstance(item, str):
            result[key] = {'omitted_chars': len(item), 'sha256': hashlib.sha256(item.encode()).hexdigest()}
        else:
            result[key] = _sanitize(item, assets)
    return result


def _sidecar(root, name, warnings, text=False):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        warnings.append(f'未读取目录外文件：{name}')
        return '' if text else {}
    if not path.exists():
        return '' if text else {}
    try:
        raw = path.read_text(encoding='utf-8')
        return raw if text else json.loads(raw)
    except (OSError, ValueError) as exc:
        warnings.append(f'{name} 无法读取：{exc}')
        return '' if text else {}


def _calls(event):
    return [c for c in event.get('calls', []) if isinstance(c, dict)]


def _marks(actions):
    points = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        params = action.get('parameters', action.get('arguments', {}))
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except ValueError:
                continue
        if not isinstance(params, dict):
            continue
        x, y = params.get('x'), params.get('y')
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y)):
            points.append({'x': x, 'y': y, 'label': str(index + 1), 'action': action.get('action_type', action.get('name', ''))})
    return points


def build_trajectory_data(path, output=None):
    """Load JSONL incrementally; intern repeated request messages and image bytes."""
    path = Path(path).resolve()
    if path.is_dir():
        path = path / ('trajectory.jsonl' if (path / 'trajectory.jsonl').exists() else 'traj.jsonl')
    root = path.parent
    output = Path(output).resolve() if output else root / 'trajectory.html'
    warnings, events, pool, pool_lookup = [], [], [], {}
    assets = _Assets(root, warnings)
    with path.open(encoding='utf-8-sig') as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError('事件不是对象')
            except ValueError as exc:
                warnings.append(f'第 {line_number} 行未解析：{exc}')
                continue
            event = _sanitize(raw, assets)
            if 'event' not in event:
                event['event'] = 'legacy_step'
                event.setdefault('decision_id', event.get('step_num', len(events) + 1))
                if event.get('screenshot_file'):
                    event.setdefault('observation', {'screenshot_file': event['screenshot_file']})
            event['_line'] = line_number
            messages = event.pop('request_messages', None)
            if isinstance(messages, list):
                refs = []
                for message in messages:
                    encoded = json.dumps(message, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
                    digest = hashlib.sha256(encoded.encode()).hexdigest()
                    if digest not in pool_lookup:
                        pool_lookup[digest] = len(pool)
                        pool.append(message)
                    refs.append(pool_lookup[digest])
                event['_message_refs'] = refs
            events.append(event)

    observation_hashes = {}
    for event in events:
        observations = [event.get('observation')] + event.get('history_observations', [])
        for obs in observations:
            if isinstance(obs, dict) and obs.get('screenshot_file') and isinstance(obs.get('sha256'), str):
                key = json.dumps([obs['screenshot_file'], obs.get('task_time_s')], sort_keys=True)
                observation_hashes[key] = obs['sha256']

    def screenshot(obs, label):
        if not isinstance(obs, dict) or not obs.get('screenshot_file'):
            return None
        name = obs['screenshot_file']
        asset = assets.file(name)
        key = json.dumps([name, obs.get('task_time_s')], sort_keys=True)
        expected = obs.get('sha256') or observation_hashes.get(key)
        if expected and asset != expected:
            asset = expected if expected in assets.assets else None
            if asset is None:
                warnings.append(f'图片与轨迹哈希不一致，未展示：{name}；文件可能已被后续运行覆盖')
        return {'asset': asset, 'file': name, 'label': label, 'time': obs.get('task_time_s'), 'input': label == '决策输入'}

    request_images, decision_inputs, owners, pending_frames = {}, {}, {}, []
    request_coordinates = {}
    last_image = None
    for index, event in enumerate(events):
        kind, decision = event['event'], event.get('decision_id')
        view = {'decision': decision, 'images': [], 'marks': [], 'hidden': False}
        if kind == 'model_request':
            request_coordinates[event.get('request_id')] = event.get('coordinate_system', 'native_pixels')
            img = screenshot(event.get('observation'), '决策输入')
            if img:
                request_images[event.get('request_id')] = img
                decision_inputs[decision] = img
                view['images'] = [img]
        elif kind == 'model_response':
            img = request_images.get(event.get('request_id')) or decision_inputs.get(decision)
            if img:
                view['images'] = [img]
            view['marks'] = _marks(_calls(event))
            coordinate_system = request_coordinates.get(event.get('request_id'), 'native_pixels')
            view['coordinate_system'] = coordinate_system
            if coordinate_system != 'native_pixels':
                from mm_agents.realtime_coordinates import CoordinateAdapter

                try:
                    coordinates = CoordinateAdapter(coordinate_system)
                except ValueError:
                    warnings.append(f'未知坐标协议，未标注模型坐标：{coordinate_system}')
                    view['marks'] = []
                else:
                    native_marks = []
                    for mark in view['marks']:
                        try:
                            native_marks.append({**mark,
                                'x': coordinates.to_native_value('x', mark['x']),
                                'y': coordinates.to_native_value('y', mark['y']),
                            })
                        except ValueError:
                            # Raw arguments remain visible; invalid outputs are
                            # not guessed or silently clipped into screen bounds.
                            pass
                    view['marks'] = native_marks
            for call in _calls(event):
                if call.get('id'):
                    owners[call['id']] = decision
        elif kind == 'frame_query_artifacts':
            view['images'] = [{'asset': assets.file(f.get('file')), 'file': f.get('file'), 'label': '历史帧', 'time': f.get('actual_time_s'), 'requested_time': f.get('requested_time_s')} for f in event.get('images', []) if isinstance(f, dict)]
            pending_frames.append(index)
        elif kind == 'tool_result':
            frames = event.get('result', {}).get('frames', [])
            if pending_frames and frames:
                # Artifacts are logged synchronously immediately before the corresponding result.
                candidate = pending_frames[-1]
                if events[candidate].get('decision_id') == decision:
                    view['images'] = events[candidate]['_view']['images']
                    events[candidate]['_view']['hidden'] = True
                    pending_frames.pop()
        elif kind in {'action_submitted', 'action_execution_error', 'action_rejected'}:
            img = decision_inputs.get(decision) or last_image
            if img:
                view['images'] = [img]
            view['marks'] = _marks(event.get('actions', []))
        elif kind in {'initial_observation', 'action_executed', 'legacy_step'}:
            if kind == 'action_executed' and decision_inputs.get(decision):
                view['images'].append(decision_inputs[decision])
                view['marks'] = _marks(event.get('actions', []))
            img = screenshot(event.get('observation'), '初始画面' if kind == 'initial_observation' else '执行后')
            if img:
                view['images'].append(img)
                last_image = img
        elif kind == 'action_tool_result':
            view['hidden'] = True
            call_ids = [pair[0].get('id') for pair in event.get('calls', []) if isinstance(pair, list) and pair and isinstance(pair[0], dict)]
            linked = {owners[c] for c in call_ids if c in owners}
            if len(linked) == 1:
                view['decision'] = linked.pop()
        if not view['images'] and last_image and kind not in {'model_request', 'model_response', 'tool_result', 'frame_query_artifacts'}:
            view['images'] = [last_image]
        event['_view'] = view

    metadata = _sanitize(_sidecar(root, 'experiment.json', warnings), assets)
    metrics = _sanitize(_sidecar(root, 'agent_metrics.json', warnings), assets)
    result_file = _sanitize(_sidecar(root, 'result.json', warnings), assets)
    evaluations = [e for e in events if e['event'] == 'evaluation']
    evaluation = evaluations[-1] if evaluations else {}
    video = root / 'recording.mp4'
    video_url = None
    if video.is_file() and video.resolve().is_relative_to(root):
        video_url = quote(os.path.relpath(video, output.parent).replace(os.sep, '/'), safe='/')

    def reconnect_images(value):
        if isinstance(value, dict):
            for name in ('data_sha256', 'url_sha256', 'image_url_sha256'):
                digest = value.get(name)
                if isinstance(digest, str) and digest in assets.hashes:
                    value['asset_id'] = assets.hashes[digest]
            for child in list(value.values()):
                reconnect_images(child)
        elif isinstance(value, list):
            for child in value:
                reconnect_images(child)

    reconnect_images(events)
    reconnect_images(pool)
    return {'version': 1, 'source': path.name, 'metadata': metadata, 'metrics': metrics,
            'system_prompt': _sidecar(root, 'system_prompt.txt', warnings, text=True),
            'evaluation': evaluation, 'result_file': result_file, 'events': events,
            'messages': pool, 'assets': assets.assets, 'video': video_url,
            'warnings': list(dict.fromkeys(warnings))}


def render_trajectory(path, output=None):
    """Write an atomic standalone HTML file; keep the trajectory and PNGs intact."""
    source = Path(path).resolve()
    root = source if source.is_dir() else source.parent
    output = Path(output).resolve() if output else root / 'trajectory.html'
    if output == source or output.suffix.lower() not in {'.html', '.htm'}:
        raise ValueError('Output must be a separate .html file')
    data = build_trajectory_data(source, output)
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    for char, escaped in [('&', '\\u0026'), ('<', '\\u003c'), ('>', '\\u003e'), ('\u2028', '\\u2028'), ('\u2029', '\\u2029')]:
        encoded = encoded.replace(char, escaped)
    template = (Path(__file__).parent / 'assets/realtime_trajectory_viewer.html').read_text(encoding='utf-8')
    document = template.replace('__TRAJECTORY_DATA__', encoded)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(document)
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output
