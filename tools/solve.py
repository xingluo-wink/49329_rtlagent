"""Human-friendly file entry; the official run.sh uses the same Python code."""
import argparse,sys,tempfile
sys.dont_write_bytecode=True
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'agent'))
from api_client import run
if __name__=='__main__':
    parser=argparse.ArgumentParser(description='运行一个题目目录；只有 prompt.txt 也可以。')
    parser.add_argument('input');parser.add_argument('--output');parser.add_argument('--mode',default='agent',choices=['agent','baseline'])
    args=parser.parse_args()
    base=Path('/workspace/rtlagent/runs');base.mkdir(parents=True,exist_ok=True)
    output=args.output or tempfile.mkdtemp(prefix='answer-',dir=base)
    print('输出目录：'+output,flush=True)
    raise SystemExit(run([args.input,output,'--mode',args.mode]))
