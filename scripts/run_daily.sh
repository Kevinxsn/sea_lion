#!/usr/bin/env bash
# Morning decision cycle (design §5.2, §19.2). Run shortly after the open on every weekday; the
# pipeline itself decides whether today is a session, reconciles the broker FIRST, and then either
# makes the day's decision or records a reconcile-only outcome. One summary line is always printed.
# Exit codes: 0 ok, 1 error, 2 attention (safe mode, abort, AI fallback, ambiguous order, deadline).
# With MAILTO set in the crontab, any non-empty output is mailed; keep stdout to the summary line.
#   40 6 * * 1-5  /projects/ps-renlab2/sux002/sea_lion/scripts/run_daily.sh >> /data/tmp/sux002-sea-lion/logs/cron.log 2>&1
set -uo pipefail
source "$(dirname "$0")/env.sh"
cd "$SEA_LION_ROOT"
MODE="${SEA_LION_MODE:-shadow}"
OUT=$(python -m sea_lion.cli --mode "$MODE" run "$@" 2>>"$SEA_LION_RUNTIME_DIR/logs/run_daily.stderr")
rc=$?
echo "$OUT" | grep -v "^SEA_LION_SUMMARY" >> "$SEA_LION_RUNTIME_DIR/logs/run_daily.json.log"
LINE=$(echo "$OUT" | grep "^SEA_LION_SUMMARY" | tail -1)
echo "[$(date -Is)] rc=$rc mode=$MODE ${LINE:-no summary line}"
if [[ $rc -ne 0 ]]; then
  echo "ATTENTION: sea-lion morning run rc=$rc mode=$MODE :: ${LINE}" >&2
fi
exit $rc
