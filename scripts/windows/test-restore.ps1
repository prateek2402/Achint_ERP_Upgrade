param(
    [string]$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$SecondaryTarget = "",
    [string]$RestoreWorkRoot = "",
    [switch]$SkipPythonCompileCheck
)

$ErrorActionPreference = "Stop"

if (-not $RestoreWorkRoot) {
    $RestoreWorkRoot = Join-Path $AppRoot "restore-tests"
}
if (-not (Test-Path -LiteralPath $RestoreWorkRoot)) {
    New-Item -ItemType Directory -Path $RestoreWorkRoot -Force | Out-Null
}

$candidateDirs = @()
if ($SecondaryTarget) {
    $candidateDirs += (Join-Path $SecondaryTarget "snapshots")
}
$candidateDirs += (Join-Path $AppRoot "db_backups")

$candidates = @()
foreach ($dir in $candidateDirs) {
    if (Test-Path -LiteralPath $dir) {
        $candidates += Get-ChildItem -LiteralPath $dir -File -Filter "*.sqlite" -ErrorAction SilentlyContinue
    }
}

if (-not $candidates -or $candidates.Count -eq 0) {
    throw "No .sqlite backup files found in candidate restore sources."
}

$latest = $candidates | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
$testStamp = Get-Date -Format "yyyyMMdd_HHmmss"
$testDir = Join-Path $RestoreWorkRoot ("restore_" + $testStamp)
New-Item -ItemType Directory -Path $testDir -Force | Out-Null

$restoredDbPath = Join-Path $testDir "erp_database.sqlite"
Copy-Item -LiteralPath $latest.FullName -Destination $restoredDbPath -Force
Write-Host "Copied backup for restore test: $($latest.FullName)"

$venvCandidates = @(
    (Join-Path $AppRoot ".venv\Scripts\python.exe"),
    (Join-Path $AppRoot "venv\Scripts\python.exe"),
    "python"
)
$pythonExe = $venvCandidates | Where-Object { $_ -eq "python" -or (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $pythonExe) {
    throw "Python executable not found."
}

$validationScript = @"
import sqlite3
import sys
db_path = r"$restoredDbPath"
conn = sqlite3.connect(db_path)
cur = conn.cursor()
tables = {"users", "clients", "purchase_orders", "invoices"}
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
existing = {row[0] for row in cur.fetchall()}
missing = sorted(tables - existing)
cur.execute("PRAGMA integrity_check")
integrity = cur.fetchone()[0]
conn.close()
if integrity != "ok":
    print(f"FAIL: integrity_check={integrity}")
    sys.exit(2)
if missing:
    print("FAIL: missing_tables=" + ",".join(missing))
    sys.exit(3)
print("PASS: integrity_check=ok and core tables present")
"@

$tmpPy = Join-Path $testDir "validate_restore.py"
Set-Content -LiteralPath $tmpPy -Value $validationScript -Encoding utf8

& $pythonExe $tmpPy
if ($LASTEXITCODE -ne 0) {
    throw "Restore validation failed."
}

if (-not $SkipPythonCompileCheck) {
    & $pythonExe -m py_compile (Join-Path $AppRoot "main.py")
    if ($LASTEXITCODE -ne 0) {
        throw "main.py compile check failed."
    }
}

$reportPath = Join-Path $testDir "restore_report.txt"
$report = @(
    "Restore test timestamp: $(Get-Date -Format s)",
    "Source backup: $($latest.FullName)",
    "Restored DB: $restoredDbPath",
    "Validation: PASS (integrity + core tables)",
    "Compile check: " + ($(if ($SkipPythonCompileCheck) { "SKIPPED" } else { "PASS" }))
)
$report | Set-Content -LiteralPath $reportPath -Encoding utf8
Write-Host "Restore test completed. Report: $reportPath"
