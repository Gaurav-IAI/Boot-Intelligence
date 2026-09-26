<#
.SYNOPSIS
  Publish the latest Uttar Pradesh data to GitHub for production, in one command.

.DESCRIPTION
  1. exports the state from this machine's full database (the SQLite file) to
     deploy\up_data.sqlite3.gz (~35 MB) with `python -m app export-state`
  2. commits that file and pushes the current branch

  On the server, `sudo bash /opt/booth-intel/scripts/deploy/update_existing.sh` then
  pulls the code and imports the file (only that state's rows are replaced).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\publish_data.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\publish_data.ps1 -NoPush
#>
param(
    [string]$State = "Uttar Pradesh",
    [string]$Database = "data\uk_election.sqlite3",
    [switch]$NoPush
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "virtualenv not found at $py" }
if (-not (Test-Path $Database)) { throw "$Database not found" }
$env:PYTHONPATH = "src"
$env:PYTHONIOENCODING = "utf-8"

$slug = ($State.ToLower() -replace '[^a-z]+', ' ').Trim().Split(' ') | ForEach-Object { $_[0] }
$out = "deploy\" + (-join $slug) + "_data.sqlite3.gz"          # "Uttar Pradesh" -> deploy\up_data.sqlite3.gz
New-Item -ItemType Directory -Force deploy | Out-Null
$source = "sqlite:///" + ((Join-Path $root $Database) -replace '\\', '/')

Write-Host "1/2 exporting $State from $Database -> $out"
& $py -m app export-state --state $State --from $source --out $out
if ($LASTEXITCODE -ne 0) { throw "export-state failed" }

Write-Host "2/2 committing $out"
git add -f $out
git diff --cached --quiet -- $out
if ($LASTEXITCODE -eq 0) {
    Write-Host "no change since the last published file - nothing to commit" -ForegroundColor Yellow
    exit 0
}
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -m "Update $State data export ($stamp)" -- $out
if ($LASTEXITCODE -ne 0) { throw "git commit failed" }
if ($NoPush) { Write-Host "committed; not pushed (-NoPush)"; exit 0 }
git push
if ($LASTEXITCODE -ne 0) { throw "git push failed" }

$branch = git branch --show-current
Write-Host ""
Write-Host "published on branch '$branch'." -ForegroundColor Green
if ($branch -ne "main") { Write-Host "merge it into main (pull request), then on the server:" }
else { Write-Host "on the server:" }
Write-Host "  sudo bash /opt/booth-intel/scripts/deploy/update_existing.sh"
