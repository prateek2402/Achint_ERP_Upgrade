# Legacy launcher used by older scheduled tasks. Prefer scripts\server.ps1 start.
param(
    [string]$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)

$ErrorActionPreference = "Stop"
$serverScript = Join-Path $AppRoot "scripts\server.ps1"
if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "server.ps1 not found: $serverScript"
}

Set-Location $AppRoot
& $serverScript start -Network
