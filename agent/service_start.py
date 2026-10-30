"""Background start/inspection; never submit jobs or expose the model port."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import urllib.error
import urllib.request
from service_entry import private_token,verify_release
from native_model import identity

ROOT=Path(__file__).resolve().parent
from runtime_paths import state_root, session_root
STATE=state_root()
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))


def same(saved):
    try:
        row=identity(saved['pid'])
        return row['state']!='Z' and all(row[k]==saved[k] for k in ('pid','start_ticks','boot_id'))
    except (OSError,ValueError):return False


def health(token):
    req=urllib.request.Request('http://127.0.0.1:7860/v1/health',headers={'Authorization':'Bearer '+token})
    with OPENER.open(req,timeout=3) as response:return json.load(response)


def main():
    verify_release();token=private_token();STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'start.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        record=STATE/'service-process.json'
        if record.exists():
            saved=json.loads(record.read_bytes())
            if not same(saved['identity']):raise RuntimeError('previous session ended; inspect closure before explicit restart')
            result=health(token)
            if not result.get('ready'):raise RuntimeError('existing session is not ready; do not start a duplicate')
            print(json.dumps({'already_running':True,'health':result}));return
        if (STATE/'launch-intent.json').exists():raise RuntimeError('prior launch intent; inspect before recovery')
        session=session_root()/uuid.uuid4().hex
        session.parent.mkdir(exist_ok=True)
        with (STATE/'launch-intent.json').open('x') as f:
            json.dump({'epoch':time.time(),'run_root':str(session),'no_replay':True,'background_start':True,
                'generation_requests_sent_by_launcher':0},f)
        env=dict(os.environ)
        env['FPGACHINA_TOKEN']=token
        env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONUNBUFFERED']='1'
        with (STATE/'service.log').open('xb') as log:
            process=subprocess.Popen(['/opt/python/bin/python3','-B',str(ROOT/'service_entry.py'),str(session)],
                cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        row=identity(process.pid)
        with record.open('x') as f:json.dump({'identity':row,'run_root':str(session),'epoch':time.time()},f)
        # One startup; readiness polling never creates a second process.
        end=time.monotonic()+360
        while time.monotonic()<end:
            if process.poll() is not None:raise RuntimeError('service startup exited; preserve log and intent')
            try:
                result=health(token)
                if result.get('ready') is True and result.get('track')=='rtl':
                    with (STATE/'launch-completed.json').open('x') as f:
                        json.dump({'identity':row,'run_root':str(session),'health':result,'epoch':time.time()},f)
                    print(json.dumps({'ready':True,'identity':row,'run_root':str(session),'health':result}));return
            except (urllib.error.URLError,TimeoutError,ValueError):pass
            time.sleep(1)
        raise TimeoutError('existing process did not become ready; no automatic restart')


if __name__=='__main__':main()
