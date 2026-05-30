# Import clients, POs, invoices, and payments from old_erp.sqlite into erp_database.sqlite.
# Requires: venv + old_erp.sqlite (or pass -LegacyPath).
#
# Full replace (default): .\scripts\import-legacy.ps1 [-Force]
# Selective merge:         .\scripts\import-legacy.ps1 -Mode merge -Clients "Client A,Client B"
#                          .\scripts\import-legacy.ps1 -Mode merge -ClientsFile clients_to_import.txt

param(
    [string]$LegacyPath = "",
    [ValidateSet("replace", "merge")]
    [string]$Mode = "replace",
    [string]$Clients = "",
    [string]$ClientsFile = "",
    [switch]$Force,
    [switch]$DryRun,
    [switch]$ImportUsers,
    [switch]$ImportSettings
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$py = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    Write-Error "venv not found. Run: python -m venv venv; .\venv\Scripts\pip install -r requirements.txt"
}

$legacy = if ($LegacyPath) { $LegacyPath } else { Join-Path $RepoRoot "old_erp.sqlite" }
if (-not (Test-Path -LiteralPath $legacy)) {
    Write-Error @"
Legacy database not found: $legacy

Copy your previous ERP SQLite export to:
  $RepoRoot\old_erp.sqlite

Then re-run: .\scripts\import-legacy.ps1
"@
}

$argsList = @("migrate_sqlite.py", "--mode", $Mode)
if ($Force) { $argsList += "--force" }
if ($LegacyPath) { $argsList += @("--legacy-path", $LegacyPath) }
if ($Clients) { $argsList += @("--clients", $Clients) }
if ($ClientsFile) { $argsList += @("--clients-file", $ClientsFile) }
if ($DryRun) { $argsList += "--dry-run" }
if ($ImportUsers) { $argsList += "--import-users" }
if ($ImportSettings) { $argsList += "--import-settings" }

Write-Host "Importing from $legacy ..."
& $py @argsList
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Done. Restart the app if it is already running, then refresh the browser."
Write-Host "Import report: $RepoRoot\legacy_import_status.json"
