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

import contextlib
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
#: 被接管的"控制台"（子进程直接往 fd 1/2 写的内容），见 capture_stdio
_CON_RE = re.compile(r"^console-(\d{8})\.log$")
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
        # console-*.log 只按天数删（里面是子进程原始输出，没有分级，没法精简）
        mc = _CON_RE.match(n)
        if mc:
            day = _day_ts(mc.group(1))
            if day is not None and today is not None \
                    and int((today - day) // 86400) > KEEP_DAYS:
                try:
                    os.remove(os.path.join(d, n))
                    st["dropped"] += 1
                except OSError:
                    pass
            continue
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


# ---------------------------------------------------------------- 接管 stdout
_CAP_BUSY = threading.local()


class _LogStream:
    """把 stdout / stderr 的写入转进 app.log（见 :func:`capture_stdio`）。"""

    def __init__(self, tag: str = "out", level: str = "INFO"):
        self.tag = tag
        self.level = level

    # —— 文件对象的最小接口 ——
    def write(self, s) -> int:
        try:
            s = s if isinstance(s, str) else str(s)
        except Exception:                                    # noqa: BLE001
            return 0
        txt = s.rstrip("\r\n")
        if not txt.strip():
            return len(s)
        # 递归保护：日志本身写失败时不会再 print 回来（jobs.py 里有这种 print）
        if getattr(_CAP_BUSY, "on", False):
            return len(s)
        _CAP_BUSY.on = True
        try:
            log(txt, self.tag, self.level)
        except Exception:                                    # noqa: BLE001
            pass
        finally:
            _CAP_BUSY.on = False
        return len(s)

    def writelines(self, lines) -> None:
        for ln in lines or ():
            self.write(ln)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    @property
    def buffer(self):
        return self

    def fileno(self):
        # 有些第三方库会 fileno() 去判断是不是终端；重定向后没有真实句柄。
        raise OSError("stdout 已重定向到 userdata/logs/app-*.log")


#: fd 1/2 接管后指向的文件（必须保住引用：句柄被回收关闭，子进程就写不进去了）
_CONSOLE_SINK = None


def console_sink_path() -> str:
    """被接管后的"控制台"落到哪个文件（只有真子进程会往这儿写）。"""
    return os.path.join(log_dir(), "console-%s.log" % _today())


def capture_stdio() -> bool:
    """把进程的 stdout/stderr 接到 app.log，**不再写控制台**。

    为什么必须这么做：Windows 控制台一旦进入「选择 / 标记」模式（用户随手在窗口里
    点一下、按住鼠标、框选文字，甚至只是拖动选择），conhost 会**冻结**所有往 stdout
    写的进程 —— ``print`` 卡在管道里永远不返回。服务进程里还留着大量历史脚本的
    ``print``（stages 的导出函数、下载层的进度行），主线程撞上一次就是好几分钟不动：

        22:43:22 [job] DONE install「安装 3 部剧情」193s
        22:49:42 [job] [base] 下载音效 505 个包 / 31 MB      ← 中间空白 6 分 20 秒

    这 6 分 20 秒里 BGM 早就下完了（包数/字节停在 818 / 736 MB），下载线程全空闲，
    只是主线程卡在 ``print`` 上；界面显示的又是历史平均速度，于是用户看到
    "有速度但是一直卡进度"（2026-11-17 实测复现）。

    调用时机：启动横幅与访问地址打印完之后（那几句用户要看到），也就是
    ``api.serve()`` 里 ``print("关闭此窗口即停止服务")`` 的下一行。

    设 ``ESCAH_KEEP_CONSOLE=1`` 时不接管，方便开发时直接看窗口输出。
    """
    import sys
    if os.environ.get("ESCAH_KEEP_CONSOLE"):
        return False
    try:
        sys.stdout = _LogStream("out")
        sys.stderr = _LogStream("err", "ERROR")
    except Exception:                                        # noqa: BLE001
        return False
    # **fd 级**接管：上面换的只是 Python 的 sys.stdout；子进程（旧脚本、超分引擎、
    # 更新器）继承的是控制台句柄本身，照样会往启动窗口里打字（2026-11-18 用户：
    # 「CMD 还是会打印下载类的信息」）。把 fd 1/2 一起指到日志文件，才是"启动完成后
    # 窗口就是一块静态说明"的保证 —— 以后新加的子进程也不用各自记得接管输出。
    try:
        global _CONSOLE_SINK
        _CONSOLE_SINK = open(console_sink_path(), "a", encoding="utf-8",
                             errors="replace", buffering=1)
        os.dup2(_CONSOLE_SINK.fileno(), 1)
        os.dup2(_CONSOLE_SINK.fileno(), 2)
    except Exception:                                        # noqa: BLE001
        pass
    return True


@contextlib.contextmanager
def quiet_stdio(tag: str = "boot"):
    """临时把 stdout 接进 app.log —— 启动阶段的 print 不该落到启动窗口里。

    为什么需要：``capture_stdio()`` 必须等横幅与访问地址打印完才接管，而启动
    校正（剧情目录重建、头像来源修正）跑在它前面；那段时间里历史脚本的
    ``print`` 会直接写到窗口（实测漏出来的一句：``advindex.js：0 部本机剧情
    + 保留目录 233 条``）。用局部上下文比把这些函数的 print 一条条改掉可靠 ——
    以后新加进启动路径的旧函数也自动静音。
    设 ``ESCAH_KEEP_CONSOLE=1``（开发模式）时不接管，照旧在窗口里看得到。
    """
    import sys
    if os.environ.get("ESCAH_KEEP_CONSOLE"):
        yield
        return
    old = sys.stdout
    sys.stdout = _LogStream(tag)
    try:
        yield
    finally:
        sys.stdout = old


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
