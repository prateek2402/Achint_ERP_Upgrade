@echo off
REM LAN access — same as: server.cmd start -Network
cd /d "%~dp0.."
call server.cmd start -Network %*
