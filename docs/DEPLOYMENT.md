# Deployment on a server (VPS)

How to run the Booth Intelligence dashboard on an Ubuntu VPS, with all the data
already loaded on your workstation moved across, so the server neither downloads nor
re-parses anything. A single script does the setup, and running it again updates the
installation.

The application is one Python process (FastAPI + Uvicorn) plus a PostgreSQL
database. There is no build step, no front-end toolchain and no message queue.

---

## Updating the existing deployment (Uttarakhand-only version → multi-state with UP)

If the dashboard already runs on the server (deployed from the first version), **do not
run `setup_server.sh`**. It is meant for fresh servers and would also reconfigure nginx,
the firewall and PostgreSQL. Use the update script instead. It changes only the code
and adds data:

**Recommended: import the Uttar Pradesh export (~35 MB).** The data owner exports UP on
the workstation:

```powershell
python -m app export-state --state "Uttar Pradesh" --from "sqlite:///data/uk_election.sqlite3" --out up_data.sqlite3.gz
```

and sends `up_data.sqlite3.gz` (by email, a drive link or scp). On the server:

```bash
cd /opt/booth-intel                      # the deployed git checkout
git fetch origin && git checkout main    # or the pull-request branch, until it is merged
sudo APP_DIR=/opt/booth-intel BRANCH=main STATE_FILE=/root/up_data.sqlite3.gz \
     bash scripts/deploy/update_existing.sh
```

In order, it:
1. backs up the database (`/var/backups/booth-intel/before-update-*`);
2. checks out `BRANCH` and installs the two new Python packages;
3. adds the new database columns (automatic, nullable, nothing removed);
4. imports the file with `python -m app import-state`. Only Uttar Pradesh's rows are
   added (or, on a repeat, replaced). Uttarakhand and everything else stays as it is.
   There are no downloads and no parsing. It runs in one transaction: it either fully
   succeeds or changes nothing. Tested against PostgreSQL: 461,498 rows in about 40
   seconds;
5. restarts the `booth-intel` service.

**Alternative: load UP from the official sources on the server.** Leave out `STATE_FILE`.
The script then discovers the UP and Telangana constituencies (ECI) and loads the UP
Form 20 results and polling stations itself. That takes several hours, or about an hour
with `DATA_BUNDLE=` (the 2.4 GB source files, parsed instead of downloaded).

Notes:
- Use `BRANCH=feature/up-results-stations-deploy` if the pull request isn't merged yet.
- Pass `SERVICE=<name>` if the systemd unit has another name.
- Without a bundle, step 5 downloads the UP sources, which takes several hours.
- If a step fails (for example a network outage), run the same command again: every
  step resumes where it stopped.
- Progress is in `data/processed/update-*.log`.

To keep the data fresh, add the weekly refresh and nightly backup timers from section 6
(`booth-intel-refresh`, `booth-intel-backup`).

---

## 0. Quick start for the server administrator (fresh server)

You need the repository URL and, from the data owner, a **direct-download link to
`booth-data.tar.gz`**. The bundle holds the loaded data (~2–3 GB) and is too large for
GitHub. On a fresh Ubuntu 22.04/24.04 VPS, as root:

```bash
git clone https://github.com/Gaurav-IAI/Boot-Intelligence.git /root/booth-intel-src
cd /root/booth-intel-src
sudo REPO_URL=https://github.com/Gaurav-IAI/Boot-Intelligence.git BRANCH=main \
     DATA_BUNDLE_URL='<direct link to booth-data.tar.gz>' \
     DOMAIN=booth.example.org CONTACT_EMAIL=admin@example.org \
     bash scripts/deploy/setup_server.sh
```

- **The repository is private?** Clone with a deploy key or token and pass the same URL
  as `REPO_URL`.
- **The link opens a web page instead of downloading?** Use a direct-download link, for
  example Dropbox `?dl=1`, a OneDrive download link, or S3/GCS. You can also copy the
  file to the server by hand and pass `DATA_BUNDLE=/root/booth-data.tar.gz` instead.
- **No bundle?** Leave both `DATA_BUNDLE*` variables out. The server then downloads
  everything from the official sources itself, which takes several hours.
- **No domain yet?** Leave out `DOMAIN`. The dashboard is then HTTP on the server's IP,
  which is fine only behind a VPN.

When it finishes, the script prints `HTTP 200; database backend: postgresql`, and the
dashboard login is in `/root/booth-intel-credentials.txt`. To update later, run
`git -C /root/booth-intel-src pull` and then the same command again.

