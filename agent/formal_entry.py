"""CLI and ASGI projections of the persistent task queue."""
import asyncio
import hmac
import json
from pathlib import Path
import sys
import uuid
from adapter_contracts import BoundaryError, require
from artifact_store import _publish
from formal_jobs import Busy, Conflict, Unavailable, read, put
from prompt_builder import canonical, digest
from rtl_delivery import strict_json

BODY_ADMISSION_SECONDS=5.0


def cli(input_dir, output_dir, *, service=None):
    if service is None or not (service.offline_injection or getattr(service,'native_trial',False)):
        raise Unavailable('native CLI/model/platform are unconfigured; no task was launched')
    start=service.store.clock()
    inp=Path(input_dir).absolute();out=Path(output_dir).absolute()
    require(inp.is_dir() and not inp.is_symlink() and not out.is_symlink(), 'real formal paths required')
    a,b=inp.resolve(),out.resolve();q=service.store.root
    require(a!=b and a not in b.parents and b not in a.parents
            and b!=q and b not in q.parents and q not in b.parents, 'formal trees must be disjoint')
    out.mkdir(parents=True,exist_ok=True)
    require(not any(out.iterdir()), 'existing output/intent cannot be replayed')
    put(out/'delivery-intent.json',{'queued_at_monotonic':start,'no_replay':True,'runtime_accepted':False})
    prompt=read(inp/'prompt.txt',200000).decode('utf-8')
    interface=read(inp/'interface.txt',200000).decode('utf-8') if (inp/'interface.txt').exists() else ''
    job=service.solve({'task_id':'cli','nonce':'cli-'+uuid.uuid4().hex,'prompt':prompt,'interface':interface},queued_at=start)
    put(out/'job-binding.json',{'job_id':job['job_id']})
    state=service.dispatcher.dispatch(job['job_id'])
    if state['status']!='done':raise Unavailable('job has no closed, eligible delivery: '+state['status'])
    # The dispatcher has verified the full result and closure receipts. A CLI
    # projection still needs its own deadline and exclusive publication gate.
    ticket=service.store.ticket(job['job_id'])
    trace=state['trace'].encode();solution=state['solution'].encode()
    put(out/'trace.jsonl',trace)
    put(out/'delivery-decision.json',{'job_id':job['job_id'],'solution_sha256':digest(solution),
        'solution_bytes':len(solution),'selection_basis':state['selection_basis'],
        'functional_status':'unknown','runtime_accepted':False,'formal_submission_ready':False})
    pending=out/('pending-'+uuid.uuid4().hex);put(pending,solution)
    require(not solution or service.store.clock()<ticket['deadline_monotonic'], 'deadline before final CLI publication')
    _publish(pending,out/'solution.v')
    return 0


def main(argv=None, *, service=None):
    args=list(sys.argv[1:] if argv is None else argv)
    if len(args)!=2: return 64
    try:return cli(args[0],args[1],service=service)
    except Unavailable:return 78
    except (BoundaryError, OSError, UnicodeError, ValueError):return 1


class App:
    """ASGI app with no background worker or import-time service side effects.

    The default service reports ready=false and rejects POST. Only a test's
    explicit offline injection admits synthetic jobs; that never means ready.
    """
    def __init__(self,service,*,token):
        require(type(token) is str and token, 'explicit authentication token required')
        self.service=service;self.expected=('Bearer '+token).encode()

    async def __call__(self,scope,receive,send):
        if scope['type']=='lifespan':
            while True:
                message=await receive()
                if message['type']=='lifespan.startup':await send({'type':'lifespan.startup.complete'})
                elif message['type']=='lifespan.shutdown':
                    await send({'type':'lifespan.shutdown.complete'});return
            return
        require(scope['type']=='http', 'HTTP only')
        start=self.service.store.clock()
        headers=scope.get('headers',[])
        values=[v for k,v in headers if k.lower()==b'authorization']
        if len(values)!=1 or not hmac.compare_digest(values[0],self.expected):
            await self.reply(send,401,{'error':'unauthorized'});return
        method,path=scope['method'],scope['path']
        try:
            if method=='GET' and path=='/v1/health':code,data=200,self.service.health()
            elif method=='GET' and path.startswith('/v1/jobs/'):
                code,data=200,self.service.job(path[len('/v1/jobs/'):])
            elif method=='POST' and path in ('/v1/solve','/v1/solve-local','/v1/solve-public'):
                raw=bytearray()
                while True:
                    remaining=BODY_ADMISSION_SECONDS-(self.service.store.clock()-start)
                    if remaining<=0:raise Unavailable('request body consumed admission deadline')
                    event=await asyncio.wait_for(receive(),timeout=remaining)
                    require(event['type']=='http.request', 'request disconnected')
                    raw.extend(event.get('body',b''));require(len(raw)<=500000,'request body limit')
                    if not event.get('more_body',False):break
                # The last body chunk can itself consume all remaining time.
                # Preserve the original admission origin; do not queue an
                # already-expired fresh task merely because there is no next read.
                if self.service.store.clock() >= start+BODY_ADMISSION_SECONDS:
                    raise Unavailable('request body consumed admission deadline')
                value=strict_json(bytes(raw))
                if path=='/v1/solve-local':
                    require(set(value)=={'request','client','deadline_monotonic'},'local envelope fields')
                    code,data=202,self.service.solve_local(value['request'],value['client'],queued_at=start,outer_deadline=value['deadline_monotonic'])
                elif path=='/v1/solve-public':
                    require(set(value)=={'request','queued_at_monotonic'} and type(value['queued_at_monotonic']) in (int,float)
                        and start-10<=value['queued_at_monotonic']<=start,'public clock envelope')
                    code,data=202,self.service.solve(value['request'],queued_at=value['queued_at_monotonic'])
                else:code,data=202,self.service.solve(value,queued_at=start)
            elif method=='POST' and path.startswith('/v1/jobs/') and path.endswith('/cancel'):
                code,data=202,self.service.cancel(path[len('/v1/jobs/'):-len('/cancel')])
            else:code,data=404,{'error':'unknown endpoint'}
        except Conflict:code,data=409,{'error':'nonce conflicts with existing full request'}
        except (Busy,Unavailable,TimeoutError) as exc:code,data=503,{'error':str(exc)[:250]}
        except FileNotFoundError:code,data=404,{'error':'unknown job'}
        except (BoundaryError,ValueError,UnicodeError,KeyError):code,data=400,{'error':'invalid request or evidence'}
        await self.reply(send,code,data)

    @staticmethod
    async def reply(send,status,data):
        body=canonical(data)
        await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json'),(b'content-length',str(len(body)).encode())]})
        await send({'type':'http.response.body','body':body})


if __name__=='__main__':
    raise SystemExit(main())
