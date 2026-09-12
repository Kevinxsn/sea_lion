#!/usr/bin/env bash
# After-close research job (design §5.2): freezes the session's bars, collects news/filings/macro,
# computes features, and runs arm-B and arm-C research so the morning cycle only processes deltas.
#   30 13 * * 1-5  /projects/ps-renlab2/sux002/sea_lion/scripts/run_research.sh >> /data/tmp/sux002-sea-lion/logs/cron.log 2>&1
set -uo pipefail
source "$(dirname "$0")/env.sh"
cd "$SEA_LION_ROOT"
MODE="${SEA_LION_MODE:-shadow}"
OUT=$(python -m sea_lion.cli --mode "$MODE" research "$@" 2>>"$SEA_LION_RUNTIME_DIR/logs/run_research.stderr")
rc=$?
echo "$OUT" | grep -v "^SEA_LION_SUMMARY" >> "$SEA_LION_RUNTIME_DIR/logs/run_research.json.log"
LINE=$(echo "$OUT" | grep "^SEA_LION_SUMMARY" | tail -1)
echo "[$(date -Is)] rc=$rc mode=$MODE research ${LINE:-no summary line}"
[[ $rc -ne 0 ]] && echo "ATTENTION: sea-lion research rc=$rc mode=$MODE :: ${LINE}" >&2
exit $rc
