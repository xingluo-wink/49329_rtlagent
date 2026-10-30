"""Reject external capabilities before claiming a compiler operation.

This is a conservative capability filter, not a syntax/functional judgement.
Legal initial values, parameters and asynchronous reset syntax are permitted.
"""
import re
def source_allowed(text):
    if not isinstance(text,str) or len(text.encode())>200000 or '\x00' in text:return False
    text=re.sub(r'/\*.*?\*/|//[^\n]*',' ',text,flags=re.S)
    if '"' in text:return False
    if re.search(r'`|\b(?:final|force|release|import|export|program|interface|bind|tran|nmos|pmos)\b',text):return False
    if any(x not in ('signed','unsigned','clog2','bits','size','high','low','left','right') for x in re.findall(r'\$([A-Za-z_]\w*)',text)):return False
    mods=re.findall(r'\bmodule\s+(\w+)',text)
    return mods==['TopModule'] and len(re.findall(r'\bendmodule\b',text))==1
