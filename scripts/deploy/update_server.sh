#!/usr/bin/env bash
# Server copy of update_existing.sh for /var/www/Boot-Intelligence with the `booth` CONDA
# env instead of .venv. On the first run it also points the systemd service at the conda
# env (a drop-in, see step 7). Everything else is as in update_existing.sh:
#
# Update an EXISTING deployment (the original Uttarakhand version) to the multi-state
# version with Uttar Pradesh results and polling stations — without touching nginx, the
# firewall, .env or the data already loaded. Safe to run again.
#
#   1. database backup (pg_dump), if pg_dump is available
#   2. code: git fetch + checkout of $BRANCH (local changes on the server are stashed)
#   3. Python packages (adds xlrd, openpyxl)
#   4. schema: new nullable columns are added automatically (additive migrations)
#   5-6. data, one of:
#      state exports (default): every deploy/*.sqlite3.gz committed in the repository (made
#        with scripts/publish_data.ps1) is imported — only the states in each file are
#        added/replaced; no downloads, no parsing, a few minutes. A file already imported
#        unchanged is skipped, so running this after every `git pull` is cheap.
#        STATE_FILE / STATE_FILE_URL import one given file instead.
#      if there is no export: load from the sources — UP + Telangana constituencies (ECI),
#        UP Form 20 results, UP polling stations (DATA_BUNDLE: parse data/raw, no downloads)
#   7. restart the dashboard service
#
# Usage (as root, or as the user owning $APP_DIR with sudo for the restart):
#   sudo bash /var/www/Boot-Intelligence/scripts/deploy/update_server.sh   # the usual update
#   sudo STATE_FILE=/root/up_data.sqlite3.gz bash scripts/deploy/update_server.sh
#   sudo STATE_FILE_URL='https://...direct-link...' bash scripts/deploy/update_server.sh
#   sudo DATA_BUNDLE=/root/booth-data.tar.gz bash scripts/deploy/update_server.sh
#   sudo ENV_DIR=/opt/miniconda3/envs/booth bash scripts/deploy/update_server.sh
#
# Options: APP_DIR (default /var/www/Boot-Intelligence), BRANCH (default main), SERVICE
# (default booth-intel), CONDA_ENV (default booth) / ENV_DIR (the env's directory),
# STATE_FILE / STATE_FILE_URL, FORCE_IMPORT=1 (import unchanged files again),
# DATA_BUNDLE / DATA_BUNDLE_URL, SKIP_DATA=1.
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/Boot-Intelligence}"
BRANCH="${BRANCH:-main}"
SERVICE="${SERVICE:-booth-intel}"
CONDA_ENV="${CONDA_ENV:-booth}"
DATA_BUNDLE="${DATA_BUNDLE:-}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/booth-intel}"
LOG="${LOG:-$APP_DIR/data/processed/update-$(date +%Y%m%d-%H%M%S).log}"

