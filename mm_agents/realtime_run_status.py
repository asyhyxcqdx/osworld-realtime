"""Unscored infrastructure failures and preservation before a fresh run."""
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid


def pending_rerun(task_dir):
    path = Path(task_dir) / 'run_status.json'
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    return record if record.get('status') == 'pending_rerun' else None


def mark_pending_rerun(task_dir, error):
    record = {'status': 'pending_rerun', 'termination_reason': 'observer_network_error',
              'error': error, 'recorded_at': datetime.now(timezone.utc).isoformat()}
    path = Path(task_dir) / 'run_status.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)
    return record


def archive_pending_run(task_dir):
    """Keep the entire failed run; move its marker last so an interrupted move resumes."""
    root = Path(task_dir)
    if not pending_rerun(root):
        return
    history = root / '_previous_runs' / uuid.uuid4().hex
    history.mkdir(parents=True)
    for item in root.iterdir():
        if item.name not in {'_previous_runs', 'run_status.json'}:
            item.rename(history / item.name)
    (root / 'run_status.json').rename(history / 'run_status.json')
