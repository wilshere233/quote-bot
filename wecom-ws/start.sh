#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3。请先运行本包根目录的 install_deps.sh。"
  exit 1
fi
python3 daemonize.py start
echo
echo "实时日志：tail -f ws_client.log"
echo "停止：./stop.sh    状态：./status.sh"
