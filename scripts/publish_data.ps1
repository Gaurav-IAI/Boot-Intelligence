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
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\publish_data.ps1 -State Telangana
  (writes deploy\tg_data.sqlite3.gz)
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\publish_data.ps1 -State Uttarakhand
  (writes data\exports\uk_data.sqlite3.gz and does NOT commit it: Uttarakhand's export holds
  elector records from the 2003 roll, and the repository is public. Hand the file to the
  server team privately; see docs/DEPLOYMENT.md, "Uttarakhand data".)
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
$short = @{ "telangana" = "tg"; "uttarakhand" = "uk" }[$State.ToLower()]   # one-word names: a readable code
if (-not $short) { $short = -join $slug }
# A state whose export carries personal data (elector records) never goes into the public
# repository: its file is written outside git and handed to the server team privately.
$private = @("uttarakhand") -contains $State.ToLower()
$dir = if ($private) { "data\exports" } else { "deploy" }
$out = "$dir\" + $short + "_data.sqlite3.gz"                    # "Uttar Pradesh" -> deploy\up_data.sqlite3.gz
New-Item -ItemType Directory -Force $dir | Out-Null
$source = "sqlite:///" + ((Join-Path $root $Database) -replace '\\', '/')

Write-Host "1/2 exporting $State from $Database -> $out"
& $py -m app export-state --state $State --from $source --out $out
if ($LASTEXITCODE -ne 0) { throw "export-state failed" }

if ($private) {
    $mb = [math]::Round((Get-Item $out).Length / 1MB, 1)
    Write-Host ""
    Write-Host "wrote $out ($mb MB). Not committed: it holds elector records and the repository is public." -ForegroundColor Yellow
    Write-Host "Send it to the server team privately (not by GitHub), then on the server:"
    Write-Host "  sudo STATE_FILE=/root/$($short)_data.sqlite3.gz bash /var/www/Boot-Intelligence/scripts/deploy/update_server.sh"
    exit 0
}

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
