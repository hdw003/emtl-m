#!/usr/bin/env bash
# Capture observed-today financials, update bars and replay a paper portfolio.
set -euo pipefail
quant_repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$quant_repo_root"
mkdir -p .quant
exec flock -n .quant/value-daily.lock .venv/bin/emt quant daily \
  --config examples/quant/value_trend.toml \
  --start 2024-01-01 \
  --database .quant/value-paper.sqlite3 \
  --output .quant/value-paper.json "$@"
