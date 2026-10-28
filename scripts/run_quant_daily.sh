#!/usr/bin/env bash
# Run after 16:00 Asia/Shanghai. Only downloads public data and updates paper state.
set -euo pipefail
quant_repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$quant_repo_root"
mkdir -p .quant
exec flock -n .quant/daily.lock .venv/bin/emt quant daily \
  --config examples/quant/etf_rotation.toml \
  --start 2020-01-01 \
  --database .quant/etf-paper.sqlite3 \
  --output .quant/etf-paper.json "$@"
