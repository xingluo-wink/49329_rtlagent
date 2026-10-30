"""One complete public example, using the real file entry and official judge."""
import subprocess,sys,tempfile
sys.dont_write_bytecode=True
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'agent'))
from service_start import main as start
from api_client import run as solve
from grade import run as grade

def main():
    start()
    base=Path('/workspace/rtlagent/runs');base.mkdir(parents=True,exist_ok=True)
    output=Path(tempfile.mkdtemp(prefix='popcount-',dir=base))
    task=ROOT/'examples/popcount8'
    print('示例：8 位输入中有几个 1。题面：'+str(task/'prompt.txt'),flush=True)
    print('本次输出：'+str(output),flush=True)
    code=solve([str(task),str(output)])
    if code:return code
    grade(task,output/'solution.v',output/'grading')
    print('本示例已结束。solution.v 是代码，grading/result.json 是本地自测结果；正式分数由赛事方测评。')
    return 0

if __name__=='__main__':raise SystemExit(main())
