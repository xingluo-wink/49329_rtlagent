"""Persistent formal job handoff. No process/network service starts on import.

The only implemented execution path requires an injected, unaccepted platform.
Durable intents are never recovered by replay. An unknown closure keeps the
serial lease held. Source consistency is not native host authentication.
"""
import json
import os
from pathlib import Path
import re
import time
import uuid
from adapter_contracts import BoundaryError, require, finite
from artifact_store import _publish
from prompt_builder import canonical, digest
from rtl_delivery import strict_json
from formal_controller import Controller,Budget,Permit
from generation_policy import GenerationPolicy
from baseline_bridge import run_baseline,validate_reply,BaselineReply,invocation_spec,registry_manifest
from host_completion import Identity, ExitObserver
from compiler_repair import ArtifactReader


class Conflict(BoundaryError): pass
class Busy(BoundaryError): pass
class Unavailable(BoundaryError): pass


def put(path, value):
    raw = value if type(value) is bytes else canonical(value)
    with Path(path).open('xb') as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    return {'sha256': digest(raw), 'bytes': len(raw)}


def read(path, limit=4*1024*1024):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'real evidence file required')
    with path.open('rb') as f: raw = f.read(limit+1)
    require(len(raw) <= limit, 'evidence limit; no clipping')
    return raw


def domain(row):
    require(type(row) is dict and set(row) == {'host_id','boot_id','clock_id','pid','start_ticks'}, 'clock identity fields')
    require(all(type(row[k]) is str and row[k] for k in ('host_id','boot_id','clock_id','start_ticks'))
            and row['clock_id'] == 'CLOCK_MONOTONIC' and row['start_ticks'].isdigit()
            and type(row['pid']) is int and row['pid'] > 0, 'complete monotonic identity required')
    return json.loads(canonical(row))


def same_clock(a, b):
    domain(a); domain(b)
    require(all(a[k] == b[k] for k in ('host_id','boot_id','clock_id')), 'cross-host/boot/clock handoff rejected')


def execution_domain(row):
    domain(row)
    return {k: row[k] for k in ('boot_id','pid','start_ticks')}


def detached(value):
    return json.loads(canonical(value))


def identity_key(row):
    require(type(row) is dict and type(row.get('pid')) is int and row['pid'] > 0
            and type(row.get('start_ticks')) is str and row['start_ticks'].isdigit()
            and type(row.get('boot_id')) is str and row['boot_id'], 'complete process identity required')
    return row['boot_id'], row['pid'], row['start_ticks']


def checked_request(request):
    require(type(request) is dict and {'task_id','prompt'} <= set(request)
            <= {'task_id','nonce','mode','prompt','interface','deadline_s'}, 'formal request fields only')
    q = {'nonce':'','mode':'agent','interface':'','deadline_s':900, **request}
    require(all(type(q[k]) is str for k in ('task_id','nonce','mode','prompt','interface')), 'request text fields')
    require(q['mode'] in ('agent','baseline'), 'explicit agent or unchanged official baseline mode required')
    require(0 < len(q['task_id']) <= 256 and len(q['nonce']) <= 256, 'identifier limit')
    require(type(q['deadline_s']) is int and 0 < q['deadline_s'] <= 86400, 'deadline must be integer seconds')
    require(q['prompt'].strip() and '\x00' not in q['prompt']+q['interface'], 'public UTF-8 text required')
    require(len(q['prompt'].encode())+len(q['interface'].encode()) <= 200000, 'public input limit; no truncation')
    return q


