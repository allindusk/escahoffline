"""分级日志：按天一个文件 + 分层保留。

保留策略（按用户要求）
----------------------
- **当天**：全量。每条 API 请求、后台任务的每一步、子进程输出、异常堆栈都记，
  事后可以按时间线完整复盘。
- **1 天前**：只留 ``[ERROR]``，其余丢弃（改名为 ``app-YYYYMMDD.err.log``），
  避免几天后翻几万行噪声。
- **7 天前**：整份删除。

为什么不用 logging 模块
-----------------------
这里只需要"用户把日志文件发给我，我能看懂发生了什么"。格式固定、
一行一条、能 grep 就够，不需要 handler/formatter/logger 树那一套。

行格式::

    2026-10-30 23:59:58 [DEBUG] [http] GET /api/state 200 12ms
    2026-10-30 23:59:59 [ERROR] [http] GET /api/boom 500 3ms err=...

谁在用
------
- 服务启动 / 关闭
- 每条 HTTP API 请求（含状态码、耗时、POST 体摘要）
- 每个后台任务（开始 / 进度 / 完成 / 失败 + 异常）
- 委派出去的脚本（stdout/stderr）
- 立绘合成等关键决策（用了哪个包、落点来源、尺寸一致性）
"""

from __future__ import annotations

import os
import re
import threading
import time

from . import paths

_LOCK = threading.Lock()

#: 支持 DEBUG / INFO / WARN / ERROR
LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

#: 超过这么多天的整份日志直接删
KEEP_DAYS = 7
#: 超过这么多天的只保留 ERROR
COMPACT_DAYS = 1

MAX_LINE = 4000

_NAME_RE = re.compile(r"^app-(\d{8})\.log$")
_ERR_RE = re.compile(r"^app-(\d{8})\.err\.log$")
_LV_RE = re.compile(r"\[(DEBUG|INFO|WARN|ERROR)\] ")


def log_dir() -> str:
    d = os.path.join(paths.USERDATA, "logs")
    os.makedirs(d, exist_ok=True)
    return d


def _today() -> str:
    return time.strftime("%Y%m%d")


def log_path() -> str:
    """当前应写入的文件（按天）。"""
    return os.path.join(log_dir(), "app-%s.log" % _today())


def _day_ts(s: str):
    try:
        return time.mktime(time.strptime(s, "%Y%m%d"))
    except (ValueError, OverflowError):
        return None


def _migrate_legacy() -> None:
    """把旧的 ``app.log`` / ``app.log.N`` 并进按天的文件，别丢历史。"""
    d = log_dir()
    for name in ("app.log.2", "app.log.1", "app.log"):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            continue
        try:
            day = time.strftime("%Y%m%d", time.localtime(os.path.getmtime(p)))
            dst = os.path.join(d, "app-%s.log" % day)
            if os.path.exists(dst):
                with open(p, encoding="utf-8", errors="replace") as f:
                    txt = f.read()
                with open(dst, "a", encoding="utf-8") as f:
                    f.write(txt)
                os.remove(p)
            else:
                os.replace(p, dst)
        except OSError:
            pass


def log(msg: str, tag: str = "", level: str = "INFO") -> str:
    """追加一行。永远不抛异常 —— 日志写不进去不该影响业务。"""
    lv = (level or "INFO").upper()
    if lv not in LEVELS:
        lv = "INFO"
    line = "{} [{}] [{}] {}\n".format(
        time.strftime("%Y-%m-%d %H:%M:%S"), lv, (tag or "-")[:12],
        str(msg)[:MAX_LINE])
    with _LOCK:
        try:
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
    return line


def debug(msg: str, tag: str = "") -> str:
    return log(msg, tag, "DEBUG")


def info(msg: str, tag: str = "") -> str:
    return log(msg, tag, "INFO")


def warn(msg: str, tag: str = "") -> str:
    return log(msg, tag, "WARN")


def error(msg: str, tag: str = "") -> str:
    return log(msg, tag, "ERROR")


def _compact(p: str, day: str) -> bool:
    """把一份旧日志压成"只剩 ERROR"，改名为 ``*.err.log``。"""
    try:
        kept = []
        with open(p, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = _LV_RE.search(ln)
                if m and m.group(1) == "ERROR":
                    kept.append(ln)
        dst = os.path.join(os.path.dirname(p), "app-%s.err.log" % day)
        with open(dst, "w", encoding="utf-8") as f:
            f.write("".join(kept)
                    or "%s：无 ERROR 记录（常规日志已按 %d 天保留策略清理）\n"
                    % (day, COMPACT_DAYS))
        os.remove(p)
        return True
    except OSError:
        return False


def prune() -> dict:
    """按保留策略收敛日志。开机调用一次即可（也幂等，可随时再调）。

    - ``> KEEP_DAYS`` 天：删除（含 ``.err.log``）
    - ``>= COMPACT_DAYS`` 天：只留 ERROR，改名 ``app-YYYYMMDD.err.log``
    """
    d = log_dir()
    st = {"compacted": 0, "dropped": 0}
    _migrate_legacy()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return st
    today = _day_ts(_today())
    for n in names:
        m = _NAME_RE.match(n) or _ERR_RE.match(n)
        if not m:
            continue
        day = _day_ts(m.group(1))
        if day is None or today is None:
            continue
        age = int((today - day) // 86400)
        p = os.path.join(d, n)
        if age > KEEP_DAYS:
            try:
                os.remove(p)
                st["dropped"] += 1
            except OSError:
                pass
        elif age >= COMPACT_DAYS and _NAME_RE.match(n):
            if _compact(p, m.group(1)):
                st["compacted"] += 1
    return st


def tail(n: int = 120) -> str:
    """取最近几份日志的末尾 n 行（排查问题用）。"""
    d = log_dir()
    try:
        names = sorted(x for x in os.listdir(d) if x.startswith("app-"))
    except OSError:
        return ""
    out = []
    for name in names[-3:]:
        try:
            with open(os.path.join(d, name), encoding="utf-8",
                      errors="replace") as f:
                out.extend(f.readlines())
        except OSError:
            continue
    return "".join(out[-n:])
