@echo off
REM Allow inbound TCP to AchintERP (default port 3000). Requires Administrator.
setlocal
cd /d "%~dp0"

set PORT=3000
if not "%ACHINT_PORT%"=="" set PORT=%ACHINT_PORT%

net session >nul 2>&1
if errorlevel 1 (
    echo Run this file as Administrator: right-click -^> "Run as administrator"
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0enable-network-firewall.ps1" -Port %PORT%
exit /b %errorlevel%
