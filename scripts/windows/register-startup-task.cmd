@echo off
REM Register Achint ERP to start automatically when Windows boots (requires Admin).
setlocal
cd /d "%~dp0"
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0register-startup-task.ps1\"','-AppRoot','\"%~dp0..\..\"','-Network'"
exit /b %ERRORLEVEL%
