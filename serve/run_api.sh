#!/usr/bin/env bash
set -eo pipefail
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
exec /opt/python/bin/python3 -B "$ROOT/agent/public_api.py" "${1:-start}"
