@echo off
REM Achint ERP server control — start, stop, status, restart
REM   server.cmd start
REM   server.cmd start -Network
REM   server.cmd start -Dev
REM   server.cmd stop
REM   server.cmd status
REM   server.cmd install-startup    (Admin — auto-start after Windows boot)
REM   server.cmd remove-startup
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\server.ps1" %*
exit /b %ERRORLEVEL%
