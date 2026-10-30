"""Convert VerilogEval v2 tasks into this repository's task format.

    python3 veval_import.py --src <verilog-eval>/dataset_spec-to-rtl --out ../tasks_veval
    python3 veval_import.py --src <...> --out ../tasks_veval --limit 20

VerilogEval ships each task as three files:

    Prob004_vector2_prompt.txt      natural-language spec
    Prob004_vector2_ref.sv          module RefModule (...)
    Prob004_vector2_test.sv         testbench instantiating RefModule + TopModule

which map onto this repository's layout directly.

## Why the testbenches need patching

VerilogEval's harness requires iverilog -- `configure.ac` checks for it -- and
every one of the 156 testbenches contains:

    initial begin
        $dumpfile("wave.vcd");
        $dumpvars(1, stim1.clk, tb_mismatch, ...);
    end

`tb_mismatch` is declared five lines further down. iverilog accepts the forward
reference; Vivado's `xvlog` does not:

    ERROR: [VRFC 10-3380] identifier 'tb_mismatch' is used before its
                          declaration [..._test.sv:66]
    ERROR: [VRFC 10-8530] module 'tb' is ignored due to previous errors

The block only writes a waveform dump, so removing it is behaviour-preserving.
That single edit is the whole port: measured across all 156 spec-to-rtl tasks,
with RefModule renamed to TopModule as a known-good solution, 152 reach L3.
The other 4 are defects in the dataset itself, not in the conversion, and a
further 7 have prompts that disagree with their reference. Both lists are in
README.md; leave all 11 out when you compute a pass rate.

## What this does not give you

The converted tasks use VerilogEval's own testbenches. The evaluation set is not
public and its testbenches are not these. Treat a score here as a development
signal, not as a prediction.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# The initial block that iverilog tolerates and xvlog rejects.
DUMP_BLOCK = re.compile(
    r"\n[ \t]*initial[ \t]+begin[ \t]*\n"
    r"[ \t]*\$dumpfile\([^\n]*\n"
    r"[ \t]*\$dumpvars\([^\n]*\n"
    r"[ \t]*end[ \t]*\n"
)

# `module RefModule ( ... );` -- header only, body excluded.
MODULE_HEADER = re.compile(
    r"module\s+RefModule\s*(#\s*\([^)]*\)\s*)?\((.*?)\)\s*;", re.DOTALL
)


def patch_testbench(text: str) -> tuple[str, bool]:
    patched, n = DUMP_BLOCK.subn("\n", text)
    return patched, n > 0


def derive_interface(ref_text: str) -> str | None:
    """Port list only. The reference body must not leak into the task."""
    m = MODULE_HEADER.search(ref_text)
    if not m:
        return None
    # Keep the trailing ';' so the file reads as a declaration, matching the
    # hand-written tasks under tasks/.
    return m.group(0).replace("RefModule", "TopModule").rstrip() + "\n"


def convert_one(src: str, name: str, out_root: str) -> dict:
    prompt_p = os.path.join(src, f"{name}_prompt.txt")
    ref_p = os.path.join(src, f"{name}_ref.sv")
    test_p = os.path.join(src, f"{name}_test.sv")

    for p in (prompt_p, ref_p, test_p):
        if not os.path.isfile(p):
            return {"task": name, "ok": False, "reason": f"missing {os.path.basename(p)}"}

    with open(ref_p, encoding="utf-8", errors="replace") as fh:
        ref_text = fh.read()
    with open(test_p, encoding="utf-8", errors="replace") as fh:
        test_text = fh.read()
    with open(prompt_p, encoding="utf-8", errors="replace") as fh:
        prompt_text = fh.read()

    # 仍然解析一次 RefModule 的模块头，但**不写成 interface.txt** —— 只用来判断
    # 这道题是否规整（解析不出说明参考实现有问题，该题不该进题集）。
    if derive_interface(ref_text) is None:
        return {"task": name, "ok": False, "reason": "could not parse RefModule header"}

    patched, did_patch = patch_testbench(test_text)

    dst = os.path.join(out_root, name)
    os.makedirs(os.path.join(dst, "reference"), exist_ok=True)

    _write(os.path.join(dst, "prompt.txt"), prompt_text.strip() + "\n")
    # 不生成 interface.txt：评测题集不提供它，接口写在题面里。转换出来的题集
    # 必须和评测当天拿到的形状一致，否则本地自测过了、评测当天却少一个文件。
    _write(os.path.join(dst, "ref.sv"), ref_text)
    _write(os.path.join(dst, "tb.sv"), patched)
    _write(os.path.join(dst, "top.txt"), "TopModule\n")
    # RefModule renamed is a known-good solution: use it to check the chain,
    # never to score an agent.
    _write(os.path.join(dst, "reference", "solution.sv"),
           re.sub(r"\bRefModule\b", "TopModule", ref_text))
    _write(os.path.join(dst, "task.json"), json.dumps({
        "task_id": name,
        "top": "TopModule",
        "part": "xczu3eg-sbva484-1-e",
        "period_ns": 5,
        "reference_module": "ref.sv",
        "testbench": "tb.sv",
        "tb_top": "tb",
        "extra_files": [],
        "reference": "reference/solution.sv",
        "source": "VerilogEval v2 dataset_spec-to-rtl",
    }, ensure_ascii=False, indent=2) + "\n")

    return {"task": name, "ok": True, "patched": did_patch}


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="convert VerilogEval v2 spec-to-rtl tasks into this repo's format")
    ap.add_argument("--src", required=True,
                    help="path to verilog-eval/dataset_spec-to-rtl")
    ap.add_argument("--out", required=True, help="output task directory")
    ap.add_argument("--limit", type=int, default=0, help="convert only the first N")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.src):
        print(f"not a directory: {args.src}", file=sys.stderr)
        return 1

    names = sorted(
        f[: -len("_prompt.txt")]
        for f in os.listdir(args.src) if f.endswith("_prompt.txt")
    )
    if args.limit:
        names = names[: args.limit]
    if not names:
        print(f"no *_prompt.txt found in {args.src}", file=sys.stderr)
        return 1

    os.makedirs(args.out, exist_ok=True)
    ok = failed = unpatched = 0
    for name in names:
        res = convert_one(args.src, name, args.out)
        if res["ok"]:
            ok += 1
            if not res.get("patched"):
                unpatched += 1
                print(f"  note: {name} had no $dumpvars block to remove")
        else:
            failed += 1
            print(f"  skip: {name}: {res['reason']}")

    print(f"\n转换完成: {ok} 道 -> {args.out}")
    if unpatched:
        print(f"其中 {unpatched} 道没有需要移除的 $dumpvars 块（正常，但值得留意）")
    if failed:
        print(f"{failed} 道跳过，见上方原因")
    print("\n接下来：")
    print(f"  ./run_selftest.sh --tasks {args.out} --reference    # 先验判定链路")
    print(f"  ./run_selftest.sh --tasks {args.out} --samples 5    # 再跑你的方案")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
