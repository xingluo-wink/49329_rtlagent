"""Check distribution completeness before starting the service.

Checksums detect changed bytes. They are not signatures or runtime certification.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

EDITABLE_DOCUMENTS = frozenset({'REPORT.md'})


def verify_release(root=None, *, require_approval=True, verify_editable_documents=False):
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    root = root.resolve(strict=True)
    index = root/'checksums.json'
    release = root/'release.json'
    if index.is_symlink() or release.is_symlink():
        raise ValueError('linked integrity metadata')
    raw = index.read_bytes()
    approval = json.loads(release.read_bytes())
    if approval.get('schema') != 'rtl-source-release-v1':
        raise ValueError('unknown release schema')
    if require_approval and approval.get('package_approved') is not True:
        raise ValueError('source package has not completed release checks')
    if approval.get('checksums_sha256') != hashlib.sha256(raw).hexdigest():
        raise ValueError('release index changed')
    entries = json.loads(raw)
    if type(entries) is not dict or not entries or approval.get('file_count') != len(entries):
        raise ValueError('invalid release index')
    for name, meta in entries.items():
        path = PurePosixPath(name)
        if (not isinstance(name,str) or '\\' in name or ':' in name or path.is_absolute()
                or path.as_posix() != name or any(p in ('.','..') for p in path.parts)
                or name in ('checksums.json','release.json')):
            raise ValueError('unsafe release path')
        p = root.joinpath(*path.parts)
        if any(q.is_symlink() for q in (p,*p.parents) if q != root and root in q.parents):
            raise ValueError('linked release file')
        if (type(meta) is not dict or set(meta) != {'bytes','sha256','mode'}
                or type(meta['bytes']) is not int or meta['bytes'] < 0
                or meta['mode'] not in (420,493)
                or not re.fullmatch('[0-9a-f]{64}',meta['sha256'])):
            raise ValueError('invalid file metadata')
        body = p.read_bytes()
        if name in EDITABLE_DOCUMENTS and not verify_editable_documents:
            if len(body)>2*1024*1024:
                raise ValueError('editable report is too large')
            body.decode('utf-8',errors='strict')
            continue
        if len(body) != meta['bytes'] or hashlib.sha256(body).hexdigest() != meta['sha256']:
            raise ValueError('release file changed: '+name)
    actual=set()
    for p in root.rglob('*'):
        rel=p.relative_to(root)
        if rel.parts[0]=='.git':
            continue
        if p.is_symlink():
            raise ValueError('linked distribution entry')
        if p.is_file():
            actual.add(rel.as_posix())
    if actual != set(entries)|{'checksums.json','release.json'}:
        raise ValueError('unexpected or missing distribution files')
    return {'file_count':len(entries),'checksums_sha256':hashlib.sha256(raw).hexdigest(),
            'package_approved':approval.get('package_approved') is True,
            'editable_documents':sorted(EDITABLE_DOCUMENTS),
            'runtime_tested':approval.get('runtime_tested') is True}
