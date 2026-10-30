"""Run the unchanged official baseline with a bounded process lifecycle."""
import os,time
from pathlib import Path
from adapter_contracts import require,RUNTIME,MODEL
from baseline_bridge import BaselineReply,invocation_spec,registry_manifest
from generation_policy import GenerationPolicy,ROOT,read_original
from host_completion import Identity,ExitObserver
from prompt_builder import canonical,digest
from rtl_delivery import strict_json

def save(path,value):
    raw=value if type(value) is bytes else canonical(value)
    with Path(path).open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())

class BaselineExecutor:
    """One immutable script invocation under the original queued deadline."""
    def __init__(self,root,model,backend_factory,*,clock=time.monotonic):
        self.root=Path(root);self.model=model;self.factory=backend_factory;self.clock=clock;self.closed=[]
    def run_baseline(self,*,invocation,context,permit):
        require(type(invocation) is bytes,'original baseline invocation required')
        spec=strict_json(invocation);policy=GenerationPolicy.load()
        require(spec==invocation_spec(spec['input_dir'],spec['publication_dir'],context,policy),'baseline execution spec changed')
        require(self.root.resolve()==Path(spec['execution_evidence_dir']).resolve(),'baseline evidence directory differs from job')
        end=context['deadline_monotonic']
        require(self.clock()<end,'baseline deadline before preparation')
        self.root.mkdir(parents=True,exist_ok=False)
        save(self.root/'intent.json',{'invocation_sha256':digest(invocation),'automatic_retry':False})
        process=None;identity=None;observer=None;backend=None;start=None;observed=None
        executed=False;joined=False;idle=False;live=[];error=None;rows=[];global_slot=None
        try:
            # Same native fingerprint/identity check as the agent. This makes no
            # generation call and does not supply knowledge to the baseline.
            preflight=self.model.preflight(context=context)
            save(self.root/'model-preflight.json',preflight)
            pre=strict_json(preflight)
            require(pre['runtime_sha256']==RUNTIME and pre['run_id']==context['run_id']
                    and pre['clock_domain']==context['clock_domain'],'baseline preflight differs')
            require(spec==invocation_spec(spec['input_dir'],spec['publication_dir'],context,policy),
                    'baseline public files or configuration changed during preflight')
            backend=self.factory(self.root/'registry')
            env=backend.env_for();env.update(spec['environment_required'])
            require(env['LLM_BASE_URL']==policy.base_url and env['MODEL_NAME']==MODEL and env['TRACK']=='rtl',
                    'baseline inference environment differs')
            # One conservative global attempt slot, not a claimed actual POST.
            global_slot=self.model.claim_global(context,invocation)
            require(spec==invocation_spec(spec['input_dir'],spec['publication_dir'],context,policy),
                    'baseline public files changed during backend preparation')
            policy.require_current();self.model.guard(end)
            save(self.root/'dispatch-intent.json',{'global_attempt_slot':global_slot,'deadline_monotonic':end})
            start=permit.enter(invocation);self.model.guard(end)
            require(self.clock()<end,'baseline expired immediately before spawn')
            executed='unknown'
            process,identity=backend.spawn('official-baseline',spec['argv'],env,cwd=ROOT)
            executed=True
            require(identity['boot_id']==context['clock_domain']['boot_id'],'baseline spawned in another boot')
            save(self.root/'spawned.json',{'identity':identity,'executed':True})
            observer=ExitObserver(process,Identity(identity['pid'],identity['start_ticks'],identity['boot_id']),clock=self.clock)
            observer.start();joined=observer.join(max(0.,end-self.clock()))
            if not joined:
                backend.stop_rows([q for q in backend.capture_owned() if q['pid']!=backend.own_pid],.2)
                joined=observer.join(5.)
            observed=observer.snapshot()
            rows=backend.capture_owned();live=[q for q in rows if q['pid']!=backend.own_pid and backend.same(q)]
            if live:
                backend.stop_rows(live,.2)
                raise RuntimeError('baseline left live descendants; preserve abnormal closure')
            require(joined and observed is not None and observed.error_type is None
                    and observed.returncode==0 and observed.observed_at<=end,'baseline process exit failed or late')
            self.model.guard(end);idle=self.model.idle(end=end)
            require(idle,'baseline backend slot is still active')
            policy.require_current();require(self.clock()<end,'baseline evidence exceeded chain deadline')
        except BaseException as exc:
            error={'type':type(exc).__name__,'executed':executed}
            if backend is not None:
                try:
                    rows=backend.capture_owned();left=[q for q in rows if q['pid']!=backend.own_pid and backend.same(q)]
                    if left:backend.stop_rows(left,.2)
                    if observer is not None:joined=observer.join(5.);observed=observer.snapshot()
                except BaseException as cleanup:error['cleanup_error']=type(cleanup).__name__
            try:idle=self.model.idle(end=end)
            except BaseException:idle=False
            # Even successful emergency cleanup cannot convert an error into a
            # normal result. The serial lease remains held by validation.
            self.model.tainted=True
        output=Path(spec['output_dir'])
        solution=read_original(output/'solution.v') if (output/'solution.v').is_file() else b''
        trace=read_original(output/'trace.jsonl',4194304) if (output/'trace.jsonl').is_file() else b''
        # Official outputs are staged in a separate directory by the native
        # handoff; publication belongs to baseline_bridge, never the script.
        after_script=digest(read_original(ROOT/'baseline.py'))
        after_manifest=digest(read_original(ROOT/'manifest.json'))
        rows_raw=canonical(rows)
        try:registered=registry_manifest(self.root.parent,strict=error is None)
        except BaseException as exc:
            error={'type':type(exc).__name__,'executed':executed,'registry_manifest_failed':True}
            self.model.tainted=True
            registered={}
        receipt={'schema':'official-baseline-closure-v1','request_id':context['request_id'],'run_id':context['run_id'],
            'clock_domain':context['clock_domain'],'invocation_sha256':digest(invocation),
            'source_before_sha256':policy.baseline_sha256,'source_after_sha256':after_script,
            'manifest_before_sha256':policy.manifest_sha256,'manifest_after_sha256':after_manifest,
            'solution_sha256':digest(solution),'trace_sha256':digest(trace),'script_invocations':1 if start is not None else 0,
            'automatic_retries':0,'script_executed':executed,'process_returncode':None if observed is None else observed.returncode,
            'process_exit_observed':bool(joined and observed is not None and observed.error_type is None),
            'process_observer_joined':joined,
            'normal_closure':error is None,'remaining_live':live,
            'model_backend_idle':idle,'inflight_requests':0 if idle else None,'process_identity':identity,
            'started_at_monotonic':start,'closed_at_monotonic':self.clock(),'runtime_sha256':RUNTIME,
            'base_url':policy.base_url,'served_model':MODEL,'reported_HTTP_POSTs':None,
            'global_attempt_slot':global_slot,'error':error,'native_runtime_accepted':False,
            'owned_rows_ref':{'path':'native-baseline/original-rows.json','sha256':digest(rows_raw),'bytes':len(rows_raw)},
            'registry_manifest':registered,
            'POST_count_basis':'unknown; immutable official script permits at most one direct call',
            'exit_observation':None if observed is None else {'pid':observed.identity.pid,
                'start_ticks':observed.identity.start_ticks,'boot_id':observed.identity.boot_id,
                'returncode':observed.returncode,'observed_at_monotonic':observed.observed_at,'error_type':observed.error_type}}
        save(self.root/'original-rows.json',rows_raw);save(self.root/'closure.json',receipt)
        if error is None and idle and joined:
            self.closed.append({'request_id':context['request_id'],'invocation_sha256':digest(invocation),
                                'kind':'official_baseline_process','closed':True})
        return BaselineReply(solution,trace,canonical(receipt))

def native_executor(root,model,*,allow_native=False):
    require(not (ROOT/'offline-only.json').exists(),'R8 native baseline is disabled pending independent runtime acceptance')
    require(allow_native,'explicit native baseline opt-in required')
    from native_runtime import private_common
    def factory(registry):
        registry.mkdir()
        common=private_common(registry);common.own_pid=os.getpid()
        common.become_subreaper();common.own('baseline-worker',common.own_pid)
        return common
    return BaselineExecutor(root,model,factory)
