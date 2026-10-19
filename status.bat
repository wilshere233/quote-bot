@echo off
cd /d "%~dp0"
echo wecom-ws:
cd /d "%~dp0wecom-ws" && python daemonize.py status
echo.
echo qq-ws:
cd /d "%~dp0qq-ws" && python daemonize.py status
pause
