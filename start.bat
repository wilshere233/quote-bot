@echo off
cd /d "%~dp0"
echo Starting wecom-ws...
cd /d "%~dp0wecom-ws" && python daemonize.py start
echo Starting qq-ws...
cd /d "%~dp0qq-ws" && python daemonize.py start
echo.
echo Done. Use status.bat to check.
pause
