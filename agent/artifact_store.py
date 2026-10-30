"""Exclusive attempt claims and no-overwrite evidence publication for new runs.

Partial writes stay as pending evidence. Publication uses a same-filesystem
exclusive rename on Windows or hard link on POSIX, never overwrite/replace.
This does not implement PID/stream closure or authorize a model request.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import uuid


def _component(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', value) is None:
        raise ValueError('unsafe artifact component')
    if value in ('.', '..'):
        raise ValueError('unsafe artifact component')
    return value


def _encode(value):
    if isinstance(value, bytes):
        return value
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')


def _publish(pending, final):
    if os.name == 'nt':
        # Unlike POSIX rename, Windows os.rename raises if destination exists.
        os.rename(pending, final)
    else:
        os.link(pending, final)
        pending.unlink()


class EvidenceStore:
    def __init__(self, root):
        given = Path(root)
        if given.is_symlink():
            raise ValueError('artifact root cannot be a symlink')
        given.mkdir(parents=True, exist_ok=True)
        self.root = given.resolve()

    def _path(self, event, name):
        path = self.root / (_component(event) + '.' + _component(name))
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError('artifact path escape')
        return path

    def claim(self, event, manifest):
        path = self._path(event, 'attempt-intent.json')
        # Even a partial claim means attempted. Never remove/recover by retrying.
        with path.open('xb') as handle:
            handle.write(_encode({'event_id': event, 'manifest': manifest, 'no_replay': True}))
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def record(self, event, name, value):
        claim = self._path(event, 'attempt-intent.json')
        if not claim.is_file():
            raise ValueError('exclusive claim required before evidence')
        try:
            claimed = json.loads(claim.read_bytes())
        except (ValueError, OSError) as exc:
            raise ValueError('incomplete claim cannot publish evidence') from exc
        if (not isinstance(claimed, dict) or claimed.get('event_id') != event
                or claimed.get('no_replay') is not True or not isinstance(claimed.get('manifest'), dict)):
            raise ValueError('claim binding mismatch')
        data = _encode(value)
        final = self._path(event, name)
        pending = self._path(event, 'pending-' + uuid.uuid4().hex)
        with pending.open('xb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _publish(pending, final)
        return {'path': final.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
