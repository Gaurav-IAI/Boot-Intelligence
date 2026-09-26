#!/usr/bin/env bash
# Local-PC version of update_existing.sh: brings THIS checkout up to date and loads the
# latest state data, using the `booth` conda env and the conda-installed PostgreSQL
# (no Docker, no sudo, no systemd, no /root, no .venv). Safe to run again.
#
#   1. start the conda PostgreSQL cluster if it is not running, then back it up (pg_dump)
#   2. code: git fetch + fast-forward to origin/$BRANCH (stops if you have uncommitted
#      changes — commit them, or run with STASH=1 to stash them first)
#   3. Python packages into the booth env
#   4. schema: new nullable columns are added automatically (additive migrations)
#   5-6. data: every deploy/*.sqlite3.gz is imported (unchanged files are skipped);
#        STATE_FILE / STATE_FILE_URL import one given file instead; with no export,
#        load from the sources (DATA_BUNDLE: unpack data/raw first, no downloads)
#   7. restart: SERVE=1 starts the dashboard at the end; otherwise restart it yourself
#
# Usage (as yourself, no sudo):
#   bash scripts/deploy/update_local.sh
#   STASH=1 bash scripts/deploy/update_local.sh
#   SERVE=1 bash scripts/deploy/update_local.sh
#   STATE_FILE=~/Downloads/up_data.sqlite3.gz bash scripts/deploy/update_local.sh
#
# Options: APP_DIR (default: this checkout), BRANCH (default main), CONDA_ENV (default
# booth), PGDATA (default ~/.local/pgdata-booth), PGPORT (default 5433), SKIP_CODE=1,
# SKIP_BACKUP=1, STASH=1, STATE_FILE / STATE_FILE_URL, FORCE_IMPORT=1, DATA_BUNDLE /
# DATA_BUNDLE_URL, SKIP_DATA=1, SERVE=1, PORT (dashboard port, default 8001).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
BRANCH="${BRANCH:-main}"
CONDA_ENV="${CONDA_ENV:-booth}"
PGDATA="${PGDATA:-$HOME/.local/pgdata-booth}"
PGPORT="${PGPORT:-5433}"
PORT="${PORT:-8001}"   # dashboard port (8000 is taken by the sentiment-analysis app on this PC)
DATA_BUNDLE="${DATA_BUNDLE:-}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/data/processed/backups}"
LOG="${LOG:-$APP_DIR/data/processed/update-$(date +%Y%m%d-%H%M%S).log}"

