"""Local, authenticated IPC shared by file entry and the public API adapter."""
import http.client
import json
import os
from pathlib import Path
import socket
from runtime_paths import state_root


class EngineError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message); self.status = status


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=5):
        super().__init__('localhost', timeout=timeout); self.path = str(path)
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout); self.sock.connect(self.path)


def current():
    try:
        row = json.loads((state_root()/'current.json').read_bytes())
        token = Path(row['token_file'])
        if token.is_symlink() or token.stat().st_mode & 0o077:
            raise EngineError('本地引擎凭据权限不正确；没有发送题目。')
        return row, token.read_text().strip()
    except FileNotFoundError as exc:
        raise EngineError('解题引擎尚未启动。请先运行 bash serve/start.sh；没有发送题目。') from exc


def call(path, data=None, *, timeout=5, method=None):
    row, token = current()
    connection = UnixConnection(row['socket'], timeout)
    try:
        connection.request(method or ('GET' if data is None else 'POST'), path,
            body=None if data is None else json.dumps(data,ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        response = connection.getresponse(); body = response.read(8*1024*1024+1)
        if len(body)>8*1024*1024: raise EngineError('引擎响应超过限制。')
        value = json.loads(body)
        if response.status not in (200,202):
            raise EngineError(str(value.get('error','引擎请求失败')),response.status)
        return value
    except (OSError, http.client.HTTPException) as exc:
        raise EngineError('无法连接本地解题引擎。运行 bash serve/start.sh 查看具体原因；不要重复提交已有任务。') from exc
    finally:
        connection.close()


def ready():
    value = call('/v1/health')
    if value.get('ready') is not True or value.get('track')!='rtl':
        raise EngineError('引擎未就绪：'+str(value.get('reason','启动或资源清理尚未完成'))+'；没有发送题目。',503)
    return value