class JobStore:
    def __init__(self, root, *, clock=time.monotonic, clock_domain):
        self.clock = clock; self.domain = domain(clock_domain)
        self.root = Path(root).absolute()
        require(not self.root.is_symlink(), 'linked job store rejected')
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        for name in ('jobs','leases'): (self.root/name).mkdir(exist_ok=True)

    def job(self, job_id):
        require(type(job_id) is str and re.fullmatch(r'[0-9a-f]{32,64}',job_id), 'safe job ID required')
        p = self.root/'jobs'/job_id
        require(not p.is_symlink() and p.resolve().parent == self.root/'jobs', 'job path escape')
        return p

    def submit(self, request, *, queued_at=None, outer_deadline=None):
        now = self.clock(); start = now if queued_at is None else queued_at
        require(finite(now) and finite(start) and start <= now, 'enqueue clock')
        q = checked_request(request)
        end = start + min(900, q['deadline_s'])
        if outer_deadline is not None:
            require(finite(outer_deadline), 'finite outer deadline'); end = min(end,outer_deadline)
        policy=GenerationPolicy.load().audit()
        if self.clock()>=end:raise Unavailable('request admission consumed its absolute task deadline')
        binding = {'request':q,'outer_deadline_monotonic':outer_deadline,'generation_policy':policy}
        request_sha = digest(canonical(binding))
        job_id = digest(('nonce:'+q['nonce']).encode()) if q['nonce'] else uuid.uuid4().hex
        p = self.job(job_id)
        try: p.mkdir()
        except FileExistsError:
            try: old = strict_json(read(p/'ticket.json'))
            except Exception as e: raise Busy('existing partial job is not replayable') from e
            if old['request_sha256'] != request_sha: raise Conflict('nonce already binds a different full request')
            return self.status(job_id)
        put(p/'enqueue-intent.json', {'job_id':job_id,'request_sha256':request_sha,'no_replay':True})
        inp=p/'input'; inp.mkdir()
        refs={}
        for name,text in [('prompt.txt',q['prompt']),('interface.txt',q['interface'])]: refs[name]={'path':name,**put(inp/name,text.encode())}
        ticket={'schema':'formal-job-ticket-v1','job_id':job_id,'request_sha256':request_sha,'binding':binding,
                'queued_at_monotonic':start,'deadline_monotonic':end,'enqueue_clock_domain':self.domain,
                'public_files':refs,'public_fields_sha256':digest(canonical({'public_spec':q['prompt'],'interface':q['interface']})),
                'max_model_attempts':1 if q['mode']=='baseline' else 3,
                'generation_policy':policy,
                'automatic_resume':False,'runtime_accepted':False}
        put(p/'ticket.json',ticket)
        # A partial directory or missing ready receipt is never dispatchable.
        put(p/'queued.json', {'ticket_sha256':digest(canonical(ticket))})
        return self.status(job_id)

    def ticket(self, job_id):
        p=self.job(job_id)
        if not p.exists():raise FileNotFoundError('unknown job')
        raw=read(p/'ticket.json'); ticket=strict_json(raw)
        require(strict_json(read(p/'queued.json')) == {'ticket_sha256':digest(raw)}, 'queue commit missing or changed')
        require(ticket['job_id']==job_id and ticket['request_sha256']==digest(canonical(ticket['binding'])), 'request binding changed')
        q=checked_request(ticket['binding']['request'])
        require(ticket['max_model_attempts']==(1 if q['mode']=='baseline' else 3)
                and ticket['generation_policy']==ticket['binding']['generation_policy']==GenerationPolicy.load().audit()
                and ticket['automatic_resume'] is False, 'job budget or generation policy changed')
        start=ticket['queued_at_monotonic']; outer=ticket['binding']['outer_deadline_monotonic']
        require(finite(start) and (outer is None or finite(outer)), 'stored enqueue clock')
        expected=start+min(900,q['deadline_s'])
        require(ticket['deadline_monotonic']==(expected if outer is None else min(expected,outer)), 'deadline reset')
        domain(ticket['enqueue_clock_domain'])
        require(set(ticket['public_files'])=={'prompt.txt','interface.txt'}, 'public file whitelist')
        for name,field in [('prompt.txt','prompt'),('interface.txt','interface')]:
            raw=read(p/'input'/name,200000)
            require(ticket['public_files'][name]=={'path':name,'sha256':digest(raw),'bytes':len(raw)} and raw==q[field].encode(), 'public snapshot changed')
        require(ticket['public_fields_sha256']==digest(canonical({'public_spec':q['prompt'],'interface':q['interface']})), 'public semantic binding')
        return ticket

    def status(self, job_id):
        p=self.job(job_id);ticket=self.ticket(job_id)
        out={'job_id':job_id,'task_id':ticket['binding']['request']['task_id'],'status':'queued','runtime_accepted':False}
        if (p/'final.json').exists():
            final=strict_json(read(p/'final.json'));out['status']=final['status']
            require(final['job_id']==job_id and final['ticket_sha256']==digest(canonical(ticket)), 'final ticket binding')
            launch=strict_json(read(p/'launch-intent.json'));raw=read(p/'launcher-receipt.json')
            require(digest(raw)==final['launcher_receipt_sha256'] and launch['lease_id']==final['lease_id'], 'final closure binding')
            worker,output=validate_closure(p,ticket,launch,raw,self.clock())
            published=final['published_at_monotonic'];observed=strict_json(raw)['observed_at_monotonic']
            require(final['status'] in ('done','closed_late') and finite(published) and published>=observed,
                    'final publication state/time invalid')
            require(final['status']!='done' or published<ticket['deadline_monotonic'], 'late final cannot claim done')
            require(worker==final['execution_clock_domain'] and final['known_identity_sources']==output['known_identity_sources'], 'final identity evidence changed')
            if final['status']=='done':
                require(final['output_refs']==output['refs'], 'completed output changed')
                out.update(solution=output['solution'].decode(),trace=output['trace'].decode(),selection_basis=output['decision']['selection_basis'],functional_status='unknown',
                           elapsed_s=published-ticket['queued_at_monotonic'])
        elif (p/'launch-intent.json').exists():out['status']='attempted_or_unknown'
        elif (p/'expired.json').exists():out['status']='expired_not_started'
        return out


