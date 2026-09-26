#!/usr/bin/env bash
# Nightly PostgreSQL backup (custom format, restorable with pg_restore). Keeps 14 days.
#
# Restore:  pg_restore --clean --if-exists -d "$DATABASE_URL_PLAIN" <file>.dump
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/booth-intel}"
mkdir -p "$BACKUP_DIR"

# DATABASE_URL in .env is SQLAlchemy style (postgresql+psycopg://); pg_dump wants postgresql://
url="$(grep -E '^DATABASE_URL=' "$APP_DIR/.env" | cut -d= -f2- | sed 's#^postgresql+psycopg://#postgresql://#')"
out="$BACKUP_DIR/booth_intel-$(date +%Y%m%d-%H%M%S).dump"
pg_dump --format=custom --no-owner --dbname="$url" --file="$out"
echo "backup written: $out ($(du -h "$out" | cut -f1))"

ls -1t "$BACKUP_DIR"/booth_intel-*.dump | tail -n +15 | xargs -r rm -f
