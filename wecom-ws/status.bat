@echo off
cd /d "%~dp0"
python daemonize.py status
echo.
echo 日志末 5 行：
if exist ws_client.log (powershell -Command "Get-Content ws_client.log -Tail 5")
pause
