#!/usr/bin/env bash
# Update an EXISTING deployment (the original Uttarakhand version) to the multi-state
# version with Uttar Pradesh results and polling stations — without touching nginx, the
# firewall, .env or the data already loaded. Safe to run again.
#
#   1. database backup (pg_dump), if pg_dump is available
#   2. code: git fetch + checkout of $BRANCH (local changes on the server are stashed)
#   3. Python packages (adds xlrd, openpyxl)
#   4. schema: new nullable columns are added automatically (additive migrations)
#   5-6. data, one of:
#      STATE_FILE (recommended): import a state export (up_data.sqlite3.gz, ~35 MB, made with
#        `python -m app export-state`) — only that state's rows are added/replaced; no
#        downloads, no parsing, a few minutes
#      or load from the sources: UP + Telangana constituencies (ECI), UP Form 20 results, UP
#        polling stations — optionally parsing data/raw from DATA_BUNDLE instead of downloading
#   7. restart the dashboard service
#
# Usage (as root, or as the user owning $APP_DIR with sudo for the restart):
#   sudo STATE_FILE=/root/up_data.sqlite3.gz bash scripts/deploy/update_existing.sh
#   sudo STATE_FILE_URL='https://...direct-link...' bash scripts/deploy/update_existing.sh
#   sudo DATA_BUNDLE=/root/booth-data.tar.gz bash scripts/deploy/update_existing.sh
#   sudo APP_DIR=/opt/booth-intel BRANCH=main bash scripts/deploy/update_existing.sh
#
# Options: APP_DIR (default /opt/booth-intel), BRANCH (default main), SERVICE (default
# booth-intel), STATE_FILE / STATE_FILE_URL, DATA_BUNDLE / DATA_BUNDLE_URL, SKIP_DATA=1.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/booth-intel}"
BRANCH="${BRANCH:-main}"
SERVICE="${SERVICE:-booth-intel}"
DATA_BUNDLE="${DATA_BUNDLE:-}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/booth-intel}"
LOG="${LOG:-$APP_DIR/data/processed/update-$(date +%Y%m%d-%H%M%S).log}"

log() { echo -e "\n\033[1;34m==> $*\033[0m"; }
die() { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

[ -d "$APP_DIR/.git" ] || die "$APP_DIR is not a git checkout (set APP_DIR to the deployed directory)"
[ -x "$APP_DIR/.venv/bin/python" ] || die "no virtualenv at $APP_DIR/.venv"
OWNER="$(stat -c %U "$APP_DIR")"
run() {   # as the owner of the deployment, in APP_DIR, with PYTHONPATH set
    if [ "$(id -un)" = "$OWNER" ]; then (cd "$APP_DIR" && PYTHONPATH=src bash -c "$*")
    else sudo -u "$OWNER" -H bash -c "cd '$APP_DIR' && PYTHONPATH=src $*"; fi
}
mkdir -p "$(dirname "$LOG")"

# ------------------------------------------------------------------ 1. backup
log "1/7 database backup"
url="$(grep -E '^DATABASE_URL=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- | sed 's#+psycopg##' || true)"
if [[ "$url" == postgresql* ]] && command -v pg_dump >/dev/null; then
    mkdir -p "$BACKUP_DIR"
    out="$BACKUP_DIR/before-update-$(date +%Y%m%d-%H%M%S).dump"
    pg_dump --format=custom --no-owner --dbname="$url" --file="$out" && echo "backup: $out"
elif [[ "$url" == postgresql* ]] && command -v docker >/dev/null && docker ps --format '{{.Names}}' | grep -q uk-election-poc-db; then
    mkdir -p "$BACKUP_DIR"
    out="$BACKUP_DIR/before-update-$(date +%Y%m%d-%H%M%S).sql"
    docker exec uk-election-poc-db pg_dump -U poc uk_election > "$out" && echo "backup (docker): $out"
else
    echo "WARNING: could not back up automatically (no pg_dump, or DATABASE_URL is not PostgreSQL)."
    read -r -t 30 -p "Continue without a backup? [y/N] " ok || true
    [ "${ok:-n}" = y ] || die "stopped — back up the database first"
fi

# ------------------------------------------------------------------ 2. code
log "2/7 code -> $BRANCH"
cd "$APP_DIR"
run "git stash push --include-untracked -m 'before update $(date -Is)' >/dev/null 2>&1 || true"
run "git fetch --quiet origin '$BRANCH' && git checkout --quiet '$BRANCH' && git reset --quiet --hard 'origin/$BRANCH'"
run "git log --oneline -1"

# ------------------------------------------------------------------ 3. packages
log "3/7 Python packages"
run ".venv/bin/pip install --quiet -r requirements.txt"

# ------------------------------------------------------------------ 4. schema
log "4/7 schema (additive, nullable columns only)"
run ".venv/bin/python -c 'from app.database.session import init_db; print(\"backend:\", init_db())'"

STATE_FILE="${STATE_FILE:-}"
if [ -n "${STATE_FILE_URL:-}" ] && [ -z "$STATE_FILE" ]; then
    STATE_FILE=/root/up_data.sqlite3.gz
    [ -s "$STATE_FILE" ] || curl -fL --retry 3 -o "$STATE_FILE" "$STATE_FILE_URL" || die "download failed"
fi

if [ "${SKIP_DATA:-0}" = 1 ]; then
    log "SKIP_DATA=1: code updated, no data loaded"
elif [ -n "$STATE_FILE" ]; then
    log "5-6/7 importing $STATE_FILE (only the states in the file are added or replaced)"
    [ -f "$STATE_FILE" ] || die "STATE_FILE not found: $STATE_FILE"
    case "$STATE_FILE" in *.gz) gzip -t "$STATE_FILE" || die "$STATE_FILE is not gzip (a web page instead of the file?)";; esac
    # /root is not readable by the app user: import from a copy inside the app directory
    staged="$APP_DIR/data/processed/$(basename "$STATE_FILE")"
    [ "$(realpath "$STATE_FILE")" = "$(realpath -m "$staged")" ] || cp "$STATE_FILE" "$staged"
    chown "$OWNER:" "$staged"
    run ".venv/bin/python -m app import-state --file '$staged'" 2>&1 | tee -a "$LOG" \
        || die "import failed — nothing was changed (it runs in one transaction); see $LOG"
