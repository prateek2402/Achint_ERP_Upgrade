# Achint ERP — start / stop / status for the uvicorn server (Windows).
#
# Usage:
#   .\scripts\server.ps1 start              # background, host from .env (default 127.0.0.1)
#   .\scripts\server.ps1 start -Network     # background, LAN (0.0.0.0)
#   .\scripts\server.ps1 start -Dev         # foreground with hot reload
#   .\scripts\server.ps1 stop
#   .\scripts\server.ps1 restart [-Network]
#   .\scripts\server.ps1 status
#   .\scripts\server.ps1 install-startup   # Windows boot auto-start (Admin)
#   .\scripts\server.ps1 remove-startup
#
# Shortcuts: server.cmd, scripts\server-start.cmd, scripts\server-stop.cmd

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'install-startup', 'remove-startup')]
    [string]$Command = 'status',

    [switch]$Network,
    [switch]$Local,
    [switch]$Dev,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$LogDir = Join-Path $RepoRoot 'logs'
$PidFile = Join-Path $LogDir 'server.pid'
$OutLog = Join-Path $LogDir 'server.out.log'
$ErrLog = Join-Path $LogDir 'server.err.log'

function Get-PythonExe {
    foreach ($rel in @('venv\Scripts\python.exe', '.venv\Scripts\python.exe')) {
        $p = Join-Path $RepoRoot $rel
        if (Test-Path -LiteralPath $p) { return $p }
    }
    throw "Python venv not found. Create it: python -m venv venv; then pip install -r requirements.txt"
}

function Read-ServerEnv {
    $hostBind = '127.0.0.1'
    $port = 3000
    $envFile = Join-Path $RepoRoot '.env'
    if (Test-Path -LiteralPath $envFile) {
        Get-Content -LiteralPath $envFile | ForEach-Object {
            $line = $_.Trim()
            if (-not $line -or $line.StartsWith('#') -or $line -notmatch '=') { return }
            $key, $value = $line.Split('=', 2)
            $key = $key.Trim()
            $value = $value.Trim().Trim('"').Trim("'")
            if ($key -eq 'HOST' -and $value) { $hostBind = $value }
            if ($key -eq 'PORT' -and $value) { $port = [int]$value }
        }
    }
    if ($env:ACHINT_PORT) { $port = [int]$env:ACHINT_PORT }
    [PSCustomObject]@{ Host = $hostBind; Port = $port }
}

function Get-ListenerOnPort([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $conn) { return $null }
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    [PSCustomObject]@{
        Port = $Port
        Pid  = [int]$conn.OwningProcess
        Name = if ($proc) { $proc.ProcessName } else { 'unknown' }
    }
}

