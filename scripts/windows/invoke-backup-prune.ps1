param(
    [string]$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$SecondaryTarget,
    [int]$DailyKeep = 7,
    [int]$WeeklyKeep = 4,
    [int]$MonthlyKeep = 12,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

if (-not $SecondaryTarget) {
    throw "SecondaryTarget is required. Example: D:\ERP_Backups or \\NAS\AchintERP"
}

function Get-EnvValue {
    param(
        [string]$EnvPath,
        [string]$Name,
        [string]$DefaultValue = ""
    )
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        return $DefaultValue
    }

    foreach ($line in Get-Content -LiteralPath $EnvPath) {
        if (-not $line -or $line.Trim().StartsWith("#")) {
            continue
        }
        $parts = $line.Split("=", 2)
        if ($parts.Count -eq 2 -and $parts[0].Trim() -eq $Name) {
            return $parts[1].Trim().Trim("'`"")
        }
    }
    return $DefaultValue
}

function Invoke-SafeRemove {
    param([string]$PathToRemove, [switch]$DryRun)
    if ($DryRun) {
        Write-Host "[WhatIf] Remove $PathToRemove"
        return
    }
    Remove-Item -LiteralPath $PathToRemove -Force
}

function Select-RetentionSet {
    param(
        [System.IO.FileInfo[]]$Files,
        [int]$KeepDaily,
        [int]$KeepWeekly,
        [int]$KeepMonthly
    )

    $keep = New-Object 'System.Collections.Generic.HashSet[string]'
    $sorted = $Files | Sort-Object LastWriteTimeUtc -Descending

    foreach ($f in ($sorted | Select-Object -First $KeepDaily)) {
        [void]$keep.Add($f.FullName)
    }

    $weeklyGroups = @{}
    foreach ($f in $sorted) {
        $calendar = [System.Globalization.CultureInfo]::InvariantCulture.Calendar
        $weekRule = [System.Globalization.CalendarWeekRule]::FirstFourDayWeek
        $dayOfWeek = [DayOfWeek]::Monday
        $week = $calendar.GetWeekOfYear($f.LastWriteTimeUtc, $weekRule, $dayOfWeek)
        $key = "{0}-W{1:00}" -f $f.LastWriteTimeUtc.Year, $week
        if (-not $weeklyGroups.ContainsKey($key)) {
            $weeklyGroups[$key] = $f
        }
    }
    foreach ($f in ($weeklyGroups.Values | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First $KeepWeekly)) {
        [void]$keep.Add($f.FullName)
    }

    $monthlyGroups = @{}
    foreach ($f in $sorted) {
        $key = "{0}-{1:00}" -f $f.LastWriteTimeUtc.Year, $f.LastWriteTimeUtc.Month
        if (-not $monthlyGroups.ContainsKey($key)) {
            $monthlyGroups[$key] = $f
        }
    }
    foreach ($f in ($monthlyGroups.Values | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First $KeepMonthly)) {
        [void]$keep.Add($f.FullName)
    }

    return $keep
}

$envPath = Join-Path $AppRoot ".env"
$backupDirRaw = Get-EnvValue -EnvPath $envPath -Name "DB_BACKUP_DIR" -DefaultValue (Join-Path $AppRoot "db_backups")
if ([System.IO.Path]::IsPathRooted($backupDirRaw)) {
    $localBackupDir = $backupDirRaw
}
else {
    $localBackupDir = Join-Path $AppRoot $backupDirRaw
}

$dbFile = Join-Path $AppRoot "erp_database.sqlite"
$secondarySnapshots = Join-Path $SecondaryTarget "snapshots"

if (-not (Test-Path -LiteralPath $secondarySnapshots)) {
    New-Item -ItemType Directory -Path $secondarySnapshots -Force | Out-Null
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
if (Test-Path -LiteralPath $dbFile) {
    $dbTarget = Join-Path $secondarySnapshots ("erp_database_" + $stamp + ".sqlite")
    Copy-Item -LiteralPath $dbFile -Destination $dbTarget -Force
    Write-Host "Copied live database to $dbTarget"
}
else {
    Write-Warning "Live DB not found at $dbFile"
}

if (Test-Path -LiteralPath $localBackupDir) {
    $files = Get-ChildItem -LiteralPath $localBackupDir -File | Sort-Object LastWriteTimeUtc -Descending
    foreach ($f in $files) {
        $dest = Join-Path $secondarySnapshots $f.Name
        Copy-Item -LiteralPath $f.FullName -Destination $dest -Force
    }
    Write-Host ("Synced {0} file(s) from {1}" -f $files.Count, $localBackupDir)
}
else {
    Write-Warning "Local backup directory not found: $localBackupDir"
}

$snapshotFiles = Get-ChildItem -LiteralPath $secondarySnapshots -File -Filter "*.sqlite" | Sort-Object LastWriteTimeUtc -Descending
$keepSet = Select-RetentionSet -Files $snapshotFiles -KeepDaily $DailyKeep -KeepWeekly $WeeklyKeep -KeepMonthly $MonthlyKeep

$deleted = 0
foreach ($file in $snapshotFiles) {
    if (-not $keepSet.Contains($file.FullName)) {
        Invoke-SafeRemove -PathToRemove $file.FullName -DryRun:$WhatIf
        $deleted += 1
    }
}

Write-Host ("Retention complete. Total snapshots: {0}, deleted: {1}, kept: {2}" -f $snapshotFiles.Count, $deleted, ($snapshotFiles.Count - $deleted))
