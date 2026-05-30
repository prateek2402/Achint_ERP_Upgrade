# Register or remove a Windows Scheduled Task to start Achint ERP at boot.
# Requires Administrator (use register-startup-task.cmd to elevate).
#
# Examples:
#   .\register-startup-task.ps1
#   .\register-startup-task.ps1 -DelaySeconds 60 -Network
#   .\register-startup-task.ps1 -Unregister

param(
    [string]$TaskName = "AchintERP-AutoStart",
    [string]$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [int]$DelaySeconds = 45,
    [switch]$Network,
    [switch]$Local,
    [switch]$Unregister,
    [switch]$RunAsSystem
)

$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
    throw "Run as Administrator. Use: scripts\windows\register-startup-task.cmd"
}

if ($Unregister) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    exit 0
}

$serverScript = Join-Path $AppRoot "scripts\server.ps1"
if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "server.ps1 not found: $serverScript"
}

$startArgs = if ($Network) { "start -Network" } else { "start" }

$psArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$serverScript`" $startArgs"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs -WorkingDirectory $AppRoot

$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = [TimeSpan]::FromSeconds([Math]::Max(0, $DelaySeconds))

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew

if ($RunAsSystem) {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
} else {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Highest
}

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Starts Achint ERP (uvicorn) after Windows boots. Managed by register-startup-task.ps1."

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host ""
Write-Host "Registered startup task: $TaskName" -ForegroundColor Green
Write-Host "  App root:     $AppRoot"
Write-Host "  Delay:        $DelaySeconds seconds after boot"
Write-Host "  Command:      powershell.exe $psArgs"
Write-Host "  Runs as:      $(if ($RunAsSystem) { 'SYSTEM' } else { $env:USERNAME })"
Write-Host ""
Write-Host "Test now (optional):  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Check status:         Get-ScheduledTask -TaskName '$TaskName' | Format-List State,TaskName"
Write-Host "After reboot:         server.cmd status"
Write-Host "Remove:               .\scripts\windows\unregister-startup-task.cmd"
Write-Host ""