function Test-ServerHttp([string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 4
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-StoredPid {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    if ($raw -match '^\d+$') { return [int]$raw }
    return $null
}

function Test-ProcessAlive([int]$ProcessId) {
    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Show-Urls([string]$HostBind, [int]$Port) {
    Write-Host ''
    Write-Host "  This PC:  http://localhost:$Port/" -ForegroundColor Green
    if ($HostBind -eq '0.0.0.0') {
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
            ForEach-Object {
                Write-Host "  LAN:      http://$($_.IPAddress):$Port/" -ForegroundColor Cyan
            }
        Write-Host ''
        Write-Host '  Firewall (once, Admin): .\scripts\windows\enable-network-firewall.cmd' -ForegroundColor DarkGray
    }
    Write-Host ''
}

function Invoke-ServerStatus {
    $cfg = Read-ServerEnv
    $listener = Get-ListenerOnPort $cfg.Port
    $storedPid = Get-StoredPid
    $url = "http://127.0.0.1:$($cfg.Port)/"

    Write-Host ''
    Write-Host '=== Achint ERP server ===' -ForegroundColor White
    if ($listener) {
        $alive = Test-ServerHttp $url
        $health = if ($alive) { 'responding' } else { 'port open (app may still be starting)' }
        Write-Host "  Status:   RUNNING ($health)" -ForegroundColor Green
        Write-Host "  PID:      $($listener.Pid) ($($listener.Name))"
        Write-Host "  Port:     $($cfg.Port)"
        if ($storedPid -and $storedPid -ne $listener.Pid) {
            Write-Host "  Note:     PID file $storedPid differs from listener - run: .\scripts\server.ps1 stop" -ForegroundColor Yellow
        }
        $bind = (Get-NetTCPConnection -LocalPort $cfg.Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1).LocalAddress
        Write-Host "  Bind:     $bind"
        Show-Urls $(if ($bind -eq '0.0.0.0') { '0.0.0.0' } else { '127.0.0.1' }) $cfg.Port
        Write-Host "  Logs:     $OutLog"
        return
    }

    Write-Host '  Status:   STOPPED' -ForegroundColor DarkYellow
    Write-Host "  Port:     $($cfg.Port) (not listening)"
    if ($storedPid) {
        Write-Host "  Stale PID file: $storedPid" -ForegroundColor DarkGray
    }
    Write-Host ''
    Write-Host '  Start:    .\server.cmd start' -ForegroundColor DarkGray
    Write-Host '  Network:  .\server.cmd start -Network' -ForegroundColor DarkGray
    Write-Host ''
}

function Stop-ServerProcess {
    $cfg = Read-ServerEnv
    $stopped = $false
    $storedPid = Get-StoredPid

    $listener = Get-ListenerOnPort $cfg.Port
    if ($listener) {
        Stop-Process -Id $listener.Pid -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped listener on port $($cfg.Port) (PID $($listener.Pid))"
        $stopped = $true
    }

    if ($storedPid -and (Test-ProcessAlive $storedPid) -and (-not $listener -or $storedPid -ne $listener.Pid)) {
        Stop-Process -Id $storedPid -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped parent process $storedPid"
        $stopped = $true
    }

    if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }

    Start-Sleep -Milliseconds 400
    if (Get-ListenerOnPort $cfg.Port) {
        if (-not $Force) {
            Write-Error "Port $($cfg.Port) is still in use. Try: .\scripts\server.ps1 stop -Force"
        }
        $listener = Get-ListenerOnPort $cfg.Port
        Stop-Process -Id $listener.Pid -Force
        Write-Host "Force-stopped PID $($listener.Pid)"
    }

    if (-not $stopped -and -not (Get-ListenerOnPort $cfg.Port)) {
        Write-Host 'Server was not running.'
    } else {
        Write-Host 'Server stopped.'
    }
}

function Start-ServerProcess {
    $cfg = Read-ServerEnv
    $hostBind = $cfg.Host
    if ($Network) { $hostBind = '0.0.0.0' }
    if (-not $Network -and -not $Dev -and $hostBind -eq '0.0.0.0') {
        # .env requests LAN bind
        $Network = $true
    }

    if ($Dev) {
        $hostBind = '127.0.0.1'
    }

    $listener = Get-ListenerOnPort $cfg.Port
    if ($listener) {
        Write-Host "Port $($cfg.Port) is already in use (PID $($listener.Pid))." -ForegroundColor Yellow
        Invoke-ServerStatus
        exit 1
    }

    $python = Get-PythonExe
    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }

    if ($Dev) {
        Write-Host 'Dev mode (foreground, hot reload). Press Ctrl+C to stop.' -ForegroundColor Cyan
        Show-Urls $hostBind $cfg.Port
        $devScript = Join-Path $RepoRoot 'scripts\run-dev.ps1'
        if (Test-Path -LiteralPath $devScript) {
            & $devScript
            return
        }
        Push-Location $RepoRoot
        try {
            & $python -m uvicorn main:app --host $hostBind --port $cfg.Port --reload --reload-delay 1.0
        } finally {
            Pop-Location
        }
        return
    }

    $uvicornArgs = @('-m', 'uvicorn', 'main:app', '--host', $hostBind, '--port', "$($cfg.Port)")
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "[$stamp] Starting on ${hostBind}:$($cfg.Port)" | Out-File -FilePath $OutLog -Encoding utf8 -Append

    $proc = Start-Process -FilePath $python `
        -ArgumentList $uvicornArgs `
        -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -PassThru

    $proc.Id | Set-Content -LiteralPath $PidFile -Encoding ascii -NoNewline

    $deadline = (Get-Date).AddSeconds(15)
    $ready = $false
    $listenerPid = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 300
        $ln = Get-ListenerOnPort $cfg.Port
        if ($ln) { $listenerPid = $ln.Pid }
        if (Test-ServerHttp "http://127.0.0.1:$($cfg.Port)/") {
            $ready = $true
            break
        }
        if ($proc.HasExited -and -not $listenerPid) {
            Write-Error "Server exited during startup. See $ErrLog and $OutLog"
        }
    }
    Start-Sleep -Milliseconds 500
    $lnFinal = Get-ListenerOnPort $cfg.Port
    if ($lnFinal) {
        $listenerPid = $lnFinal.Pid
        $listenerPid | Set-Content -LiteralPath $PidFile -Encoding ascii -NoNewline
    }

    $modeLabel = if ($hostBind -eq '0.0.0.0') { 'network' } else { 'local' }
    $reportPid = if ($listenerPid) { $listenerPid } else { $proc.Id }
    Write-Host ('Server started ({0}, PID {1}).' -f $modeLabel, $reportPid) -ForegroundColor Green
    if (-not $ready) {
        Write-Host 'Startup is slow; check status in a few seconds.' -ForegroundColor Yellow
    }
    Show-Urls $hostBind $cfg.Port
    Write-Host "  Stop:     .\server.cmd stop"
    Write-Host "  Status:   .\server.cmd status"
    Write-Host "  Logs:     $OutLog"
}

function Invoke-InstallStartup {
    $regScript = Join-Path $RepoRoot 'scripts\windows\register-startup-task.ps1'
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    $regArgs = @('-AppRoot', $RepoRoot)
    if ($Network) { $regArgs += '-Network' }
    if ($Local) { $regArgs += '-Local' }
    if (-not $isAdmin) {
        $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $regScript) + $regArgs
        Start-Process powershell -Verb RunAs -ArgumentList $argList | Out-Null
        Write-Host 'Opening elevated window to register startup task...'
        return
    }
    & $regScript @regArgs
}

function Invoke-RemoveStartup {
    $unregScript = Join-Path $RepoRoot 'scripts\windows\unregister-startup-task.ps1'
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Start-Process powershell -Verb RunAs -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $unregScript
        ) | Out-Null
        Write-Host 'Opening elevated window to remove startup task...'
        return
    }
    & $unregScript
}

Set-Location $RepoRoot
switch ($Command) {
    'start'   { Start-ServerProcess }
    'stop'    { Stop-ServerProcess }
    'restart' {
        Stop-ServerProcess
        Start-Sleep -Seconds 1
        Start-ServerProcess
    }
    'status'  { Invoke-ServerStatus }
    'install-startup' { Invoke-InstallStartup }
    'remove-startup'  { Invoke-RemoveStartup }
    default   { Invoke-ServerStatus }
}
