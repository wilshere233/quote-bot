@echo off
cd /d "%~dp0"
echo Stopping wecom-ws...
cd /d "%~dp0wecom-ws" && python daemonize.py stop
echo Stopping qq-ws...
cd /d "%~dp0qq-ws" && python daemonize.py stop
pause
