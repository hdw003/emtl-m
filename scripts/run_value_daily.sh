#!/usr/bin/env bash
# Capture observed-today financials, update bars and replay a paper portfolio.
set -euo pipefail
quant_repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$quant_repo_root"
mkdir -p .quant
exec flock -n .quant/full-market-daily.lock .venv/bin/emt quant daily \
  --config examples/quant/full_market.toml \
  --start 2024-01-01 \
  --database .quant/full-market-rotation-paper.sqlite3 \
  --output .quant/full-market-rotation-paper.json "$@"
