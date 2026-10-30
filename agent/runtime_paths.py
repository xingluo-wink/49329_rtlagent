"""Deployment data and writable state live outside the source distribution."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import uuid

REPO = Path(__file__).resolve().parent.parent


def external_path(value):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts:
        raise ValueError('an absolute external path is required')
    q = p.resolve()
    if q == REPO or REPO in q.parents or q in REPO.parents:
        raise ValueError('runtime paths must be separate from the source tree')
    if p.is_symlink():
        raise ValueError('linked runtime root is not allowed')
    return p


def state_root():
    return external_path(os.environ.get('RTL_STATE_DIR', '/workspace/rtlagent/state'))


def session_root():
    return external_path(os.environ.get('RTL_SESSION_DIR', '/var/tmp/rtlagent'))


def deployment():
    path = external_path(os.environ.get('RTL_DEPLOYMENT_FILE', '/workspace/rtlagent/deployment.local.json'))
    raw = path.read_bytes()
    if len(raw) > 8192:
        raise ValueError('deployment configuration too large')
    value = json.loads(raw)
    fields = {'hostname','boot_id','execution_ids','workspace_owner','persistent_root'}
    if type(value) is not dict or set(value) != fields:
        raise ValueError('deployment field set differs')
    if not isinstance(value['hostname'], str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', value['hostname']):
        raise ValueError('invalid deployment hostname')
    if str(uuid.UUID(value['boot_id'])) != value['boot_id']:
        raise ValueError('invalid deployment boot identity')
    if any(type(value[k]) is not list or len(value[k])!=n or any(type(x) is not int or x<0 for x in value[k])
           for k,n in (('execution_ids',4),('workspace_owner',2))):
        raise ValueError('invalid deployment execution/storage identities')
    root = PurePosixPath(value['persistent_root'])
    if not root.is_absolute() or '..' in root.parts or root.parts[:2] != ('/','workspace'):
        raise ValueError('persistent storage must be under /workspace')
    return value