def checked_output(job, ticket, worker):
    if ticket['binding']['request']['mode']=='baseline':
        return checked_baseline_output(job,ticket,worker)
    output=job/'output'; decision_raw=read(output/'selection.json');decision=strict_json(decision_raw)
    solution=read(output/'solution.v',1024*1024);trace=read(output/'trace.jsonl')
    require(decision['run_id']==ticket['job_id'] and decision['public_fields_sha256']==ticket['public_fields_sha256']
            and decision['clock_domain']==execution_domain(worker)
            and decision['absolute_deadline_monotonic']==ticket['deadline_monotonic'], 'worker output task/clock mismatch')
    require(decision['solution_sha256']==digest(solution) and decision['solution_bytes']==len(solution), 'solution incomplete or changed')
    if solution:
        stage=decision['selected_stage']
        require(stage in ('initial','compiler_repair') and decision['selection_basis']=='compile_verified', 'eligible selected stage required')
        event=stage+'-compile'
        manifest_raw=read(output/(event+'.manifest.json'));att_raw=read(output/(event+'.attestation.json'))
        manifest=strict_json(manifest_raw);evidence=decision['compiler_evidence']
        require(evidence['manifest_sha256']==digest(manifest_raw) and evidence['attestation_sha256']==digest(att_raw), 'selected compiler receipt changed')
        require(ArtifactReader(output/(event+'-artifacts')).read(manifest['source'],200000)==solution, 'selected solution differs from compiled source')
    else:
        require(decision['selection_basis']=='no_eligible_candidate' and decision['selected_stage'] is None
                and decision['compiler_evidence'] is None, 'empty output cannot claim selected compiler success')
    require(decision['functional_status']==decision['interface_compatibility']=='unknown'
            and decision['runtime_accepted'] is False and decision['formal_submission_ready'] is False, 'unaccepted result promoted')
    require(0 <= decision['model_claims'] <= 3 and decision['post_outcome_unknown']==decision['tool_outcome_unknown']==0, 'unclosed request/tool evidence')
    rows=[strict_json(line) for line in trace.splitlines()]
    require(rows and rows[-1]['tool']=='task_closed' and rows[-1]['native_closure_verified'] is False
            and all(row['sequence']==i for i,row in enumerate(rows)), 'trace incomplete')
    return {'decision':decision,'solution':solution,'trace':trace,'refs':{name:{'sha256':digest(raw),'bytes':len(raw)} for name,raw in [('selection.json',decision_raw),('solution.v',solution),('trace.jsonl',trace)]}}


def checked_baseline_output(job,ticket,worker):
    output=job/'output';raw=read(output/'selection.json');decision=strict_json(raw)
    solution=read(output/'solution.v',1048576);trace=read(output/'trace.jsonl')
    spec=strict_json(read(output/'baseline-invocation.json'))
    expected_context={'run_id':ticket['job_id'],'request_id':ticket['job_id']+'.baseline',
        'clock_domain':execution_domain(worker),'public_fields_sha256':ticket['public_fields_sha256'],
        'queued_at_monotonic':ticket['queued_at_monotonic'],'deadline_monotonic':ticket['deadline_monotonic']}
    require(spec==invocation_spec(job/'input',output,expected_context,GenerationPolicy.load()),
            'baseline invocation paths, environment or fixed controls changed')
    receipt_raw=read(output/'baseline-executor-receipt.json');receipt=strict_json(receipt_raw)
    require(spec['run_id']==ticket['job_id'] and spec['request_id']==ticket['job_id']+'.baseline'
            and spec['public_fields_sha256']==ticket['public_fields_sha256']
            and spec['clock_domain']==execution_domain(worker)
            and spec['queued_at_monotonic']==ticket['queued_at_monotonic']
            and spec['deadline_monotonic']==ticket['deadline_monotonic']
            and spec['generation_policy']==ticket['generation_policy'], 'baseline invocation cross-bound')
    dispatch=strict_json(read(job/'baseline-dispatch.json'))
    require(dispatch['invocation_sha256']==digest(canonical(spec))
            and dispatch['clock_domain']==execution_domain(worker),'baseline dispatch binding')
    validate_reply(BaselineReply(solution,trace,receipt_raw),spec,dispatch['started_at_monotonic'],receipt['closed_at_monotonic'])
    require(solution==read(output/'baseline-solution.original') and trace==read(output/'baseline-trace.original'),
            'official baseline output was rewritten')
    require(decision['schema']=='formal-baseline-output-v1' and decision['run_id']==ticket['job_id']
            and decision['public_fields_sha256']==ticket['public_fields_sha256']
            and decision['clock_domain']==execution_domain(worker)
            and decision['absolute_deadline_monotonic']==ticket['deadline_monotonic']
            and decision['invocation_sha256']==digest(canonical(spec))
            and decision['solution_sha256']==digest(solution) and decision['solution_bytes']==len(solution)
            and decision['baseline_process_identity']==receipt['process_identity'], 'baseline decision changed')
    require(decision['selection_basis']=='official_baseline_unchecked' and decision['compiler_evidence'] is None
            and decision['selected_stage'] is None and decision['model_claims']==decision['baseline_script_invocations']==1
            and decision['post_outcome_unknown']==decision['tool_outcome_unknown']==decision['tool_handoffs']==0
            and decision['reported_HTTP_POSTs']==receipt['reported_HTTP_POSTs']
            and decision['functional_status']==decision['interface_compatibility']=='unknown'
            and decision['runtime_accepted'] is False and decision['formal_submission_ready'] is False
            and decision['official_trace_unmodified'] is True, 'baseline must not inherit agent/compiler acceptance')
    require(not any(output.glob('*-compile*')) and not any(output.glob('*.request.json')),
            'baseline output contains agent/compiler claims')
    return {'decision':decision,'solution':solution,'trace':trace,'refs':{name:{'sha256':digest(data),'bytes':len(data)}
            for name,data in [('selection.json',raw),('solution.v',solution),('trace.jsonl',trace)]}}


