@echo off
REM Remove Achint ERP auto-start scheduled task (requires Admin).
setlocal
cd /d "%~dp0"
powershell -NoProfile -Command ^
  "Start-Process powershell -Verb RunAs -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0unregister-startup-task.ps1\"')"
exit /b %ERRORLEVEL%
