#!/usr/bin/env bash
# Local self-test: run the agent over every task, grade L0-L3, aggregate.
#
#   ./run_selftest.sh                      agent + baseline, 1 sample each
#   ./run_selftest.sh --samples 5          the sampling the real evaluation uses
#   ./run_selftest.sh --no-baseline        skip the baseline (no gain metric)
#   ./run_selftest.sh --baseline          run the baseline even in mock mode
#                                         (mock does not apply to baseline.py --
#                                          only use this if a real endpoint is up)
#   ./run_selftest.sh --reference          grade the reference solutions instead
#                                          of running the agent -- verifies the
#                                          judging chain without a model
#   ./run_selftest.sh --tasks DIR          use a different task set, e.g. the
#                                          output of veval_import.py
#   ./run_selftest.sh --check              environment check only
#
# Reads ./env.sh if present. Copy env.sh.example and point it at your own Vivado
# install. Installing Vivado is up to you; this script only locates it.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

TASKS_DIR="$ROOT/tasks"
AGENT_DIR="$ROOT/example"
OUT_DIR="${OUT_DIR:-$HERE/out}"
SAMPLES=1
DO_BASELINE=1
MODE=agent
CHECK_ONLY=0
TIMEOUT=1800

while [ $# -gt 0 ]; do
    case "$1" in
        --tasks)        TASKS_DIR="$2"; shift 2 ;;
        --agent)        AGENT_DIR="$2"; shift 2 ;;
        --out)          OUT_DIR="$2"; shift 2 ;;
        --samples)      SAMPLES="$2"; shift 2 ;;
        --timeout)      TIMEOUT="$2"; shift 2 ;;
        --no-baseline)  DO_BASELINE=0; shift ;;
        --baseline)     FORCE_BASELINE=1; shift ;;
        --reference)    MODE=reference; DO_BASELINE=0; shift ;;
        --check)        CHECK_ONLY=1; shift ;;
        -h|--help)      sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ -f "$HERE/env.sh" ] && . "$HERE/env.sh"

# 桩模式下不跑基线。mock 只对 agent/llm.py 有效，baseline.py 读的是
# LLM_BASE_URL，照样会去连真的推理服务——两边用不同的模型，算出来的增益
# 没有意义，却看不出异常。宁可不出这个数。
if [ "${LLM_BACKEND:-mock}" = mock ] && [ "$MODE" = agent ] && [ "${FORCE_BASELINE:-0}" -eq 0 ]; then
    DO_BASELINE=0
    NO_BASELINE_REASON="桩模式，增益不计"
fi

# ---------------------------------------------------------------- env check

echo "=================================================================="
echo " 环境检查"
echo "=================================================================="

PY=$(command -v python3 || true)
if [ -z "$PY" ]; then echo "✗ python3 未找到"; exit 1; fi
echo "✓ python3        $PY"

VIVADO_OK=1
for c in xvlog xelab xsim vivado; do
    p=$(command -v $c || true)
    if [ -n "$p" ]; then
        case $c in
            xvlog) note="（L1 分析）" ;;
            xelab) note="（L1 详细描述）" ;;
            xsim)  note="（L2 仿真）" ;;
            *)     note="（L3 综合）" ;;
        esac
        printf "✓ %-8s %s %s\n" "$c" "$p" "$note"
    else
        printf "✗ %-8s 未找到\n" "$c"
        VIVADO_OK=0
    fi
done

if [ "$VIVADO_OK" -eq 0 ]; then
    cat <<'EOF'

  未检测到 Vivado，分级判定无法进行，本脚本不会改用 iverilog 或 Verilator 替代。
  赛事方全流程仅使用 AMD 工具链，本地自测应与之保持一致。

  可以做的：
    1. source 本机的 Vivado 环境，例如
         source /tools/Xilinx/2026.1/Vivado/settings64.sh
    2. 或 cp env.sh.example env.sh 并填入安装路径

  仍然可以做的：用 LLM_BACKEND=mock 跑 example/run.sh，验证输入输出契约。
  Vivado 的安装由队伍自行完成，本仓库不提供安装指导。

EOF
fi

TASK_LIST=$(find "$TASKS_DIR" -maxdepth 2 -name task.json -printf '%h\n' 2>/dev/null | sort)
if [ -z "$TASK_LIST" ]; then echo "✗ 在 $TASKS_DIR 下没有找到题目"; exit 1; fi
echo "✓ 题目           $(echo "$TASK_LIST" | wc -l) 道，来自 $TASKS_DIR"
echo "  LLM 后端       ${LLM_BACKEND:-mock}${LLM_BASE_URL:+  ->  $LLM_BASE_URL}"
echo "  采样次数       $SAMPLES"
echo "  输出目录       $OUT_DIR"
[ -n "${NO_BASELINE_REASON:-}" ] && echo "  基线           跳过（${NO_BASELINE_REASON}）"

[ "$CHECK_ONLY" -eq 1 ] && exit 0
if [ "$VIVADO_OK" -eq 0 ] && [ "$MODE" != "agent" ]; then exit 1; fi

mkdir -p "$OUT_DIR/results"

# --------------------------------------------------------------- one sample

run_one() {   # run_one <mode> <task_dir> <k>
    local mode="$1" task_dir="$2" k="$3"
    local tid; tid=$(basename "$task_dir")
    local dst="$OUT_DIR/$mode/$tid/s$k"
    mkdir -p "$dst"

    if [ "$mode" = "reference" ]; then
        local ref; ref=$($PY -c "import json,sys;print(json.load(open(sys.argv[1]))['reference'])" \
                          "$task_dir/task.json" 2>/dev/null)
        cp "$task_dir/$ref" "$dst/solution.v" 2>/dev/null || : > "$dst/solution.v"
    else
        local script=run.sh
        [ "$mode" = "baseline" ] && script=run_baseline.sh
        "$AGENT_DIR/$script" "$task_dir" "$dst" >"$dst/run.log" 2>&1
    fi

    $PY "$HERE/judge.py" \
        --task "$task_dir" \
        --solution "$dst/solution.v" \
        --outdir "$OUT_DIR/logs" \
        --timeout "$TIMEOUT" \
        --json "$OUT_DIR/results/$mode.$tid.s$k.json"
}

# ------------------------------------------------------------------- drive

for mode in $( [ "$MODE" = "reference" ] && echo reference || { echo agent; [ "$DO_BASELINE" -eq 1 ] && echo baseline; } ); do
    echo
    echo "=================================================================="
    echo " $mode"
    echo "=================================================================="
    for task_dir in $TASK_LIST; do
        k=0
        while [ "$k" -lt "$SAMPLES" ]; do
            printf 's%-2s ' "$k"
            run_one "$mode" "$task_dir" "$k"
            k=$((k + 1))
        done
    done
done

$PY "$HERE/score.py" --results "$OUT_DIR/results" --json "$OUT_DIR/score.json"
