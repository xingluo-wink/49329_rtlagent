"""Pinned, offline prompt construction. No transport, tokenizer, EDA or grading.

Public-field provenance remains the caller's responsibility. This builder never
loads case folders or private tests and never treats a router contract as truth.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import route_adapter

LOCK_SHA256 = 'd804f874a9bd851ec8ab8f58edc95350c62d74514442957cd430059474c20ea0'
MAX_PUBLIC_BYTES = 200000
MAX_ROUTE_BYTES = 32768
SOLVE_POLICY = ('依据公开需求和接口，交付完整可综合SystemVerilog模块。'
                '公开原文优先；router_contract_advisory是路由器的辅助短答，可能遗漏或理解错误，'
                '不能覆盖公开要求。知识卡是通用提示，选中卡或引用匹配都不证明代码正确。'
                '仅输出完整RTL，不输出分析过程。')
BASELINE_RTL_SYSTEM = ('You are an expert Verilog designer. Reply with a single synthesizable '
                       'SystemVerilog module named TopModule. Output only code — no prose, no '
                       'markdown fences.')


class PromptInputError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class PreparedPrompt:
    messages: tuple
    audit_json: str

    def to_chat_messages(self):
        return [{'role':m.role, 'content':m.content} for m in self.messages]

    def audit(self):
        return json.loads(self.audit_json)

    def verify_exact_payload(self, payload):
        try:
            return canonical(payload) == canonical(self.to_chat_messages())
        except (TypeError, ValueError, UnicodeError):
            return False


class PromptBuilder:
    def __init__(self, asset_root=None):
        self.root = Path(asset_root) if asset_root is not None else Path(__file__).resolve().parent.parent/'skill'
        self.root = self.root.absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            raise PromptInputError('asset root must be a real directory')
        self._resolved_root = self.root.resolve(strict=True)
        lock_raw = self._read('prompt-inputs.lock.json')
        if digest(lock_raw) != LOCK_SHA256:
            raise PromptInputError('input lock trust anchor mismatch')
        lock = json.loads(lock_raw)
        if lock.get('schema') != 'v19-r3-prompt-inputs-v1':
            raise PromptInputError('input lock schema')
        # The imported parser is part of the reviewed source, not model-selected.
        code_root = Path(__file__).resolve().parent
        parser_path = code_root/'route_adapter.py'
        if (parser_path.is_symlink() or Path(route_adapter.__file__).resolve() != parser_path
                or digest(parser_path.read_bytes()) != lock['route_adapter_source_sha256']
                or digest(route_adapter.ROUTE_SYSTEM.encode('utf-8')) != lock['route_system_utf8_sha256']):
            raise PromptInputError('route adapter source or prompt mismatch')
        self._route_system = route_adapter.ROUTE_SYSTEM
        self._lock = lock
        self._bytes = {}
        for name, expected in lock['knowledge'].items():
            if (not name.startswith('knowledge/') or len(PurePosixPath(name).parts) != 2
                    or PurePosixPath(name).name not in ('ROOT.md','catalog.json')
                    and re.fullmatch(r'[a-z][a-z0-9_]{0,63}\.md', PurePosixPath(name).name) is None):
                raise PromptInputError('invalid locked knowledge path')
            raw = self._read(name)
            if len(raw) != expected['bytes'] or digest(raw) != expected['sha256']:
                raise PromptInputError('knowledge bytes mismatch: '+name)
            try:
                raw.decode('utf-8')
            except UnicodeError as error:
                raise PromptInputError('knowledge is not UTF-8') from error
            self._bytes[name] = raw
        catalog = json.loads(self._bytes['knowledge/catalog.json'])
        if (type(catalog) is not list or not catalog
                or any(type(row) is not dict or set(row) != {'id','summary'}
                       or type(row['id']) is not str or type(row['summary']) is not str
                       or re.fullmatch(r'[a-z][a-z0-9_]{0,63}', row['id']) is None for row in catalog)):
            raise PromptInputError('catalog schema')
        ids = tuple(row['id'] for row in catalog)
        expected_paths = {'knowledge/ROOT.md','knowledge/catalog.json'} | {'knowledge/'+i+'.md' for i in ids}
        if len(set(ids)) != len(ids) or set(self._bytes) != expected_paths:
            raise PromptInputError('catalog and pinned leaf set mismatch')
        self._ids = ids

    def _read(self, rel):
        name = PurePosixPath(rel)
        if (name.is_absolute() or name.as_posix() != rel
                or any(p in ('..','.') for p in name.parts)):
            raise PromptInputError('unsafe asset path')
        path = self.root.joinpath(*name.parts)
        current = self.root
        for part in name.parts:
            current = current/part
            if current.is_symlink():
                raise PromptInputError('linked input is not permitted')
        if path.resolve(strict=True) != self._resolved_root.joinpath(*name.parts):
            raise PromptInputError('asset escaped root')
        if not path.is_file():
            raise PromptInputError('asset is not a file')
        return path.read_bytes()

    @staticmethod
    def _public(public_input):
        if (type(public_input) is not dict or 'public_spec' not in public_input
                or set(public_input)-{'public_spec','interface'}):
            raise PromptInputError('only explicit public_spec and interface fields are accepted')
        out = {'public_spec':public_input['public_spec'], 'interface':public_input.get('interface','')}
        if (any(type(v) is not str or '\x00' in v for v in out.values())
                or not out['public_spec'].strip()):
            raise PromptInputError('public fields must be nonempty spec text and optional interface text')
        try:
            size = sum(len(v.encode('utf-8')) for v in out.values())
        except UnicodeError as error:
            raise PromptInputError('public fields are not valid UTF-8') from error
        if size > MAX_PUBLIC_BYTES:
            raise PromptInputError('public input too large; it will not be truncated')
        return out

    def _prepared(self, kind, public, system, user, exposed, extra=None):
        messages = (Message('system',system), Message('user',canonical(user).decode('utf-8')))
        payload = [{'role':m.role,'content':m.content} for m in messages]
        audit = {'kind':kind, 'input_lock_sha256':LOCK_SHA256,
                 'validated_files':self._lock['knowledge'],
                 'exposed_files':{n:self._lock['knowledge'][n] for n in exposed},
                 'public_input_sha256':digest(canonical(public)),
                 'messages_canonical_sha256':digest(canonical(payload)),
                 'message_content_sha256':[digest(m.content.encode('utf-8')) for m in messages],
                 'route_adapter_source_sha256':self._lock['route_adapter_source_sha256'],
                 'route_system_utf8_sha256':self._lock['route_system_utf8_sha256'],
                 'raw_knowledge_newlines_preserved':True,
                 'public_input_provenance':'caller supplied explicit public fields; content provenance is not inferred',
                 'token_budget_status':'unverified_requires_real_tokenizer_and_remaining_budget',
                 'ready_for_post':False, 'transport_calls':0, 'model_benefit_verified':False}
        if extra:
            audit.update(extra)
        return PreparedPrompt(messages, canonical(audit).decode('utf-8'))

    def build_route(self, public_input):
        public = self._public(public_input)
        system = self._route_system+'\n\n知识目录（仅ID和公开描述）：\n'+self._bytes['knowledge/catalog.json'].decode('utf-8')
        return self._prepared('route',public,system,{'public_requirements':public},['knowledge/catalog.json'])

    def build_solve(self, public_input, raw_route):
        public = self._public(public_input)
        if type(raw_route) is not str:
            raise PromptInputError('raw route response text required; caller acceptance flags are not trusted')
        try:
            route_raw = raw_route.encode('utf-8')
        except UnicodeError as error:
            raise PromptInputError('route response is not UTF-8') from error
        if len(route_raw) > MAX_ROUTE_BYTES:
            return self.build_direct(public,'route_unusable',digest(route_raw),
                                     {'route_error':'route response too large','semantic_no_card_confirmed':False})
        evidence_text = public['public_spec']+'\n'+public['interface']
        try:
            parsed = route_adapter.parse_route_packet(raw_route,evidence_text,self._ids)
        except route_adapter.RouteProtocolError as error:
            return self.build_direct(public, 'route_unusable', digest(route_raw),
                                     {'route_error':str(error), 'semantic_no_card_confirmed':False})
        route = parsed['route']
        if not route['cards']:
            return self.build_direct(public, 'explicit_no_card', digest(route_raw),
                                     {'quote_audit':parsed['quote_audit'],
                                      'route_encoding_audit':parsed['encoding_audit'],
                                      'router_selected_no_card':True,
                                      'semantic_no_card_confirmed':False})
        exposed = ['knowledge/ROOT.md']+['knowledge/'+card+'.md' for card in route['cards']]
        system = SOLVE_POLICY+'\n\n'+'\n\n'.join('['+name+']\n'+self._bytes[name].decode('utf-8') for name in exposed)
        user = {'public_requirements':public,'router_contract_advisory':route['contract']}
        return self._prepared('solve',public,system,user,exposed,
                              {'selected_leaf_ids':route['cards'],'quote_audit':parsed['quote_audit'],
                               'route_encoding_audit':parsed['encoding_audit'],
                               'quote_audit_text_layout':'public_spec + newline + interface',
                               'raw_route_sha256':digest(route_raw),'public_spec_overrides_contract':True})

    def build_direct(self, public_input, reason, route_sha256=None, extra=None):
        public = self._public(public_input)
        if reason not in ('explicit_no_card','route_unusable'):
            raise PromptInputError('unknown direct solve reason')
        # Match baseline.py's open(...).read() universal newline semantics.
        normalize = lambda value:value.replace('\r\n','\n').replace('\r','\n')
        text = normalize(public['public_spec'])
        interface = normalize(public['interface'])
        if interface:
            text += '\n\nInterface:\n'+interface
        messages = (Message('system',BASELINE_RTL_SYSTEM),Message('user',text))
        audit = {'kind':'direct_solve','routing_decision':'direct','reason':reason,
                 'selected_leaf_ids':[], 'exposed_files':{},
                 'public_input_sha256':digest(canonical(public)),
                 'messages_canonical_sha256':digest(canonical([{'role':m.role,'content':m.content} for m in messages])),
                 'raw_route_sha256':route_sha256,
                 'baseline_message_equivalence':True,
                 'route_contract_forwarded':False,'ready_for_post':False,
                 'model_benefit_verified':False}
        if extra: audit.update(extra)
        return PreparedPrompt(messages,canonical(audit).decode('utf-8'))
