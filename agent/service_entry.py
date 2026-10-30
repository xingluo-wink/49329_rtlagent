"""Authenticated API and serial dispatch owner."""
import json
import os
from pathlib import Path
import threading
import time
from adapter_contracts import require
from api_shutdown import shutdown_api
from formal_entry import App
from formal_jobs import put
from native_runtime import NativeOwner
from prompt_builder import digest

ROOT=Path(__file__).resolve().parent


from release_integrity import verify_release


def private_token():
    # Do not inherit the bearer secret into llama, compiler or job processes.
    from_env=os.environ.pop('FPGACHINA_TOKEN',None)
    if from_env:
        require('\n' not in from_env and '\r' not in from_env,'invalid credential')
        return from_env
    p=Path(os.environ.get('FPGACHINA_TOKEN_FILE','/workspace/.fpgachina-49329/token.env'))
    require(p.is_file() and not p.is_symlink() and p.stat().st_mode & 0o077 == 0,'private credential file required')
    import shlex
    fields=shlex.split(p.read_text())
    require(len(fields)==2 and fields[0]=='export' and fields[1].startswith('FPGACHINA_TOKEN='),'credential file format')
    token=fields[1].split('=',1)[1]
    require(token and '\n' not in token and '\r' not in token,'invalid credential')
    return token


class SerialPump:
    def __init__(self,service):
        self.service=service;self.stop=threading.Event();self.error=None
        self.thread=threading.Thread(target=self.run,name='rtl-serial-dispatcher',daemon=True)
    def run(self):
        try:
            while not self.stop.wait(.2):
                store=self.service.store;pending=[]
                for path in (store.root/'jobs').iterdir():
                    if not (path/'queued.json').exists():continue
                    if (path/'final.json').exists() or (path/'expired.json').exists():continue
                    ticket=store.ticket(path.name)
                    if (path/'launch-intent.json').exists():
                        # A dispatch intent is never replayable after uncertainty.
                        self.service.owner.tainted=True
                        return
                    pending.append((ticket['queued_at_monotonic'],path.name))
                for _,job in sorted(pending):
                    if self.stop.is_set():return
                    result=store and self.service.dispatcher.dispatch(job)
                    if result['status']=='attempted_or_unknown':
                        self.service.owner.tainted=True;return
        except BaseException as exc:
            self.error=type(exc).__name__;self.service.owner.tainted=True
    def close(self):
        self.stop.set();self.thread.join(15.)
        require(not self.thread.is_alive(),'serial dispatcher did not exit')


def main(run_root):
    verify_release();token=private_token()
    owner=NativeOwner(run_root,allow_native=True)
    pump=None;server=None;watcher=None;stop_watch=None;primary_error=None
    try:
        service=owner.start()
        pump=SerialPump(service);owner.pump=pump;pump.thread.start()
        import uvicorn
        server=uvicorn.Server(uvicorn.Config(App(service,token=token),host='127.0.0.1',port=7860,
            workers=1,access_log=False,log_level='warning',timeout_keep_alive=5))
        stop_watch=threading.Event()
        def watch():
            while not stop_watch.wait(.2):
                try:owner.guard()
                except Exception:server.should_exit=True;return
        watcher=threading.Thread(target=watch,name='rtl-api-owner-monitor',daemon=True);watcher.start()
        put(owner.root/'api-configured.json',{'host':'127.0.0.1','port':7860,'track':'rtl',
            'authenticated_all_endpoints':True,'automatic_test_solve_requests':0,
            'real_agent_and_baseline_dispatcher':True,'solve_execution_tested':False})
        server.run()
    except BaseException as exc:
        primary_error=exc
        raise
    finally:
        shutdown_api(owner=owner,pump=pump,server=server,watcher=watcher,stop_watch=stop_watch,
            primary_error=primary_error,publish=lambda report:put(owner.root/'api-shutdown.json',report))


if __name__=='__main__':
    import sys
    if len(sys.argv)!=2:raise SystemExit('usage: service_entry.py NEW_SESSION_ROOT')
    main(sys.argv[1])