def run_worker(job, ticket, launch, *, clock, worker_domain, adapters, close_resources):
    """Dedicated-worker body; tests call with a synthetic identity and platform.

    This function does not create or authenticate an OS process. A launcher must
    establish that isolation. It must not be called in an API server as native work.
    """
    ticket=detached(ticket);launch=detached(launch)
    require(read(job/'ticket.json')==canonical(ticket)
            and strict_json(read(job/'queued.json'))=={'ticket_sha256':digest(canonical(ticket))}
            and read(job/'launch-intent.json')==canonical(launch), 'worker handoff differs from durable originals')
    worker=domain(worker_domain);same_clock(ticket['enqueue_clock_domain'],worker)
    require(identity_key(worker)!=identity_key(launch['supervisor_clock_domain']), 'API/supervisor process cannot be its own worker')
    require(launch['ticket_sha256']==digest(canonical(ticket)) and launch['job_id']==ticket['job_id'], 'launch binding')
    now=clock(); require(ticket['queued_at_monotonic']<=now<ticket['deadline_monotonic'], 'queue budget exhausted')
    transfer={'enqueue_clock_domain':ticket['enqueue_clock_domain'],'execution_clock_domain':worker,
              'queued_at_monotonic':ticket['queued_at_monotonic'],'deadline_monotonic':ticket['deadline_monotonic'],
              'observed_at_monotonic':now,'same_host_boot_clock_required':True,'ticket_sha256':digest(canonical(ticket))}
    put(job/'worker-intent.json',transfer)
    class BoundAdapters:
        # Controller snapshots input again inside the worker. Compare its actual
        # public hash BEFORE the first model-facing operation, not only at export.
        def invoke(self,method,kwargs):
            context=kwargs['context']
            require(context['run_id']==ticket['job_id']
                    and context['public_fields_sha256']==ticket['public_fields_sha256']
                    and context['clock_domain']==execution_domain(worker)
                    and context['deadline_monotonic']==ticket['deadline_monotonic'], 'worker public/clock binding changed')
            return getattr(adapters,method)(**kwargs)
        def preflight(self,**kw):return self.invoke('preflight',kw)
        def tokenize(self,**kw):return self.invoke('tokenize',kw)
        def generate(self,**kw):return self.invoke('generate',kw)
        def compile(self,**kw):return self.invoke('compile',kw)
    if ticket['binding']['request']['mode']=='baseline':
        for name,meta in ticket['public_files'].items():
            original=read(job/'input'/name,200000)
            require(meta=={'path':name,'bytes':len(original),'sha256':digest(original)},'baseline public snapshot changed')
        budget=Budget(clock,execution_domain(worker),ticket['queued_at_monotonic'],ticket['deadline_monotonic'])
        context={'run_id':ticket['job_id'],'request_id':ticket['job_id']+'.baseline',
            'clock_domain':execution_domain(worker),'public_fields_sha256':ticket['public_fields_sha256'],
            'queued_at_monotonic':ticket['queued_at_monotonic'],'deadline_monotonic':ticket['deadline_monotonic']}
        class BaselinePermit(Permit):
            def enter(self,payload):
                started=super().enter(payload)
                put(job/'baseline-dispatch.json',{'invocation_sha256':digest(payload),
                    'clock_domain':execution_domain(worker),'started_at_monotonic':started})
                # Publication time is inside the same budget; returned start is
                # the permit observation, not a claim of a native POST timestamp.
                budget.check()
                return started
        def permission(payload):
            return BaselinePermit(budget,digest(payload),lambda tool,**kw:None)
        decision=run_baseline(job/'input',job/'output',context=context,clock=clock,
            executor=getattr(adapters,'run_baseline',None),permit=permission,put=put)
    else:
        decision=Controller(job/'input',job/'output',adapters=BoundAdapters(),clock=clock,
            clock_domain=execution_domain(worker),queued_at=ticket['queued_at_monotonic'],
            outer_deadline=ticket['deadline_monotonic'],run_id=ticket['job_id']).run()
    closure=close_resources(job,detached(ticket),detached(worker))
    require(type(closure) is bytes, 'raw worker resource receipt required')
    resource_ref=put(job/'worker-resource-receipt.json',closure)
    result={'job_id':ticket['job_id'],'ticket_sha256':digest(canonical(ticket)),
            'execution_clock_domain':worker,'worker_resources':resource_ref,
            'decision_sha256':digest(canonical(decision)), 'worker_exit_claimed':False,
            'returned_at_monotonic':clock(),'runtime_accepted':False}
    put(job/'worker-returned.json',result)
    return result


