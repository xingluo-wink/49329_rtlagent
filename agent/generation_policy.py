"""One manifest snapshot for agent requests and the immutable official baseline.

Integrity checks establish byte consistency, not authentication of a native host.
There is deliberately no stage-specific thinking override or runtime startup.
"""
from dataclasses import dataclass
from pathlib import Path
from adapter_contracts import require,MODEL,RUNTIME,TEMPLATE,WEIGHT
from prompt_builder import canonical,digest
from rtl_delivery import strict_json

ROOT=Path(__file__).resolve().parent.parent
BASELINE_SHA='17aaa609d4dbd36a94d1390e39f769394448f8588f3ccdd2bd37db9e34ed05e2'
RUN_BASELINE_SHA='5c46c40d32c0cf4c4e1dc52ae12e60f8396a0deccafaa4e6f69d7316a3482e75'
GENERATION=canonical({'chat_template_kwargs':{'enable_thinking':True}})

def read_original(path,limit=1048576):
    p=Path(path)
    require(p.is_file() and not p.is_symlink(),'regular configuration source required')
    with p.open('rb') as f:raw=f.read(limit+1)
    require(len(raw)<=limit,'configuration limit exceeded; no truncation')
    return raw

@dataclass(frozen=True)
class GenerationPolicy:
    manifest_sha256:str
    generation_bytes:bytes
    baseline_sha256:str=BASELINE_SHA
    run_baseline_sha256:str=RUN_BASELINE_SHA
    base_url:str='http://127.0.0.1:8000/v1'
    model_alias:str=MODEL

    def fields(self):
        return strict_json(self.generation_bytes)

    def audit(self):
        return {'manifest_sha256':self.manifest_sha256,'generation_sha256':digest(self.generation_bytes),
            'generation':self.fields(),'same_for_route_initial_repair_baseline':True,
            'baseline_script_sha256':self.baseline_sha256,'base_url':self.base_url,'model_alias':self.model_alias,
            'native_uniform_settings_verified':False}

    @classmethod
    def load(cls,root=ROOT):
        root=Path(root)
        raw=read_original(root/'manifest.json');m=strict_json(raw)
        require(type(m) is dict and m.get('schema_version')==1 and m.get('track')=='rtl','manifest schema/track')
        gen=m.get('generation')
        require(type(gen) is dict and set(gen)=={'chat_template_kwargs'}
                and type(gen['chat_template_kwargs']) is dict
                and set(gen['chat_template_kwargs'])=={'enable_thinking'}
                and gen['chat_template_kwargs']['enable_thinking'] is True,
                'this candidate requires the same locked thinking settings in every mode')
        require(canonical(gen)==GENERATION,'unknown generation override')
        require(m['model']['name']=='Qwen/Qwen3.6-35B-A3B'
                and m['model']['weight_sha256']==WEIGHT and m['model']['weight_bytes']==22134528992
                and m['model']['quantization']=='UD-Q4_K_M'
                and m['model']['revision']=='a483e9e6cbd595906af30beda3187c2663a1118c'
                and m['model']['finetuned'] is False and m['auxiliary_models']==[], 'model declaration drift')
        require(m['context']=={'max_model_len':32768,'max_output_tokens':16384,'rope_scaling':None},'context drift')
        serving=m['serving']
        require(serving['backend']=='llama.cpp' and serving['alias']==MODEL
                and serving['runtime_sha256']==RUNTIME and serving['template_sha256']==TEMPLATE
                and serving['backend_sha256']=='290708d22f8b495d36b8aedd0afb0cdc081c415548f855e6a950a75dd742ad97'
                and serving['base_url']=='http://127.0.0.1:8000/v1'
                and serving['parallel_slots']==1 and serving['port']==8000,'inference service drift')
        require(m['agent']['stage_thinking_overrides'] is False
                and m['agent']['max_rounds']==3 and m['agent']['deadline_s']==900
                and m['agent']['route_output_cap']==4096 and m['agent']['other_output_cap']==16384,'agent budget drift')
        runtime=strict_json(read_original(root/'model'/'model-runtime-lock.json'))
        require(runtime['required_runtime_sha256']==RUNTIME
                and canonical(runtime['required_runtime']['generation'])==GENERATION,'runtime generation differs from manifest')
        require(digest(read_original(root/'baseline.py'))==BASELINE_SHA
                and digest(read_original(root/'run_baseline.sh'))==RUN_BASELINE_SHA,'official baseline bytes changed')
        return cls(digest(raw),GENERATION)

    def require_current(self,root=ROOT):
        current=self.load(root)
        require(current==self,'manifest or baseline changed after request admission')

