@echo off
REM Import legacy ERP data without PowerShell execution-policy restrictions.
REM Full replace:  import-legacy.cmd
REM Selective merge: import-legacy.cmd --mode merge --clients "Client A,Client B"
REM                  import-legacy.cmd --mode merge --clients-file clients_to_import.txt
REM List legacy clients: venv\Scripts\python.exe migrate_sqlite.py list-clients
setlocal
cd /d "%~dp0.."

set PY=%CD%\venv\Scripts\python.exe
if not exist "%PY%" (
    echo venv not found. Create it first: python -m venv venv
    exit /b 1
)

if not exist "%CD%\old_erp.sqlite" (
    echo Legacy database not found: %CD%\old_erp.sqlite
    echo Copy your old ERP export there, then run this script again.
    exit /b 1
)

echo Importing from %CD%\old_erp.sqlite ...
"%PY%" migrate_sqlite.py %*
if errorlevel 1 exit /b %errorlevel%

echo.
echo Done. Restart the app if it is running, then refresh the browser.
echo Report: %CD%\legacy_import_status.json
