<#
.SYNOPSIS
  Bundle this machine's downloaded sources and loaded database for a server, so the
  server needs no downloads and no re-parsing.

.DESCRIPTION
  Creates
    booth-data.tar.gz   data/raw/ (every downloaded official document) and the loaded
                        database as data/uk_election.sqlite3, which setup_server.sh copies
                        into the server's PostgreSQL with `python -m app migrate-db`
    booth-code.tar.gz   the code only (no .venv, data, .git or .env)

  The database bundled is the SQLite file data\uk_election.sqlite3, as it is: this
  script never modifies it. Its row counts are printed so you can see what is shipped.
  With -FromPostgres, the local PostgreSQL (DATABASE_URL) is exported instead, into
  data\_bundle\, and bundled under the same name.

  booth-data.tar.gz is too large for GitHub: share it by another route (drive link,
  scp) and give the link to whoever runs setup_server.sh (DATA_BUNDLE_URL=...).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\pack_data.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\pack_data.ps1 -FromPostgres
#>
param(
    [string]$Out = "booth-data.tar.gz",
    [switch]$FromPostgres
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "virtualenv not found at $py" }
$env:PYTHONPATH = "src"

function Show-Counts([string]$file) {
    & $py -c "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True); print('   ' + ', '.join('%s=%s' % (t, format(c.execute('select count(*) from '+t).fetchone()[0], ',')) for t in ('election_results','polling_stations','electors','source_fetches')))" $file
}

$sqlite = "data\uk_election.sqlite3"
$bundleDb = $sqlite
if ($FromPostgres) {
    # exported into a staging folder under the final name; the main SQLite file is not touched
    $bundleDb = "data\_bundle\data\uk_election.sqlite3"
    New-Item -ItemType Directory -Force (Split-Path $bundleDb) | Out-Null
    if (Test-Path $bundleDb) { Remove-Item $bundleDb }
    $target = "sqlite:///" + ((Join-Path $root $bundleDb) -replace '\\', '/')
    $source = & $py -c "from app.config import settings; print(settings.database_url)"
    Write-Host "exporting PostgreSQL ($($source.Split('@')[-1])) into $bundleDb ..."
    & $py -m app migrate-db --from $source --to $target
    if ($LASTEXITCODE -ne 0) { throw "migrate-db failed" }
}
if (-not (Test-Path $bundleDb)) { throw "$bundleDb not found - nothing to bundle" }
if (-not (Test-Path "data\raw")) { throw "data\raw not found" }

Write-Host "database to bundle: $bundleDb"
Show-Counts $bundleDb
$raw = (Get-ChildItem data\raw -Recurse -File | Measure-Object Length -Sum).Sum / 1GB
Write-Host ("bundling data\raw ({0:N2} GB) and the database -> {1}" -f $raw, $Out)

# Windows 10+ ships bsdtar as tar.exe; the database is always stored as data/uk_election.sqlite3
if ($FromPostgres) {
    tar -czf $Out data/raw -C data/_bundle data/uk_election.sqlite3
} else {
    tar -czf $Out data/raw data/uk_election.sqlite3
}
if ($LASTEXITCODE -ne 0) { throw "tar failed" }
Write-Host ("written {0} ({1:N2} GB)" -f (Resolve-Path $Out), ((Get-Item $Out).Length / 1GB)) -ForegroundColor Green

# the code, without the Windows virtualenv, data, caches or secrets (.env is written on the server)
$code = "booth-code.tar.gz"
tar -czf $code --exclude=.venv --exclude=data --exclude=.git --exclude=.env --exclude=__pycache__ `
    --exclude=.pytest_cache --exclude="*.tar.gz" --exclude=research/network --exclude=research/portal .
if ($LASTEXITCODE -ne 0) { throw "tar (code) failed" }
Write-Host ("written {0} ({1:N1} MB)" -f (Resolve-Path $code), ((Get-Item $code).Length / 1MB)) -ForegroundColor Green

Write-Host ""
Write-Host "next: share $Out with the server administrator (e.g. a drive link); on the server:"
Write-Host "  sudo REPO_URL=<github url> DATA_BUNDLE_URL=<link to $Out> bash scripts/deploy/setup_server.sh"
