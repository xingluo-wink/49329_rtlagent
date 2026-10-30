"""Linux service owner and serial worker lifecycle. Importing starts no process."""
import builtins
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import types
from adapter_contracts import require, RUNTIME, WEIGHT
from formal_jobs import (JobStore, Dispatcher, ObservedLauncher, Service, domain,
                         known_tool_identities, put, read)
from native_model import NativeModel, identity, runtime_fingerprint
from generation_policy import GenerationPolicy
from prompt_builder import canonical, digest
from compiler_adapter import pinned_sources
from runtime_environment import install,vivado

ROOT = Path(__file__).resolve().parent
from runtime_paths import deployment, state_root
STATE = state_root()


def full_domain(pid=None):
    q = identity(os.getpid() if pid is None else pid)
    return domain({'host_id':socket.gethostname(),'clock_id':'CLOCK_MONOTONIC',
                   **{k:q[k] for k in ('pid','start_ticks','boot_id')}})


def private_common(root):
    require(sys.platform == 'linux', 'Linux native trial only')
    sources = pinned_sources()
    mod = types.ModuleType('private_native_common')
    mod.__dict__.update(__file__=str(ROOT/'common.py'), __builtins__=dict(vars(builtins)))
    exec(compile(sources['common.py'], str(ROOT/'common.py'), 'exec'), mod.__dict__)
    mod.ROOT = Path(root); mod.REG = Path(root)/'processes'
    mod.REG.mkdir(exist_ok=False)
    install(mod)
    return mod


def compact_rows(common, rows):
    unique = {}
    for row in rows:
        key = common.identity_key(row)
        q = common.ident(row['pid'])
        state = 'exited' if not q or common.identity_key(q) != key else q['state']
        unique[key] = {'pid':row['pid'],'start_ticks':row['start_ticks'],'boot_id':row['boot_id'],'state':state}
    return sorted(unique.values(), key=lambda x:(x['pid'],x['start_ticks']))


def registry_rows(root):
    """Read only native registry files produced under this exact owned job."""
    rows = []
    for path in Path(root).rglob('registry'):
        if not path.is_dir() or path.is_symlink(): continue
        for record in (path/'processes').glob('*.json'):
            rows.append(json.loads(record.read_bytes()))
        history = path/'process-history.jsonl'
        if history.exists(): rows.extend(json.loads(x) for x in history.read_bytes().splitlines())
        descendants = path/'process-descendants.json'
        if descendants.exists(): rows.extend(json.loads(descendants.read_bytes()).get('identities',[]))
    return rows