log() { echo -e "\n\033[1;34m==> $*\033[0m"; }
die() { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

# the booth conda env: its own path, or the usual install locations
if [ -z "${ENV_DIR:-}" ]; then
    for d in "$HOME/miniconda3/envs/$CONDA_ENV" "$HOME/anaconda3/envs/$CONDA_ENV" \
             "$HOME/miniforge3/envs/$CONDA_ENV" "$HOME/.conda/envs/$CONDA_ENV"; do
        [ -x "$d/bin/python" ] && ENV_DIR="$d" && break
    done
    if [ -z "${ENV_DIR:-}" ] && command -v conda >/dev/null; then
        ENV_DIR="$(conda env list | awk -v n="$CONDA_ENV" '$1 == n {print $NF}')"
    fi
fi
[ -n "${ENV_DIR:-}" ] && [ -x "$ENV_DIR/bin/python" ] || die "conda env '$CONDA_ENV' not found (set ENV_DIR)"
PY="$ENV_DIR/bin/python"
PGBIN="$ENV_DIR/bin"

[ -d "$APP_DIR/.git" ] || die "$APP_DIR is not a git checkout (set APP_DIR)"
run() { (cd "$APP_DIR" && PYTHONPATH=src bash -c "$*"); }   # in APP_DIR, with PYTHONPATH set
mkdir -p "$(dirname "$LOG")"
echo "app: $APP_DIR   env: $ENV_DIR"

# ------------------------------------------------------------------ PostgreSQL
# The cluster lives in the conda env and does not survive a reboot. If it is down, the app
# silently falls back to SQLite (ALLOW_SQLITE_FALLBACK=true) — so start it first.
if "$PGBIN/pg_isready" -q -h localhost -p "$PGPORT"; then
    echo "PostgreSQL: running on port $PGPORT"
else
    [ -d "$PGDATA" ] || die "no PostgreSQL cluster at $PGDATA (set PGDATA)"
    log "starting PostgreSQL ($PGDATA, port $PGPORT)"
    "$PGBIN/pg_ctl" -D "$PGDATA" -o "-p $PGPORT -k /tmp" -l "$PGDATA/server.log" -w start \
        || die "PostgreSQL did not start — see $PGDATA/server.log"
fi

SELF="$APP_DIR/scripts/deploy/update_local.sh"
if [ "${CODE_UPDATED:-0}" = 1 ]; then
    log "1-2/7 backup and code already done — continuing with the updated script"
else
# ------------------------------------------------------------------ 1. backup
log "1/7 database backup"
url="$(grep -E '^DATABASE_URL=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- | sed 's#+psycopg##' || true)"
if [ "${SKIP_BACKUP:-0}" = 1 ]; then
    echo "SKIP_BACKUP=1: no backup"
elif [[ "$url" == postgresql* ]]; then
    mkdir -p "$BACKUP_DIR"
    out="$BACKUP_DIR/before-update-$(date +%Y%m%d-%H%M%S).dump"
    "$PGBIN/pg_dump" --format=custom --no-owner --dbname="$url" --file="$out" && echo "backup: $out"
else
    echo "WARNING: DATABASE_URL in .env is not PostgreSQL — no backup made."
    read -r -t 30 -p "Continue without a backup? [y/N] " ok || true
    [ "${ok:-n}" = y ] || die "stopped — back up the database first"
fi

# ------------------------------------------------------------------ 2. code
if [ "${SKIP_CODE:-0}" = 1 ]; then
    log "2/7 SKIP_CODE=1: code left as it is"
else
    log "2/7 code -> origin/$BRANCH"
    cd "$APP_DIR"
    before="$(sha256sum "$SELF" 2>/dev/null | cut -d' ' -f1 || true)"
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        if [ "${STASH:-0}" = 1 ]; then
            git stash push -m "before update $(date -Is)" && echo "local changes stashed (git stash pop to restore)"
        else
            git status --short --untracked-files=no
            die "you have uncommitted changes — commit them, or run with STASH=1 (or SKIP_CODE=1)"
        fi
    fi
    git fetch --quiet origin "$BRANCH"
    git checkout --quiet "$BRANCH"
    git merge --ff-only --quiet "origin/$BRANCH" \
        || die "local $BRANCH has diverged from origin/$BRANCH — rebase or merge it yourself, then re-run with SKIP_CODE=1"
    git log --oneline -1
    # the pull may have brought a newer version of this script: run the rest with that one
    if [ -f "$SELF" ] && [ "$(sha256sum "$SELF" | cut -d' ' -f1)" != "$before" ]; then
        log "this script was updated by the pull — restarting it"
        CODE_UPDATED=1 exec bash "$SELF"
    fi
fi
fi

# ------------------------------------------------------------------ 3. packages
log "3/7 Python packages (conda env $CONDA_ENV)"
run "'$PY' -m pip install --quiet -r requirements.txt"

# ------------------------------------------------------------------ 4. schema
log "4/7 schema (additive, nullable columns only)"
backend="$(run "'$PY' -c 'from app.database.session import init_db; print(init_db())'" | tail -1)"
echo "backend: $backend"
[[ "$backend" == *sqlite* ]] && die "the app is on the SQLite fallback, not PostgreSQL — check DATABASE_URL and that port $PGPORT is up"

STATE_FILE="${STATE_FILE:-}"
if [ -n "${STATE_FILE_URL:-}" ] && [ -z "$STATE_FILE" ]; then
    STATE_FILE="$APP_DIR/data/processed/downloaded_state.sqlite3.gz"
    [ -s "$STATE_FILE" ] || curl -fL --retry 3 -o "$STATE_FILE" "$STATE_FILE_URL" || die "download failed"
fi
if [ -n "$STATE_FILE" ]; then
    STATE_FILES=("$STATE_FILE")
else
    shopt -s nullglob
    STATE_FILES=("$APP_DIR"/deploy/*.sqlite3.gz)
    shopt -u nullglob
fi

import_file() {   # import one export unless this exact file was imported already
    local f="$1" name marker sum
    [ -f "$f" ] || die "state file not found: $f"
    gzip -t "$f" 2>/dev/null || die "$f is not gzip (a web page instead of the file?)"
    name="$(basename "$f")"
    marker="$APP_DIR/data/processed/.imported-$name.sha256"
    sum="$(sha256sum "$f" | cut -d' ' -f1)"
    if [ "${FORCE_IMPORT:-0}" != 1 ] && [ -f "$marker" ] && [ "$(cat "$marker")" = "$sum" ]; then
        echo "  $name: already imported (unchanged) — skipped; FORCE_IMPORT=1 to import again"
        return
    fi
    echo "  $name: importing (only the states in the file are added or replaced)"
    run "'$PY' -m app import-state --file '$(realpath "$f")'" 2>&1 | tee -a "$LOG" \
        || die "import of $name failed — nothing was changed (one transaction); see $LOG"
    echo "$sum" > "$marker"
}

if [ "${SKIP_DATA:-0}" = 1 ]; then
    log "SKIP_DATA=1: code updated, no data loaded"
elif [ "${#STATE_FILES[@]}" -gt 0 ]; then
    log "5-6/7 importing ${#STATE_FILES[@]} state file(s)"
    for f in "${STATE_FILES[@]}"; do import_file "$f"; done
else
    # -------------------------------------------------------------- 5. cached sources
    if [ -n "${DATA_BUNDLE_URL:-}" ] && [ -z "$DATA_BUNDLE" ]; then
        DATA_BUNDLE="$APP_DIR/data/processed/booth-data.tar.gz"
        [ -s "$DATA_BUNDLE" ] || curl -fL --retry 3 -o "$DATA_BUNDLE" "$DATA_BUNDLE_URL" || die "download failed"
    fi
    if [ -n "$DATA_BUNDLE" ]; then
        log "5/7 unpacking data/raw from $DATA_BUNDLE (existing files kept; the bundled database is ignored)"
        gzip -t "$DATA_BUNDLE" || die "$DATA_BUNDLE is not a .tar.gz (a web page instead of the file?)"
        tar -xzf "$DATA_BUNDLE" -C "$APP_DIR" --skip-old-files data/raw
    else
        log "5/7 no bundle: step 6 downloads the UP sources (several hours)"
    fi

    # -------------------------------------------------------------- 6. data
    log "6/7 data (log: $LOG)"
    step() {
        echo "--- $(date -Is) $*" | tee -a "$LOG"
        run "'$PY' -m app $*" >>"$LOG" 2>&1 && echo "    ok" \
            || echo "    FAILED — see $LOG; re-running this script resumes where it stopped"
    }
    step "pipeline --missing-only --quiet"        # UP + Telangana districts/ACs from ECI
    step "results --state 'Uttar Pradesh'"         # UP Form 20 2012/2017/2022
    step "stations"                                 # UP polling stations (75 district lists)
fi

# ------------------------------------------------------------------ 7. restart
log "7/7 dashboard"
if pgrep -f "python.* -m app serve" >/dev/null; then
    echo "a dashboard is already running (pid $(pgrep -f 'python.* -m app serve' | tr '\n' ' ')) — stop it (Ctrl+C) and start it again to load the new code"
fi
if [ "${SERVE:-0}" = 1 ]; then
    ss -ltn 2>/dev/null | grep -q ":$PORT " && die "port $PORT is already in use by another program — run with PORT=<free port>"
    echo "starting the dashboard at http://127.0.0.1:$PORT (Ctrl+C to stop)"
    cd "$APP_DIR" && PYTHONPATH=src exec "$PY" -m app serve --skip-pipeline --port "$PORT"
fi
echo
echo "Done. Start the dashboard with:"
echo "  cd '$APP_DIR' && conda activate $CONDA_ENV && PYTHONPATH=src python -m app serve --skip-pipeline --port $PORT"
echo "Then open http://127.0.0.1:$PORT -> State dropdown -> Uttar Pradesh."
