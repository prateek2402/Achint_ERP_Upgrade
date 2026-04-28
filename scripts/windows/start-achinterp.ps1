param(
    [string]$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$LogDir = ""
)

$ErrorActionPreference = "Stop"

if (-not $LogDir) {
    $LogDir = Join-Path $AppRoot "logs"
}

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$venvCandidates = @(
    (Join-Path $AppRoot ".venv\Scripts\python.exe"),
    (Join-Path $AppRoot "venv\Scripts\python.exe")
)
$pythonExe = $null
foreach ($candidate in $venvCandidates) {
    if (Test-Path -LiteralPath $candidate) {
        $pythonExe = $candidate
        break
    }
}

if (-not $pythonExe) {
    throw "No virtual environment python executable found. Expected .venv\Scripts\python.exe or venv\Scripts\python.exe under $AppRoot."
}

$mainPy = Join-Path $AppRoot "main.py"
if (-not (Test-Path -LiteralPath $mainPy)) {
    throw "main.py was not found at $mainPy."
}

$datePart = Get-Date -Format "yyyyMMdd"
$logFile = Join-Path $LogDir ("app-" + $datePart + ".log")
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"[$stamp] Starting AchintERP with $pythonExe" | Out-File -FilePath $logFile -Encoding utf8 -Append

Push-Location $AppRoot
try {
    & $pythonExe $mainPy *>> $logFile
    $exitCode = $LASTEXITCODE
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$stamp] AchintERP process exited with code $exitCode." | Out-File -FilePath $logFile -Encoding utf8 -Append
    exit $exitCode
}
finally {
    Pop-Location
}