class NativePlatform:
    def __init__(self, owner):
        self.owner = owner; self.common = owner.common; self.jobs = {}

    def spawn(self, job, ticket, launch):
        self.owner.guard()
        require(time.monotonic() < ticket['deadline_monotonic'], 'worker expired before Popen')
        process, row = self.common.spawn('worker-'+ticket['job_id'],
            ['/opt/python/bin/python3','-B',str(ROOT/'native_worker.py'),str(job),str(self.owner.root/'model-service.json')],
            self.common.env_for(), cwd=ROOT)
        self.jobs[process.pid] = (job, row)
        return process

    def identity(self, process):
        job, saved = self.jobs[process.pid]
        current = full_domain(process.pid)
        require(all(saved[k] == current[k] for k in ('pid','start_ticks','boot_id')), 'worker identity changed')
        return current

    def client_alive(self,job):
        p=job/'client-owner.json'
        if not p.exists():return True
        saved=json.loads(p.read_bytes())
        try:
            row=identity(saved['pid'])
            return row['state']!='Z' and all(row[k]==saved[k] for k in ('pid','start_ticks','boot_id'))
        except FileNotFoundError:return False

    def failure_resources(self,job,worker):
        self.owner.guard()
        saved=self.jobs[worker['pid']][1]
        rows=[saved]+registry_rows(job)
        service_keys={self.common.identity_key(q) for q in (self.owner.model_identity,self.owner.watchdog_identity) if q}
        rows += [q for q in self.common.capture_owned() if q['pid']!=os.getpid()
                 and self.common.identity_key(q) not in service_keys
                 and not service_keys.intersection(q.get('owner_keys',[]))]
        observed=compact_rows(self.common,rows)
        require(all(q['state'] in ('exited','Z') for q in observed),'failure still has live owned processes')
        end=time.monotonic()+4.;idle=False
        while time.monotonic()<end:
            slots=self.common.http('/slots')
            idle=type(slots) is list and len(slots)==1 and slots[0].get('is_processing') is False
            if idle:break
            time.sleep(.1)
        require(idle,'cancelled backend request has not become idle')
        return {'job_id':job.name,'worker':worker,'owned_snapshot':observed,
                'model_backend_idle':True,'observed_at_monotonic':time.monotonic()}

    def close_owned(self, process, worker, *, terminate):
        job, saved = self.jobs[process.pid]
        key = self.common.identity_key(saved)
        require(all(saved[k] == worker[k] for k in ('pid','start_ticks','boot_id')), 'cleanup identity mismatch')
        rows = [saved] + registry_rows(job)
        service_keys={self.common.identity_key(x) for x in (self.owner.model_identity,self.owner.watchdog_identity) if x}
        def collect_nonservices():
            # A newly adopted EDA child may first be observed with only OWNER
            # lineage after its worker exited. In this serial owner it still
            # blocks closure. Preserve only the exact bound model/watchdog trees.
            return [q for q in self.common.capture_owned() if q['pid']!=os.getpid()
                    and self.common.identity_key(q) not in service_keys
                    and not service_keys.intersection(q.get('owner_keys',[]))]
        rows += collect_nonservices()
        if terminate:
            self.common.stop_rows([q for q in rows if q['pid'] != os.getpid()], .2)
            try: process.wait(timeout=5)
            except Exception: pass
        # A normal worker must already have closed its compiler descendants.
        # Residuals trigger qualified cleanup but are still recorded as abnormal.
        rows += collect_nonservices()
        left = [q for q in rows if self.common.same(q)]
        if left:
            self.common.stop_rows(left, .2)
            put(job/'native-residual-cleanup.json',{'remaining_before_cleanup':left,'normal_closure':False})
            raise RuntimeError('worker left live native resources')
        # A final scan is part of closure, never a new verdict based on old rows.
        final=collect_nonservices();rows+=final
        if any(self.common.same(q) for q in final):
            self.common.stop_rows([q for q in final if self.common.same(q)],.2)
            put(job/'native-late-adoption.json',{'normal_closure':False,'observed':final})
            raise RuntimeError('newly adopted process at closure')
        return compact_rows(self.common, rows)


