param(
    [string]$TaskName = "AchintERP-AutoStart"
)

$ErrorActionPreference = "Stop"
$registerScript = Join-Path $PSScriptRoot "register-startup-task.ps1"
& $registerScript -TaskName $TaskName -Unregister
