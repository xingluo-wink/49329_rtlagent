"""Offline candidate: validate route structure; audit quote separately from routing.

Not installed into frozen V19, not a model/EDA acceptance result.
"""
import json,re,hashlib

class RouteProtocolError(ValueError):pass

MAX_ROUTE_BYTES=32768
MAX_IGNORED_SUMMARY_CHARS=1000

def _unique_object(pairs):
 out={}
 for key,value in pairs:
  if key in out:raise RouteProtocolError('duplicate JSON key')
  out[key]=value
 return out

def _reject_constant(_):raise RouteProtocolError('nonfinite JSON value')

ROUTE_SYSTEM='''你是RTL知识路由器。依据公开要求中的状态更新、数值规则或传输条件选知识，不依据题材、模块名称或题号。默认选1张卡；仅当另一个独立机制确实需要第二张卡时选2张。不要仅因题目有输出就追加生命周期卡。只返回JSON，字段为cards、contract、evidence。cards使用目录中的精确ID；contract用不超过240字说明决定实现的条件，明确采样时点、关键优先级或数学操作顺序；evidence给一段简短公开依据。依据文字会单独审计，选卡本身不证明理解或代码正确。 contract只写题面明确决定实现的规则：区分同一状态写入的互斥优先级与不同输出的独立使能；独立动作不要写成互斥。沿前取值与更新后取值必须明确。数值题先说明完整运算再截断或饱和；位宽和端点若未推导确认就不编造具体范围。evidence从公开原文复制一个连续短片段，不自行解释或改写。'''

ROUTE_SYSTEM+=' cards必须是精确ID字符串数组，例如{"cards":["edge_state"],"contract":"公开规则的简短说明","evidence":"从题面连续复制的短片段"}；不要复制目录中的id/summary对象。示例ID不是默认答案，须依据题意选择。'

def parse_route_packet(text,spec,catalog_ids):
 if type(text)!=str or type(spec)!=str:raise RouteProtocolError('body and public spec must be text')
 try:raw=text.encode('utf-8')
 except UnicodeError as error:raise RouteProtocolError('body is not UTF-8') from error
 if len(raw)>MAX_ROUTE_BYTES:raise RouteProtocolError('body too large; not truncated')
 body=text.strip()
 fenced=body.startswith('```')
 if body.startswith('```'):
  match=re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```',body,re.S)
  if not match:raise RouteProtocolError('incomplete JSON fence')
  body=match[1].strip()
 try:packet=json.loads(body,object_pairs_hook=_unique_object,parse_constant=_reject_constant)
 except (ValueError,TypeError,RecursionError) as e:raise RouteProtocolError('invalid or ambiguous JSON') from e
 if not isinstance(packet,dict) or set(packet)!={'cards','contract','evidence'}:raise RouteProtocolError('schema')
 proposed=packet['cards'];ignored_summaries=0
 if type(proposed)!=list or not 1<=len(proposed)<=2:raise RouteProtocolError('cards count')
 if all(type(item)==str for item in proposed):
  cards,encoding=list(proposed),'string_array'
 elif all(type(item)==dict for item in proposed):
  cards,encoding=[],'exact_id_object_array'
  for item in proposed:
   if set(item) not in ({'id'},{'id','summary'}) or type(item['id'])!=str:raise RouteProtocolError('card object schema')
   if 'summary' in item:
    if type(item['summary'])!=str or len(item['summary'])>MAX_IGNORED_SUMMARY_CHARS or '\x00' in item['summary']:raise RouteProtocolError('card summary shape')
    ignored_summaries+=1
   cards.append(item['id'])
 else:raise RouteProtocolError('mixed card encodings')
 if len(set(cards))!=len(cards) or any(k not in catalog_ids for k in cards):raise RouteProtocolError('duplicate or unknown exact card ID')
 if type(packet['contract'])!=str or not 1<=len(packet['contract'])<=1500:raise RouteProtocolError('contract')
 quote=packet['evidence']
 if type(quote)!=str or not 1<=len(quote)<=5000:raise RouteProtocolError('evidence')
 # Object summaries are never forwarded to a solver or used to select a card.
 packet={'cards':cards,'contract':packet['contract'],'evidence':quote}
 try:json.dumps(packet,ensure_ascii=False,allow_nan=False).encode('utf-8')
 except UnicodeError as error:raise RouteProtocolError('decoded text is not UTF-8') from error
 exact=6<=len(quote)<=600 and quote in spec
 return {'route':packet,'encoding_audit':{'raw_sha256':hashlib.sha256(raw).hexdigest(),'input_encoding':encoding,'normalized_cards':cards,'ignored_summary_count':ignored_summaries,'json_fence_present':fenced,'exact_ids_only':True,'semantic_correctness_verified':False,'new_model_requests':0},'quote_audit':{'exact_public_span':exact,'literal_position':spec.find(quote) if exact else None,'status':'exact_span' if exact else 'requires_review','not_a_semantic_correctness_proof':True},'dispatch_eligible_by_structure':True,'public_spec_overrides_contract':True}

def solve_contract_note(packet):
 return '以下是路由器的公开契约短答，可能不完整或有误，必须以原始公开需求为准。\n'+packet['route']['contract']