class ObservedLauncher:
    """Actual ExitObserver code around an injected platform's worker handle.

    No native platform is supplied. The platform owns identity-qualified cleanup
    and must account for the worker and all descendants, including adopted ones.
    """
    def __init__(self, platform=None, *, clock=time.monotonic):
        self.platform=platform;self.clock=clock
        self.available=platform is not None
        self.evidence_scope='synthetic_or_unaccepted_platform'

    def execute(self, job, ticket, launch):
        require(self.available, 'native platform/model is unconfigured')
        ticket=detached(ticket);launch=detached(launch)
        process=None;identity=None;observer=None;report={'spawn_handoff':False,'executed':False,'normal_closure':False}
        try:
            require(self.clock()<ticket['deadline_monotonic'], 'deadline before worker spawn')
            report['spawn_handoff']=True;report['executed']='unknown'
            process=self.platform.spawn(job,detached(ticket),detached(launch))
            report['executed']=True
            identity=domain(self.platform.identity(process));same_clock(ticket['enqueue_clock_domain'],identity)
            report['execution_clock_domain']=identity
            put(job/'worker-spawned.json',report)
            observer=ExitObserver(process,Identity(identity['pid'],identity['start_ticks'],identity['boot_id']),clock=self.clock)
            observer.start()
            remaining=max(0.,ticket['deadline_monotonic']-self.clock())
            joined=observer.join(remaining)
            # An elapsed deadline is not proof of exit. Only the platform's
            # identity-bound close operation may terminate an owned process.
            rows=self.platform.close_owned(process,identity,terminate=not joined)
            if not joined: joined=observer.join(10.)
            obs=observer.snapshot()
            observation=None if obs is None else {**vars(obs.identity),'returncode':obs.returncode,
                'observed_at_monotonic':obs.observed_at,'error_type':obs.error_type}
            report.update(observer_joined=joined,exit_observation=observation,
                          owned_snapshot=rows,normal_closure=bool(joined and obs is not None))
        except BaseException as exc:
            report['error_type']=type(exc).__name__
            # If registration failed after Popen, raw PID killing is unsafe.
            # Preserve uncertainty. Emergency cleanup is not normal closure.
            if process is not None and identity is not None:
                try: report['emergency_snapshot']=self.platform.close_owned(process,identity,terminate=True)
                except BaseException as cleanup:report['cleanup_error_type']=type(cleanup).__name__
            if observer is not None: report['emergency_observer_joined']=observer.join(0.)
        report.update(job_id=ticket['job_id'],ticket_sha256=digest(canonical(ticket)),
                      lease_id=launch['lease_id'],observed_at_monotonic=self.clock(),evidence_scope=self.evidence_scope)
        return canonical(report)


