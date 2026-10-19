#!/bin/bash
cd "$(dirname "$0")"
python3 daemonize.py status
echo
echo "日志末 5 行："
[ -f ws_client.log ] && tail -n 5 ws_client.log
