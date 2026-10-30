"""Loopback model fingerprint, template tokenization, SSE and cancellation checks."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import threading
import time
from adapter_contracts import ModelReply, MODEL, RUNTIME, TEMPLATE, WEIGHT, require
from prompt_builder import canonical, digest

ROOT = Path(__file__).resolve().parent
# Service lifetime is owner-controlled; every task retains its finite monotonic deadline.
MAX_WIRE = 32 * 1024 * 1024


def runtime_fingerprint(value):
    """Same canonical JSON bytes used by the original pinned runtime lock."""
    return digest(canonical(value))


def put(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = body if type(body) is bytes else canonical(body)
    with path.open('xb') as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    return {'sha256': digest(raw), 'bytes': len(raw)}


def identity(pid):
    p = Path('/proc') / str(pid)
    fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': fields[19], 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'state': fields[0]}


def parse_sse(raw):
    """One choice, terminal finish and DONE are all mandatory; retain raw elsewhere."""
    text = raw.decode('utf-8', errors='strict')
    data = []; content = []; finished = None; done = False; usage = None; seen = False
    for line in (text + '\n\n').splitlines():
        if line.startswith('data:'):
            require(not done, 'data after DONE')
            data.append(line[5:].lstrip())
        elif not line and data:
            event = '\n'.join(data); data = []
            if event == '[DONE]':
                done = True
                continue
            val = json.loads(event)
            require(type(val) is dict and 'error' not in val, 'upstream SSE error')
            if val.get('usage') is not None:
                v = val['usage']
                usage = {k: v[k] for k in ('prompt_tokens', 'completion_tokens')}
            choices = val.get('choices', [])
            require(type(choices) is list and len(choices) <= 1, 'one native SSE choice required')
            for choice in choices:
                require(choice.get('index') == 0, 'unexpected SSE choice')
                seen = True
                delta = choice.get('delta') or {}
                body = delta.get('content')
                if body is not None:
                    require(type(body) is str, 'non-text content')
                    content.append(body)
                if choice.get('finish_reason') is not None:
                    require(finished is None, 'duplicate terminal choice')
                    finished = choice['finish_reason']
    require(done and seen and finished in ('stop', 'length'), 'incomplete native stream')
    return ''.join(content).encode('utf-8'), usage, finished


class NativeModel:
    def __init__(self, evidence, service_receipt, *, allow_native=False, clock=time.monotonic,
                 wall=time.time, connection_factory=http.client.HTTPConnection, identity_reader=identity):
        require(not (ROOT/'offline-only.json').exists(), 'R7 native model disabled')
        require(allow_native, 'native model adapter is opt-in')
        self.root = Path(evidence); self.root.mkdir(parents=True, exist_ok=False)
        self.service = json.loads(Path(service_receipt).read_bytes())
        self.clock = clock; self.wall = wall; self.connection_factory = connection_factory
        self.identity_reader = identity_reader
        self.tainted = False; self.closed = []; self.sequence = 0

    def claim_global(self, context, payload):
        journal = Path(self.service['owner_root'])/'global-posts'
        require(journal.is_dir() and not journal.is_symlink() and self.service['max_POSTs_per_job'] == 3,
                'per-job owner request journal required')
        require(type(context['run_id']) is str and bool(context['run_id']), 'bound job ID required')
        root = journal/digest(context['run_id'].encode())
        root.mkdir(exist_ok=True)
        require(not root.is_symlink(), 'linked request journal rejected')
        for n in range(1,4):
            slot = root/('%02d'%n)
            try: slot.mkdir()
            except FileExistsError: continue
            put(slot/'claim.json',{'request_id':context['request_id'],
                'payload_sha256':digest(payload),'context':context,'no_replay':True})
            return n
        raise RuntimeError('three POST per-job limit')

    def guard(self, end):
        require(not self.tainted, 'prior model closure is unresolved')
        for role in ('owner_identity','watchdog_identity'):
            expected = self.service[role]
            actual = self.identity_reader(expected['pid'])
            require(all(actual[k] == expected[k] for k in ('pid','start_ticks','boot_id'))
                    and actual['state'] != 'Z', 'native '+role+' is no longer running')
        owner_root=Path(self.service['owner_root'])
        require(not (owner_root/'watchdog-stop.json').exists() and not (owner_root/'monitor-failed.json').exists(),
                'owner stopped accepting model operations')
        if self.clock() >= end:
            raise TimeoutError('native absolute deadline')

    def json_http(self, path, data=None, *, end):
        self.guard(end)
        c = self.connection_factory('127.0.0.1', 8000, timeout=max(.05, min(6., end-self.clock())))
        timer=None; sock=None
        def cancel_metadata():
            try:
                target=sock or c.sock
                if target:target.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            c.close()
        try:
            c.connect(); sock=c.sock; self.guard(end)
            timer=threading.Timer(max(.001,min(6.,end-self.clock())),cancel_metadata)
            timer.daemon=True;timer.start()
            c.request('GET' if data is None else 'POST', path,
                      body=None if data is None else canonical(data), headers={'Content-Type':'application/json'})
            r = c.getresponse(); raw = r.read(8*1024*1024+1)
            self.guard(end)
            require(r.status == 200 and len(raw) <= 8*1024*1024, 'metadata HTTP status/size')
            self.sequence += 1
            put(self.root/('metadata-%04d.json'%self.sequence), {'path':path, 'request':data,
                'response':json.loads(raw), 'response_raw_sha256':digest(raw), 'epoch':self.wall()})
            put(self.root/('metadata-%04d.raw'%self.sequence), raw)
            return json.loads(raw)
        finally:
            if timer:timer.cancel()
            cancel_metadata()
            if timer:
                timer.join(2.)
                if timer.is_alive():self.tainted=True;raise RuntimeError('metadata cancellation thread did not exit')

    def idle(self, *, end):
        slots = self.json_http('/slots', end=end)
        return type(slots) is list and len(slots) == 1 and slots[0].get('is_processing') is False

    def preflight(self, *, context):
        self.guard(context['deadline_monotonic'])
        expected = json.loads((ROOT.parent/'model'/'model-runtime-lock.json').read_bytes())
        row = self.service
        require(row['runtime_sha256'] == expected['required_runtime_sha256'] == RUNTIME
                and row['weight_sha256'] == WEIGHT and row['weight_bytes'] == 22134528992,
                'owner runtime or weight verification differs')
        actual = self.identity_reader(row['identity']['pid'])
        require(all(actual[k] == row['identity'][k] for k in ('pid','start_ticks','boot_id'))
                and actual['state'] != 'Z' and actual['boot_id'] == context['clock_domain']['boot_id'],
                'backend identity changed')
        argv = (Path('/proc')/str(actual['pid'])/'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
        require(argv == expected['required_runtime']['argv'], 'actual model argv differs')
        props = self.json_http('/props', end=context['deadline_monotonic'])
        req = expected['required_runtime']
        observed = {**req, 'model_properties':{k:props.get(k) for k in req['model_properties']},
                    'chat_template_sha256':digest(props.get('chat_template','').encode())}
        require(runtime_fingerprint(observed) == RUNTIME and self.idle(end=context['deadline_monotonic']), 'live model props/slot mismatch')
        receipt = {'schema':'host-runtime-evidence-v1', 'run_id':context['run_id'],
            'clock_domain':context['clock_domain'], 'runtime_sha256':RUNTIME, 'template_sha256':TEMPLATE,
            'weight_sha256':WEIGHT, 'context_tokens':32768, 'slots':1, 'automatic_retries':0,
            'evidence_scope':'trusted_native_adapter', 'owner_receipt_sha256':digest(canonical(row)),
            'identity':actual, 'runtime_accepted':False}
        return canonical(receipt)

    def tokenize(self, *, messages, generation, context):
        end = context['deadline_monotonic']
        rendered = self.json_http('/apply-template', {'messages':json.loads(messages),
            'add_generation_prompt':True, **json.loads(generation)}, end=end).get('prompt')
        require(type(rendered) is str and rendered, 'actual template response missing')
        counts = []
        for special in (False, True):
            v = self.json_http('/tokenize', {'content':rendered, 'add_special':special}, end=end)
            require(type(v.get('tokens')) is list and all(type(t) is int for t in v['tokens']), 'actual tokens required')
            counts.append(len(v['tokens']))
        return canonical({'schema':'template-token-evidence-v1', 'request_id':context['request_id'],
            'clock_domain':context['clock_domain'], 'runtime_sha256':RUNTIME, 'template_sha256':TEMPLATE,
            'messages_sha256':digest(messages), 'generation_sha256':digest(generation), 'context_tokens':32768,
            'method':'maximum_actual_token_count_no_clipping', 'special_mode_counts':counts,
            'rendered_prompt_sha256':digest(rendered.encode()), 'evidence_scope':'trusted_native_adapter'})

    def generate(self, *, payload, context, permit):
        stage = context['request_id'].rsplit('.', 1)[-1]
        require(stage in ('route','initial','compiler_repair'), 'unknown stage')
        end = min(context['deadline_monotonic']-90., self.clock()+(180. if stage=='route' else (840. if context.get('solve_mode')=='direct' else 540.) if stage=='initial' else 720.))
        self.guard(end); require(self.idle(end=end), 'model busy before claim')
        folder = self.root/context['request_id']; folder.mkdir(exist_ok=False)
        put(folder/'request.json', payload)
        put(folder/'attempt-intent.json', {'request_id':context['request_id'],'context':context,
            'deadline_monotonic':end,'global_stop_epoch':None,'automatic_retries':0})
        global_slot = self.claim_global(context,payload)
        c = self.connection_factory('127.0.0.1', 8000, timeout=max(.05,end-self.clock()))
        sock = None; timer = None; expired = threading.Event(); attempted = False; sent = False
        status = None; body = b''; stream = b''; wire = False; usage = None; finish = None; error = None
        closed_transport = False; backend_idle = False; start = None
        def interrupt():
            expired.set()
            try:
                target = sock or c.sock
                if target: target.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            c.close()
        try:
            c.connect(); sock = c.sock
            self.guard(end)
            timer = threading.Timer(max(.001,end-self.clock()), interrupt)
            timer.daemon = True; timer.start()
            put(folder/'dispatch-intent.json', {'epoch':self.wall(),'not_a_server_receipt':True})
            start = permit.enter(payload)
            self.guard(end); require(not expired.is_set(), 'request expired before POST')
            attempted = True
            c.request('POST','/v1/chat/completions',body=payload,headers={'Content-Type':'application/json'})
            sent = True; r = c.getresponse(); status = r.status
            require(status == 200 and 'text/event-stream' in r.getheader('Content-Type','').lower(), 'SSE HTTP response required')
            pieces = []; total = 0
            with (folder/'response.raw').open('xb') as f:
                while True:
                    self.guard(end)
                    chunk = r.readline(MAX_WIRE+1)
                    if not chunk: break
                    total += len(chunk); f.write(chunk); f.flush()
                    require(total <= MAX_WIRE, 'raw stream limit')
                    pieces.append(chunk)
                    if chunk.strip() == b'data: [DONE]': break
                os.fsync(f.fileno())
            stream = b''.join(pieces)
            body, usage, finish = parse_sse(stream); wire = True
        except BaseException as exc:
            error = {'type':type(exc).__name__, 'post_call_attempted':attempted, 'post_returned':sent}
        finally:
            if timer: timer.cancel()
            interrupt()
            if timer: timer.join(2.)
            closed_transport = timer is None or not timer.is_alive()
            raw = folder/'response.raw'
            if raw.exists(): stream = raw.read_bytes()
            # Client/socket close only attempts cancellation. Require actual idle separately.
            try:
                until = min(context['deadline_monotonic']-1., self.clock()+10.)
                while self.clock() < until:
                    if self.idle(end=until): backend_idle = True; break
                    time.sleep(.05)
            except Exception: backend_idle = False
            if not closed_transport or not backend_idle: self.tainted = True
        receipt = {'schema':'model-request-closure-v1','request_id':context['request_id'],
            'clock_domain':context['clock_domain'],'runtime_sha256':RUNTIME,'request_sha256':digest(payload),
            'body_sha256':digest(body),'stream_sha256':digest(stream),'http_status':status,
            'posts':1 if sent else 0 if not attempted else None,'post_call_attempted':attempted,
            'automatic_retries':0,'wire_complete':wire,'transport_closed':closed_transport,
            'backend_idle':backend_idle,'inflight_requests':0 if backend_idle else None,
            'started_at_monotonic':start,'closed_at_monotonic':self.clock(),'usage':usage,
            'finish_reason':finish,'error':error,'cancel_method':'close_owned_HTTP_socket_then_observe_slots',
            'global_attempt_slot':global_slot,
            'native_runtime_accepted':False}
        put(folder/'closure.json', receipt); put(folder/'body.txt', body)
        if sent and wire and closed_transport and backend_idle:
            self.closed.append({'request_id':context['request_id'],'payload_sha256':digest(payload),'closed':True})
        return ModelReply(body, stream, canonical(receipt))
