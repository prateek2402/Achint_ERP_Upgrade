@echo off
REM Selective merge: replace only named clients from old_erp.sqlite (keeps other local clients).
REM Stop the ERP server and back up erp_database.sqlite before continuing.
setlocal
cd /d "%~dp0.."

set PY=%CD%\venv\Scripts\python.exe
if not exist "%PY%" (
    echo venv not found. Create it first: python -m venv venv
    exit /b 1
)

if not exist "%CD%\old_erp.sqlite" (
    echo Legacy database not found: %CD%\old_erp.sqlite
    exit /b 1
)

set CLIENTS_FILE=%CD%\clients_to_import.txt
if not exist "%CLIENTS_FILE%" (
    echo Create %CLIENTS_FILE% with one client name per line, then run this script again.
    exit /b 1
)

echo Listing legacy clients...
"%PY%" migrate_sqlite.py list-clients
echo.
echo Dry-run merge using %CLIENTS_FILE% ...
"%PY%" migrate_sqlite.py --mode merge --clients-file "%CLIENTS_FILE%" --dry-run
if errorlevel 1 exit /b %errorlevel%

echo.
set /p CONFIRM=Apply merge to erp_database.sqlite? Type YES to continue: 
if /i not "%CONFIRM%"=="YES" (
    echo Cancelled.
    exit /b 0
)

"%PY%" migrate_sqlite.py --mode merge --clients-file "%CLIENTS_FILE%"
exit /b %errorlevel%
