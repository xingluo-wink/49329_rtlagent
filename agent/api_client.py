"""File-in/file-out entry with one deadline, explicit errors and cancellation."""
import argparse,hashlib,json,math,os,signal,sys,time,uuid
from pathlib import Path
from engine_link import call,ready,EngineError
from native_model import identity

def write(path,value):
    raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,indent=2).encode()
    with path.open('xb') as f:f.write(raw)

def run(argv=None):
    started=time.monotonic()
    ap=argparse.ArgumentParser(description='读取题目目录的 prompt.txt，自动解题并写出 solution.v。')
    ap.add_argument('input');ap.add_argument('output')
    ap.add_argument('--mode',choices=['agent','baseline'],default='agent')
    ap.add_argument('--deadline',type=int,default=900)
    args=ap.parse_args(argv);end=started+min(900,max(1,args.deadline))
    source=Path(args.input).resolve();output=Path(args.output).absolute()
    accepted=None;attempted=False;created=False;stopped=[];old_handlers={}
    def on_signal(number,frame):stopped.append(number)
    def cancel(job):
        try:
            call('/v1/jobs/'+job+'/cancel',{},timeout=2)
            until=time.monotonic()+6
            while time.monotonic()<until:
                result=call('/v1/jobs/'+job,timeout=2)
                if result.get('status')=='done':return result
                time.sleep(.2)
        except Exception:pass
        return None
    try:
        if not source.is_dir() or output.is_symlink() or source==output.resolve() or source in output.resolve().parents or output.resolve() in source.parents:
            raise ValueError('题目目录与输出目录必须分开，输出目录不能是链接。')
        prompt=(source/'prompt.txt').read_text(encoding='utf-8-sig')
        interface=(source/'interface.txt').read_text(encoding='utf-8-sig') if (source/'interface.txt').exists() else ''
        if not prompt.strip():raise ValueError('prompt.txt 是空的，请填写题面。')
        output.mkdir(parents=True,exist_ok=True)
        if any(output.iterdir()):raise ValueError('输出目录已有文件。请使用新的输出目录；旧任务不会重复运行。')
        created=True
        ready()  # No POST or attempt is made while unavailable.
        for number in (signal.SIGINT,signal.SIGTERM):
            old_handlers[number]=signal.signal(number,on_signal)
        nonce='file-'+uuid.uuid4().hex;job=hashlib.sha256(('nonce:'+nonce).encode()).hexdigest()
        request={'task_id':source.name,'nonce':nonce,'mode':args.mode,'prompt':prompt,'interface':interface,
                 'deadline_s':max(1,int(end-time.monotonic()))}
        if stopped or time.monotonic()>=end:raise TimeoutError('在发送题目前已到期或被取消。')
        write(output/'intent.json',{'nonce':nonce,'expected_job_id':job,'mode':args.mode,
            'epoch':time.time(),'entry_deadline_monotonic':end,'automatic_retry':False})
        attempted=True
        accepted=call('/v1/solve-local',{'request':request,'client':identity(os.getpid()),'deadline_monotonic':end},timeout=min(5,max(.1,end-time.monotonic())))
        if accepted.get('job_id')!=job:raise EngineError('服务返回了不匹配的任务编号。')
        write(output/'accepted.json',accepted)
        print('已接收题目：'+source.name+'；自动读取、解题、编译。',flush=True)
        last_message=0
        while time.monotonic()<end and not stopped:
            result=call('/v1/jobs/'+job,timeout=min(5,max(.1,end-time.monotonic())))
            if result['status']=='done':
                if stopped or time.monotonic()>=end:raise TimeoutError('结果返回时已取消或超过原时限，保留空交付。')
                write(output/'trace.jsonl',result.get('trace','').encode())
                write(output/'result.json',result)
                raw=result.get('solution','').encode();pending=output/'received-candidate.pending';write(pending,raw)
                if stopped or time.monotonic()>=end:raise TimeoutError('发布答案前已取消或到期，候选保留。')
                os.rename(pending,output/'solution.v')
                if stopped or time.monotonic()>=end:
                    os.rename(output/'solution.v',output/'late-candidate.v')
                    raise TimeoutError('最终发布跨过时限，候选留存，交付为空。')
                size=(output/'solution.v').stat().st_size
                print(('解题完成。' if size else '本题未产出可交付代码。')+'答案：'+str(output/'solution.v'),flush=True)
                print('生成代码不等于功能得分；带测试台的题目可再运行 tools/grade.py。',flush=True)
                return 0 if size else 2
            if time.monotonic()-last_message>=15:
                print('排队中……' if result['status']=='queued' else '正在解题……',flush=True);last_message=time.monotonic()
            time.sleep(min(1,max(0,end-time.monotonic())))
        raise InterruptedError('已请求取消本题。') if stopped else TimeoutError('已到单题时限，正在取消本题。')
    except Exception as exc:
        closure=cancel(job) if attempted else None
        detail=str(exc);status=getattr(exc,'status',None)
        if created:
            error={'tool':'host_delivery','ts':time.time(),'rc':1,'error':detail,'http_status':status,
                'request_attempted':attempted,'server_closure':'observed_closed' if closure and closure.get('resource_closure')=='observed_closed' else ('not_submitted' if not attempted else 'inspect_job'),
                'automatic_retry':False}
            if not (output/'trace.jsonl').exists():write(output/'trace.jsonl',(json.dumps(error,ensure_ascii=False)+'\n').encode())
            if not (output/'error.json').exists():write(output/'error.json',error)
            if not (output/'solution.v').exists():write(output/'solution.v',b'')
        print('未完成：'+detail,file=sys.stderr)
        if attempted:print('任务记录已保留；不要把相同题目当作恢复操作重复提交。',file=sys.stderr)
        return 128+stopped[0] if stopped else 1
    finally:
        for number,handler in old_handlers.items():signal.signal(number,handler)

if __name__=='__main__':raise SystemExit(run())
