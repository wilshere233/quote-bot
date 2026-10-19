#!/bin/bash
cd "$(dirname "$0")"
python3 daemonize.py status
echo
echo "日志末 5 行："
[ -f qq_client.log ] && tail -n 5 qq_client.log
