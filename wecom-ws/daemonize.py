#!/usr/bin/env python3
"""
守护启动器：把长连接客户端挂到独立会话下运行，脱离调用它的 shell 会话生命周期。
用法: python daemonize.py start|stop|status
"""
import os
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).parent
PY = sys.executable  # 跨平台：使用运行本脚本的同一解释器（Windows 上即 python.exe，免改路径）
SCRIPT = str(DIR / "ws_client.py")
LOG = str(DIR / "ws_client.log")
PIDFILE = DIR / "daemon.pid"


def read_pid():
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid, 0)  # 检测存活
            return pid
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            return None
    return None


def start():
    existing = read_pid()
    if existing:
        print(f"已在运行 (PID {existing})")
        return

    # 清除 WorkBuddy 注入的环境变量：
    # - PYTHONPATH/PYTHONHOME/VIRTUAL_ENV：sitecustomize broker 会拦截文件写入
    # - HTTP(S)_PROXY：WorkBuddy 会话代理随会话结束而失效，会导致长连接反复断线，
    #   企微网关需直连
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        )
    }
    env["PYTHONUNBUFFERED"] = "1"

    logf = open(LOG, "a")
    # start_new_session=True → setsid，脱离当前进程组/会话
    proc = subprocess.Popen(
        [PY, SCRIPT],
        stdout=logf,
        stderr=logf,
        cwd=str(DIR),
        env=env,
        start_new_session=True,  # 核心：独立会话，不被父进程连带回收
        close_fds=True,
    )
    PIDFILE.write_text(str(proc.pid))

    time.sleep(3)
    if proc.poll() is None:
        print(f"已启动 (PID {proc.pid})，独立会话运行")
    else:
        print(f"启动失败，退出码 {proc.returncode}，请查看 {LOG}")


def stop():
    pid = read_pid()
    if pid:
        os.kill(pid, 15)  # SIGTERM，触发脚本优雅退出
        time.sleep(1)
        PIDFILE.unlink(missing_ok=True)
        print("已停止")
    else:
        PIDFILE.unlink(missing_ok=True)
        print("未在运行")


def status():
    pid = read_pid()
    if pid:
        print(f"运行中 (PID {pid})")
    else:
        print("未在运行")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    {"start": start, "stop": stop, "status": status}.get(cmd, status)()