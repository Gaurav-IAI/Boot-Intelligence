#!/usr/bin/env bash
# One-command setup (and update) of the Booth Intelligence dashboard on an Ubuntu VPS
# (22.04 or 24.04). Safe to run again: it updates the code, keeps the existing
# database password, .env and data, and restarts the service.
#
# What it does
#   1. system packages: Python 3.11+, PostgreSQL, nginx, htpasswd, (certbot, Tesseract)
#   2. an app user, the code in $APP_DIR (git clone/pull, or the copy this script sits in)
#   3. a Python virtualenv with requirements.txt
#   4. a PostgreSQL role + database with a generated password, and .env
#   5. data — from a bundle made with scripts/pack_data.ps1 (no downloads, no re-parsing),
#      or, without one, the full pipeline from the official sources (several hours)
#   6. systemd: the dashboard, a weekly data refresh, a nightly database backup
#   7. nginx in front, with a login (HTTP basic auth) and, given a domain, HTTPS
#
# Usage (as root):
#   sudo DATA_BUNDLE=/root/booth-data.tar.gz DOMAIN=booth.example.org \
#        CONTACT_EMAIL=you@example.org bash scripts/deploy/setup_server.sh
#
# Options (environment variables):
#   REPO_URL       git URL to clone (default: the copy this script is in — no git needed)
#   BRANCH         branch to deploy (default: main)
#   APP_DIR        install location (default: /opt/booth-intel)
#   DATA_BUNDLE    path to booth-data.tar.gz from pack_data.ps1 (optional)
#   DATA_BUNDLE_URL  or a direct-download link to it (Dropbox ?dl=1, OneDrive download link,
#                  S3/GCS/any web server); it is downloaded to /root/booth-data.tar.gz
#   DOMAIN         public hostname; enables HTTPS via Let's Encrypt (optional)
#   CONTACT_EMAIL  used in the crawler User-Agent and for Let's Encrypt (recommended)
#   WEB_USER       dashboard login name (default: admin); password is generated
#   WITH_OCR=1     also install Tesseract (only needed for scanned-PDF experiments)
#   SKIP_DATA=1    set up everything but do not load data
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/booth-intel}"
APP_USER="${APP_USER:-booth}"
BRANCH="${BRANCH:-main}"
DB_NAME="${DB_NAME:-booth_intel}"
DB_USER="${DB_USER:-booth}"
WEB_USER="${WEB_USER:-admin}"
CONTACT_EMAIL="${CONTACT_EMAIL:-}"
DOMAIN="${DOMAIN:-}"
DATA_BUNDLE="${DATA_BUNDLE:-}"
SRC_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CRED_FILE="/root/booth-intel-credentials.txt"

log()  { echo -e "\n\033[1;34m==> $*\033[0m"; }
die()  { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }
as_app() { sudo -u "$APP_USER" -H bash -c "cd '$APP_DIR' && $*"; }

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
. /etc/os-release
[ "${ID:-}" = ubuntu ] || echo "warning: tested on Ubuntu 22.04/24.04; this is ${PRETTY_NAME:-unknown}"
if [ -n "${DATA_BUNDLE_URL:-}" ] && [ -z "$DATA_BUNDLE" ]; then
    DATA_BUNDLE=/root/booth-data.tar.gz
    if [ ! -s "$DATA_BUNDLE" ]; then
        log "downloading the data bundle"
        curl -fL --retry 3 -o "$DATA_BUNDLE.part" "$DATA_BUNDLE_URL" || die "could not download DATA_BUNDLE_URL"
        mv "$DATA_BUNDLE.part" "$DATA_BUNDLE"
    fi
    # a sharing page (HTML) instead of the file: stop before anything is unpacked
    gzip -t "$DATA_BUNDLE" 2>/dev/null || { rm -f "$DATA_BUNDLE"; die "DATA_BUNDLE_URL did not return a .tar.gz — use a direct-download link"; }
fi
[ -z "$DATA_BUNDLE" ] || [ -f "$DATA_BUNDLE" ] || die "DATA_BUNDLE not found: $DATA_BUNDLE"

# ---------------------------------------------------------------- 1. packages
log "1/7 system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git curl rsync ca-certificates software-properties-common \
    postgresql postgresql-contrib nginx apache2-utils ufw
