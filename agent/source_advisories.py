"""Narrow source observations, not a SystemVerilog parser or acceptance gate.

No task names, expected answers, tests, scores, or public-spec interpretation.
An observed mixed dependency may be intentional; only review can determine it.
"""
import hashlib,re

IGNORED = re.compile(r'//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"')
EXTENSION = re.compile(r'\{\s*\{\s*(?P<count>[1-9][0-9]{0,5})\s*\{\s*'
    r'(?P<sign>[A-Za-z_][A-Za-z_0-9]*)\s*\[\s*(?P<index>[0-9]+)\s*\]\s*\}\s*\}\s*,\s*'
    r'(?P<value>[A-Za-z_][A-Za-z_0-9]*)\s*\}')
NEGATIVE = re.compile(r"\b(?P<width>[0-9]+)\s*'\s*[sS]?\s*[dDhHbBoO]\s*-\s*[0-9a-fA-FxXzZ_]+\b")


def inspect_source(source):
    if type(source) is not bytes:
        raise TypeError('exact source bytes required')
    base = {'schema':'rtl-source-advisories-v1','source_sha256':hashlib.sha256(source).hexdigest(),
            'source_bytes':len(source),'not_a_verdict':True,'allows_repair_request':False,
            'functional_status':'unknown','interface_status':'unknown',
            'coverage':'literal simple-identifier replication and negative based literals only; not a full parser',
            'preprocessor_elaboration_performed':False,'issues':[]}
    if len(source)>200000:
        return {**base,'status':'not_inspected_size_limit'}
    try:text=source.decode('utf-8')
    except UnicodeError:return {**base,'status':'not_inspected_encoding'}
    code=IGNORED.sub(lambda m:''.join(c if c in '\r\n' else ' ' for c in m.group()),text)
    candidates=[]
    for m in EXTENSION.finditer(code):
        if m['sign'] != m['value']:
            candidates.append((m,'possible_sign_extension_mixed_operand',
                {'extension_bit_source':m['sign'],'payload_source':m['value'],
                 'literal_bit_index':int(m['index']),'replications':int(m['count']),
                 'requires_public_contract_review':True}))
    for m in NEGATIVE.finditer(code):
        candidates.append((m,'negative_sign_after_base_marker',{'requires_real_compiler_diagnostic_for_repair':True}))
    candidates.sort(key=lambda x:x[0].start())
    for m,kind,extra in candidates[:64]:
        base['issues'].append({'kind':kind,'line':1+text.count('\n',0,m.start()),
          'column':1+m.start()-(text.rfind('\n',0,m.start())+1),
          'utf8_byte_start':len(text[:m.start()].encode()),'utf8_byte_end':len(text[:m.end()].encode()),
          'excerpt':text[m.start():m.end()],**extra})
    return {**base,'status':'observations_only','omitted_issue_count':max(0,len(candidates)-64)}
