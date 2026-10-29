"""子进程统一入口：**不弹黑窗、不留残留**。

为什么必须有这一层
------------------
服务端要跑不少外部进程：解包脚本（compose_stand_all 等）、视频导出的截帧、
pip 安装依赖。Windows 上如果不指定 ``CREATE_NO_WINDOW``，每次 ``Popen`` 都会
**闪一个黑窗口**——用户会以为中病毒，而且是"装一部剧情弹三次"这种密度。

残留问题
--------
1. 子进程一旦启动，父进程被强杀（用户关窗口/任务管理器结束）就会变成孤儿，
   继续占 CPU、占端口、占浏览器实例。
2. 所以这里维护一张"活着的子进程"表，``atexit`` 与 SIGINT/SIGTERM 时统一清理。
3. ``reap()`` 在正常结束后把自己从表里摘掉，避免误杀复用了 PID 的新进程。
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys

#: Windows 专用：不创建控制台窗口
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: pid -> Popen（只在子进程还活着时保留）
_LIVE: dict[int, subprocess.Popen] = {}


def _hide(kw: dict) -> dict:
    """给 Popen 参数补上"别弹窗"。"""
    if os.name == "nt":
        kw.setdefault("creationflags", _NO_WINDOW)
        # 有些组合（比如 CREATE_NEW_CONSOLE 的第三方库）会忽略 creationflags，
        # startupinfo 是第二道保险。
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kw.setdefault("startupinfo", si)
    return kw


def popen(cmd, **kw) -> subprocess.Popen:
    """同 ``subprocess.Popen``，但不弹窗，并登记以便退出时清理。"""
    p = subprocess.Popen(cmd, **_hide(kw))
    _LIVE[p.pid] = p
    return p


def run(cmd, **kw):
    """同 ``subprocess.run``，但不弹窗。"""
    try:
        return subprocess.run(cmd, **_hide(kw))
    except Exception:
        raise


def reap(p: subprocess.Popen) -> None:
    """子进程正常结束后调用，把它从清理表里摘掉。"""
    if p is not None:
        _LIVE.pop(p.pid, None)


def alive() -> list[int]:
    """当前还活着的子进程（调试/状态用）。"""
    out = []
    for pid, p in list(_LIVE.items()):
        if p.poll() is None:
            out.append(pid)
        else:
            _LIVE.pop(pid, None)
    return out


def kill_all() -> None:
    """强制结束所有子进程（退出时调用，避免残留）。"""
    for pid, p in list(_LIVE.items()):
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except Exception:                              # noqa: BLE001
                    p.kill()
        except Exception:                                      # noqa: BLE001
            pass
        _LIVE.pop(pid, None)


@atexit.register
def _on_exit() -> None:
    kill_all()


def install_signal_handlers() -> None:
    """Ctrl+C / 关闭窗口时也走清理，而不是直接死掉留一堆孤儿。"""

    def _handler(signum, frame):
        kill_all()
        raise SystemExit(0)

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass        # 非主线程 / 不支持时忽略


def python_exe() -> str:
    """当前解释器。子进程一律用它，避免命到另一个 Python。"""
    return sys.executable
