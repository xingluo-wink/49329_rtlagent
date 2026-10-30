"""Bounded actual Vivado checks; outputs bound to staged DUT and test bytes."""
from common import *
import re,math
from host_completion import Identity,ExitObserver,ResourceClosure,assess_completion

def uncomment(text):
 # Comments are lexical whitespace. Preserve quoted strings so a quote cannot
 # hide a following actual directive; source strings themselves are rejected.
 out=[];i=0;state='code'
 while i<len(text):
  c=text[i];pair=text[i:i+2]
  if state=='line':
   out.append('\n' if c=='\n' else ' ')
   if c=='\n':state='code'
  elif state=='block':
   if pair=='*/':out.extend('  ');i+=1;state='code'
   else:out.append('\n' if c=='\n' else ' ')
  elif state=='string':
   out.append(c)
   if c=='\\' and i+1<len(text):i+=1;out.append(text[i])
   elif c=='"':state='code'
  else:
   if pair=='//':state='line';out.extend('  ');i+=1
   elif pair=='/*':state='block';out.extend('  ');i+=1
   else:
    out.append(c)
    if c=='"':state='string'
  i+=1
 if state in ('block','string'):raise ValueError('unterminated comment or string')
 return ''.join(out)

def source_allowed(text):
 if len(text.encode())>200000 or '\x00' in text:return False
 try:text=uncomment(text)
 except ValueError:return False
 if '"' in text:return False
 # Fail closed on external/runtime capabilities. SystemVerilog arithmetic casts only.
 if re.search(r'`|\b(?:initial|final|forever|force|release|import|export|program|interface|bind|tran|nmos|pmos)\b',text):return False
 if any(x not in ('signed','unsigned','clog2','bits') for x in re.findall(r'\$([A-Za-z_]\w*)',text)):return False
 if re.search(r'#|@\s*\([^)]*\b(?:negedge|posedge)\b[^)]*\b(?:posedge|negedge)\b',text):return False
 mods=re.findall(r'\bmodule\s+(\w+)',text)
 return mods==['TopModule'] and len(re.findall(r'\bendmodule\b',text))==1

def _finite_number(value,name):
 if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
  raise ValueError(name+' must be a finite number')
 return float(value)


def _post_spawn_unknown(error,p,q,observer,argv,folder,role,log,started,end,phase_deadline):
 # Popen returned a real process object. Every later infrastructure error must
 # preserve that execution fact, even if identity registration or receipt I/O fails.
 errors=[{'stage':'execution','error_type':type(error).__name__,'detail':str(error)[:300]}]
 def note(stage,exc):errors.append({'stage':stage,'error_type':type(exc).__name__,'detail':str(exc)[:300]})
 def qualified(value):
  try:return value if value['pid']==p.pid and Identity(value['pid'],value['start_ticks'],value['boot_id']) else None
  except (KeyError,TypeError,ValueError):return None
 q=qualified(q)
 if q is None:
  try:q=qualified(ident(p.pid))
  except BaseException as exc:note('identity_lookup',exc)
 if q is not None:
  try:stop(q,.2)
  except BaseException as exc:
   note('identity_cleanup',exc)
   try:stop_rows(descendants(q),.2)
   except BaseException as inner:note('descendant_cleanup',inner)
 else:errors.append({'stage':'identity','error_type':'UnverifiedIdentity','detail':'No identity-qualified signal sent'})
 emergency_wait=None
 try:
  code=p.wait(timeout=3)
  emergency_wait={'returncode':code,'observed_at_monotonic':time.monotonic(),'identity':q}
 except BaseException as exc:note('emergency_wait',exc)
 observation=None;joined=False
 if observer is not None:
  try:
   value=observer.snapshot()
   if value is not None:observation={'identity':q,'returncode':value.returncode,'observed_at_monotonic':value.observed_at,'error_type':value.error_type}
   joined=observer.join(2)
  except BaseException as exc:note('observer_join',exc)
 # A partial emergency snapshot never establishes the normal global closure gate.
 remaining=None;snapshot_sha=None
 if q is not None:
  try:
   rows=descendants(q);remaining=[x for x in rows if same(x)]
   snapshot_sha=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
  except BaseException as exc:note('emergency_snapshot',exc)
 text='';log_sha=None;log_bytes=None
 try:
  text=log.read_text(errors='replace');log_sha=sha(log);log_bytes=log.stat().st_size
 except BaseException as exc:note('log_read',exc)
 out={'argv':argv,'executed':True,'identity':q,'returncode':p.returncode,
      'timed_out':time.monotonic()>=end,'remaining_live':remaining,
      'exit_observation':observation,'emergency_wait':emergency_wait,'observer_joined':joined,
      'log_sha256':log_sha,'log_bytes':log_bytes,'errors':errors,
      'completion_accounting':{'verdict_eligible':False,'process_status':'post_spawn_infrastructure_failure','issues':['normal_completion_evidence_incomplete']},
      'resource_scope':'partial emergency cleanup only; normal/global closure not established',
      'resource_closure_unknown':True,'resource_snapshot_sha256':snapshot_sha,
      'started_at_monotonic':started,'process_deadline_monotonic':end,'phase_deadline_monotonic':phase_deadline}
 try:save(folder/(role+'.json'),out)
 except BaseException as exc:note('receipt_write',exc)
 return out,text


