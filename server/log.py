"""极简日志：写到 ``userdata/logs/app.log``，按大小轮转。

为什么不用 logging 模块
-----------------------
这里只需要"用户把日志文件发给我，我能看懂发生了什么"。所以格式固定、
一行一条、带时间与标签、自动轮转就够，不需要 handler/formatter 那一套。

谁在用
------
- 服务启动（版本、端口、Python、根目录是否含中文）
- 每个后台任务（开始 / 完成 / 失败 + 失败的异常信息）
- HTTP 500 与解包脚本的非零退出
"""

from __future__ import annotations

import os
import threading
import time

from . import paths

_LOCK = threading.Lock()
#: 单个文件上限 1 MB，超过就 app.log → app.log.1 → app.log.2，多的丢掉
MAX_BYTES = 1 << 20
KEEP = 2


def log_dir() -> str:
    d = os.path.join(paths.USERDATA, "logs")
    os.makedirs(d, exist_ok=True)
    return d


def log_path() -> str:
    return os.path.join(log_dir(), "app.log")


def _rotate(p: str) -> None:
    try:
        if not (os.path.exists(p) and os.path.getsize(p) > MAX_BYTES):
            return
        for i in range(KEEP, 0, -1):
            src = p if i == 1 else f"{p}.{i - 1}"
            dst = f"{p}.{i}"
            if os.path.exists(src):
                os.replace(src, dst)
    except OSError:
        pass


def log(msg: str, tag: str = "") -> str:
    """追加一行。永远不抛异常 —— 日志写不进去不该影响业务。"""
    line = "{} [{}] {}\n".format(
        time.strftime("%Y-%m-%d %H:%M:%S"), (tag or "-")[:12], str(msg)[:4000])
    with _LOCK:
        p = log_path()
        try:
            _rotate(p)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
    return line


def tail(n: int = 60) -> str:
    """取最后 n 行（排查问题用，会拼上上一份的尾巴）。"""
    out = []
    for name in ("app.log.1", "app.log"):
        p = os.path.join(log_dir(), name)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                out.extend(f.readlines())
        except OSError:
            continue
    return "".join(out[-n:])
