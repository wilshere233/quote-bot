@echo off
cd /d "%~dp0"
python daemonize.py status
echo.
echo 日志末 5 行：
if exist qq_client.log (powershell -Command "Get-Content qq_client.log -Tail 5")
pause
