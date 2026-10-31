#!/usr/bin/env bash
# Preview the latest version on your own Ubuntu PC before it goes to the server.
#
# For a checkout that already runs (set up from an earlier version): it brings the
# database up to date with the new code and the latest data, checks the result, and
# starts the dashboard. No downloads from election websites, no sudo.
#
#   1. Python: the checkout's .venv (or the `booth` conda env); packages updated
#   2. database: whichever the app uses (.env: PostgreSQL, or its SQLite fallback);
#      new tables/columns are added automatically, nothing is removed
#   3. backup of the current data -> data/processed/backups/ (restorable, see the end)
#   4. data: deploy/*.sqlite3.gz (Uttar Pradesh, Telangana; they come with git pull)
#      and the Uttarakhand file you were sent privately. Each import replaces only
#      that state's rows, in one transaction
#   5. checks: what each state holds, and that nothing is left for the pipeline to
#      process (so the server will not process it again either)
#   6. starts the dashboard at http://127.0.0.1:$PORT (Ctrl+C to stop)
#
# Usage, from the checkout (after `git pull` on main):
#   bash scripts/deploy/preview_local.sh ~/Downloads/uk_data.sqlite3.gz
#   PORT=8001 bash scripts/deploy/preview_local.sh ~/uk_data.sqlite3.gz
#   NO_SERVE=1 bash scripts/deploy/preview_local.sh ~/uk_data.sqlite3.gz   # load + check only
#
# Without an argument the Uttarakhand file is looked for in data/exports/, ~/ and
# ~/Downloads/. Options: PY=/path/to/python, PORT (default 8000), NO_SERVE=1, NO_BACKUP=1.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
ROOT="$PWD"
PORT="${PORT:-8000}"
export PYTHONPATH=src PYTHONIOENCODING=utf-8

step() { echo -e "\n\033[1;34m==> $*\033[0m"; }
ok()   { echo -e "    \033[32m$*\033[0m"; }
warn() { echo -e "    \033[33m$*\033[0m"; }
die()  { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

[ -f src/app/__main__.py ] || die "run this from the project checkout"

# ------------------------------------------------------------------ 0. code
step "0/6 code"
branch="$(git rev-parse --abbrev-ref HEAD)"
echo "    branch $branch at $(git log --oneline -1)"
[ -f src/app/web/templates/results.html ] \
    || die "this checkout does not have the latest code yet: git checkout main && git pull"
[ -z "$(git status --porcelain --untracked-files=no)" ] || warn "you have local changes to tracked files"

# ------------------------------------------------------------------ 1. python
step "1/6 Python and packages"
if [ -z "${PY:-}" ]; then
    for p in .venv/bin/python venv/bin/python "$HOME/miniconda3/envs/booth/bin/python" \
             "$HOME/anaconda3/envs/booth/bin/python" "$HOME/miniforge3/envs/booth/bin/python"; do
        [ -x "$p" ] && { PY="$p"; break; }
    done
fi
if [ -z "${PY:-}" ]; then
    command -v python3 >/dev/null || die "python3 not found: sudo apt install python3 python3-venv"
    echo "    no virtual environment found: creating .venv"
    python3 -m venv .venv || die "could not create .venv: sudo apt install python3-venv"
    PY=.venv/bin/python
fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
    || die "$PY is older than Python 3.11"
"$PY" -m pip install -q --disable-pip-version-check -r requirements.txt || die "pip install failed"
ok "$PY ($("$PY" -V 2>&1)), requirements installed"

# ------------------------------------------------------------------ 2. database
step "2/6 database"
[ -f .env ] || { cp .env.example .env; warn "no .env: copied .env.example"; }
backend() { "$PY" -c 'from app.database.session import init_db; print(init_db())' 2>/dev/null | tail -1; }
BACKEND="$(backend)" || true
[ -n "$BACKEND" ] || die "the database cannot be opened; run: PYTHONPATH=src $PY -m app quality"
if [[ "$BACKEND" == *FALLBACK* ]] && [ -f docker-compose.yml ] && command -v docker >/dev/null; then
    echo "    PostgreSQL is not reachable: trying docker compose up -d"
    if docker compose up -d >/dev/null 2>&1 || docker-compose up -d >/dev/null 2>&1; then
        for _ in $(seq 1 20); do
            BACKEND="$(backend)"; [[ "$BACKEND" != *FALLBACK* ]] && break; sleep 2
        done
    fi
fi
DB_URL="$("$PY" -c 'from app.database.session import get_engine; print(get_engine().url.render_as_string(hide_password=False))' 2>/dev/null | tail -1)"
SHOWN="$("$PY" -c 'from app.database.session import get_engine; print(get_engine().url.render_as_string(hide_password=True))' 2>/dev/null | tail -1)"
ok "using: $SHOWN"
[[ "$BACKEND" == *FALLBACK* ]] && warn "PostgreSQL was not reachable, so the SQLite file data/uk_election.sqlite3 is used.
    That is fine for a preview; the dashboard reads the same database."

# the states this database holds now
STATES="$("$PY" - <<'EOF' 2>/dev/null | tail -1
from sqlalchemy import select
from app.database.session import get_session
from app.database.models import State
with get_session() as db:
    print("|".join(db.scalars(select(State.state_name).order_by(State.state_name))))
EOF
)"
echo "    states before: ${STATES:-none}"

