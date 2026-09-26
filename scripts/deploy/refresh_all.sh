#!/usr/bin/env bash
# The whole data pipeline, in order. Every step is resumable: files already in data/raw
# are reused (no re-download), constituency-years/lists already processed are skipped,
# and an interrupted run simply continues next time.
#
#   1. pipeline  — ECI structure for all three states + all Uttarakhand data
#   2. results   — Uttar Pradesh Form 20 booth results (2012, 2017, 2022)
#   3. stations  — Uttar Pradesh polling stations from the 75 district lists
#
# Usage:  scripts/deploy/refresh_all.sh            (as the app user, from the app directory)
# Logs:   $LOG_DIR/refresh-<timestamp>.log          (default /var/log/booth-intel)
set -uo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
LOG_DIR="${LOG_DIR:-/var/log/booth-intel}"
PY="$APP_DIR/.venv/bin/python"
if ! mkdir -p "$LOG_DIR" 2>/dev/null || [ ! -w "$LOG_DIR" ]; then
    LOG_DIR="$APP_DIR/data/processed/logs"          # e.g. a manual run on a workstation
    mkdir -p "$LOG_DIR"
fi
LOG="$LOG_DIR/refresh-$(date +%Y%m%d-%H%M%S).log"

cd "$APP_DIR"
export PYTHONPATH="$APP_DIR/src"

# one refresh at a time (a scheduled run must not overlap a manual one)
exec 9>"$APP_DIR/data/.refresh.lock"
if ! flock -n 9; then
    echo "another refresh is already running; exiting" | tee -a "$LOG"
    exit 0
fi

failed=0
step() {
    local name="$1"; shift
    echo "=== $(date -Is) $name: $*" | tee -a "$LOG"
    if "$PY" -m app "$@" >>"$LOG" 2>&1; then
        echo "=== $(date -Is) $name: ok" | tee -a "$LOG"
    else
        echo "=== $(date -Is) $name: FAILED (exit $?) — later steps still run; the next refresh retries" | tee -a "$LOG"
        failed=1
    fi
}

step "1/3 pipeline" pipeline --quiet
step "2/3 UP results" results --state "Uttar Pradesh"
step "3/3 UP stations" stations

# keep the last 30 logs
ls -1t "$LOG_DIR"/refresh-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f
echo "=== $(date -Is) refresh finished (failed=$failed); log: $LOG" | tee -a "$LOG"
exit "$failed"
