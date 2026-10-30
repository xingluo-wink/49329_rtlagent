"""Start one background engine; retain sessions and nonce journals intact."""
import fcntl,json,os,secrets,socket,subprocess,sys,time,uuid
from pathlib import Path
from native_model import identity
from release_integrity import verify_release
from runtime_paths import state_root,session_root
ROOT=Path(__file__).resolve().parent

def put(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)

def same(saved):
    try:
        row=identity(saved['pid'])
        return row['state']!='Z' and all(row[k]==saved[k] for k in ('pid','start_ticks','boot_id'))
    except FileNotFoundError:return False

def previous_closed(row):
    if same(row['identity']):return False
    root=Path(row['run_root']);boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if row['identity']['boot_id']!=boot:return True
    for p in (root/'processes').glob('*.json'):
        if same(json.loads(p.read_bytes())):return False
    closed=root/'owner-resources-closed.json';ended=root/'watchdog-ended.json'
    if not closed.is_file():return False
    value=json.loads(closed.read_bytes())
    if value['remaining_live'] or value['monitor_joined'] is not True:return False
    if (root/'watchdog-ready.json').exists():
        return ended.is_file() and json.loads(ended.read_bytes())['cleanup_verified'] is True
    return True

def snapshot_deployment(path):
    root=Path('/workspace');st=root.stat()
    put(path,{'hostname':socket.gethostname(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'execution_ids':[os.getuid(),os.geteuid(),os.getgid(),os.getegid()],
        'workspace_owner':[st.st_uid,st.st_gid],'persistent_root':str(root)})

def main(action='start'):
    if action not in ('start','stop','status'):raise ValueError('用法：serve/start.sh [start|stop|status]')
    verify_release();state=state_root();state.mkdir(parents=True,exist_ok=True)
    with (state/'start.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pointer=state/'current.json';old=json.loads(pointer.read_bytes()) if pointer.exists() else None
        for previous in (state/'sessions').glob('*'):
            if (previous/'launch-intent.json').exists() and not (previous/'launched.json').exists():
                raise RuntimeError('存在启动结果不明的记录：'+str(previous)+'；没有启动重复模型。')
            if (previous/'launched.json').exists():
                candidate=json.loads((previous/'launched.json').read_bytes())
                if same(candidate['identity']) and (old is None or candidate['identity']!=old['identity']):
                    raise RuntimeError('存在另一个已启动会话：'+str(previous)+'；保留原会话，不重启。')
        if action=='status':
            from engine_link import call
            print(json.dumps(call('/v1/health'),ensure_ascii=False));return
        if action=='stop':
            if old is None or not same(old['identity']):print('引擎已停止。');return
            for p in (state/'store/jobs').glob('*'):
                if not any((p/n).exists() for n in ('final.json','failure-final.json','expired.json')):
                    raise RuntimeError('仍有未结束任务。先等待结束或取消任务，再停止服务。')
            request=Path(old['run_root'])/'stop-requested.json'
            if not request.exists():put(request,{'epoch':time.time(),'reason':'operator_stop'})
            end=time.monotonic()+35
            while same(old['identity']) and time.monotonic()<end:time.sleep(.2)
            if not previous_closed(old):raise RuntimeError('引擎关闭尚未确认；保留所有记录，不能重复启动。')
            print('引擎及所属子进程已停止，历史任务保留。');return
        if old is not None and same(old['identity']):
            from engine_link import ready
            ready()
            if old['release_sha256']!=verify_release()['checksums_sha256']:
                raise RuntimeError('现有引擎运行的是另一版源码；请结束任务后 stop，再 start。')
            print('引擎已就绪，无需重复启动。');return
        if old is not None and not previous_closed(old):
            raise RuntimeError('旧会话仍有未确认关闭的资源；没有启动重复模型。查看 '+str(state))
        for job in (state/'store/jobs').glob('*'):
            if not any((job/n).exists() for n in ('final.json','failure-final.json','expired.json')):
                if (job/'launch-intent.json').exists():raise RuntimeError('旧任务已启动但关闭不明：'+job.name)
                put(job/'expired.json',{'status':'expired_not_started','model_requests':0,'worker_spawned':False,'reason':'previous_session_ended'})
        sid=uuid.uuid4().hex;journal=state/'sessions'/sid;journal.mkdir(parents=True,exist_ok=False)
        runtime=session_root()/sid;runtime.parent.mkdir(parents=True,exist_ok=True)
        credential=journal/'engine.token';fd=os.open(credential,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32))
        deployment=journal/'deployment.json';snapshot_deployment(deployment)
        env=dict(os.environ);env.pop('FPGACHINA_TOKEN',None)
        env.update(RTL_DEPLOYMENT_FILE=str(deployment),RTL_ENGINE_TOKEN_FILE=str(credential),
            PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1')
        env.setdefault('EDA_TMP','/var/tmp/eda');env.setdefault('AGENT_TMP','/var/tmp/rtlagent-work')
        put(journal/'launch-intent.json',{'epoch':time.time(),'run_root':str(runtime),'no_task_replay':True})
        with (journal/'engine.log').open('xb') as log:
            process=subprocess.Popen([sys.executable,'-B',str(ROOT/'service_entry.py'),str(runtime)],
                cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        row={'identity':identity(process.pid),'run_root':str(runtime),'socket':str(runtime/'engine.sock'),
            'token_file':str(credential),'journal':str(journal),'release_sha256':verify_release()['checksums_sha256']}
        put(journal/'launched.json',row)
        pending=state/('current-'+sid+'.pending');put(pending,row);os.replace(pending,pointer)
        print('正在加载模型，通常需要约 1–3 分钟……',flush=True)
        end=time.monotonic()+360
        while time.monotonic()<end:
            if process.poll() is not None:raise RuntimeError('引擎启动失败，详情在 '+str(journal/'engine.log'))
            try:
                from engine_link import ready
                value=ready();put(journal/'ready.json',{'epoch':time.time(),'health':value})
                print('引擎已就绪。现在可以运行 run.sh 或 python3 tools/solve.py。');return
            except (OSError,ValueError,RuntimeError):time.sleep(.5)
        raise TimeoutError('模型未在启动时限内就绪；查看 '+str(journal/'engine.log'))

if __name__=='__main__':
    try:main(sys.argv[1] if len(sys.argv)>1 else 'start')
    except Exception as exc:print(str(exc),file=sys.stderr);raise SystemExit(1)
