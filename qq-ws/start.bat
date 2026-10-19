@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python。请先安装 Python 3.10~3.12 并勾选 "Add Python to PATH"，或运行本包根目录的 install_deps.bat。
  pause
  exit /b 1
)
python daemonize.py start
echo.
echo 启动后实时日志：在文件资源管理器打开 qq_client.log
echo 停止请双击 stop.bat；查看状态双击 status.bat
pause
