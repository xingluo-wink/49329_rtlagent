"""Formal file entry: the same authenticated API and the same serial queue."""
import argparse,json,os,time,uuid,urllib.request
from urllib.parse import urlsplit
from pathlib import Path
NEW_OUTPUT=False
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None
def main():
    global NEW_OUTPUT
    ap=argparse.ArgumentParser();ap.add_argument('input');ap.add_argument('output');ap.add_argument('--mode',choices=['agent','baseline'],default='agent');a=ap.parse_args()
    src=Path(a.input);out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    if any(out.iterdir()):raise SystemExit('Output must be empty; an existing intent must not be replayed.')
    NEW_OUTPUT=True
    token=os.environ.get('FPGACHINA_TOKEN')
    if not token:
        from service_entry import private_token
        token=private_token()
    base=os.environ.get('FPGACHINA_URL','http://127.0.0.1:7860').rstrip('/')
    u=urlsplit(base)
    if u.username or u.password or u.query or u.fragment or (u.scheme!='https' and not (u.scheme=='http' and u.hostname in ('127.0.0.1','localhost','::1'))):raise ValueError('HTTPS or local loopback required; no embedded credentials')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    req={'task_id':src.name,'nonce':'file-'+uuid.uuid4().hex,'mode':a.mode,'prompt':(src/'prompt.txt').read_text(encoding='utf-8'),'interface':(src/'interface.txt').read_text(encoding='utf-8') if (src/'interface.txt').exists() else '', 'deadline_s':900}
    def call(path,data=None):
        body=None if data is None else json.dumps(data,ensure_ascii=False).encode()
        r=urllib.request.Request(base+path,data=body,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with opener.open(r,timeout=20) as f:return json.load(f)
    (out/'intent.json').write_text(json.dumps({'nonce':req['nonce'],'epoch':time.time(),'mode':a.mode}),encoding='utf-8')
    accepted=call('/v1/solve',req);(out/'accepted.json').write_text(json.dumps(accepted),encoding='utf-8')
    end=time.monotonic()+920
    while time.monotonic()<end:
        result=call('/v1/jobs/'+accepted['job_id'])
        if result['status']=='done':
            (out/'trace.jsonl').write_text(result.get('trace',''),encoding='utf-8',newline='\n')
            (out/'result.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
            (out/'solution.v').write_text(result.get('solution',''),encoding='utf-8',newline='\n')
            print(json.dumps({'status':'done','solution_bytes':(out/'solution.v').stat().st_size,'output':str(out)},ensure_ascii=False));return 0
        if result['status'] not in ('queued','running'):raise RuntimeError('Host closure is unknown: '+result['status'])
        time.sleep(2)
    raise TimeoutError('No completed delivery; keep intent and inspect job, do not resubmit')
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        # Preserve intents. Empty delivery is not proof of server-side closure.
        import sys
        if NEW_OUTPUT and len(sys.argv)>=3:
            out=Path(sys.argv[2]);out.mkdir(parents=True,exist_ok=True)
            if not (out/'solution.v').exists():
                if not (out/'trace.jsonl').exists():
                    (out/'trace.jsonl').write_text(json.dumps({'ts':time.time(),'tool':'host_delivery','rc':1,'excerpt':type(error).__name__,'tokens_in':None,'tokens_out':None,'server_closure':'unknown','retry_allowed':False})+'\n',encoding='utf-8')
                (out/'solution.v').write_bytes(b'')
        print('No verified solution; preserved intent and empty output. '+type(error).__name__,file=sys.stderr)
        raise SystemExit(0)