**The data owner** makes the bundle on the workstation with
`powershell -ExecutionPolicy Bypass -File scripts\pack_data.ps1` (details in section 3),
uploads `booth-data.tar.gz` somewhere with a direct link, and sends the link.

---

## 1. What runs where

| Piece | What it is | On the server |
|---|---|---|
| Dashboard | `uvicorn app.web.app:app` | systemd service `booth-intel`, on 127.0.0.1:8000 |
| Front door | nginx with a login (HTTP basic auth), HTTPS via Let's Encrypt | port 80/443 |
| Database | PostgreSQL (Ubuntu package) | database `booth_intel`, local only |
| Data pipeline | `scripts/deploy/refresh_all.sh` | timer `booth-intel-refresh`, Sundays 02:00 |
| Backups | `scripts/deploy/backup.sh` (pg_dump) | timer `booth-intel-backup`, nightly 01:30, 14 kept |
| Source files | `data/raw/` — every downloaded official document | reused, never downloaded twice |

The pipeline is three commands, always in this order. `refresh_all.sh` runs them:

```bash
python -m app pipeline                          # ECI structure (3 states) + all Uttarakhand data
python -m app results --state "Uttar Pradesh"   # UP Form 20 booth results 2012/2017/2022
python -m app stations                          # UP polling stations, 75 district lists
```

Every step is resumable. Files already in `data/raw/` are reused, and work already
done (a constituency-year, a district list) is skipped. An interrupted run, for
example after a network outage, carries on the next time it starts.

---

## 2. Server requirements

| Need | Detail |
|---|---|
| OS | Ubuntu 22.04 or 24.04 |
| Size | 2 vCPU, 4 GB RAM, **30 GB+ disk** (`data/raw` is ~2.5 GB, the database ~1–2 GB) |
| Network in | 22 (SSH), 80/443 (nginx). The setup enables `ufw` with only these open. |
| Network out | HTTPS to `gateway-voters.eci.gov.in`, `election.uk.gov.in`, `ceouttarpradesh.nic.in`, `*.nic.in`, `cdn.s3waas.gov.in` (needed only for refreshes) |
| Optional | a domain name pointing at the server, for HTTPS |

---

## 3. Deploy without downloading anything again (recommended)

Everything this workstation has already downloaded and loaded moves to the server.

**On the workstation (Windows):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\pack_data.ps1
```

This writes two files:

- `booth-data.tar.gz`: `data/raw/` (all source documents) and the loaded database,
  about 2–3 GB. If the local data is in PostgreSQL rather than the SQLite file, the
  script copies it into SQLite for the bundle first.
- `booth-code.tar.gz`: the code only, a few MB. It leaves out the Windows `.venv`,
  `data/`, `.git` and `.env`.

**Upload both:**

```powershell
scp booth-data.tar.gz booth-code.tar.gz root@SERVER:/root/
```

(Instead of the code archive you can `git clone` the repository on the server, or
pass `REPO_URL=` to the setup.)

**On the server:**

```bash
mkdir -p /root/booth-intel-src && tar -xzf /root/booth-code.tar.gz -C /root/booth-intel-src
cd /root/booth-intel-src
sudo DATA_BUNDLE=/root/booth-data.tar.gz \
     DOMAIN=booth.example.org CONTACT_EMAIL=you@example.org \
     bash scripts/deploy/setup_server.sh
