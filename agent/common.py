"""Shared identity-qualified cloud process controls; no inference on import."""
from pathlib import Path
import os,sys,json,time,hashlib,subprocess,signal,socket,fcntl,threading,urllib.request,shutil
ROOT=Path(__file__).resolve().parent
BOOT=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
PY='/opt/python/bin/python3'
REG=ROOT/'processes'
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def read(p,default=None):
 try:return json.loads(Path(p).read_text(encoding='utf-8'))
 except (OSError,ValueError):return default

def save(p,x):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
 tmp=p.with_name(p.name+'.'+str(os.getpid())+'.'+str(threading.get_ident())+'.pending')
 with tmp.open('w',encoding='utf-8') as f:
  json.dump(x,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
 tmp.replace(p)

def append_json(p,x):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
 with p.open('a',encoding='utf-8') as f:f.write(json.dumps(x,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())

def ident(pid):
 try:
  fields=Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()
  return {'pid':int(pid),'start_ticks':fields[19],'ppid':int(fields[1]),'pgid':int(fields[2]),'state':fields[0],'boot_id':BOOT}
 except (OSError,ValueError):return None

def same(i):
 q=ident(i['pid']) if i else None
 return bool(q and q['state']!='Z' and q['start_ticks']==i['start_ticks'] and q['boot_id']==i['boot_id'])

def descendants(i):
 if not same(i):return []
 table={int(p.name):ident(int(p.name)) for p in Path('/proc').iterdir() if p.name.isdigit()}
 wanted={i['pid']};old=set()
 while old!=wanted:
  old=set(wanted);wanted.update(pid for pid,q in table.items() if q and q['ppid'] in wanted)
 return [table[pid] for pid in wanted if table.get(pid)]

def identity_key(i):
 return str(i['boot_id'])+':'+str(i['pid'])+':'+str(i['start_ticks'])

def registered_identities():
 rows=[]
 if (ROOT/'process-history.jsonl').exists():
  for line in (ROOT/'process-history.jsonl').read_text().splitlines():
   try:rows.append(json.loads(line))
   except ValueError:pass
 rows.extend(read(p) for p in REG.glob('*.json'))
 return [i for i in rows if i and all(k in i for k in ('pid','start_ticks','boot_id'))]

def snapshot_owned():
 # Read-only inspection shares the same identity/lineage rules without persisting.
 roots=registered_identities()
 known={identity_key(i):dict(i) for i in read(ROOT/'process-descendants.json',{}).get('identities',[])}
 for i in roots:
  key=identity_key(i);prior=known.get(key,{})
  known[key]={**prior,**i,'owner_keys':sorted(set(prior.get('owner_keys',[])+[key]))}
 table={int(p.name):ident(int(p.name)) for p in Path('/proc').iterdir() if p.name.isdigit()}
 live={}
 for key,i in list(known.items()):
  q=table.get(i['pid'])
  if q and q['state']!='Z' and identity_key(q)==key:live[i['pid']]={**i,**q}
 changed=True
 while changed:
  changed=False
  for pid,q in table.items():
   if not q or q['state']=='Z' or q['ppid'] not in live:continue
   key=identity_key(q);parent=live[q['ppid']]
   previous=live.get(pid) or known.get(key) or {'role':'descendant','first_seen':time.time()}
   owners=sorted(set(previous.get('owner_keys',[])+parent.get('owner_keys',[])+[identity_key(parent)]))
   if pid not in live or live[pid].get('owner_keys')!=owners:
    live[pid]={**previous,**q,'owner_keys':owners};changed=True
 for i in live.values():known[identity_key(i)]=i
 return list(known.values())

def capture_owned(pause_identity=None):
 # Keep identity-qualified descendants after their parent exits/reparents.
 # Separate opens of flock also serialize the supervisor's monitor thread.
 with (ROOT/'process-descendants.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  # Pause only while owning the registry lock, never freeze its current holder.
  if pause_identity and same(pause_identity):
   try:os.kill(pause_identity['pid'],signal.SIGSTOP)
   except ProcessLookupError:pass
  rows=snapshot_owned()
  save(ROOT/'process-descendants.json',{'ts':time.time(),'identities':rows})
  return rows

def become_subreaper():
 import ctypes
 libc=ctypes.CDLL(None,use_errno=True)
 if libc.prctl(36,1,0,0,0)!=0:raise RuntimeError('cannot enable event child subreaper')
 save(ROOT/'subreaper-enabled.json',{'ts':time.time(),'identity':ident(os.getpid())})

def stop_rows(rows,grace=4):
 rows=list({identity_key(q):q for q in rows if q and q['pid']!=os.getpid()}.values())
 for q in reversed(rows):
  if same(q):
   try:os.kill(q['pid'],signal.SIGTERM)
   except ProcessLookupError:pass
 end=time.monotonic()+max(0,grace)
 while any(same(q) for q in rows) and time.monotonic()<end:time.sleep(.1)
 for q in reversed(rows):
  if same(q):
   try:os.kill(q['pid'],signal.SIGKILL)
   except ProcessLookupError:pass
 # Give SIGKILL time to be observed before asserting cleanup completion.
 end=time.monotonic()+2
 while any(same(q) for q in rows) and time.monotonic()<end:time.sleep(.05)
 return [q for q in rows if same(q)]

def live_event_processes():
 return [i for i in capture_owned() if i.get('role') not in ('supervisor','watchdog') and i['pid']!=os.getpid() and same(i)]

def cleanup_event_processes(grace=4):
 # Re-scan after shutdown to include children adopted by the supervisor.
 for _ in range(2):
  rows=live_event_processes()
  if not rows:break
  stop_rows(rows,grace)
 return live_event_processes()

def stop(i,grace=4):
 if not i:return []
 key=identity_key(i)
 rows=[q for q in capture_owned() if identity_key(q)==key or key in q.get('owner_keys',[])]
 rows.extend(descendants(i))
 return stop_rows(rows,grace)

def own(role,pid):
 q=ident(pid)
 if not q:raise RuntimeError('process exited before identity registration: '+role)
 q.update(role=role,registered=time.time());save(REG/(role+'.json'),q)
 append_json(ROOT/'process-history.jsonl',q)
 return q

def spawn(role,cmd,env=None,cwd=None):
 log=(ROOT/(role+'.log')).open('ab')
 try:
  p=subprocess.Popen(cmd,cwd=cwd or ROOT,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 finally:log.close()
 try:
  q=own(role,p.pid);capture_owned();return p,q
 except BaseException:
  # Registration errors may occur after Popen succeeded. Never leave that child.
  q=ident(p.pid)
  if q:stop_rows(descendants(q),.5)
  try:p.wait(timeout=3)
  except subprocess.TimeoutExpired:pass
  raise


def event(phase,**kw):
 row={'ts':time.time(),'phase':phase,**kw}
 save(ROOT/'status.json',row)
 append_json(ROOT/'events.jsonl',row)

def http(path,body=None,port=8000,timeout=3,token=None):
 headers={'Content-Type':'application/json'}
 if token:headers['Authorization']='Bearer '+token
 req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
 with OPENER.open(req,timeout=timeout) as r:return json.load(r)

def gpu():
 devices={}
 nodes=list(Path('/dev/dri').glob('card[0-9]*')) or list(Path('/dev/dri').glob('renderD*'))
 for n in nodes:
  p=Path('/sys/class/drm')/n.name/'device'
  try:devices[str(p.resolve())]={'node':n.name,'used':int((p/'mem_info_vram_used').read_text()),'total':int((p/'mem_info_vram_total').read_text())}
  except OSError:pass
 return list(devices.values())

def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
 return h.hexdigest()

def identity_gate():
 c=read(ROOT/'connection.json')
 if not c or socket.gethostname()!=c['hostname'] or BOOT!=c['boot_id'] or (os.getuid(),os.geteuid(),os.getgid(),os.getegid())!=(0,0,0,0):raise RuntimeError('REMOTE_IDENTITY_CHANGED')

def idle():
 try:
  slots=http('/slots');return bool(slots) and all(not s.get('is_processing',False) for s in slots)
 except Exception:return False

def env_for(model=None):
 env=dict(os.environ)
 for k in list(env):
  if 'proxy' in k.lower():env.pop(k,None)
 env.update(PATH='/opt/python/bin:/tools/Xilinx/2026.1/Vivado/bin:/tools/Xilinx/2026.1/Vitis/bin:'+env.get('PATH',''),PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',XILINX_ROOT='/tools/Xilinx',XILINX_VIVADO='/tools/Xilinx/2026.1/Vivado',XILINXD_LICENSE_FILE='2100@vivado-lic.fpgachina.svc.cluster.local',FLEXLM_TIMEOUT='300000000',ROCBLAS_TENSILE_LIBPATH='/opt/llamacpp/rocblas/library',NO_PROXY='*',no_proxy='*',TRACK='rtl',LLM_BASE_URL='http://127.0.0.1:8001/v1',SELFTEST_KEEP_WORK='1')
 for key,leaf in [('AGENT_TMP','agent'),('EDA_TMP','eda'),('SELFTEST_TMP','judge')]:
  folder=ROOT/'scratch'/leaf;folder.mkdir(parents=True,exist_ok=True);env[key]=str(folder)
 if model:env.update(MODEL_NAME=model['id'],LLM_MODEL=model['id'])
 return env

def phase_guard(kind,seconds,**kw):
 row={'kind':kind,'started':time.time(),'deadline_epoch':time.time()+seconds,'max_s':seconds,**kw}
 save(ROOT/'phase-guard.json',row);return row

def clear_guard():save(ROOT/'phase-guard.json',{'kind':'between_steps','started':time.time(),'deadline_epoch':time.time()+120,'max_s':120})
