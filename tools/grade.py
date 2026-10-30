"""Run the unchanged official judge on a task with public test fixtures."""
import argparse,json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'agent'))
from native_runtime import private_common,compact_rows

def run(task,solution,output):
    task=Path(task).resolve();solution=Path(solution).resolve();output=Path(output).absolute()
    if not (task/'task.json').is_file():
        raise ValueError('这份题目只有题面，没有 task.json 和测试台，不能自动给出功能分数。请使用赛方提供的自测题。')
    if not solution.is_file():raise ValueError('找不到 solution.v，请先解题。')
    output.mkdir(parents=True,exist_ok=False)
    registry=output/'process-evidence';registry.mkdir()
    common=private_common(registry);common.become_subreaper();common.own('grade-supervisor',os.getpid())
    env=common.env_for();env['SELFTEST_KEEP_WORK']='0'
    env['SELFTEST_TMP']=os.environ.get('SELFTEST_TMP','/var/tmp/rtlagent-judge')
    Path(env['SELFTEST_TMP']).mkdir(parents=True,exist_ok=True)
    proc=None
    print('正在使用赛事官方判定器：编译 → 仿真 → 综合……',flush=True)
    try:
        proc,row=common.spawn('official-judge',[sys.executable,'-B',str(ROOT/'selftest/judge.py'),
            '--task',str(task),'--solution',str(solution),'--outdir',str(output/'logs'),
            '--json',str(output/'result.json'),'--timeout','600'],env,cwd=env['SELFTEST_TMP'])
        end=time.monotonic()+615
        while proc.poll() is None and time.monotonic()<end:
            common.capture_owned();time.sleep(.3)
        if proc.poll() is None:raise TimeoutError('官方判定器未在时限内结束，保留日志。')
        proc.wait()
    finally:
        rows=common.capture_owned();live=[q for q in rows if q['pid']!=os.getpid() and common.same(q)]
        if live:common.stop_rows(live,.2)
        if proc is not None:
            try:proc.wait(timeout=5)
            except Exception:pass
        # Reap adopted children as well; a zombie observation is not a wait.
        while True:
            try:
                pid,_=os.waitpid(-1,os.WNOHANG)
                if pid==0:break
            except ChildProcessError:break
        rows=common.capture_owned();live=[q for q in rows if q['pid']!=os.getpid() and common.same(q)]
        common.save(output/'process-closure.json',{'remaining_live':live,'identities':compact_rows(common,rows)})
        if live:raise RuntimeError('判分子进程尚未全部关闭，结果暂不可采用。')
    if not (output/'result.json').exists():raise RuntimeError('未生成官方判分结果，查看 '+str(output/'logs'))
    value=json.loads((output/'result.json').read_bytes())
    if value.get('tool_error'):print('工具/授权异常：'+str(value['tool_error'])+'。这不代表模型答错。')
    else:print('本地官方判定：L'+str(value['level'])+'；编译='+str(value['stages']['compile'])+'，仿真='+str(value['stages']['simulate'])+'，综合='+str(value['stages']['synth']))
    print('判分结果：'+str(output/'result.json'))
    return value

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('task');parser.add_argument('solution');parser.add_argument('--output')
    args=parser.parse_args();output=args.output or str(Path(args.solution).resolve().parent/('grade-'+str(time.time_ns())))
    try:run(args.task,args.solution,output)
    except Exception as exc:print(str(exc),file=sys.stderr);raise SystemExit(1)