def known_tool_identities(job):
    """Union of this job's bound R4 snapshots and retained native registries.

    This checks omission of KNOWN identities. It cannot authenticate the kernel
    or prove a native recorder discovered every child in the first place.
    """
    identities=set();refs={};stages=[]
    for stage in ('initial','compiler_repair'):
        event=stage+'-compile';art=job/'output'/(event+'-artifacts')
        claim=job/'output'/(event+'.attempt-intent.json')
        if not claim.exists():
            require(not art.exists() and not (job/'output'/(event+'.attestation.json')).exists(), 'unclaimed compiler artifacts')
            continue
        stages.append(stage)
        require(art.is_dir() and not art.is_symlink(), 'claimed compiler artifact directory missing')
        read(claim);read(job/'output'/(event+'.job.json'))
        reader=ArtifactReader(art)
        att=strict_json(read(job/'output'/(event+'.attestation.json')))
        manifest_raw=read(job/'output'/(event+'.manifest.json'));manifest=strict_json(manifest_raw)
        require(att['manifest_sha256']==digest(manifest_raw), 'compiler manifest/attestation binding')
        # A compiled stage stays required even if its directory was later removed.
        # Every artifact referenced by the original compiler manifest must remain.
        for name in ('source','body','staged_dut','result'):reader.read(manifest[name],4*1024*1024)
        for step in manifest['steps']:
            reader.read(step['record'],4*1024*1024);reader.read(step['log'],4*1024*1024)
        original=att['r1_derivation']['original_rows'];raw=reader.read(original,4*1024*1024)
        sources=[(art/original['path'],raw,strict_json(raw))]
        registry=art/'registry'
        p=registry/'process-descendants.json'
        if p.exists():
            raw=read(p);sources.append((p,raw,strict_json(raw)['identities']))
        p=registry/'process-history.jsonl'
        if p.exists():
            raw=read(p);sources.append((p,raw,[strict_json(line) for line in raw.splitlines()]))
        directory=registry/'processes'
        if directory.exists():
            require(directory.is_dir() and not directory.is_symlink(), 'linked native registry rejected')
            for p in sorted(directory.glob('*.json')):
                raw=read(p);sources.append((p,raw,[strict_json(raw)]))
        for p,raw,rows in sources:
            require(type(rows) is list, 'identity registry list required')
            refs[p.relative_to(job).as_posix()]={'sha256':digest(raw),'bytes':len(raw)}
            identities.update(identity_key(row) for row in rows)
    baseline=job/'native-baseline'
    receipt_path=job/'output/baseline-executor-receipt.json'
    if baseline.exists() or receipt_path.exists():
        require(baseline.is_dir() and not baseline.is_symlink(),'baseline native evidence directory missing or linked')
        receipt_raw=read(receipt_path);receipt=strict_json(receipt_raw)
        original=receipt['owned_rows_ref']
        require(original['path']=='native-baseline/original-rows.json','baseline owned rows path changed')
        original_raw=ArtifactReader(job).read(original,4194304)
        sources=[(job/original['path'],original_raw,strict_json(original_raw))]
        declared=receipt.get('registry_manifest')
        require(type(declared) is dict and declared==registry_manifest(job),
                'baseline registry originals missing, added or changed')
        for name,meta in declared.items():
            data=ArtifactReader(job).read(meta,4194304);p=job/name
            if p.name=='process-descendants.json':rows=strict_json(data)['identities']
            elif p.name=='process-history.jsonl':rows=[strict_json(line) for line in data.splitlines()]
            else:rows=[strict_json(data)]
            sources.append((p,data,rows))
        require(sources and any(identity_key(q)==identity_key(receipt['process_identity'])
                for q in strict_json(original_raw)),'baseline primary identity missing from original rows')
        refs[receipt_path.relative_to(job).as_posix()]={'sha256':digest(receipt_raw),'bytes':len(receipt_raw)}
        for p,data,rows in sources:
            require(type(rows) is list,'baseline registry must contain identity rows')
            refs[p.relative_to(job).as_posix()]={'sha256':digest(data),'bytes':len(data)}
            identities.update(identity_key(row) for row in rows)
    return identities,refs,stages