class TrialService(Service):
    def __init__(self, store, dispatcher, owner):
        super().__init__(store, dispatcher)
        self.native_trial = True; self.owner = owner
    def health(self):
        value=self.owner.health()
        if (STATE/'admission-paused.json').exists():value={**value,'ready':False,'reason':'服务正在独立评分，暂停接题'}
        return value
    def solve(self, request, *, queued_at=None, outer_deadline=None):
        import fcntl
        from formal_jobs import Unavailable
        if (STATE/'admission-paused.json').exists():raise Unavailable('服务正在独立评分，暂停接题；本次没有启动模型')
        with (STATE/'admission.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise Unavailable('准入暂忙；本次没有启动模型')
            if (STATE/'admission-paused.json').exists():raise Unavailable('private grading in progress')
            self.owner.guard()
            require(self.health()['ready'], 'real service unavailable')
            return self.store.submit(request,queued_at=queued_at,outer_deadline=outer_deadline)

    def solve_local(self,request,client,*,queued_at=None,outer_deadline=None):
        from formal_jobs import TRANSITIONS
        actual=identity(client['pid'])
        require(actual['state']!='Z' and all(actual[k]==client[k] for k in ('pid','start_ticks','boot_id')),'local client identity changed')
        with TRANSITIONS:
            result=self.solve(request,queued_at=queued_at,outer_deadline=outer_deadline)
            path=self.store.job(result['job_id'])/'client-owner.json'
            if not path.exists():put(path,client)
            else:require(json.loads(path.read_bytes())==client,'local client cannot be rebound')
            return result


class NativeOwner:
    def __init__(self, run_root, *, allow_native=False):
        require(not (ROOT/'offline-only.json').exists(), 'R7 is offline only; owner startup disabled')
        require(allow_native and sys.platform == 'linux', 'explicit Linux native trial opt-in required')
        self.root = Path(run_root); self.root.mkdir(parents=False, exist_ok=False)
        self.started = time.monotonic()
        self.tainted = False; self.monitor_stop = threading.Event(); self.monitor = None
        self.pump = None; self.service = None; self.ready_observation = None
        self.common = private_common(self.root)
        self.common.become_subreaper(); self.common.own('supervisor',os.getpid())
        self.model = None; self.model_identity = None; self.watchdog=None; self.watchdog_identity=None
        put(self.root/'owner-intent.json',{'identity':full_domain(),'stop_epoch':None,
            'stop_policy':'explicit stop, owner death, model/watchdog failure or unresolved resource closure',
            'max_model_POSTs_per_job':3,'task_deadline_seconds':900,'automatic_resume':False,
            'automatic_task_submission':False,'validation_scope':'final_acceptance_candidate'})
        (self.root/'global-posts').mkdir()

    def guard(self):
        require(not self.tainted, 'native owner is tainted')
        require(not (self.root/'stop-requested.json').exists(), 'service stop requested')
        for process,row in ((self.model,self.model_identity),(self.watchdog,self.watchdog_identity)):
            if process is not None:
                require(process.poll() is None and self.common.same(row), 'owned service process exited or identity changed')

    def prerequisite(self):
        expected = deployment()
        require(socket.gethostname() == expected['hostname'] and self.common.BOOT == expected['boot_id']
                and [os.getuid(),os.geteuid(),os.getgid(),os.getegid()] == expected['execution_ids'], 'deployment identity changed')
        storage = Path(expected['persistent_root']).stat()
        require([storage.st_uid, storage.st_gid] == expected['workspace_owner'], 'persistent storage identity changed')
        for port in (8000,8001):
            with socket.socket() as s:
                require(s.connect_ex(('127.0.0.1',port)) != 0, 'model port occupied')
        GenerationPolicy.load(); pinned_sources()
        compiler=vivado()/'bin/xvlog'
        require(compiler.is_file() and os.access(compiler,os.X_OK),'compiler entry unavailable')
        for p in (STATE/'store'/'jobs').glob('*'):
            require((p/'final.json').is_file() or (p/'failure-final.json').is_file() or (p/'expired.json').is_file(),
                    'unfinished persistent job: no automatic restart or replay')
        for p in (STATE/'store'/'leases').glob('*'):
            require((p/'released.json').is_file(),'persistent lease closure unknown')
        put(self.root/'prerequisite.json',{'deployment_predecessor_gate':'verified separately before service launch',
            'compiler_entry_exists':True,'compiler_execution_tested':False,
            'new_test_solve_requests':0,'epoch':time.time()})

    def start(self):
        self.prerequisite(); self.guard()
        lock = json.loads((ROOT.parent/'model'/'model-runtime-lock.json').read_bytes()); req = lock['required_runtime']
        require(runtime_fingerprint(req) == lock['required_runtime_sha256'] == RUNTIME, 'runtime lock changed')
        require(self.common.sha(req['argv'][0]) == req['backend_sha256'], 'backend binary changed')
        require(self.common.sha(ROOT.parent/'model'/'models.lock.json') == req['model_lock_sha256'], 'model manifest changed')
        weights = Path(req['argv'][req['argv'].index('-m')+1])
        require(weights.stat().st_size == 22134528992 and self.common.sha(weights) == WEIGHT, 'model weights changed')
        self.guard()
        self.watchdog,self.watchdog_identity=self.common.spawn('watchdog',
            ['/opt/python/bin/python3','-B',str(ROOT/'native_watchdog.py'),str(self.root)],self.common.env_for(),cwd=ROOT)
        ready_end=min(self.started+300,time.monotonic()+8)
        while time.monotonic()<ready_end and not (self.root/'watchdog-ready.json').exists():
            require(self.watchdog.poll() is None,'native watchdog exited before ready');time.sleep(.02)
        require((self.root/'watchdog-ready.json').is_file(),'independent watchdog not ready')
        self.model,self.model_identity = self.common.spawn('model',req['argv'],self.common.env_for(),cwd=ROOT)
        end = time.monotonic()+300
        # Use the existing loopback HTTP helper; no task generation during loading.
        while time.monotonic() < end:
            self.guard(); require(self.model.poll() is None,'model exited while loading')
            try:
                props = self.common.http('/props'); slots = self.common.http('/slots')
                observed={**req,'model_properties':{k:props.get(k) for k in req['model_properties']},
                          'chat_template_sha256':digest(props.get('chat_template','').encode())}
                require(runtime_fingerprint(observed)==RUNTIME and len(slots)==1 and slots[0].get('is_processing') is False,
                        'actual model fingerprint/slot differs')
                actual_argv=(Path('/proc')/str(self.model_identity['pid'])/'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
                require(actual_argv==req['argv'],'actual backend argv differs')
                put(self.root/'model-service.json',{'identity':self.model_identity,'runtime_sha256':RUNTIME,
                    'owner_identity':self.common.ident(os.getpid()),'watchdog_identity':self.watchdog_identity,
                    'weight_sha256':WEIGHT,'weight_bytes':weights.stat().st_size,'props':props,
                    'owner_root':str(self.root),'stop_epoch':None,'max_POSTs_per_job':3,
                    'cold_load_seconds':time.monotonic()-self.started,'runtime_accepted':False})
                store = JobStore(STATE/'store',clock_domain=full_domain())
                platform = NativePlatform(self); launcher = ObservedLauncher(platform)
                launcher.evidence_scope = 'native_process_observation_connectivity_only'
                self.service=TrialService(store,Dispatcher(store,launcher),self)
                self.refresh_health()
                self.monitor=threading.Thread(target=self._monitor,name='rtl-owner-monitor',daemon=True)
                self.monitor.start()
                return self.service
            except (OSError,ValueError):time.sleep(.2)
        raise TimeoutError('original model loading window expired')

    def _monitor(self):
        try:
            while not self.monitor_stop.wait(1.):
                self.common.capture_owned()
                self.guard(); self.refresh_health()
        except BaseException as exc:
            self.tainted = True
            put(self.root/'monitor-failed.json',{'error_type':type(exc).__name__})

    def refresh_health(self):
        self.guard()
        req=json.loads((ROOT.parent/'model'/'model-runtime-lock.json').read_bytes())['required_runtime']
        props=self.common.http('/props');slots=self.common.http('/slots')
        observed={**req,'model_properties':{k:props.get(k) for k in req['model_properties']},
                  'chat_template_sha256':digest(props.get('chat_template','').encode())}
        require(runtime_fingerprint(observed)==RUNTIME,'live runtime fingerprint changed')
        require(type(slots) is list and len(slots)==1,'one backend slot required')
        cards=self.common.gpu()
        require(len(cards)==1 and cards[0]['used']>=0,'measured GPU memory unavailable')
        self.ready_observation={'at_monotonic':time.monotonic(),'epoch':time.time(),
            'runtime_sha256':RUNTIME,'template_sha256':req['chat_template_sha256'],
            'gpu':cards[0],'backend_busy':bool(slots[0].get('is_processing'))}
        self.common.save(self.root/'health-observed.json',self.ready_observation)

    def health(self):
        observed=self.ready_observation
        live=bool(observed and time.monotonic()-observed['at_monotonic']<10. and not self.tainted)
        try:self.guard()
        except Exception:live=False
        live=live and self.monitor is not None and self.monitor.is_alive()
        live=live and self.pump is not None and self.pump.thread.is_alive() and self.pump.error is None
        return {'ready':bool(live),'track':'rtl','model':'Qwen/Qwen3.6-35B-A3B',
            'vram_gb':round(observed['gpu']['used']/1e9,3) if observed else None,
            'vram_bytes':observed['gpu']['used'] if observed else None,
            'reason':None if live else '模型、调度线程或资源状态尚未就绪'}

    def close(self):
        self.monitor_stop.set()
        if self.monitor: self.monitor.join(3.)
        rows=self.common.capture_owned()
        self.common.stop_rows([q for q in rows if q['pid'] != os.getpid()
            and (self.watchdog_identity is None or self.common.identity_key(q)!=self.common.identity_key(self.watchdog_identity))],.2)
        if self.model:
            try:self.model.wait(timeout=5)
            except Exception:pass
        if self.watchdog:
            put(self.root/'watchdog-stop.json',{'epoch':time.time()})
            self.watchdog.wait(timeout=15.)
            ended=json.loads((self.root/'watchdog-ended.json').read_bytes())
            require(ended['cleanup_verified'] is True and not self.common.same(self.watchdog_identity),'independent watchdog closure missing')
            put(self.root/'watchdog-exit-observed.json',{'identity':self.watchdog_identity,
                'returncode':self.watchdog.returncode,'wait_completed':True,'nonrunning':True,'epoch':time.time()})
        rows=self.common.capture_owned()
        left=[q for q in rows if q['pid']!=os.getpid() and self.common.same(q)]
        put(self.root/'owner-resources-closed.json',{'identity':full_domain(),'remaining_live':left,
            'registered_snapshot':compact_rows(self.common,rows),'owner_still_running':True,
            'monitor_joined':self.monitor is None or not self.monitor.is_alive(),'epoch':time.time()})
        require(not left and (self.monitor is None or not self.monitor.is_alive()),'native owner closure unknown')
