# AchintERP Windows 11 On-Prem Deployment Runbook

This runbook deploys AchintERP on one Windows 11 plant machine, with auto-start and layered backup/restore controls.

## 1) Runtime setup on fresh machine

1. Install Python 3.12+ and check "Add python.exe to PATH".
2. Copy this repository to a fixed path, recommended: `C:\AchintERP`.
3. Open an elevated PowerShell session:
   - `Set-ExecutionPolicy -Scope Process Bypass`
   - `cd C:\AchintERP`
   - `py -3.12 -m venv .venv`
   - `.\.venv\Scripts\python.exe -m pip install --upgrade pip`
   - `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
4. First startup test:
   - `.\scripts\windows\start-achinterp.ps1 -AppRoot C:\AchintERP`
5. Confirm app is reachable at [http://localhost:3000](http://localhost:3000).

## 2) Production environment hardening

1. Create `C:\AchintERP\.env` from `.env.production.template`.
2. Set a strong `JWT_SECRET_KEY` (64+ random characters).
3. Set `APP_ENV=production` and `ENABLE_API_DOCS=0`.
4. Set first-run admin bootstrap credentials, then clear `BOOTSTRAP_ADMIN_PASSWORD` after admin creation.
5. Keep `DB_BACKUP_DIR` on a persistent disk path, e.g. `C:\AchintERP\db_backups`.
6. Restrict file ACLs so only local admins/service account can read:
   - `C:\AchintERP\.env`
   - `C:\AchintERP\erp_database.sqlite`
   - backup folders.
7. Restrict firewall rule for TCP 3000 to required LAN only:
   - Example:
     `New-NetFirewallRule -DisplayName "AchintERP-3000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 3000 -RemoteAddress 192.168.1.0/24`

## 3) Auto-start after reboot

Run once as Administrator (from the repo root):

```cmd
server.cmd install-startup
```

Or double-click `scripts\windows\register-startup-task.cmd` (also elevates).

This registers scheduled task **AchintERP-AutoStart** (45s after boot) which runs `server.cmd start -Network`.

Remove auto-start: `server.cmd remove-startup` or `scripts\windows\unregister-startup-task.cmd`

Validation:
- `Get-ScheduledTask -TaskName AchintERP-AutoStart | Format-List State,TaskName,Actions,Triggers`
- Optional test without reboot: `Start-ScheduledTask -TaskName AchintERP-AutoStart` then `server.cmd status`
- Reboot machine and confirm app returns on `http://localhost:3000`.

## 4) Power outage recovery (BIOS/UEFI)

Set in BIOS/UEFI:
- `Restore on AC Power Loss` (or equivalent) = `Power On`.

Validation:
1. Shut down.
2. Cut AC power and restore AC.
3. Confirm machine boots automatically.
4. Confirm scheduled task starts app and endpoint becomes reachable.

## 5) Backup strategy (local + secondary + retention)

### Layer 1: app-level local backup

`main.py` already performs periodic SQLite backup using:
- `DB_BACKUP_DIR`
- `DB_BACKUP_INTERVAL_SECONDS`

### Layer 2 and 3: secondary copy + retention prune

Use script:

`.\scripts\windows\invoke-backup-prune.ps1 -AppRoot C:\AchintERP -SecondaryTarget D:\ERP_Backups -DailyKeep 7 -WeeklyKeep 4 -MonthlyKeep 12`

Recommended scheduled task (daily at 02:00):

`schtasks /Create /TN "AchintERP-BackupPrune" /SC DAILY /ST 02:00 /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\AchintERP\scripts\windows\invoke-backup-prune.ps1 -AppRoot C:\AchintERP -SecondaryTarget D:\ERP_Backups -DailyKeep 7 -WeeklyKeep 4 -MonthlyKeep 12" /RU SYSTEM /F`

Use `-WhatIf` first to dry run pruning:

`.\scripts\windows\invoke-backup-prune.ps1 -AppRoot C:\AchintERP -SecondaryTarget D:\ERP_Backups -WhatIf`

### Layer 4: off-machine replication

Point `-SecondaryTarget` to a UNC share (example `\\NAS\AchintERP`) or replicate `D:\ERP_Backups` to external machine/NAS.

## 6) Restore validation workflow

Run monthly restore drill:

`.\scripts\windows\test-restore.ps1 -AppRoot C:\AchintERP -SecondaryTarget D:\ERP_Backups`

Script checks:
- latest backup copy can be restored
- SQLite `PRAGMA integrity_check` is `ok`
- core tables exist (`users`, `clients`, `purchase_orders`, `invoices`)
- optional `main.py` compile check

Output:
- report in `C:\AchintERP\restore-tests\restore_<timestamp>\restore_report.txt`

## 7) Operations checklist

Daily:
- confirm app reachable
- confirm latest local backup exists
- confirm secondary snapshot copied

Weekly:
- check free disk space on app and backup volumes
- review `C:\AchintERP\logs\app-YYYYMMDD.log` for crashes/restarts

Monthly:
- run restore test script and archive report
- verify Task Scheduler last run result for startup and backup tasks
- verify firewall scope still matches plant LAN requirements

Quarterly:
- validate BIOS/UEFI power recovery after maintenance outage
- rotate admin credentials and review local account access

## 8) Quick verification notes

- Startup script is idempotent and safe to re-run.
- Startup task registration replaces existing task with same name.
- Backup script is safe to re-run and prunes only files outside retention set.
- Restore script creates timestamped test directories and does not modify production DB.
