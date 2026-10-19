@echo off
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python。请先到 python.org 下载安装 Python 3.10~3.12，安装时务必勾选 "Add Python to PATH"。
  pause
  exit /b 1
)
echo 正在安装依赖（企微侧 + QQ 侧）...
python -m pip install --upgrade pip
python -m pip install wecom-aibot-python-sdk websockets cryptography
echo.
echo ========== 依赖安装完成 ==========
echo 接下来：
echo   1. 编辑 wecom-ws\ws_client.py 顶部 BOTS，填入 bot_id / secret
echo   2. （如需 QQ）编辑 qq-ws\qq_config.json，填入 appid / appsecret
echo   3. 双击 wecom-ws\start.bat 启动（QQ 同理 qq-ws\start.bat）
echo   4. 详细配置与开机自启见 部署手册.md
echo.
pause
