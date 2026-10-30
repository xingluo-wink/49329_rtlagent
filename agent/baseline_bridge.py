"""Immutable official baseline handoff, separate from agent and skill messages.

No executor is provided here: an injected executor must enforce the absolute
deadline, retain process evidence and observe model idleness. A trace or matching
SHA alone is not native authentication. No official baseline is run on import.
"""
from dataclasses import dataclass
from pathlib import Path
import math
from adapter_contracts import require,finite,RUNTIME,WEIGHT,TEMPLATE,MODEL
from prompt_builder import canonical,digest
from rtl_delivery import strict_json
from generation_policy import GenerationPolicy,ROOT,read_original
from compiler_repair import ArtifactReader

@dataclass(frozen=True)
class BaselineReply:
    solution:bytes
    trace:bytes
    receipt:bytes

def registry_manifest(job,*,strict=True):
    """Exact bounded registry file set; never treat missing originals as empty."""
    job=Path(job);registry=job/'native-baseline/registry';processes=registry/'processes'
    if strict:
        require(registry.is_dir() and not registry.is_symlink() and processes.is_dir() and not processes.is_symlink(),
                'baseline registry directories missing or linked')
    if not registry.is_dir() or registry.is_symlink():return {}
    paths=[]
    for name in ('process-history.jsonl','process-descendants.json'):
        p=registry/name
        if strict:require(p.is_file() and not p.is_symlink(),'required baseline registry original missing')
        if p.is_file() and not p.is_symlink():paths.append(p)
    if processes.is_dir() and not processes.is_symlink():paths+=sorted(processes.glob('*.json'))
    if strict:require(len(paths)>2,'baseline process identity records missing')
    result={}
    for p in paths:
        data=read_original(p,4194304);name=p.relative_to(job).as_posix()
        result[name]={'path':name,'bytes':len(data),'sha256':digest(data)}
    return result

def invocation_spec(input_dir,output_dir,context,policy):
    require(type(policy) is GenerationPolicy,'checked generation policy required')
    policy.require_current()
    input_dir=Path(input_dir)
    fields={};refs={}
    for name,field in (('prompt.txt','public_spec'),('interface.txt','interface')):
        if name=='interface.txt' and not (input_dir/name).exists():
            fields[field]='';continue
        data=read_original(input_dir/name,200000)
        fields[field]=data.decode('utf-8');refs[name]={'bytes':len(data),'sha256':digest(data)}
    require(digest(canonical(fields))==context['public_fields_sha256'],'baseline public bytes no longer match the admitted task')
    staged=Path(output_dir).resolve()/'official-output'
    return {'schema':'official-baseline-invocation-v1','request_id':context['request_id'],
        'run_id':context['run_id'],'clock_domain':context['clock_domain'],
        'public_fields_sha256':context['public_fields_sha256'],'public_files':refs,
        'deadline_monotonic':context['deadline_monotonic'],'queued_at_monotonic':context['queued_at_monotonic'],
        'script':str(ROOT/'baseline.py'),'script_sha256':policy.baseline_sha256,
        'shell_entry':str(ROOT/'run_baseline.sh'),'shell_entry_sha256':policy.run_baseline_sha256,
        'input_dir':str(Path(input_dir).resolve()),'output_dir':str(staged),'publication_dir':str(Path(output_dir).resolve()),
        'execution_evidence_dir':str(Path(output_dir).resolve().parent/'native-baseline'),
        'argv':['/bin/sh',str(ROOT/'run_baseline.sh'),str(Path(input_dir).resolve()),str(staged)],
        'environment_required':{'LLM_BASE_URL':policy.base_url,'MODEL_NAME':MODEL,'TRACK':'rtl'},
        'generation_policy':policy.audit(),'runtime_sha256':RUNTIME,'weight_sha256':WEIGHT,
        'template_sha256':TEMPLATE,'max_script_invocations':1,'automatic_retries':0,
        'baseline_HTTP_timeout_s_unchanged':840,'outer_deadline_must_cancel_process_and_observe_backend':True,
        'baseline_prompt_and_sampling_must_remain_unmodified':True,
        'no_agent_no_skills_no_compiler':True}

