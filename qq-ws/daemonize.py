#!/usr/bin/env python3
"""
守护启动器：把 QQ 长连接客户端挂为独立后台进程，脱离调用它的命令行窗口生命周期。

用法: python daemonize.py start|stop|status

Windows 适配要点（本包专用版本，与 macOS 版略有差异）：
- 解释器用sys.executable（本机正在跑的 python），不写死任何绝对路径
- 停止进程用 taskkill /T /F（Windows 没有 SIGTERM 语义）
- 存活检测用 tasklist 查 PID（Windows 下 os.kill(pid,0) 不可靠）
- 不使用 start_new_session（Windows 无对应概念）
"""

import os
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).parent
PY = sys.executable          # 用当前解释器，避免写死路径
SCRIPT = str(DIR / "qq_client.py")
LOG = str(DIR / "qq_client.log")
PIDFILE = DIR / "daemon.pid"
IS_WIN = os.name == "nt"


def read_pid():
    """返回存活进程 PID，不存活返回 None。"""
    if not PIDFILE.exists():
        return None
    try:
        pid = int(PIDFILE.read_text().strip())
    except (ValueError, OSError):
        return None
    if IS_WIN:
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=15).stdout
            return pid if str(pid) in out else None
        except Exception:
            return None
    try:
        os.kill(pid, 0)  # 检测存活
        return pid
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return None


def start():
    existing = read_pid()
    if existing:
        print(f"已在运行 (PID {existing})")
        return

    # 沿用生产环境的两条经验：
    # - 清掉 PYTHONPATH/PYTHONHOME/VIRTUAL_ENV，避免外部 sitecustomize 拦截文件写入
    # - 清掉 HTTP(S)_PROXY，长连接不应走本地代理端口
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

    logf = open(LOG, "a", encoding="utf-8")
    kwargs = dict(stdout=logf, stderr=logf, cwd=str(DIR), env=env, close_fds=True)
    if not IS_WIN:
        # macOS/Linux：setsid 脱离进程组
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([PY, SCRIPT], **kwargs)
    PIDFILE.write_text(str(proc.pid), encoding="utf-8")

    time.sleep(3)
    if proc.poll() is None:
        print(f"已启动 (PID {proc.pid}），后台独立运行")
        print(f"日志：{LOG}")
    else:
        print(f"启动失败，退出码 {proc.returncode}，请查看 {LOG}")


def stop():
    pid = read_pid()
    if pid:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True)
        else:
            try:
                os.kill(pid, 15)  # SIGTERM，触发脚本优雅退出
            except OSError:
                pass
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
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"start": start, "stop": stop, "status": status}.get(cmd, status)()
