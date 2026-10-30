"""Offline delivery classification and evidence-only storage; never adopts RTL.

A module envelope is not syntax, interface, synthesis or functional acceptance.
Transport fields are caller evidence, not a replacement for native SSE closure.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from artifact_store import EvidenceStore

MAX_BODY_BYTES=1024*1024
MAX_SOURCE_BYTES=200000

class DeliveryError(ValueError):pass

def sha(raw):return hashlib.sha256(raw).hexdigest()

def strict_json(raw):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result:raise DeliveryError('duplicate JSON key')
            result[k]=v
        return result
    def invalid(value):raise DeliveryError('nonfinite JSON number')
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)
    except (ValueError,UnicodeError,RecursionError) as exc:raise DeliveryError('invalid JSON') from exc


def _mask(text):
    """Lexical envelope check only. Strings/comments cannot invent module tokens."""
    chars=list(text);i=0
    while i<len(text):
        if text.startswith('//',i):
            end=text.find('\n',i);end=len(text) if end<0 else end
        elif text.startswith('/*',i):
            end=text.find('*/',i+2)
            if end<0:raise DeliveryError('unterminated comment')
            end+=2
        elif text[i]=='"':
            end=i+1
            while end<len(text):
                if text[end]=='\\':end+=2
                elif text[end]=='"':end+=1;break
                else:end+=1
            else:raise DeliveryError('unterminated string')
        else:i+=1;continue
        for j in range(i,min(end,len(text))):
            if text[j] not in '\r\n':chars[j]=' '
        if text[i]=='"':chars[i]='"'  # A string outside the module is not whitespace.
        i=end
    return ''.join(chars)


@dataclass(frozen=True)
class Delivery:
    kind:str
    candidate:bytes|None
    reason:str
    body_sha256:str


def inspect_body(raw):
    if type(raw) is not bytes:raise DeliveryError('raw body bytes required')
    digest=sha(raw)
    def reject(kind,reason):return Delivery(kind,None,reason,digest)
    if len(raw)>MAX_BODY_BYTES:return reject('body_too_large','raw body limit')
    try:text=raw.decode('utf-8')
    except UnicodeError:return reject('invalid_utf8','not UTF-8 text')
    if not text.strip():return reject('empty_body','no visible answer body')
    if '\x00' in text:return reject('protocol_rejected','NUL byte')
    candidate=text
    if '```' in text:
        match=re.fullmatch(r'\s*```(?:systemverilog|verilog|sv)?[ \t]*\r?\n(?P<body>.*?)```\s*',text,re.S|re.I)
        if not match:return reject('protocol_rejected','one RTL code fence only; no prose or extra block')
        candidate=match.group('body')
    data=candidate.encode('utf-8')
    if len(data)>MAX_SOURCE_BYTES:return reject('body_too_large','source limit; source not truncated')
    try:masked=_mask(candidate)
    except DeliveryError as exc:return reject('no_complete_rtl',str(exc))
    modules=list(re.finditer(r'\bmodule\b',masked));ends=list(re.finditer(r'\bendmodule\b',masked))
    if len(modules)!=1 or len(ends)!=1:return reject('no_complete_rtl','one complete module required')
    begin,end=modules[0],ends[0]
    if masked[:begin.start()].strip() or not re.match(r'module\s+TopModule\b',masked[begin.start():]):
        return reject('no_complete_rtl','TopModule required without explanatory preface')
    semicolon=masked.find(';',begin.end())
    if semicolon<0 or semicolon>=end.start():return reject('no_complete_rtl','module header incomplete')
    if not re.fullmatch(r'\s*(?::\s*TopModule\s*)?',masked[end.end():]):
        return reject('no_complete_rtl','unexpected text following endmodule')
    return Delivery('candidate_unchecked',data,'envelope only; policy/compiler/interface/function unverified',digest)


class DeliveryStore:
    """One immutable result per claimed request; no selected/solution writer."""
    def __init__(self,root):self.store=EvidenceStore(root)
    def begin(self,event,request_id):
        if type(request_id) is not str or not request_id or len(request_id)>160:
            raise DeliveryError('request identity required')
        return self.store.claim(event,{'request_id':request_id,'scope':'delivery artifacts only; not request authorization'})
    def record(self,event,raw_body,wire_receipt):
        # Save raw originals even when parsing, envelope or caller closure fails.
        raw=self.store.record(event,'raw-body.bin',raw_body)
        proof=self.store.record(event,'transport-receipt.json',wire_receipt)
        body=inspect_body(raw_body)
        claim=strict_json(self.store._path(event,'attempt-intent.json').read_bytes())
        wire_ok=False;wire_reason='invalid or unbound caller transport receipt'
        try:
            wire=strict_json(wire_receipt)
            wire_ok=(type(wire) is dict and wire.get('schema')=='transport-body-closure-v1'
                and wire.get('request_id')==claim['manifest']['request_id']
                and wire.get('body_sha256')==body.body_sha256
                and type(wire.get('http_status')) is int and wire['http_status']==200
                and all(wire.get(k) is True for k in ('wire_complete','transport_closed','backend_idle')))
            if wire_ok:wire_reason='bound caller closure receipt; native transport not verified by this component'
        except (DeliveryError,KeyError,TypeError):pass
        result={'schema':'rtl-delivery-artifacts-v1','body_kind':body.kind,'body_reason':body.reason,
                'body_sha256':body.body_sha256,'raw_artifact':raw,'transport_artifact':proof,
                'transport_status':'caller_reported_closed' if wire_ok else 'unknown',
                'transport_reason':wire_reason,'candidate':None,'selected_written':False,
                'policy_verified':False,'compiler_verified':False,'functional_verified':False,'runtime_accepted':False}
        if wire_ok and body.candidate is not None:
            result['candidate']=self.store.record(event,'candidate.sv',body.candidate)
        self.store.record(event,'delivery.json',result)
        return result
