param(
    [string]$TaskName = "AchintERP-AutoStart",
    [string]$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [int]$DelaySeconds = 45
)

$ErrorActionPreference = "Stop"

$launcherScript = Join-Path $AppRoot "scripts\windows\start-achinterp.ps1"
if (-not (Test-Path -LiteralPath $launcherScript)) {
    throw "Launcher script not found: $launcherScript"
}

$delayDuration = [TimeSpan]::FromSeconds([Math]::Max(0, $DelaySeconds))
$argList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", ('"{0}"' -f $launcherScript),
    "-AppRoot", ('"{0}"' -f $AppRoot)
) -join " "

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argList -WorkingDirectory $AppRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = $delayDuration

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null
Write-Host "Registered startup task '$TaskName' for AchintERP."
Write-Host "Validate with: Get-ScheduledTask -TaskName $TaskName | Format-List *"
