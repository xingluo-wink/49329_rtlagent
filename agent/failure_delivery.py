"""An observed, closed failure is an empty result, never a successful RTL."""
import json
import math
from pathlib import Path
from adapter_contracts import require
from prompt_builder import canonical,digest


def validate(job,ticket,launch,report,resources):
    require(report['job_id']==ticket['job_id'] and report['ticket_sha256']==digest(canonical(ticket))
            and report['lease_id']==launch['lease_id'],'failure launch binding')
    require(report['executed'] is True and report['normal_closure'] is True
            and report['observer_joined'] is True,'actual worker exit required')
    worker=report['execution_clock_domain'];obs=report['exit_observation']
    keys=('pid','start_ticks','boot_id')
    require(all(worker[k]==ticket['enqueue_clock_domain'][k] for k in ('host_id','boot_id','clock_id'))
            and tuple(worker[k] for k in keys)!=tuple(launch['supervisor_clock_domain'][k] for k in keys),'failure execution domain')
    require(all(obs[k]==worker[k] for k in keys) and obs['error_type'] is None
            and type(obs['returncode']) is int,'failure exit observation')
    require(resources['job_id']==ticket['job_id'] and resources['worker']==worker
            and resources['model_backend_idle'] is True,'failure backend closure')
    require(resources['observed_at_monotonic']>=report['observed_at_monotonic'], 'failure resource order')
    require(launch['handoff_at_monotonic']<=obs['observed_at_monotonic']<=report['observed_at_monotonic'], 'failure exit clock order')
    rows=resources['owned_snapshot'];ids=[tuple(q[k] for k in keys) for q in rows]
    expected={tuple(q[k] for k in keys) for q in report['owned_snapshot']}|{tuple(worker[k] for k in keys)}
    require(len(ids)==len(set(ids)) and expected<=set(ids) and all(q['state'] in ('exited','Z') for q in rows),
            'failure descendants unresolved')


def read_failure(job,ticket,read):
    final=json.loads(read(job/'failure-final.json'))
    launch=json.loads(read(job/'launch-intent.json'));report_raw=read(job/'launcher-receipt.json')
    resource_raw=read(job/'failure-resources.json')
    require(final['ticket_sha256']==digest(canonical(ticket)) and final['lease_id']==launch['lease_id']
            and final['launcher_sha256']==digest(report_raw) and final['resources_sha256']==digest(resource_raw),
            'closed failure evidence changed')
    validate(job,ticket,launch,json.loads(report_raw),json.loads(resource_raw))
    require(type(final['elapsed_s']) in (float,int) and math.isfinite(final['elapsed_s']) and final['elapsed_s']>=0,'failure elapsed time')
    trace=read(job/'failure-trace.jsonl')
    require(final['trace_sha256']==digest(trace),'failure trace changed')
    return {'job_id':ticket['job_id'],'task_id':ticket['binding']['request']['task_id'],
            'status':'done','solution':'','trace':trace.decode(),'elapsed_s':final['elapsed_s'],
            'execution_evidence_state':'closed_failure','resource_closure':'observed_closed'}


def publish_failure(job,ticket,launch,raw,platform,put,clock,reason):
    launcher_raw=raw
    report=json.loads(launcher_raw)
    resources=platform.failure_resources(job,report['execution_clock_domain'])
    validate(job,ticket,launch,report,resources)
    rr=canonical(resources);put(job/'failure-resources.json',rr)
    events=[];refs={}
    for path in sorted((job/'output').glob('chain.trace-*.json')):
        raw=path.read_bytes();events.append(json.loads(raw));refs[path.relative_to(job).as_posix()]=digest(raw)
    # Preserve recorded calls; a durable attempt is not falsely counted as a
    # completed HTTP request. Never copy response reasoning or test fixtures.
    for stage in ('route','initial','compiler_repair'):
        path=job/'output'/(stage+'.request.json')
        if path.exists():
            refs[path.relative_to(job).as_posix()]=digest(path.read_bytes())
            events.append({'tool':'llm_attempt_evidence','stage':stage,'request_sha256':digest(path.read_bytes()),
                'tokens_in':None,'tokens_out':None,'outcome':'see_retained_native_receipts','not_an_additional_request':True})
    if ticket['binding']['request']['mode']=='baseline':
        for name in ('output/baseline-invocation.json','output/official-output/trace.jsonl',
                     'output/baseline-trace.original','native-baseline/dispatch-intent.json','native-baseline/closure.json'):
            path=job/name
            if path.is_file():
                data=path.read_bytes();refs[name]=digest(data)
                if name.endswith('trace.jsonl') or name.endswith('trace.original'):
                    for line in data.splitlines():
                        try:
                            row=json.loads(line)
                            if row not in events:events.append(row)
                        except (ValueError,UnicodeError):pass
        events.append({'tool':'baseline_attempt_evidence','retained_evidence':dict(refs),
            'tokens_in':None,'tokens_out':None,'reported_HTTP_POSTs':None,
            'outcome':'closed_failure','not_an_additional_request':True})
    events.append({'tool':'host_delivery','rc':1,'result':'closed_failure',
        'reason':reason,'resource_closure':'observed_closed','retry_allowed':False,
        'model_output_accepted':False,'retained_event_sha256':refs})
    trace=''.join(canonical(x).decode()+'\n' for x in events)
    put(job/'failure-trace.jsonl',trace.encode())
    final={'schema':'closed-failure-v1','ticket_sha256':digest(canonical(ticket)),
        'lease_id':launch['lease_id'],'launcher_sha256':digest(launcher_raw),'resources_sha256':digest(rr),
        'trace_sha256':digest(trace.encode()),'elapsed_s':max(0,clock()-ticket['queued_at_monotonic'])}
    from formal_jobs import commit
    commit(job/'failure-final.json',final)
    return final
