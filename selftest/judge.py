"""对一份 RTL 解给出分级结果。

    L1 可编译  →  L2 仿真通过  →  L3 可综合

严格递进：低一级没过，高一级就不算。得分取达到的最高级别对应的系数。

    python3 judge.py --task ../tasks/ex01_popcount8 --solution /tmp/out/solution.v

本文件是 judge/veval-judge 的适配层，后者与赛事方正式评测同源，见 judge/PROVENANCE.md。
本层做三件事：

  1. 把本仓库的任务布局（task.json 指明 ref.sv / tb.sv）交给 veval-judge 的
     --ref / --test 接口；
  2. 把 veval-judge 的输出字段翻译成 score.py 期望的字段；
  3. 把环境类判定（授权、器件档位、题目损坏）标成 tool_error，由 score.py 排除
     出算分。

判分逻辑不在此处重新实现，否则自测与正式评测会产生分歧。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VEVAL_JUDGE = os.path.join(HERE, "judge", "veval-judge")

COEFFICIENT = {0: 0.0, 1: 0.2, 2: 0.7, 3: 1.0}

SCRATCH = os.environ.get("SELFTEST_TMP", "/tmp")

# 环境或题目异常的判定结果，由 score.py 排除出平均分。
TOOL_VERDICTS = {
    "LICENSE_ERROR":     "xsim 未能签出仿真授权（2026.1 的 xsim/综合都需要席位）",
    "PART_NOT_LICENSED": "授权档位不覆盖该器件，L3 无法判定",
    "FIXTURE_ERROR":     "题目自带的参考实现或测试台有问题，与待测代码无关",
    "JUDGE_ERROR":       "判定器本身异常退出",
}


def load_task(task_dir: str) -> dict:
    with open(os.path.join(task_dir, "task.json"), encoding="utf-8") as fh:
        return json.load(fh)


def judge(task_dir: str, solution_path: str, outdir: str | None = None,
          timeout_s: float = 1800.0) -> dict:
    task = load_task(task_dir)
    result = {
        "task_id": task.get("task_id", os.path.basename(task_dir)),
        "top": task.get("top", "TopModule"),
        "level": 0,
        "coefficient": 0.0,
        "stages": {"compile": False, "simulate": False, "synth": False},
        "tool_error": None,
        "elapsed_s": 0.0,
    }

    t0 = time.time()

    try:
        with open(solution_path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError as exc:
        result["tool_error"] = f"读不到解文件：{exc}"
        result["elapsed_s"] = round(time.time() - t0, 1)
        return result

    if not source.strip():
        # 空解表示未能求解，属于 L0。
        result["elapsed_s"] = round(time.time() - t0, 1)
        return result

    if not os.path.isfile(VEVAL_JUDGE):
        result["tool_error"] = f"找不到判定器 {VEVAL_JUDGE}"
        result["elapsed_s"] = round(time.time() - t0, 1)
        return result

    work = os.path.join(SCRATCH, f"judge_{result['task_id']}_{os.getpid()}")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    dut = os.path.join(work, "dut.sv")
    with open(dut, "w", encoding="utf-8") as fh:
        fh.write(source)

    part = task.get("part", "xczu3eg-sbva484-1-e")
    period = float(task.get("period_ns", 5))
    js = os.path.join(work, "verdict.json")

    cmd = [
        sys.executable, VEVAL_JUDGE,
        "--dut", dut,
        "--ref", os.path.join(os.path.abspath(task_dir), task["reference_module"]),
        "--test", os.path.join(os.path.abspath(task_dir), task["testbench"]),
        "--workdir", os.path.join(work, "w"),
        "--level", "3",
        "--part", part,
        "--clock-ns", str(period),
        "--json", js,
        "--clean", "--quiet",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, errors="replace")
        raw = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        result["tool_error"] = f"判定超时（>{timeout_s:.0f}s）"
        result["elapsed_s"] = round(time.time() - t0, 1)
        _cleanup(work, result)
        return result

    _dump(outdir, f"{result['task_id']}.judge.log", raw)

    try:
        with open(js, encoding="utf-8") as fh:
            verdict = json.load(fh)
    except (OSError, json.JSONDecodeError):
        result["tool_error"] = TOOL_VERDICTS["JUDGE_ERROR"]
        result["elapsed_s"] = round(time.time() - t0, 1)
        _cleanup(work, result)
        return result

    _translate(verdict, result)
    result["elapsed_s"] = round(time.time() - t0, 1)
    _cleanup(work, result)
    return result


def _translate(verdict: dict, result: dict) -> None:
    """把 veval-judge 的输出翻译成 score.py 认的字段。"""
    v = verdict.get("verdict")

    if v in TOOL_VERDICTS:
        # 环境/题目问题：级别留 0，但打上 tool_error，由 score.py 排除出平均。
        result["tool_error"] = verdict.get("note") or TOOL_VERDICTS[v]
        return

    level = verdict.get("level")
    if level is None:
        result["tool_error"] = verdict.get("note") or TOOL_VERDICTS["JUDGE_ERROR"]
        return

    result["level"] = level
    result["coefficient"] = COEFFICIENT.get(level, 0.0)

    # 分级严格递进，直接由级别反推三个布尔，不必二次解析日志。
    for lvl, key in ((1, "compile"), (2, "simulate"), (3, "synth")):
        result["stages"][key] = level >= lvl

    # 失配数与采样数对调试很有用：采样数为 0 说明激励根本没跑起来，
    # 那和"跑了但结果不对"是两回事。
    for key in ("mismatches", "samples"):
        if verdict.get(key) is not None:
            result[key] = verdict[key]
    if verdict.get("sim_timeout"):
        result["sim_timeout"] = True
    if verdict.get("note"):
        result["note"] = verdict["note"]


def _cleanup(work: str, result: dict) -> None:
    if os.environ.get("SELFTEST_KEEP_WORK") == "1":
        result["work_dir"] = work
    else:
        shutil.rmtree(work, ignore_errors=True)


def _dump(outdir: str | None, name: str, text: str) -> None:
    if not outdir:
        return
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="判定一份 RTL 解，L0–L3")
    ap.add_argument("--task", required=True, help="含 task.json 的题目目录")
    ap.add_argument("--solution", required=True, help="solution.v 路径")
    ap.add_argument("--outdir", default=None, help="工具日志写到哪")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--timeout", type=float, default=1800.0)
    args = ap.parse_args(argv)

    res = judge(args.task, args.solution, args.outdir, args.timeout)

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)

    label = f"L{res['level']}"
    stages = "".join(
        c if res["stages"][k] else "-"
        for c, k in (("C", "compile"), ("M", "simulate"), ("S", "synth"))
    )
    extra = ""
    if res.get("mismatches") is not None:
        extra = f'  mismatches={res["mismatches"]} samples={res.get("samples")}'
    note = f"  [{res['tool_error']}]" if res["tool_error"] else ""
    print(f"{res['task_id']:<20} {label}  {stages}  coeff={res['coefficient']:.1f}  "
          f"{res['elapsed_s']:.0f}s{extra}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
