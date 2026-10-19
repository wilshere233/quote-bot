#!/bin/bash
cd "$(dirname "$0")"
echo "启动 wecom-ws..."
(cd wecom-ws && python3 daemonize.py start)
echo "启动 qq-ws..."
(cd qq-ws && python3 daemonize.py start)
echo
echo "查看状态：./status_all.sh"