def validate_closure(job,ticket,launch,raw,now):
    q=strict_json(raw)
    require(q['job_id']==ticket['job_id'] and q['ticket_sha256']==digest(canonical(ticket))
            and q['lease_id']==launch['lease_id'] and q['executed'] is True and q['normal_closure'] is True
            and q['observer_joined'] is True, 'worker exit not established')
    worker=domain(q['execution_clock_domain']);same_clock(ticket['enqueue_clock_domain'],worker)
    require(identity_key(worker)!=identity_key(launch['supervisor_clock_domain']), 'not a dedicated worker identity')
    obs=q['exit_observation']
    require(obs['pid']==worker['pid'] and obs['start_ticks']==worker['start_ticks'] and obs['boot_id']==worker['boot_id']
            and obs['error_type'] is None and type(obs['returncode']) is int and finite(obs['observed_at_monotonic'])
            and launch['handoff_at_monotonic']<=obs['observed_at_monotonic']<=q['observed_at_monotonic']<=now, 'exit observation binding')
    rows=q['owned_snapshot'];require(type(rows) is list and rows, 'owned process snapshot required')
    keys=[identity_key(row) for row in rows]
    require(len(set(keys))==len(keys) and identity_key(worker) in keys, 'worker/unique identities required')
    require(all(row['boot_id']==worker['boot_id'] and row.get('state') in ('exited','Z') for row in rows), 'live/unknown descendant')
    known,known_refs,stages=known_tool_identities(job)
    if ticket['binding']['request']['mode']=='baseline':
        original=strict_json(read(job/'output/baseline-executor-receipt.json'))
        known.add(identity_key(original['process_identity']))
        known_refs['output/baseline-executor-receipt.json']={'sha256':digest(read(job/'output/baseline-executor-receipt.json')),
                                                         'bytes':len(read(job/'output/baseline-executor-receipt.json'))}
    require(known<=set(keys), 'owned snapshot omitted a registered compiler/descendant')
    returned=strict_json(read(job/'worker-returned.json'))
    require(returned['job_id']==ticket['job_id'] and returned['ticket_sha256']==digest(canonical(ticket))
            and returned['execution_clock_domain']==worker and returned['worker_exit_claimed'] is False, 'worker returned binding')
    transfer=strict_json(read(job/'worker-intent.json'))
    require(transfer['enqueue_clock_domain']==ticket['enqueue_clock_domain']
            and transfer['execution_clock_domain']==worker and transfer['queued_at_monotonic']==ticket['queued_at_monotonic']
            and transfer['deadline_monotonic']==ticket['deadline_monotonic']
            and transfer['ticket_sha256']==digest(canonical(ticket))
            and finite(transfer['observed_at_monotonic']) and finite(returned['returned_at_monotonic'])
            and launch['handoff_at_monotonic']<=transfer['observed_at_monotonic']
            <=returned['returned_at_monotonic']<=obs['observed_at_monotonic'], 'worker transfer/return ordering')
    resource_raw=read(job/'worker-resource-receipt.json')
    require(returned['worker_resources']=={'sha256':digest(resource_raw),'bytes':len(resource_raw)}, 'worker resources changed')
    resources=strict_json(resource_raw)
    require(resources['job_id']==ticket['job_id'] and resources['execution_clock_domain']==worker
            and resources['model_backend_idle'] is True and resources['inflight_requests']==0
            and resources['tools_closed'] is True and resources['remaining_live']==[], 'model/tools not closed')
    output=checked_output(job,ticket,worker)
    require(len(stages)==output['decision']['tool_handoffs'], 'compiler handoff coverage incomplete')
    require(returned['decision_sha256']==digest(read(job/'output/selection.json')), 'worker decision changed')
    expected=[]
    if ticket['binding']['request']['mode']=='baseline':
        expected=[{'request_id':ticket['job_id']+'.baseline',
            'invocation_sha256':digest(read(job/'output/baseline-invocation.json')),
            'kind':'official_baseline_process','closed':True}]
    else:
        for stage in ('route','initial','compiler_repair'):
            path=job/'output'/(stage+'.request.json')
            if path.exists():expected.append({'request_id':ticket['job_id']+'.'+stage,'payload_sha256':digest(read(path)),'closed':True})
    require(resources['requests']==expected and len(expected)==output['decision']['model_claims'], 'model request closure ledger incomplete')
    require(obs['returncode']==0, 'worker returned nonzero; artifacts retained')
    output['known_identity_sources']=known_refs
    return worker,output