def validate_reply(reply,spec,start,now):
    require(type(reply) is BaselineReply and type(reply.solution) is bytes and type(reply.trace) is bytes,
            'original baseline outputs required')
    require(len(reply.solution)<=1048576 and len(reply.trace)<=4194304,'baseline output limit; no clipping')
    r=strict_json(reply.receipt)
    require(type(r) is dict and r.get('schema')=='official-baseline-closure-v1'
            and r.get('request_id')==spec['request_id'] and r.get('run_id')==spec['run_id']
            and r.get('clock_domain')==spec['clock_domain']
            and r.get('invocation_sha256')==digest(canonical(spec))
            and r.get('source_before_sha256')==r.get('source_after_sha256')==spec['script_sha256']
            and r.get('manifest_before_sha256')==r.get('manifest_after_sha256')==spec['generation_policy']['manifest_sha256']
            and r.get('solution_sha256')==digest(reply.solution) and r.get('trace_sha256')==digest(reply.trace),
            'baseline provenance mismatch')
    require(r.get('script_invocations')==1 and r.get('automatic_retries')==0
            and r.get('script_executed') is True and r.get('normal_closure') is True and r.get('process_returncode')==0
            and type(r.get('process_returncode')) is int
            and r.get('process_exit_observed') is True and r.get('remaining_live')==[]
            and r.get('model_backend_idle') is True and r.get('inflight_requests')==0,
            'baseline process/backend closure unknown')
    p=r.get('process_identity')
    require(type(p) is dict and type(p.get('pid')) is int and p['pid']>0
            and type(p.get('start_ticks')) is str and p['start_ticks'].isdigit()
            and p.get('boot_id')==spec['clock_domain']['boot_id'],'baseline identity must be bound to this boot')
    ref=r.get('owned_rows_ref')
    require(type(ref) is dict and ref.get('path')=='native-baseline/original-rows.json',
            'baseline owned rows original must be retained at the bound job path')
    rows=strict_json(ArtifactReader(Path(spec['publication_dir']).parent).read(ref,4194304))
    require(type(rows) is list and bool(rows) and all(type(q) is dict and type(q.get('pid')) is int
            and type(q.get('start_ticks')) is str and q['start_ticks'].isdigit()
            and q.get('boot_id')==spec['clock_domain']['boot_id'] for q in rows)
            and any(all(q[k]==p[k] for k in ('pid','start_ticks','boot_id')) for q in rows),
            'baseline identity missing from original owned rows')
    require(type(r.get('registry_manifest')) is dict
            and r['registry_manifest']==registry_manifest(Path(spec['publication_dir']).parent),
            'baseline registry file set or original bytes changed')
    observation=r.get('exit_observation')
    require(type(observation) is dict and all(observation.get(k)==p[k] for k in ('pid','start_ticks','boot_id'))
            and r.get('process_observer_joined') is True and type(observation.get('returncode')) is int
            and observation['returncode']==0 and observation.get('error_type') is None
            and finite(observation.get('observed_at_monotonic'))
            and start<=observation['observed_at_monotonic']<=r.get('closed_at_monotonic',-1),
            'baseline exit observer identity/time mismatch')
    require(r.get('started_at_monotonic')==start and finite(r.get('closed_at_monotonic'))
            and spec['queued_at_monotonic']<=start<=r['closed_at_monotonic']<=now<spec['deadline_monotonic'],
            'baseline did not close inside original absolute budget')
    require(r.get('runtime_sha256')==RUNTIME and r.get('base_url')==spec['environment_required']['LLM_BASE_URL']
            and r.get('served_model')==MODEL,'baseline and agent must use the same model service')
    # The unchanged script emits metadata followed by exactly one llm record.
    # This is a script-attempt count, not independent proof of an HTTP POST.
    rows=[strict_json(line) for line in reply.trace.splitlines()]
    require(len(rows)==2 and type(rows[0]) is dict and type(rows[1]) is dict,'official baseline trace shape')
    meta,call=rows
    require(meta.get('tool')=='baseline_meta' and meta.get('script_sha256')==spec['script_sha256']
            and meta.get('served_model')==MODEL and meta.get('track')=='rtl'
            and meta.get('temperature')==0 and meta.get('top_p')==1
            and meta.get('max_tokens')==16384 and meta.get('generation')==spec['generation_policy']['generation'],
            'official baseline metadata differs from declared service/generation')
    require(call.get('tool')=='llm' and call.get('round')==0 and finite(call.get('sec')) and call['sec']>=0,
            'baseline must be one direct call, without tools or repair')
    posts=r.get('reported_HTTP_POSTs')
    require(posts is None or type(posts) is int and posts in (0,1),'invalid reported baseline POST count')
    require(posts!=0 or not reply.solution.strip(),'nonempty baseline output contradicts zero reported POSTs')
    return r,rows

def run_baseline(input_dir,output_dir,*,context,clock,executor,permit,put):
    require(callable(executor),'baseline executor remains unconfigured')
    policy=GenerationPolicy.load();spec=invocation_spec(input_dir,output_dir,context,policy)
    require(clock()<context['deadline_monotonic'],'baseline queue deadline exhausted')
    output=Path(output_dir);output.mkdir()
    put(output/'baseline-invocation.json',spec)
    payload=canonical(spec)
    permission=permit(payload)
    reply=executor(invocation=payload,context=dict(context),permit=permission)
    require(type(reply) is BaselineReply,'baseline executor reply type')
    # Preserve originals before validation; failed assertions never replay.
    put(output/'baseline-solution.original',reply.solution)
    put(output/'baseline-trace.original',reply.trace)
    put(output/'baseline-executor-receipt.json',reply.receipt)
    require(permission.started is not None,'baseline bypassed dispatch permit')
    receipt,rows=validate_reply(reply,spec,permission.started,clock())
    policy.require_current()
    require(clock()<context['deadline_monotonic'],'baseline publication deadline exhausted')
    decision={'schema':'formal-baseline-output-v1','run_id':context['run_id'],
        'public_fields_sha256':context['public_fields_sha256'],'clock_domain':context['clock_domain'],
        'absolute_deadline_monotonic':context['deadline_monotonic'],
        'solution_sha256':digest(reply.solution),'solution_bytes':len(reply.solution),
        'selection_basis':'official_baseline_unchecked','functional_status':'unknown','interface_compatibility':'unknown',
        'compiler_evidence':None,'selected_stage':None,'model_claims':1,'baseline_script_invocations':1,
        'reported_HTTP_POSTs':receipt['reported_HTTP_POSTs'],'post_outcome_unknown':0,
        'tool_handoffs':0,'tool_outcome_unknown':0,'runtime_accepted':False,'formal_submission_ready':False,
        'physical_model_calls_verified':False,'model_benefit_verified':False,
        'invocation_sha256':digest(payload),'baseline_process_identity':receipt['process_identity'],
        'official_trace_unmodified':True,'native_closure_verified':False}
    put(output/'selection.json',decision)
    put(output/'trace.jsonl',reply.trace)
    require(clock()<context['deadline_monotonic'],'baseline final publication deadline exhausted')
    put(output/'solution.v',reply.solution)
    return decision

