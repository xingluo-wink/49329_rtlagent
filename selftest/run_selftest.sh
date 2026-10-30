#!/usr/bin/env bash
# One public example through the product entry and the unchanged official judge.
set -eo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$#" -gt 0 ]; then
    if [ "$#" -eq 1 ] && { [ "$1" = "--help" ] || [ "$1" = "-h" ]; }; then
        printf '%s\n' 'Usage: bash selftest/run_selftest.sh' 'Runs one public popcount example, then the official L0-L3 judge.' 'For your own prompt.txt: python3 tools/solve.py INPUT_DIR'
        exit 0
    fi
    printf '%s\n' 'This product entry runs one example only; no batch or repeated-sampling options.' >&2
    exit 64
fi
export PYTHONDONTWRITEBYTECODE=1
exec /opt/python/bin/python3 -B "$ROOT/tools/demo.py"