else
    # -------------------------------------------------------------- 5. cached sources
    if [ -n "${DATA_BUNDLE_URL:-}" ] && [ -z "$DATA_BUNDLE" ]; then
        DATA_BUNDLE=/root/booth-data.tar.gz
        [ -s "$DATA_BUNDLE" ] || curl -fL --retry 3 -o "$DATA_BUNDLE" "$DATA_BUNDLE_URL" || die "download failed"
    fi
    if [ -n "$DATA_BUNDLE" ]; then
        log "5/7 unpacking data/raw from $DATA_BUNDLE (existing files kept; the bundled database is ignored)"
        gzip -t "$DATA_BUNDLE" || die "$DATA_BUNDLE is not a .tar.gz (a web page instead of the file?)"
        tar -xzf "$DATA_BUNDLE" -C "$APP_DIR" --skip-old-files data/raw
        chown -R "$OWNER:" "$APP_DIR/data/raw"
    else
        log "5/7 no bundle: step 6 downloads the UP sources (several hours)"
    fi

    # -------------------------------------------------------------- 6. data
    log "6/7 data (log: $LOG)"
    step() {
        echo "--- $(date -Is) $*" | tee -a "$LOG"
        run ".venv/bin/python -m app $*" >>"$LOG" 2>&1 && echo "    ok" \
            || echo "    FAILED — see $LOG; re-running this script resumes where it stopped"
    }
    step "pipeline --missing-only --quiet"        # UP + Telangana districts/ACs from ECI
    step "results --state 'Uttar Pradesh'"         # UP Form 20 2012/2017/2022
    step "stations"                                 # UP polling stations (75 district lists)
fi

# ------------------------------------------------------------------ 7. restart
log "7/7 restart $SERVICE"
if systemctl list-unit-files "$SERVICE.service" >/dev/null 2>&1 && systemctl cat "$SERVICE" >/dev/null 2>&1; then
    systemctl restart "$SERVICE" && sleep 3 && systemctl --no-pager --lines=0 status "$SERVICE" | head -3
else
    echo "no systemd service '$SERVICE' — restart the dashboard the way it is run on this server"
fi
echo
echo "Done. Check the dashboard: State dropdown -> Uttar Pradesh."
echo "Optional weekly refresh + nightly backup timers: see docs/DEPLOYMENT.md, section 6."
