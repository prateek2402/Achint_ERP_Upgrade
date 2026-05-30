@echo off
REM Double-click this file (or run from Explorer) to open firewall port 3000 with UAC elevation.
powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','cd /d \"%~dp0\" && call enable-network-firewall.cmd && pause' -Verb RunAs"