# ------------------------------------------------------------------ 3. backup
step "3/6 backup"
if [ "${NO_BACKUP:-0}" = 1 ] || [ -z "$STATES" ]; then
    echo "    skipped"
else
    mkdir -p data/processed/backups
    BACKUP="data/processed/backups/before-preview-$(date +%Y%m%d-%H%M%S).sqlite3.gz"
    args=(); IFS='|' read -ra names <<<"$STATES"; for s in "${names[@]}"; do args+=(--state "$s"); done
    "$PY" -m app export-state "${args[@]}" --from "$DB_URL" --out "$BACKUP" >/dev/null \
        || die "backup failed; nothing was changed"
    ok "$BACKUP"
fi

# ------------------------------------------------------------------ 4. data
step "4/6 data"
UK_FILE="${1:-${UK_FILE:-}}"
if [ -z "$UK_FILE" ]; then
    for f in data/exports/uk_data.sqlite3.gz "$HOME/uk_data.sqlite3.gz" "$HOME/Downloads/uk_data.sqlite3.gz"; do
        [ -f "$f" ] && { UK_FILE="$f"; break; }
    done
fi
[ -n "$UK_FILE" ] && [ -f "$UK_FILE" ] || die "Uttarakhand file not found.
    Pass the file you were sent:  bash scripts/deploy/preview_local.sh /path/to/uk_data.sqlite3.gz"
gzip -t "$UK_FILE" 2>/dev/null || die "$UK_FILE is not a complete .gz file (download interrupted?)"
# keep a copy inside the checkout; data/exports/ is ignored by git, so it is never committed
mkdir -p data/exports
[ "$(realpath "$UK_FILE")" = "$(realpath -m data/exports/uk_data.sqlite3.gz)" ] \
    || cp "$UK_FILE" data/exports/uk_data.sqlite3.gz

shopt -s nullglob
for f in deploy/*.sqlite3.gz data/exports/uk_data.sqlite3.gz; do
    printf '    %-34s ' "$(basename "$f")"
    out="$("$PY" -m app import-state --file "$f" 2>&1)" || { echo; echo "$out" | tail -15; die "import of $f failed; that state is unchanged (one transaction)"; }
    echo "$out" | grep -o 'imported .*' | tail -1
done
shopt -u nullglob

# ------------------------------------------------------------------ 5. checks
step "5/6 checks"
"$PY" - <<'EOF' 2>/dev/null || warn "the summary could not be computed (the dashboard may still work)"
from sqlalchemy import func, select
from app.database.session import get_session
from app.database.models import AcResult, AssemblyConstituency, District, Election, ElectionResult, PollingStation, State
with get_session() as db:
    for st in db.scalars(select(State).order_by(State.state_name)):
        acs = db.scalar(select(func.count()).select_from(AssemblyConstituency).join(District)
                        .where(District.state_id == st.id, AssemblyConstituency.delimitation == "current"))
        ps = db.scalar(select(func.count()).select_from(PollingStation).join(AssemblyConstituency).join(District)
                       .where(District.state_id == st.id, PollingStation.edition == "SIR-2026"))
        cy = sorted(db.scalars(select(Election.election_year).join(AcResult).where(Election.state == st.state_name).distinct()))
        by = sorted(db.scalars(select(Election.election_year).join(ElectionResult).where(Election.state == st.state_name).distinct()))
        print(f"    {st.state_name:<14} {acs:>4} constituencies · {ps:>6,} current polling stations · "
              f"constituency results {', '.join(map(str, cy)) or '—'} · booth results {', '.join(map(str, by)) or '—'}")
    ch = db.execute(select(func.count(), func.count(PollingStation.area_description))
                    .join(AssemblyConstituency).join(District).join(State)
                    .where(State.state_name == "Uttarakhand", AssemblyConstituency.ac_number == 55,
                           PollingStation.edition == "SIR-2026")).one()
    print(f"    Champawat (AC 55): {ch[1]} of {ch[0]} polling stations have 'Areas covered'")
EOF
echo
echo "    what the pipeline would still do (no network stage should say 'run'):"
"$PY" -m app pipeline --dry-run 2>/dev/null | sed -n '/Current state/,/^$/p' | sed 's/^/    /'

# ------------------------------------------------------------------ 6. dashboard
step "6/6 dashboard"
echo "    To undo this preview's data changes later:"
[ -n "${BACKUP:-}" ] && echo "      cd '$ROOT' && PYTHONPATH=src '$PY' -m app import-state --file $BACKUP" || echo "      (no backup was made)"
if [ "${NO_SERVE:-0}" = 1 ]; then
    echo "    NO_SERVE=1: start it yourself with"
    echo "      cd '$ROOT' && PYTHONPATH=src '$PY' -m app serve --skip-pipeline --port $PORT"
    exit 0
fi
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    die "port $PORT is in use (an old dashboard still running?). Stop it, or run with PORT=8001"
fi
cat <<EOF

    Open http://127.0.0.1:$PORT and check:
      - top menu has "Election Results"; Uttarakhand 2022: BJP won 47 of 70 seats
      - a constituency page (e.g. Raipur) has year tabs 2012 | 2017 | 2022
      - Champawat (AC 55): "Areas covered" filled for its polling stations
      - State menu -> Telangana (INC 64 of 119 in 2023) and Uttar Pradesh still load
    Ctrl+C stops the dashboard.
EOF
exec "$PY" -m app serve --skip-pipeline --port "$PORT"
