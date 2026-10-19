#!/bin/bash
cd "$(dirname "$0")"
echo "wecom-ws:"
(cd wecom-ws && python3 daemonize.py status)
echo "qq-ws:"
(cd qq-ws && python3 daemonize.py status)
