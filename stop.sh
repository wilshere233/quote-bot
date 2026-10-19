#!/bin/bash
cd "$(dirname "$0")"
echo "停止 wecom-ws..."
(cd wecom-ws && python3 daemonize.py stop)
echo "停止 qq-ws..."
(cd qq-ws && python3 daemonize.py stop)
