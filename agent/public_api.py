"""Authenticated official HTTP contract, forwarding to the same local engine."""
import argparse,hmac,http.client,http.server,json,os,re,signal,socket,subprocess,sys,threading,time,uuid
from pathlib import Path
from engine_link import call,ready,EngineError
from runtime_paths import state_root
from service_entry import private_token
from service_start import same,put
from native_model import identity
from release_integrity import verify_release

def health(token):
    c=http.client.HTTPConnection('127.0.0.1',7860,timeout=5)
    try:
        c.request('GET','/v1/health',headers={'Authorization':'Bearer '+token})
        response=c.getresponse();raw=response.read(65536)
        if response.status!=200:raise RuntimeError('投题端口健康检查失败：HTTP '+str(response.status))
        return json.loads(raw)
    finally:c.close()

def serve(sid):
    verify_release();token=private_token();expected='Bearer '+token
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,data):
            raw=json.dumps(data,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.send_header('Connection','close');self.end_headers()
            self.wfile.write(raw);self.close_connection=True
        def handle_one_request(self):
            self.connection.settimeout(5)
            return super().handle_one_request()
        def run(self):
            arrived=time.monotonic()
            values=self.headers.get_all('Authorization',[])
            if len(values)!=1 or not hmac.compare_digest(values[0],expected):
                self.reply(401,{'error':'unauthorized'});return
            allowed=self.command=='GET' and (self.path=='/v1/health' or re.fullmatch(r'/v1/jobs/[0-9a-f]{32,64}',self.path))
            allowed=allowed or self.command=='POST' and self.path=='/v1/solve'
            if not allowed:self.reply(404,{'error':'unknown endpoint'});return
            try:
                data=None
                if self.command=='POST':
                    lengths=self.headers.get_all('Content-Length',[])
                    if len(lengths)!=1 or self.headers.get('Transfer-Encoding'):raise ValueError('one content length required')
                    n=int(lengths[0])
                    if n<1 or n>500000:raise ValueError('request body size')
                    chunks=[];received=0;body_end=arrived+5.
                    while received<n:
                        remaining=body_end-time.monotonic()
                        if remaining<=0:raise TimeoutError('body deadline')
                        self.connection.settimeout(remaining)
                        chunk=self.rfile.read1(min(65536,n-received))
                        if not chunk:break
                        chunks.append(chunk);received+=len(chunk)
                    raw=b''.join(chunks)
                    if time.monotonic()>=body_end:raise TimeoutError('body deadline')
                    if len(raw)!=n:raise ValueError('incomplete body')
                    data=json.loads(raw)
                value=call('/v1/solve-public',{'request':data,'queued_at_monotonic':arrived},timeout=5) if self.path=='/v1/solve' else call(self.path,data,timeout=5)
                if self.path=='/v1/health':value={**value,'gateway_id':sid}
                self.reply(202 if self.command=='POST' else 200,value)
            except EngineError as exc:self.reply(exc.status or 503,{'error':str(exc)[:250]})
            except (ValueError,UnicodeError):self.reply(400,{'error':'invalid JSON request'})
            except (OSError,TimeoutError):self.reply(503,{'error':'engine temporarily unavailable'})
        do_GET=run;do_POST=run
    class Server(http.server.ThreadingHTTPServer):
        daemon_threads=True
        request_queue_size=16
    server=Server(('127.0.0.1',7860),Handler)
    def stop(signum,frame):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()

def supervise(sid):
    """Restart only the stateless HTTP adapter, never a model or a task."""
    token=private_token();journal=state_root()/'gateway-sessions'/sid;stopping=[]
    def stop(signum,frame):stopping.append(signum)
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    attempt=0;child=None;saved=None
    try:
        while not stopping:
            attempt+=1;env=dict(os.environ);env['FPGACHINA_TOKEN']=token
            child=subprocess.Popen([sys.executable,'-B',__file__,'serve',sid],env=env,stdin=subprocess.DEVNULL)
            saved=identity(child.pid);put(journal/('http-%04d-start.json'%attempt),{'identity':saved,'epoch':time.time()})
            while child.poll() is None and not stopping:time.sleep(.1)
            if stopping:
                if same(saved):os.kill(child.pid,signal.SIGTERM)
                try:child.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    if same(saved):os.kill(child.pid,signal.SIGKILL)
                    child.wait(timeout=3)
            else:child.wait()
            put(journal/('http-%04d-ended.json'%attempt),{'identity':saved,'returncode':child.returncode,'wait_completed':True,'epoch':time.time()})
            if not stopping:time.sleep(.1)
    finally:
        if child is not None and child.poll() is None:
            if saved and same(saved):os.kill(child.pid,signal.SIGTERM)
            child.wait(timeout=8)
        put(journal/'ended.json',{'epoch':time.time(),'adapter_restarts':max(0,attempt-1),'task_replays':0,'model_restarts':0})

def main(action='start'):
    import fcntl
    if action not in ('start','stop','status'):raise ValueError('用法：serve/run_api.sh [start|stop|status]')
    state=state_root();state.mkdir(parents=True,exist_ok=True)
    verify_release()
    with (state/'gateway.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        record=state/'gateway.json';old=json.loads(record.read_bytes()) if record.exists() else None
        if action=='stop':
            if old and same(old['identity']):
                os.kill(old['identity']['pid'],signal.SIGTERM)
                until=time.monotonic()+8
                while same(old['identity']) and time.monotonic()<until:time.sleep(.1)
                if same(old['identity']):raise RuntimeError('投题服务还未退出，未发送强杀。')
            print('投题服务已停止。解题引擎独立保留。');return
        token=private_token()
        if action=='status':print(json.dumps(health(token),ensure_ascii=False));return
        ready()
        if old and same(old['identity']):
            value=health(token)
            if value.get('gateway_id')!=old['session'] or value.get('ready') is not True:
                raise RuntimeError('投题端口与当前服务不匹配或引擎未就绪。')
            print('投题服务已就绪，无需重复启动。');return
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1',7860))==0:raise RuntimeError('7860 被其他服务占用；没有启动重复投题服务。')
        sid=uuid.uuid4().hex;journal=state/'gateway-sessions'/sid;journal.mkdir(parents=True,exist_ok=False)
        env=dict(os.environ);env['FPGACHINA_TOKEN']=token;env['PYTHONDONTWRITEBYTECODE']='1'
        put(journal/'launch-intent.json',{'epoch':time.time(),'host':'127.0.0.1','port':7860})
        with (journal/'api.log').open('xb') as log:
            proc=subprocess.Popen([sys.executable,'-B',__file__,'supervise',sid],env=env,
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        row={'identity':identity(proc.pid),'session':sid};put(journal/'launched.json',row)
        temp=state/('gateway-'+sid+'.pending');put(temp,row);os.replace(temp,record)
        until=time.monotonic()+15
        while time.monotonic()<until:
            if proc.poll() is not None:raise RuntimeError('投题服务启动失败：'+str(journal/'api.log'))
            try:
                value=health(token)
                if value.get('gateway_id')==sid and value.get('ready') is True:
                    print('投题服务就绪：127.0.0.1:7860；可按赛方要求开启隧道。');return
            except (OSError,ValueError,RuntimeError):pass
            time.sleep(.2)
        raise RuntimeError('投题服务未在时限内就绪；查看 '+str(journal/'api.log'))

if __name__=='__main__':
    try:
        if len(sys.argv)>1 and sys.argv[1]=='serve':serve(sys.argv[2])
        elif len(sys.argv)>1 and sys.argv[1]=='supervise':supervise(sys.argv[2])
        else:main(sys.argv[1] if len(sys.argv)>1 else 'start')
    except Exception as exc:print(str(exc),file=sys.stderr);raise SystemExit(1)
