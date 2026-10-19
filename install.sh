#!/bin/bash
# 安装依赖（macOS：Apple 芯片 arm64 与 Intel x86_64 通用）
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3。"
  echo "请先安装 Python 3.10+（推荐 3.12）：https://www.python.org/downloads/macos/"
  echo "Apple 芯片选 macOS 64-bit universal2 installer；Intel 选 macOS 64-bit Intel installer。"
  exit 1
fi

echo "Python: $(python3 --version 2>&1)"
echo "芯片架构: $(uname -m)（本包为纯 Python，arm64 / x86_64 通用）"
echo
echo "正在安装依赖（企微 + QQ）..."
python3 -m pip install --upgrade pip
python3 -m pip install wecom-aibot-python-sdk websockets cryptography
echo
echo "========== 依赖安装完成 =========="
echo "接下来："
echo "  1. 编辑 wecom-ws/ws_client.py 顶部 BOTS，填入 bot_id / secret"
echo "  2. （如需 QQ）编辑 qq-ws/qq_config.json，填入 appid / appsecret"
echo "  3. 运行 ./start_all.sh 启动"
echo "  4. 详细说明见 macOS操作指南.md"