PY=python3
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    # Ubuntu 22.04 ships 3.10: take 3.12 from the deadsnakes PPA
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -q
    apt-get install -y -q python3.12 python3.12-venv
    PY=python3.12
else
    apt-get install -y -q python3-venv python3-pip
fi
[ -n "$DOMAIN" ] && apt-get install -y -q certbot python3-certbot-nginx
[ "${WITH_OCR:-0}" = 1 ] && apt-get install -y -q tesseract-ocr tesseract-ocr-hin
systemctl enable --now postgresql nginx

# ---------------------------------------------------------------- 2. user + code
log "2/7 app user and code in $APP_DIR"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"
mkdir -p "$APP_DIR"
if [ -n "${REPO_URL:-}" ]; then
    if [ -d "$APP_DIR/.git" ]; then
        git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
        git -C "$APP_DIR" checkout --quiet "$BRANCH"
        git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
    else
        git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
    fi
elif [ "$SRC_DIR" != "$APP_DIR" ]; then
    # deploy the copy this script sits in (uploaded with scp/rsync); never overwrite data or .env
    rsync -a --delete --exclude '.venv/' --exclude 'data/' --exclude '.env' --exclude '.git/' \
        "$SRC_DIR/" "$APP_DIR/"
fi
mkdir -p "$APP_DIR/data/raw" "$APP_DIR/data/processed" /var/log/booth-intel /var/backups/booth-intel
chown -R "$APP_USER:$APP_USER" "$APP_DIR" /var/log/booth-intel /var/backups/booth-intel
chmod +x "$APP_DIR"/scripts/deploy/*.sh

# ---------------------------------------------------------------- 3. virtualenv
log "3/7 Python environment"
[ -x "$APP_DIR/.venv/bin/python" ] || as_app "$PY -m venv .venv"
as_app ".venv/bin/pip install --quiet --upgrade pip && .venv/bin/pip install --quiet -r requirements.txt"

# ---------------------------------------------------------------- 4. database + .env
log "4/7 PostgreSQL database and .env"
if [ -f "$APP_DIR/.env" ] && grep -q '^DATABASE_URL=postgresql' "$APP_DIR/.env"; then
    echo "keeping existing .env"
else
    DB_PASS="$(openssl rand -hex 18)"
    if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
        sudo -u postgres psql -qc "ALTER ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASS'"
    else
        sudo -u postgres psql -qc "CREATE ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASS'"
    fi
    sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
        || sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
    cat > "$APP_DIR/.env" <<EOF
# written by setup_server.sh on $(date -I)
DATABASE_URL=postgresql+psycopg://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME
# a server must fail loudly rather than fall back to a local SQLite file
ALLOW_SQLITE_FALLBACK=false
HTTP_CONCURRENCY=3
HTTP_DELAY_SECONDS=0.4
HTTP_TIMEOUT=60
HTTP_RETRIES=4
USER_AGENT=booth-intelligence/0.1 (research; contact: ${CONTACT_EMAIL:-set-CONTACT_EMAIL})
OCR_LANGUAGES=hin+eng
OCR_DPI=300
EOF
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

# ---------------------------------------------------------------- 5. data
log "5/7 data"
rows_in_db() {   # 0 when the tables do not exist yet
    sudo -u postgres psql -d "$DB_NAME" -tAc "SELECT count(*) FROM election_results" 2>/dev/null || echo 0
}
if [ "${SKIP_DATA:-0}" = 1 ]; then
    echo "SKIP_DATA=1: no data loaded"
elif [ -n "$DATA_BUNDLE" ]; then
    echo "unpacking $DATA_BUNDLE (existing files are kept)"
    tar -xzf "$DATA_BUNDLE" -C "$APP_DIR" --skip-old-files
    chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
    as_app "PYTHONPATH=src .venv/bin/python -c 'from app.database.session import init_db; print(init_db())'"
    if [ "$(rows_in_db)" = 0 ] && [ -f "$APP_DIR/data/uk_election.sqlite3" ]; then
        echo "copying the bundled database into PostgreSQL (no downloads, no re-parsing)"
        as_app "PYTHONPATH=src .venv/bin/python -m app migrate-db --from sqlite:///$APP_DIR/data/uk_election.sqlite3"
    else
        echo "database already has data — not copying (use 'migrate-db --replace' by hand to overwrite)"
    fi
elif [ "$(rows_in_db)" = 0 ]; then
    echo "no bundle: running the full pipeline from the official sources (several hours)."
    echo "progress: tail -f /var/log/booth-intel/refresh-*.log"
    as_app "APP_DIR='$APP_DIR' scripts/deploy/refresh_all.sh" || echo "some steps failed; the weekly refresh will retry"
else
    echo "database already has data"
fi

# ---------------------------------------------------------------- 6. systemd
log "6/7 services: dashboard, weekly refresh, nightly backup"
cat > /etc/systemd/system/booth-intel.service <<EOF
[Unit]
Description=Booth Intelligence dashboard
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONPATH=$APP_DIR/src
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn app.web.app:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/booth-intel-refresh.service <<EOF
[Unit]
Description=Booth Intelligence data refresh (pipeline + UP results + UP stations)
After=network-online.target postgresql.service

[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=APP_DIR=$APP_DIR
ExecStart=$APP_DIR/scripts/deploy/refresh_all.sh
TimeoutStartSec=12h
EOF
cat > /etc/systemd/system/booth-intel-refresh.timer <<EOF
[Unit]
Description=Weekly Booth Intelligence data refresh

[Timer]
OnCalendar=Sun *-*-* 02:00:00
Persistent=true
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/booth-intel-backup.service <<EOF
[Unit]
Description=Booth Intelligence database backup
After=postgresql.service

[Service]
Type=oneshot
User=$APP_USER
Environment=APP_DIR=$APP_DIR
ExecStart=$APP_DIR/scripts/deploy/backup.sh
EOF
cat > /etc/systemd/system/booth-intel-backup.timer <<EOF
[Unit]
Description=Nightly Booth Intelligence database backup

[Timer]
OnCalendar=*-*-* 01:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now booth-intel.service booth-intel-refresh.timer booth-intel-backup.timer
systemctl restart booth-intel.service

# ---------------------------------------------------------------- 7. nginx + login + HTTPS
log "7/7 nginx with login${DOMAIN:+ and HTTPS for $DOMAIN}"
if [ ! -f /etc/nginx/booth-intel.htpasswd ]; then
    WEB_PASS="$(openssl rand -base64 15 | tr -d '/+=' | cut -c1-16)"
    htpasswd -bc /etc/nginx/booth-intel.htpasswd "$WEB_USER" "$WEB_PASS" >/dev/null
    { echo "Dashboard login: $WEB_USER / $WEB_PASS"; echo "Created: $(date -Is)"; } > "$CRED_FILE"
    chmod 600 "$CRED_FILE"
fi
cat > /etc/nginx/sites-available/booth-intel <<EOF
server {
    listen 80;
    server_name ${DOMAIN:-_};
    client_max_body_size 5m;

    # the dashboard shows elector-level data: never without a login
    auth_basic           "Booth Intelligence";
    auth_basic_user_file /etc/nginx/booth-intel.htpasswd;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/booth-intel /etc/nginx/sites-enabled/booth-intel
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
if [ -n "$DOMAIN" ]; then
    if [ -n "$CONTACT_EMAIL" ]; then email_args=(-m "$CONTACT_EMAIL"); else email_args=(--register-unsafely-without-email); fi
    certbot --nginx --non-interactive --agree-tos --redirect -d "$DOMAIN" "${email_args[@]}" \
        || echo "certbot failed — check that $DOMAIN points at this server, then re-run"
fi
ufw allow OpenSSH >/dev/null && ufw allow 'Nginx Full' >/dev/null && ufw --force enable >/dev/null

# ---------------------------------------------------------------- check
log "checking"
sleep 3
code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ || true)"
backend="$(as_app "PYTHONPATH=src .venv/bin/python -c 'from app.database.session import active_backend; print(active_backend())'" 2>/dev/null | tail -1)"
echo "dashboard on 127.0.0.1:8000 -> HTTP $code; database backend: $backend"
[ "$backend" = postgresql ] || echo "WARNING: backend is not postgresql — check $APP_DIR/.env"
echo
echo "Done. Open: ${DOMAIN:+https://$DOMAIN}${DOMAIN:-http://<server-ip>}/"
echo "Login is in $CRED_FILE"
echo "Refresh now: sudo systemctl start booth-intel-refresh   (logs: /var/log/booth-intel/)"
echo "Update code: re-run this script."
