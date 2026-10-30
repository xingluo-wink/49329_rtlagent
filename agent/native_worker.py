"""Dedicated Linux task worker, separate from the API process."""
import json
import os
from pathlib import Path
import sys
import time
from adapter_contracts import require
from formal_jobs import run_worker, read, known_tool_identities
from native_model import NativeModel, identity
from native_runtime import full_domain, registry_rows
from prompt_builder import canonical
from compiler_adapter import CompilerBridge, CompilerConnectedAdapters, native_backend
from native_baseline import native_executor


def main(job, receipt):
    job=Path(job); ticket=json.loads(read(job/'ticket.json'));launch=json.loads(read(job/'launch-intent.json'))
    model=NativeModel(job/'native-model',receipt,allow_native=True)
    def factory(registry,folder,context,clock):
        return native_backend(registry,folder,context,clock,allow_native=True)
    adapters=CompilerConnectedAdapters(model,CompilerBridge(factory))
    baseline=None
    if ticket['binding']['request']['mode']=='baseline':
        baseline=native_executor(job/'native-baseline',model,allow_native=True)
        adapters.run_baseline=baseline.run_baseline
    def closure(job,ticket,worker):
        known,refs,stages=known_tool_identities(job)
        rows=registry_rows(job);left=[]
        for q in rows:
            if q['pid']==os.getpid():continue
            try:actual=identity(q['pid'])
            except FileNotFoundError:continue
            if all(actual[k]==q[k] for k in ('pid','start_ticks','boot_id')) and actual['state']!='Z':left.append(q)
        idle=model.idle(end=ticket['deadline_monotonic'])
        return canonical({'job_id':ticket['job_id'],'execution_clock_domain':worker,
            'model_backend_idle':idle,'inflight_requests':0 if idle else None,
            'tools_closed':not left,'remaining_live':left,'requests':baseline.closed if baseline is not None else model.closed,
            'known_identity_count':len(known),'native_runtime_accepted':False})
    run_worker(job,ticket,launch,clock=time.monotonic,worker_domain=full_domain(),adapters=adapters,close_resources=closure)
    return 0


if __name__=='__main__':
    raise SystemExit(main(*sys.argv[1:]))
