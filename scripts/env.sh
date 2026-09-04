#!/usr/bin/env bash
# Source this: `source scripts/env.sh`. Puts the NVMe venv on PATH and loads .env.
export SEA_LION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SEA_LION_RUNTIME_DIR=/data/tmp/sux002-sea-lion
export PATH="$SEA_LION_RUNTIME_DIR/venv/bin:$PATH"
export PYTHONPATH="$SEA_LION_ROOT${PYTHONPATH:+:$PYTHONPATH}"
[[ -f "$SEA_LION_ROOT/.env" ]] && set -a && source "$SEA_LION_ROOT/.env" && set +a
alias sea-lion='python -m sea_lion.cli'
