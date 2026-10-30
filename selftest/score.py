"""Aggregate per-sample judgements into a score sheet.

    题集得分 = Σ(题目系数) / 题数
    能力得分 = 30 × 题集得分
    增益 g   = (方案题集得分 − 基线题集得分) / (1 − 基线题集得分)
    增益得分 = 40 × min(g / 0.6, 1)，g ≤ 0 时为 0

pass@1 is the mean coefficient over all samples of a task, averaged over tasks --
the unbiased estimator, extended from pass/fail to the graded coefficients.
pass@5 takes the best sample per task. **pass@1 is what scores**; pass@5 is a
stability diagnostic and is reported separately, exactly as in the rules.

The gain full mark (0.6) is the announced value. The wall-clock baseline for
the cost item is announced separately and is not computed here.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict

CAPABILITY_WEIGHT = 30.0
GAIN_WEIGHT = 40.0

# 归一化增益的满分线，与评分细则 3.2 一致。
GAIN_FULL_MARK = float(os.environ.get("GAIN_FULL_MARK", "0.6"))


def collect(results_dir: str) -> dict[str, dict[str, list[dict]]]:
    """mode -> task_id -> [per-sample result]"""
    out: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for path in sorted(glob.glob(os.path.join(results_dir, "*.json"))):
        name = os.path.basename(path)
        if name == "score.json":
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                res = json.load(fh)
        except (OSError, ValueError):
            continue
        # <mode>.<task_id>.s<k>.json
        mode = name.split(".", 1)[0]
        out[mode][res.get("task_id", "unknown")].append(res)
    return out


def summarize(by_task: dict[str, list[dict]]) -> dict:
    """汇总各题各次采样，得出题集得分。

    带 tool_error 的样本**不计入**平均分。这类样本是授权签不出、题目自带的测试台
    坏了之类的环境问题，判定器能把它们和"代码写错了"分开（这正是换用赛事方判定器
    的意义）。分开之后若仍按系数 0 计入平均，等于把环境抖动算成参赛者失分，那和
    不分开没有区别。它们改为在 excluded 里单列。
    """
    tasks = sorted(by_task)
    pass1_terms, pass5_terms = [], []
    levels = defaultdict(int)
    excluded = []
    scored_tasks = 0
    elapsed = 0.0

    per_task = []
    for t in tasks:
        samples = by_task[t]
        for s in samples:
            elapsed += s.get("elapsed_s", 0.0)

        good = [s for s in samples if not s.get("tool_error")]
        bad = [s for s in samples if s.get("tool_error")]
        for s in bad:
            excluded.append({
                "task_id": t,
                "reason": str(s.get("tool_error"))[:200],
            })

        for s in good:
            levels[s.get("level", 0)] += 1

        entry = {
            "task_id": t,
            "samples": len(samples),
            "scored_samples": len(good),
            "excluded_samples": len(bad),
            "levels": [s.get("level", 0) for s in good],
        }

        if not good:
            # 整题均为环境失败：不计 0 分，也不静默丢弃，否则会缩小分母。
            entry["pass@1"] = None
            entry["pass@5"] = None
            entry["note"] = "全部采样均为环境失败，该题不计入题集得分"
        else:
            coeffs = [s.get("coefficient", 0.0) for s in good]
            mean_c = sum(coeffs) / len(coeffs)
            best_c = max(coeffs)
            pass1_terms.append(mean_c)
            pass5_terms.append(best_c)
            scored_tasks += 1
            entry["pass@1"] = round(mean_c, 4)
            entry["pass@5"] = round(best_c, 4)

        per_task.append(entry)

    n = scored_tasks or 1
    return {
        "tasks": len(tasks),
        "scored_tasks": scored_tasks,
        "samples_per_task": len(by_task[tasks[0]]) if tasks else 0,
        "set_score": round(sum(pass1_terms) / n, 4),   # 题集得分，即 pass@1
        "pass@1": round(sum(pass1_terms) / n, 4),
        "pass@5": round(sum(pass5_terms) / n, 4),
        "level_counts": {f"L{k}": levels[k] for k in sorted(levels)},
        "tool_errors": len(excluded),
        "excluded": excluded,
        "judge_elapsed_s": round(elapsed, 1),
        "per_task": per_task,
    }


def gain_score(agent_set: float, baseline_set: float) -> tuple[float, float]:
    """归一化增益 g 与增益得分。基线已是满分时没有提升空间，本项计满分。"""
    if baseline_set >= 1.0:
        return 1.0, GAIN_WEIGHT
    g = (agent_set - baseline_set) / (1.0 - baseline_set)
    if g <= 0:
        return g, 0.0
    return g, min(GAIN_WEIGHT, GAIN_WEIGHT * g / GAIN_FULL_MARK)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="aggregate judgements into a score sheet")
    ap.add_argument("--results", required=True, help="directory of per-sample .json")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(argv)

    data = collect(args.results)
    if not data:
        print(f"no results found in {args.results}")
        return 1

    report = {"modes": {}, "gain_full_mark": GAIN_FULL_MARK}
    for mode in sorted(data):
        report["modes"][mode] = summarize(data[mode])

    agent = report["modes"].get("agent")
    base = report["modes"].get("baseline")

    print()
    print("=" * 68)
    for mode, s in report["modes"].items():
        print(f"\n[{mode}]  {s['tasks']} 题 × {s['samples_per_task']} 次采样")
        print(f"{'task':<20} {'levels':<16} {'pass@1':>8} {'pass@5':>8}")
        print("-" * 56)
        for t in s["per_task"]:
            lv = ",".join(f"L{x}" for x in t["levels"]) or "—"
            if t["pass@1"] is None:
                print(f"{t['task_id']:<20} {lv:<16} {'（整题排除）':>8}")
            else:
                print(f"{t['task_id']:<20} {lv:<16} "
                      f"{t['pass@1']:>8.3f} {t['pass@5']:>8.3f}")
        print("-" * 56)
        print(f"{'题集得分 (pass@1)':<20} {'':<16} {s['set_score']:>8.3f}")
        print(f"{'pass@5':<20} {'':<16} {'':>8} {s['pass@5']:>8.3f}")
        print(f"分级分布: {s['level_counts']}")
        if s["tool_errors"]:
            # 说清楚是"没算进去"而不是"算了 0 分"，否则容易被当成失分。
            print(f"⚠ {s['tool_errors']} 个样本属环境失败，已排除出算分"
                  f"（题集得分按 {s['scored_tasks']}/{s['tasks']} 题计）：")
            for e in s["excluded"][:5]:
                print(f"    {e['task_id']}: {e['reason']}")
            if len(s["excluded"]) > 5:
                print(f"    …… 另有 {len(s['excluded']) - 5} 个")

    print()
    print("=" * 68)
    if agent:
        cap = CAPABILITY_WEIGHT * agent["set_score"]
        report["capability_score"] = round(cap, 2)
        print(f"能力  = 30 × {agent['set_score']:.3f} = {cap:.1f} / 30")

        if base:
            a, b = agent["set_score"], base["set_score"]
            g, gscore = gain_score(a, b)
            report["gain_g"] = round(g, 4)
            report["gain_score"] = round(gscore, 2)
            report["gain_ratio"] = round(a / b, 4) if b > 0 else None
            print(f"增益  g = ({a:.3f} − {b:.3f}) / (1 − {b:.3f}) = {g:.3f}")
            print(f"       40 × min({g:.3f} / {GAIN_FULL_MARK}, 1) = {gscore:.1f} / 40")
            if b > 0:
                print(f"       诊断：倍数 {a:.3f} / {b:.3f} = {a / b:.2f}，不计分")
        else:
            print("增益  = 未跑基线，本项无法计算。桩模式下基线被跳过，"
                  "因为 mock 对 baseline.py 无效；接上真模型后会自动计算")

        spread = agent["pass@5"] - agent["pass@1"]
        print(f"诊断  pass@5 − pass@1 = {spread:.3f}"
              f"{'   （差值偏大，方案方差高）' if spread > 0.15 else ''}")

    print("\n代价与工程质量两项本地无法自评：前者需独占环境计时，后者含人工评定。")
    print("=" * 68)

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"\nscore.json -> {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
