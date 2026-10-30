"""Read-only source, model, prompt and baseline consistency check. No task runs."""
import ast
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'agent'))
from release_integrity import verify_release
from generation_policy import GenerationPolicy
from prompt_builder import PromptBuilder
from compiler_adapter import pinned_sources


def main():
    result=verify_release()
    for p in ROOT.rglob('*.py'):
        ast.parse(p.read_bytes(),filename=p.relative_to(ROOT).as_posix())
    policy=GenerationPolicy.load()
    PromptBuilder()
    pinned_sources()
    print(json.dumps({**result,'source_syntax':'ok','knowledge':'locked',
        'official_baseline':'unchanged','generation':policy.fields(),
        'model_requests':0,'EDA_processes':0,'service_started':False},ensure_ascii=False))


if __name__=='__main__':
    main()