class Dispatcher:
    def __init__(self,store,launcher=None):self.store=store;self.launcher=launcher or ObservedLauncher(clock=store.clock)

    def _lease(self,ticket):
        root=self.store.root/'leases'
        dirs=sorted(root.iterdir())
        require(all(p.is_dir() and not p.is_symlink() and re.fullmatch(r'[0-9]{8}',p.name) for p in dirs), 'lease journal invalid')
        require([int(p.name) for p in dirs]==list(range(1,len(dirs)+1)), 'lease journal gap')
        if dirs:
            previous=dirs[-1]
            if not (previous/'released.json').exists(): raise Busy('earlier worker closure remains unknown')
            released=strict_json(read(previous/'released.json')); prior=strict_json(read(previous/'claimed.json'))
            final_path=self.store.job(prior['job_id'])/'final.json'
            require(released['final_sha256']==digest(read(final_path)) and released['lease_id']==previous.name, 'lease release not bound to final evidence')
            require(self.store.status(prior['job_id'])['status'] in ('done','closed_late'), 'prior closure evidence no longer valid')
        lease=root/('%08d'%(len(dirs)+1))
        try:lease.mkdir()
        except FileExistsError as exc:raise Busy('concurrent dispatcher claimed lease') from exc
        put(lease/'claimed.json',{'job_id':ticket['job_id'],'ticket_sha256':digest(canonical(ticket)),
                                 'supervisor_clock_domain':self.store.domain,'lease_id':lease.name})
        return lease

    def dispatch(self,job_id):
        s=self.store;p=s.job(job_id);ticket=s.ticket(job_id)
        if (p/'launch-intent.json').exists() or (p/'expired.json').exists():return s.status(job_id)
        same_clock(ticket['enqueue_clock_domain'],s.domain)
        now=s.clock();require(finite(now) and now>=ticket['queued_at_monotonic'], 'queue clock regression')
        if now>=ticket['deadline_monotonic']:
            put(p/'expired.json',{'status':'expired_not_started','model_requests':0,'worker_spawned':False})
            return s.status(job_id)
        if not self.launcher.available:raise Unavailable('no accepted native launcher/model adapter')
        lease=self._lease(ticket)
        launch={'job_id':job_id,'ticket_sha256':digest(canonical(ticket)),'lease_id':lease.name,
                'supervisor_clock_domain':s.domain,'handoff_at_monotonic':s.clock(),'no_replay':True}
        put(p/'launch-intent.json',launch)
        try:
            require(s.clock()<ticket['deadline_monotonic'], 'expired before launch handoff')
            raw=self.launcher.execute(p,detached(ticket),detached(launch))
            put(p/'launcher-receipt.json',raw)
            worker,output=validate_closure(p,ticket,launch,raw,s.clock())
            published=s.clock()
            require(finite(published) and published>=strict_json(raw)['observed_at_monotonic'], 'publication clock regressed')
            status='done' if published<ticket['deadline_monotonic'] else 'closed_late'
            final={'status':status,'job_id':job_id,'ticket_sha256':digest(canonical(ticket)),
                   'execution_clock_domain':worker,'output_refs':output['refs'],
                   'known_identity_sources':output['known_identity_sources'],
                   'launcher_receipt_sha256':digest(raw),'lease_id':lease.name,'published_at_monotonic':published,
                   'runtime_accepted':False,'formal_submission_ready':False,'model_benefit_verified':False}
            put(p/'final.json',final)
            put(lease/'released.json',{'lease_id':lease.name,'final_sha256':digest(canonical(final)),
                                     'native_closure_verified':False,'basis':'validated injected platform evidence'})
        except Exception as exc:
            put(p/'dispatch-unknown.json',{'status':'attempted_or_unknown','error_type':type(exc).__name__,
                                         'automatic_resume':False,'lease_retained':True})
        return s.status(job_id)


class Service:
    def __init__(self,store,dispatcher=None,*,offline_injection=False):
        self.store=store;self.dispatcher=dispatcher or Dispatcher(store);self.offline_injection=offline_injection
    def health(self):
        return {'ready':False,'runtime_accepted':False,'formal_submission_ready':False,
                'track':'rtl','model':'Qwen/Qwen3.6-35B-A3B','vram_gb':None,
                'offline_injection':self.offline_injection,'model_service':'unconfigured_or_unaccepted'}
    def solve(self,request,*,queued_at=None):
        if not self.offline_injection:raise Unavailable('native service is not accepted or configured')
        return self.store.submit(request,queued_at=queued_at)
    def job(self,job_id):
        state=self.store.status(job_id)
        # Keep the internal evidence state. Projection alone never releases a
        # serial lease or claims process closure.
        if state['status']=='attempted_or_unknown':
            ticket=self.store.ticket(job_id)
            if self.store.clock()>=ticket['deadline_monotonic']:
                # API outcome can be empty/L0 while resource closure remains
                # unknown. Never release the durable lease from this projection.
                trace=canonical({'tool':'host_deadline','rc':1,'result':'no_eligible_closed_delivery',
                    'resource_closure':'unknown','automatic_retry':False,'model_calls':None}).decode()+'\n'
                return {**state,'status':'done','solution':'','trace':trace,
                    'execution_evidence_state':'attempted_or_unknown','resource_closure':'unknown',
                    'elapsed_s':max(0.,self.store.clock()-ticket['queued_at_monotonic'])}
            return {**state,'status':'running','execution_evidence_state':'attempted_or_unknown'}
        if state['status'] in ('expired_not_started','closed_late'):
            ticket=self.store.ticket(job_id)
            if state['status']=='closed_late':trace=read(self.store.job(job_id)/'output/trace.jsonl').decode()
            else:trace=canonical({'tool':'host_admission','status':'expired_not_started',
                'model_calls':0,'no_baseline_or_agent_started':True}).decode()+'\n'
            return {**state,'status':'done','solution':'','trace':trace,
                'execution_evidence_state':state['status'],
                'elapsed_s':max(0.,self.store.clock()-ticket['queued_at_monotonic'])}
        return state