```

The script:
- unpacks `data/raw/`;
- copies the database into PostgreSQL with `python -m app migrate-db` (about 490,000 rows, under a minute);
- starts everything.

The dashboard login is written to `/root/booth-intel-credentials.txt`.

Leave out `DOMAIN` to serve plain HTTP on the server's IP. That's fine behind a VPN,
but not on the open internet.

## 4. Deploy from scratch (no bundle)

```bash
sudo CONTACT_EMAIL=you@example.org bash scripts/deploy/setup_server.sh
```

Without `DATA_BUNDLE`, the setup runs the full pipeline against the official
sources. This takes several hours, and the district sites are slow. Follow progress with
`tail -f /var/log/booth-intel/refresh-*.log`.

## 5. Setup options

| Variable | Meaning | Default |
|---|---|---|
| `DATA_BUNDLE` | bundle from `pack_data.ps1` | none (full pipeline) |
| `DOMAIN` | hostname for HTTPS (Let's Encrypt) | none (HTTP) |
| `CONTACT_EMAIL` | crawler User-Agent contact, Let's Encrypt account | none |
| `REPO_URL`, `BRANCH` | clone or update from git instead of the uploaded copy | uploaded copy, `main` |
| `APP_DIR` | install location | `/opt/booth-intel` |
| `WEB_USER` | dashboard login name (the password is generated) | `admin` |
| `WITH_OCR=1` | also install Tesseract + Hindi | off |
| `SKIP_DATA=1` | set up everything but load no data | off |

---

## 6. Day-to-day

```bash
sudo systemctl status booth-intel                  # dashboard
sudo systemctl start booth-intel-refresh           # refresh the data now
ls /var/log/booth-intel/                           # refresh logs (30 kept)
systemctl list-timers 'booth-intel*'               # next refresh / backup
sudo -u booth /opt/booth-intel/scripts/deploy/backup.sh   # backup now -> /var/backups/booth-intel
```

**Update the code:** upload the new code (or set `REPO_URL`) and re-run
`setup_server.sh`. It keeps `.env`, the database password, the login and `data/`.
Then it reinstalls requirements and restarts the service.

**Add a dashboard user:** `sudo htpasswd /etc/nginx/booth-intel.htpasswd NAME`

**Restore a backup:**

```bash
url=$(grep ^DATABASE_URL= /opt/booth-intel/.env | cut -d= -f2- | sed 's#+psycopg##')
pg_restore --clean --if-exists --no-owner -d "$url" /var/backups/booth-intel/booth_intel-<date>.dump
```

**Move data later:** `python -m app migrate-db --from <source-url> --to <target-url>`
copies every table. It refuses a non-empty target unless given `--replace`.

The dashboard needs no restart after a refresh. Its cached counts notice changed data
by themselves.

---

## 7. Configuration (`.env`)

`setup_server.sh` writes this file once (mode 600) and never overwrites it:

```ini
DATABASE_URL=postgresql+psycopg://booth:<generated>@localhost:5432/booth_intel
ALLOW_SQLITE_FALLBACK=false        # a server must never fall back to a SQLite file
HTTP_CONCURRENCY=3                 # politeness toward the official portals
HTTP_DELAY_SECONDS=0.4
HTTP_TIMEOUT=60
HTTP_RETRIES=4
USER_AGENT=booth-intelligence/0.1 (research; contact: you@example.org)
OCR_LANGUAGES=hin+eng
OCR_DPI=300
```

---

## 8. Security

> **The dashboard has no login of its own and shows elector-level data.** The setup
> therefore binds Uvicorn to 127.0.0.1, puts nginx with a password in front, opens only
> SSH and HTTP(S) in the firewall, and keeps PostgreSQL local-only. Do not remove the
> nginx login, and read [PRIVACY_AND_COMPLIANCE.md](PRIVACY_AND_COMPLIANCE.md) before
> giving anyone access.

---

## 9. Verifying the install

| Check | Expected |
|---|---|
| end of `setup_server.sh` | `HTTP 200; database backend: postgresql` |
| `sudo -u booth bash -c 'cd /opt/booth-intel && PYTHONPATH=src .venv/bin/python -m pytest -q'` | all pass |
| dashboard footer | backend **postgresql**, not `sqlite (FALLBACK …)` |
| Uttar Pradesh dashboard | 403 constituencies; Form 20 rows; current polling stations for the loaded districts |

## 10. Troubleshooting

**`database backend: sqlite (FALLBACK …)` or the service will not start.**
PostgreSQL is not reachable at `DATABASE_URL`. Check `systemctl status postgresql` and
`.env`. On a server `ALLOW_SQLITE_FALLBACK=false`, so this fails loudly instead of
serving an empty SQLite file.

**A refresh step failed.** Read the latest `/var/log/booth-intel/refresh-*.log`. A
network outage (`getaddrinfo failed`) needs nothing: the next run resumes. The later
steps still run when an earlier one fails.

**certbot failed.** The domain's DNS does not point at the server yet. Fix DNS and
re-run the setup.

**`ghaziabad.nic.in` answers "Blocked site".** That is expected. The code reads the
same page from the district's government S3WaaS host.

**TLS errors against `election.uk.gov.in`.** That host needs legacy TLS renegotiation,
which is enabled for that host only in `src/app/http_client.py`. A corporate proxy that
intercepts TLS can still break it.

---

## 11. Working on a Windows workstation

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
docker compose up -d            # PostgreSQL on host port 5433 (or let it fall back to SQLite)
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m app serve
```
