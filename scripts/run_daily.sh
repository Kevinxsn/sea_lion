#!/usr/bin/env bash
# Daily cron entry. One decision cycle per session; never runs live unless the env gates
# in .env are set.
#
# Timing: the decision uses the previous session's completed bars (as_of = last close), and
# orders are marketable limit orders that need a fresh reference price, so run it shortly
# AFTER THE OPEN of the next session (design §8: "during regular market hours"). The
# machine is in Pacific time; 9:40 ET = 6:40 PT on weekdays:
#   40 6 * * 1-5  /projects/ps-renlab2/sux002/sea_lion/scripts/run_daily.sh >> /data/tmp/sux002-sea-lion/logs/cron.log 2>&1
# For shadow/sim (no broker) the time of day does not matter.
set -uo pipefail
source "$(dirname "$0")/env.sh"
cd "$SEA_LION_ROOT"
MODE="${SEA_LION_MODE:-shadow}"
python -m sea_lion.cli --mode "$MODE" run "$@"
rc=$?
if [[ $rc -ne 0 ]]; then
  echo "[$(date -Is)] sea-lion run failed rc=$rc mode=$MODE" >&2
fi
exit $rc
