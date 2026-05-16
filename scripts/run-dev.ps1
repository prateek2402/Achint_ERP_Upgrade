# AchintERP local dev server with saner uvicorn reload on Windows.
#
# Why this exists:
# With `uvicorn --reload`, uvicorn terminates the worker using CTRL+BREAK-equivalent
# signalling on Windows. That raises KeyboardInterrupt while Python may still be
# importing uvicorn/stdlib (click/ipaddress/etc.), which prints noisy tracebacks.
# That is not an application bug --- it means the reload happened during startup.
#
# This script narrows reload triggers (!) excludes noisy directories and bumps
# --reload-delay so batch saves/tests do not cause reload storms.

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$py = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    Write-Error "venv not found at $py — create the venv first (see README Quick start)."
}

$testsDir = Join-Path $RepoRoot "tests"
$dbBackups = Join-Path $RepoRoot "db_backups"
$pytestCache = Join-Path $RepoRoot ".pytest_cache"
$alembicVersions = Join-Path $RepoRoot "alembic\versions"

$argsList = @(
    "-m", "uvicorn",
    "main:app",
    "--host", "127.0.0.1",
    "--port", "3000",
    "--reload",
    "--reload-delay", "1.0"
)

foreach ($d in @($testsDir, $dbBackups, $pytestCache, $alembicVersions)) {
    if (Test-Path -LiteralPath $d) {
        $argsList += @("--reload-exclude", $d)
    }
}

& $py @argsList