log() { echo -e "\n\033[1;34m==> $*\033[0m"; }
die() { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

[ -d "$APP_DIR/.git" ] || die "$APP_DIR is not a git checkout (set APP_DIR to the deployed directory)"
OWNER="$(stat -c %U "$APP_DIR")"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
# the booth conda env: ENV_DIR, or the usual install locations (system-wide first)
if [ -z "${ENV_DIR:-}" ]; then
    for base in /opt/miniconda3 /opt/miniforge3 /opt/conda /opt/anaconda3 \
                "$OWNER_HOME/miniconda3" "$OWNER_HOME/miniforge3" "$OWNER_HOME/anaconda3" \
                "$HOME/miniconda3" "$HOME/miniforge3" "$HOME/anaconda3"; do
        [ -x "$base/envs/$CONDA_ENV/bin/python" ] && ENV_DIR="$base/envs/$CONDA_ENV" && break
    done
fi
[ -n "${ENV_DIR:-}" ] && [ -x "$ENV_DIR/bin/python" ] \
    || die "conda env '$CONDA_ENV' not found — create it (see docs) or set ENV_DIR=/path/to/envs/$CONDA_ENV"
PY="$ENV_DIR/bin/python"
echo "app: $APP_DIR (owner $OWNER)   env: $ENV_DIR"
# this script must be committed: step 2 stashes untracked files, which would include it
git -C "$APP_DIR" ls-files --error-unmatch scripts/deploy/update_server.sh >/dev/null 2>&1 \
    || die "scripts/deploy/update_server.sh is not committed in $APP_DIR — commit and push it, then git pull on the server"
run() {   # as the owner of the deployment, in APP_DIR, with PYTHONPATH set
    if [ "$(id -un)" = "$OWNER" ]; then (cd "$APP_DIR" && PYTHONPATH=src bash -c "$*")
    else sudo -u "$OWNER" -H bash -c "cd '$APP_DIR' && PYTHONPATH=src $*"; fi
}
mkdir -p "$(dirname "$LOG")"
run "'$PY' -c 'import sys' " || die "$OWNER cannot run $PY — install conda somewhere $OWNER can read (e.g. /opt/miniconda3)"

SELF="$APP_DIR/scripts/deploy/update_server.sh"
if [ "${CODE_UPDATED:-0}" = 1 ]; then
    log "1-2/7 backup and code already done — continuing with the updated script"
else
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
before="$(sha256sum "$SELF" 2>/dev/null | cut -d' ' -f1 || true)"
run "git stash push --include-untracked -m 'before update $(date -Is)' >/dev/null 2>&1 || true"
run "git fetch --quiet origin '$BRANCH' && git checkout --quiet '$BRANCH' && git reset --quiet --hard 'origin/$BRANCH'"
run "git log --oneline -1"
# the pull may have brought a newer version of this script: run the rest with that one
if [ "$(sha256sum "$SELF" | cut -d' ' -f1)" != "$before" ]; then
    log "this script was updated by the pull — restarting it"
    CODE_UPDATED=1 exec bash "$SELF"
fi
fi

# ------------------------------------------------------------------ 3. packages
log "3/7 Python packages (conda env $CONDA_ENV)"
run "'$PY' -m pip install --quiet -r requirements.txt"

# ------------------------------------------------------------------ 4. schema
log "4/7 schema (additive, nullable columns only)"
backend="$(run "'$PY' -c 'from app.database.session import init_db; print(init_db())'" | tail -1)"
echo "backend: $backend"
[[ "$backend" == *sqlite* ]] && echo "WARNING: the app is on SQLite, not PostgreSQL — check DATABASE_URL in $APP_DIR/.env"

STATE_FILE="${STATE_FILE:-}"
if [ -n "${STATE_FILE_URL:-}" ] && [ -z "$STATE_FILE" ]; then
    STATE_FILE=/root/up_data.sqlite3.gz
    [ -s "$STATE_FILE" ] || curl -fL --retry 3 -o "$STATE_FILE" "$STATE_FILE_URL" || die "download failed"
fi
# The state files to import: the one given, or every export committed under deploy/
# (they arrive with `git pull`, so publishing new data is just a commit).
if [ -n "$STATE_FILE" ]; then
    STATE_FILES=("$STATE_FILE")
else
    shopt -s nullglob
    STATE_FILES=("$APP_DIR"/deploy/*.sqlite3.gz)
    shopt -u nullglob
fi

import_file() {   # import one export unless this exact file was imported already
    local f="$1" name marker sum staged
    [ -f "$f" ] || die "state file not found: $f"
    gzip -t "$f" 2>/dev/null || die "$f is not gzip (a web page instead of the file?)"
    name="$(basename "$f")"
    marker="$APP_DIR/data/processed/.imported-$name.sha256"
    sum="$(sha256sum "$f" | cut -d' ' -f1)"
    if [ "${FORCE_IMPORT:-0}" != 1 ] && [ -f "$marker" ] && [ "$(cat "$marker")" = "$sum" ]; then
        echo "  $name: already imported (unchanged) — skipped; FORCE_IMPORT=1 to import again"
        return
    fi
    # /root is not readable by the app user: import from a copy inside the app directory
    staged="$APP_DIR/data/processed/$name"
    [ "$(realpath "$f")" = "$(realpath -m "$staged")" ] || cp "$f" "$staged"
    chown "$OWNER:" "$staged"
    echo "  $name: importing (only the states in the file are added or replaced)"
    run "'$PY' -m app import-state --file '$staged'" 2>&1 | tee -a "$LOG" \
        || die "import of $name failed — nothing was changed (one transaction); see $LOG"
    echo "$sum" > "$marker"
    [ "$staged" = "$f" ] || rm -f "$staged"
}

if [ "${SKIP_DATA:-0}" = 1 ]; then
    log "SKIP_DATA=1: code updated, no data loaded"
elif [ "${#STATE_FILES[@]}" -gt 0 ]; then
    log "5-6/7 importing ${#STATE_FILES[@]} state file(s)"
    for f in "${STATE_FILES[@]}"; do import_file "$f"; done
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
        run "'$PY' -m app $*" >>"$LOG" 2>&1 && echo "    ok" \
            || echo "    FAILED — see $LOG; re-running this script resumes where it stopped"
    }
    step "pipeline --missing-only --quiet"        # UP + Telangana districts/ACs from ECI
    step "results --state 'Uttar Pradesh'"         # UP Form 20 2012/2017/2022
    step "stations"                                 # UP polling stations (75 district lists)
fi

# ------------------------------------------------------------------ 7. restart
log "7/7 restart $SERVICE"
if systemctl list-unit-files "$SERVICE.service" >/dev/null 2>&1 && systemctl cat "$SERVICE" >/dev/null 2>&1; then
    # point the service at the conda env (a drop-in keeps the original unit untouched;
    # delete /etc/systemd/system/$SERVICE.service.d/conda-env.conf to undo)
    cur="$(systemctl cat "$SERVICE" | grep -E '^ExecStart=.+' | tail -1 | cut -d= -f2-)"
    bin="${cur%% *}"; args="${cur#"$bin"}"
    case "$(basename "$bin")" in
        python*) new="$PY" ;;
        *)       new="$ENV_DIR/bin/$(basename "$bin")" ;;
    esac
    if [ "$bin" != "$new" ]; then
        if [ -x "$new" ] && [ "$(id -u)" = 0 ]; then
            mkdir -p "/etc/systemd/system/$SERVICE.service.d"
            printf '[Service]\nExecStart=\nExecStart=%s%s\n' "$new" "$args" \
                > "/etc/systemd/system/$SERVICE.service.d/conda-env.conf"
            systemctl daemon-reload
            echo "service now runs: $new$args"
        else
            echo "WARNING: $SERVICE runs $bin, not the conda env — run this script as root to switch it"
        fi
    fi
    systemctl restart "$SERVICE" && sleep 3 && systemctl --no-pager --lines=0 status "$SERVICE" | head -3
else
    echo "no systemd service '$SERVICE' — restart the dashboard the way it is run on this server"
fi
echo
echo "Done. Check the dashboard: State dropdown -> Uttar Pradesh."
echo "Optional weekly refresh + nightly backup timers: see docs/DEPLOYMENT.md, section 6."