def command(argv,folder,limit,role,phase_deadline=None):
 # Keep the caller's absolute phase deadline. A stale remaining duration must
 # never be rebased on a newer clock reading into extra execution time.
 started=_finite_number(time.monotonic(),'started')
 limit=_finite_number(limit,'limit')
 if limit<=0:raise ValueError('limit must be positive')
 step_end=_finite_number(started+limit,'step deadline')
 phase_deadline=_finite_number(step_end+10 if phase_deadline is None else phase_deadline,'phase deadline')
 end=min(step_end,phase_deadline)
 folder=Path(folder);log=folder/(role+'.log')
 def not_started(reason):
  # This only describes this attempted command: it is not evidence that other
  # processes have closed. No identity, return code or wait observation is made up.
  out={'argv':argv,'executed':False,'identity':None,'returncode':None,
       'timed_out':False,'remaining_live':[],
       'exit_observation':None,'observer_joined':False,
       'log_sha256':sha(log) if log.exists() else None,
       'log_bytes':log.stat().st_size if log.exists() else None,
       'resource_scope':'no process launched by this command; no global closure assertion',
       'completion_accounting':{'verdict_eligible':False,'process_status':'not_started','reason':reason},
       'started_at_monotonic':started,'process_deadline_monotonic':end,
       'phase_deadline_monotonic':phase_deadline,'not_started_at_monotonic':time.monotonic()}
  save(folder/(role+'.json'),out)
  return out,''
 if time.monotonic()>=end:return not_started('deadline_before_registry')
 capture_owned()
 if time.monotonic()>=end:return not_started('deadline_after_registry')
 p=None;q=None;observer=None
 try:
  with log.open('xb') as f:
   env=env_for()
   # File/environment/registry work can consume the last remaining time.
   # This is a final admission check, not a hard realtime launch guarantee.
   if time.monotonic()>=end:return not_started('deadline_before_spawn')
   p=subprocess.Popen(argv,cwd=folder,env=env,stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
  q=own(role,p.pid)
  identity=Identity(q['pid'],q['start_ticks'],q['boot_id'])
  observer=ExitObserver(p,identity);observer.start();timed=False;termination=False;known=[]
  try:
   while observer.snapshot() is None:
    known=capture_owned()
    # Registry work must not decide when wait() observed the real process exit.
    if observer.snapshot() is not None:break
    if time.monotonic()>=end:timed=True;termination=True;break
    observer.wait(min(.1,max(0,end-time.monotonic())))
  finally:
   if timed:stop(q,.2)
   observation=observer.wait(3);joined=observer.join(2)
   known=capture_owned();owner=identity_key(q)
   services=[read(REG/'model.json'),read(REG/'recorder.json')];service_keys={identity_key(x) for x in services if x}
   def live_tools(rows):
    # A freshly adopted helper may first appear with only supervisor ownership.
    # Every non-service owned process must close, including that final-scan case.
    return [x for x in rows if same(x) and x['pid']!=os.getpid() and x.get('role') not in ('watchdog','supervisor') and identity_key(x) not in service_keys and not service_keys.intersection(x.get('owner_keys',[]))]
   residual=live_tools(known)
   if residual:termination=True;stop_rows(residual,.2)
   final_rows=capture_owned();left=live_tools(final_rows)
   closure=ResourceClosure(time.monotonic(),not left and not same(q),tuple(identity_key(x) for x in left),hashlib.sha256(json.dumps(final_rows,sort_keys=True).encode()).hexdigest())
  observation_data=None if observation is None else {'identity':q,'returncode':observation.returncode,'observed_at_monotonic':observation.observed_at,'error_type':observation.error_type}
  accounting=assess_completion(identity,observation,started_at=started,process_deadline=end,phase_deadline=phase_deadline,closure=closure,metadata_finished_at=time.monotonic(),observer_joined=joined,termination_requested=termination)
  timed=timed or accounting['process_status']=='observed_after_process_deadline'
  out={'argv':argv,'executed':True,'started_at_monotonic':started,'process_deadline_monotonic':end,'identity':q,'returncode':p.returncode,'timed_out':timed,'remaining_live':left,'log_sha256':sha(log),'log_bytes':log.stat().st_size,'exit_observation':observation_data,'observer_joined':joined,'completion_accounting':accounting,'resource_snapshot_sha256':closure.identity_snapshot_sha256,'phase_deadline_monotonic':phase_deadline}
  text=log.read_text(errors='replace');out['publication_policy']='returned verdict is checked again after required log read and receipt fsync'
  save(folder/(role+'.json'),out)
  if time.monotonic()>phase_deadline:
   out['completion_accounting']=assess_completion(identity,observation,started_at=started,process_deadline=end,phase_deadline=phase_deadline,closure=closure,metadata_finished_at=time.monotonic(),observer_joined=joined,termination_requested=termination)
   out['late_receipt_publication']=True;save(folder/(role+'.json'),out)
  return out,text
 except BaseException as error:
  if p is None:raise
  return _post_spawn_unknown(error,p,q,observer,argv,folder,role,log,started,end,phase_deadline)


def evaluate(source,suite,folder,seconds=90,trust_reference=False,phase_deadline=None):
 # Start before input reads/staging, and also honor an optional enclosing chain
 # deadline without converting its remaining duration back into a later end.
 phase_started=_finite_number(time.monotonic(),'phase started');wall_started=time.time()
 seconds=_finite_number(seconds,'seconds')
 if seconds<=0:raise ValueError('seconds must be positive')
 end=_finite_number(phase_started+seconds,'phase end')
 if phase_deadline is not None:end=min(end,_finite_number(phase_deadline,'phase deadline'))
 folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
 source=Path(source);suite=Path(suite);dut=source.read_bytes();tb=suite.read_bytes()
 (folder/'dut.sv').write_bytes(dut);(folder/'tb.sv').write_bytes(tb)
 receipt={'started':wall_started,'phase_started_at_monotonic':phase_started,'phase_deadline_monotonic':end,'source_sha256':hashlib.sha256(dut).hexdigest(),'suite_sha256':hashlib.sha256(tb).hexdigest(),'staged_dut_sha256':sha(folder/'dut.sv'),'staged_suite_sha256':sha(folder/'tb.sv'),'steps':[],'compile_executed':False,'simulation_executed':False}
 roleid=hashlib.sha256(str(folder).encode()).hexdigest()[:12]
 try:
  if not trust_reference and not source_allowed(dut.decode('utf-8')):
   receipt.update(status='source_rejected',feedback='Source includes forbidden testbench/external constructs or wrong module declaration.');return receipt
  commands=[['/tools/Xilinx/2026.1/Vivado/bin/xvlog','--sv','dut.sv','tb.sv'],['/tools/Xilinx/2026.1/Vivado/bin/xelab','tb','-s','sim'],['/tools/Xilinx/2026.1/Vivado/bin/xsim','sim','-runall']]
  for j,argv in enumerate(commands):
   remaining=end-time.monotonic()
   if remaining<.5:receipt.update(status='tool_unknown',feedback='tool budget exhausted');return receipt
   row,log=command(argv,folder,30,f'eda-{roleid}-{j}',phase_deadline=end);receipt['steps'].append(row)
   if j==0 and row['executed']:receipt['compile_executed']=True
   if j==2 and row['executed']:receipt['simulation_executed']=True
   if row['timed_out'] or row['remaining_live'] or not row['completion_accounting']['verdict_eligible']:
    receipt.update(status='tool_unknown',feedback='tool timeout or live descendants');return receipt
   if row['returncode']!=0:
    # License/service and launch failures are not syntax failures.
    infra=row['returncode']<0 or bool(re.search(r'license|cannot execute|command not found|segmentation|internal error|No such file|Permission denied|out of memory|resource temporarily|killed',log,re.I))
    diagnostic=bool(re.search(r'ERROR:\s*\[(?:VRFC|XSIM)\s+\d+-\d+\]',log))
    receipt.update(status='tool_unknown' if infra or j==2 or not diagnostic else 'compile_fail',feedback=log[-5000:]);return receipt
  matches=re.findall(r'RESULT failures=(\d+)',log)
  if len(matches)!=1:receipt.update(status='tool_unknown',feedback='missing unique simulation verdict');return receipt
  n=int(matches[0]);receipt.update(status='pass' if n==0 else 'public_fail',failures=n,feedback='\n'.join(line for line in log.splitlines() if line.startswith(('MISMATCH','RESULT'))))
  return receipt
 except BaseException as e:
  receipt.update(status='tool_unknown',error_type=type(e).__name__,feedback=str(e)[:1000]);return receipt
 finally:
  receipt['closed']=time.time();receipt['elapsed_s']=receipt['closed']-receipt['started'];receipt['elapsed_monotonic_s']=time.monotonic()-phase_started
  if time.monotonic()>end:receipt.update(status='tool_unknown',feedback='full tool phase budget exceeded including closure and metadata')
  save(folder/'result.json',receipt)
  # The publication itself belongs to the phase. A late write can only publish
  # an unknown correction, never authorize a repair or an accepted DUT.
  if time.monotonic()>end:
   receipt.update(status='tool_unknown',feedback='full tool phase exceeded during final receipt publication',late_receipt_publication=True,closed=time.time())
   receipt['elapsed_s']=receipt['closed']-receipt['started'];save(folder/'result.json',receipt)
