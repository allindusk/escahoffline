"""后台任务与进度事件流。

为什么要单独的 job 层
---------------------
下载 / 解包 / 超分 / 导出都是分钟级的耗时操作，HTTP 请求不能一直挂着等。
这里把每个耗时操作包成一个 ``Job``，HTTP 只负责返回 job id，前端用
``GET /api/jobs/stream``（Server-Sent Events）拿实时进度。

约定
----
* ``total``/``done`` 用"项"计数（文件数或字节数，由调用方决定并写进 unit）
* ``speed`` 由最近 15 个采样点算滑动平均，避免抖动
* 任务可取消：``job.cancel_event`` 由下载/解包循环自行检查
* 已完成任务保留最近 40 个，供刷新页面后回看
"""

from __future__ import annotations

import itertools
import os
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque

MAX_KEEP = 40
_LOCK = threading.RLock()
_JOBS: "OrderedDict[str, Job]" = OrderedDict()
_SEQ = itertools.count(1)

#: 单元占用表：``unit -> job id``。
#:
#: 为什么需要：卸载会把 ``web/data/char_N.js`` 和该角色的素材删掉，而导出视频
#: 正在读同一批文件。2026-10-30 的 e2e 就是这样炸的 —— 视频任务刚起，卸载在同一
#: 秒删了 261 个文件，截帧进程直接 FileNotFoundError（0 帧），而任务状态还是 DONE。
#: 现在提交任务时声明"我要用这些单元"，卸载前查这张表，占着就拒绝。
_UNITS_LOCK = threading.Lock()
_UNIT_HOLDERS: dict[str, str] = {}


class Job:
    """一个可观测的耗时任务。"""

    def __init__(self, kind: str, title: str, unit: str = "", **meta):
        self.id = f"{kind}-{next(_SEQ)}"
        self.kind = kind
        self.title = title
        self.unit = unit
        self.meta = meta
        self.state = "queued"          # queued | running | done | failed | cancelled
        self.message = ""
        #: 最后一句"阶段描述"（如「② 解析剧本 → data/char_306.js」），
        #: 失败时进排错指纹：光有错误信息不知道卡在哪一步，定位会慢一半。
        self.stage = ""
        self.error = ""
        self.total = 0.0
        self.done = 0.0
        self.bytes_total = 0.0
        self.bytes_done = 0.0
        self.items_total = 0
        self.items_done = 0
        # 子进度（下载层的"包 x/y"）：多单元安装时，顶层百分比改由
        # items 驱动（不会出现"剧情100%→立绘100%→语音100%"反复跳满），
        # 而每一批 bundle 的进度单独落在 sub 上，界面当作次级信息显示。
        self.sub_total = 0.0
        self.sub_done = 0.0
        self.sub_label = ""
        # sub_mode：fetch_many / _capture 把"包计数"写进 sub 而不是顶层
        # total/done（否则每层 fetch 都把顶层进度重置到 100%）。
        self.sub_mode = False
        # 阶段百分比映射（都用"百分点"）：
        # percent = pct_base + 本阶段内部进度(0–100) × pct_span / 100。
        # 下载 + 解包这类"两段耗时"的任务靠它把解包也画进进度条，
        # 否则下载一到 100% 就冻在那里十几分钟（用户会当成卡死）。
        self.pct_base = 0.0
        self.pct_span = 100.0
        # 进度条高水位：只许前进，不许倒退。
        # 多单元安装时「下一部的第一批」会把 sub 归零（0/1），
        # 不设高水位就会在界面上看到 33% → 66% → 33% 的来回跳。
        self._pct_hi = 0.0
        self.detail: dict = {}
        self.created = time.time()
        self.started = 0.0
        self.ended = 0.0
        self._samples: deque = deque(maxlen=15)
        self._last_emit = 0.0
        self.cancel_event = threading.Event()
        # 置位 = 暂停。下载线程每次领新包前检查它（见 net.fetch_many）。
        # 这是"软暂停"：已经在传的包会传完，不会留下半个文件。
        self.pause_event = threading.Event()

    # ---------------------------------------------------------------- 写入
    def start(self) -> "Job":
        self.state = "running"
        self.started = time.time()
        self._sample()
        return self

    def set_total(self, n: float, unit: str = "") -> None:
        """设置「计数」总量：``total``/``done`` 是给「已完成 x/y 个」用的。

        ``unit`` 是**必给**的量词（个 / 张 / 部 / 包 / 帧）。前端只在 unit
        非空时才渲染「x/y 单位」那一段 —— 因为 ``total`` 一旦被拿来塞字节数，
        任务条就会把「911 KB 的更新包」说成「0/933450 个」（2026-11-03 用户报的）。
        字节口径请只写 ``bytes_total`` / ``bytes_done``。
        """
        self.total = float(n)
        if unit:
            # snapshot() 读的是 self.unit —— 以前写进了 detail["unit"]，
            # 前端永远拿不到，于是"505 个包"被按字节显示成 "470 B/505 B"。
            self.unit = unit
        self._sample()

    def set_items(self, n: int) -> None:
        self.items_total = int(n)

    def sub_progress(self, done: float, total: float, label: str = "") -> None:
        """写入"子进度"（当前这批 bundle 的包计数），不影响顶层百分比。

        多单元安装时，顶层百分比由 items 驱动，下载层的每批包进度单独记在这里，
        界面显示成「包 x/y」之类的次级信息，避免反复冲到 100%。
        """
        self.sub_done = float(done)
        self.sub_total = float(total)
        if label:
            self.sub_label = label
        self._sample()

    def phase(self, base: float, span: float, total: float | None = None,
              unit: str = "", keep_bytes: bool = False) -> None:
        """切到下一个阶段：把本阶段的内部进度映射到 ``base``~``base+span``。

        典型用法是「下载 0–60%、解包 60–100%」：只按下载字节算，下载完成就把
        进度条顶到 100%，而解包往往比下载还久，用户看到的是"100% 却一直不动"。

        新阶段默认不再按字节算（``keep_bytes=False``）：上一阶段的字节已经满了，
        留着会让百分比永远顶在 100%；清掉之后前端也不再显示那条 "893 MB / 893 MB"。
        """
        self.pct_base = float(base)
        self.pct_span = float(span)
        if not keep_bytes:
            self.bytes_total = 0.0
        if total is not None:
            self.set_total(total, unit)
            # 新阶段从 0 起算：上一阶段的 done 已经"溢出"（10/2），
            # 不归零会让这个阶段的百分比一上来就是 100%。
            self.done = 0.0
        self._sample()

    def progress(self, done: float | None = None, add: float = 0.0,
                 message: str | None = None, **detail) -> None:
        if done is not None:
            self.done = float(done)
        if add:
            self.done += float(add)
            self.items_done += 1
        if message is not None:
            self.message = message
            self._set_stage(message)
        if detail:
            self.detail.update(detail)
        self._sample()

    def _set_stage(self, message: str) -> None:
        """记住"当前卡在哪一步"（失败排错指纹里要用）。

        ``message`` 会被失败行覆盖（``[char_306] 失败：…``），那行本身没有定位
        价值 —— 真正有用的是它**之前**那句"② 解析剧本"之类的阶段描述。
        """
        s = " ".join(str(message or "").split())
        # 只要"看起来像在做某事"的行；失败/告警行本身就是结论，不是阶段。
        # 注意别把 ``[1/1] 安装 char_306（missing）`` 这类带方括号的进度行排掉 ——
        # 它恰恰最能说明卡在哪一部。
        if not s or s.startswith(("⚠", "✓")) or "失败" in s[:24]:
            return
        self.stage = s[:60]

    def log(self, message: str) -> None:
        self.message = message
        self._set_stage(message)
        self._sample()
        # 同时落盘：用户反馈"出问题时日志里什么都没有"，只记 DONE/FAIL 不够，
        # 安装/导出的每一步都得留痕，否则事后没法复盘。
        try:
            from . import log as applog
            applog.log(f"[{self.kind}] {message}"[:400], "job")
        except Exception as e:                                 # noqa: BLE001
            print(f"[log] 写日志失败：{e}", file=sys.stderr)

    def _audit(self, tag: str, detail: str = "") -> None:
        """任务收尾的统一审计日志（不吞异常，写不进去要吭声）。"""
        try:
            from . import log as applog
            dur = (self.ended or time.time()) - (self.started or self.created)
            applog.log(f"{tag} {self.kind}「{self.title}」 {dur:.0f}s {detail}"[:500],
                       "job")
        except Exception as e:                                 # noqa: BLE001
            print(f"[log] 写审计日志失败：{e}", file=sys.stderr)

    def fail(self, err) -> None:
        # 先摘掉单元占用，再置终态：调用方（e2e / 前端）是靠轮询 state 判断结束的，
        # 若等线程 finally 里才释放，会出现"任务已 done 但卸载仍被判为占用"的假冲突。
        _release(self)
        self.state = "failed"
        self.error = str(err)[:800]
        self.ended = time.time()
        # 失败必须留完整堆栈：只留一句 str(err) 往往看不出是哪一行炸的。
        # 但"业务失败"（如全部导出失败）是在没有异常的情况下主动调的，
        # 此时 format_exc() 只有一句 "NoneType: None"，存进去会误导排查。
        try:
            import traceback
            if sys.exc_info()[0] is not None:
                self.detail["traceback"] = traceback.format_exc()[-3000:]
        except Exception:                                      # noqa: BLE001
            pass
        diag = self._diag()
        if diag:
            self.detail["diag"] = diag
        # 审计行带上排错指纹：日后按码在 app.log 里也能捞到同一条
        self._audit("FAIL", (diag.get("text") or "") + " | " + self.error
                    if diag else self.error)

    def _diag(self) -> dict:
        """一行"排错指纹"，跟着失败一起显示在**操作记录**里。

        这一行里有判断"程序问题还是环境问题"最关键的几个量 ——
        版本 / 系统 / Python / 程序路径长度与是否含中文 / 磁盘余量 / 卡在哪一步。
        放在「报错详情」里是给用户自己核对的（页面上不做任何求助引导）。
        """
        d: dict = {"code": "", "text": ""}
        parts = []
        try:
            import hashlib
            raw = f"{self.kind}|{self.unit}|{self.error}".encode("utf-8", "replace")
            d["code"] = "#" + hashlib.md5(raw).hexdigest()[:4].upper()
            parts.append(d["code"])
        except Exception:                                      # noqa: BLE001
            pass
        try:
            from . import paths as _paths
            root = getattr(_paths, "ROOT", "") or ""
            d["ver"] = _paths.app_version()
            parts.append("v" + str(d["ver"]))
            d["path"] = root
            d["pathLen"] = len(root)
            d["nonAsciiPath"] = any(ord(c) > 127 for c in root)
            if root:
                parts.append(f"路径{len(root)}字"
                             + ("·含中文" if d["nonAsciiPath"] else ""))
        except Exception:                                      # noqa: BLE001
            pass
        try:
            import platform
            d["os"] = f"{platform.system()} {platform.version()}".strip()
            d["py"] = platform.python_version()
            parts.append(d["os"].replace("Windows", "Win"))
            parts.append("py" + d["py"])
        except Exception:                                      # noqa: BLE001
            pass
        try:
            import shutil
            from . import paths as _paths
            free = shutil.disk_usage(getattr(_paths, "ROOT", ".")).free
            d["diskFreeGB"] = round(free / 1073741824)
            parts.append(f"盘余{d['diskFreeGB']}G")
        except Exception:                                      # noqa: BLE001
            pass
        try:
            stage = (self.stage or self.message or "").strip()
            if stage:
                d["stage"] = stage[:60]
                parts.append("阶段:" + stage[:32])
        except Exception:                                      # noqa: BLE001
            pass
        try:
            from . import log as _applog
            d["log"] = os.path.basename(_applog.log_path())
            parts.append("日志:" + d["log"])
        except Exception:                                      # noqa: BLE001
            pass
        try:
            d["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:                                      # noqa: BLE001
            pass
        d["text"] = " · ".join(parts)
        return d

    def finish(self, message: str = "") -> None:
        _release(self)                     # 同上：先摘占用，再置终态
        if self.state not in ("failed", "cancelled"):
            self.state = "done"
        if self.total and self.done < self.total:
            self.done = self.total
        if message:
            self.message = message
        self.ended = time.time()
        # 这里原本写 self.elapsed —— 它不存在（只有 snapshot() 里算的字段），
        # 抛 AttributeError 被 except 吞掉，导致**所有任务的 DONE 日志都没写**。
        self._audit("DONE", message)

    # ---------------------------------------------------------------- 读
    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def paused(self) -> bool:
        return self.pause_event.is_set()

    def pause(self) -> None:
        if self.state != "running":
            return
        self.pause_event.set()
        self.message = ("已暂停 · 正在等已发出的请求收尾（最多 30 秒），"
                        "之后不会再下载新文件")

    def resume(self) -> None:
        self.pause_event.clear()

    def _sample(self) -> None:
        self._samples.append((time.time(), self.done, self.bytes_done))

    def speed(self) -> float:
        """滑动平均速度（单位/秒）。

        三个保护，都是实测踩出来的：
        * 已结束的任务不报速度（否则界面永远停在最后一个数字上）
        * 采样间隔太短不算（本地回环下载几十毫秒就完，dt≈0 会让结果爆炸成 1e15）
        * 明显不可能的值（>1e10/s）直接归零
        """
        if self.state in ("done", "failed", "cancelled") or self.paused:
            return 0.0
        if len(self._samples) < 2:
            return 0.0
        t0, d0, _b0 = self._samples[0]
        t1, d1, _b1 = self._samples[-1]
        dt = t1 - t0
        if dt < 0.5:
            return 0.0
        sp = (d1 - d0) / dt
        return sp if 0 < sp < 1e10 else 0.0

    def byte_speed(self) -> float:
        """字节速度（B/s）：已下字节 / 已用时间。

        不用"最近两个采样点的差"：BGM 这类几 MB 的包要下 30 秒才完成一个，
        采样点之间隔太久，区间速度会剧烈抖动甚至算出 0。
        整段平均更贴近用户看到的网卡速率，ETA 也才准。
        """
        if self.state in ("done", "failed", "cancelled") or self.paused:
            return 0.0
        # 没有字节口径的阶段（job.phase() 会清掉 bytes_total）不报字节速度：
        # 否则解包阶段会拿"已下总字节 / 已用时间"算出一个假的网速，
        # 而那时根本没有在下载。
        if not self.bytes_total:
            return 0.0
        # 字节已经下完 → 后面只剩解包 / 索引重建，**一个字节都不在传**。
        # 不加这条：整段平均速度会一路带着跑到最后，任务条上"38.93 MB / 38.93 MB"
        # 旁边还挂着 390 KB/s 在跳，用户会以为程序在偷跑流量（2026-11-04 反馈）。
        if self.bytes_done >= self.bytes_total:
            return 0.0
        dt = (self.ended or time.time()) - (self.started or self.created)
        if dt < 1 or self.bytes_done <= 0:
            return 0.0
        sp = self.bytes_done / dt
        return sp if 0 < sp < 1e12 else 0.0

    def eta(self) -> float:
        """剩余秒数。有字节总量时按字节算 —— 按"包个数"算会严重失真
        （音效包 60 KB、BGM 包 2.8 MB，混在一起时个数速度毫无意义）。

        分母取 ``max(已下, 预估总量)``：预估可能偏小（没装过的剧情算不出图片包），
        用旧值算出来的"剩余"是负数，白白把 ETA 压成 0。
        """
        bt = max(self.bytes_total, self.bytes_done)
        if bt and self.bytes_done and self.bytes_done < bt:
            sp = self.byte_speed()
            if sp > 0:
                return max(0.0, (bt - self.bytes_done) / sp)
        sp = self.speed()
        if sp <= 0 or not self.total or self.done >= self.total:
            return 0.0
        return max(0.0, (self.total - self.done) / sp)

    def snapshot(self) -> dict:
        sp = self.speed()
        # 百分比口径必须和界面上"看得见的数字"一致：
        # 多单元安装（items_total>1）时，顶层百分比由「第 i/N 部」驱动，
        # 这样装 70 部剧情只会从 0 平稳爬到 100%，不会每装一部、每下一批
        # 都在 100% 处反复跳满（"剧情100%→立绘100%→语音100%"）。
        # 有字节总量就按字节算（同一条任务条里写着 12.3 / 893 MB），
        # 否则按项数算（包 / 帧 / 张）。
        # 以前一律按项数，于是出现「70.9% · 8.40 MB / 893.01 MB」这种
        # 两个数字互相打脸的进度（8 MB 才 1%）。
        if self.items_total > 1:
            sub_frac = (self.sub_done / self.sub_total) if self.sub_total else 0.0
            # 0.95 而不是 1.0：当前这部的下载批"下完了"时，解包还没做完，
            # 若让 sub_frac 顶到 1，多单元安装会在**倒数第一部还没装完**时
            # 就把进度条画到 100%（实测 3 部：第 2 部结束时 100%）。
            sub_frac = min(0.95, max(0.0, sub_frac))
            inner = 100.0 * (self.items_done + sub_frac) / self.items_total
            inner = min(100.0, max(0.0, inner))
        elif self.bytes_total:
            inner = 100.0 * self.bytes_done / self.bytes_total
        elif self.total:
            inner = 100.0 * self.done / self.total
        else:
            inner = 0.0
        inner = min(100.0, max(0.0, inner))
        pct = self.pct_base + inner * self.pct_span / 100.0
        pct = min(100.0, max(0.0, pct))
        # 进度条只许前进：多单元安装时下一部的第一批会把 sub 归零，
        # 不设高水位就会看到 66% → 33% 这种倒退（用户以为任务重启了）。
        self._pct_hi = max(self._pct_hi, pct)
        pct = self._pct_hi
        # 字节分母可能估小：预扫只能读到已解包的剧本（没装过的剧情算不出图片包），
        # 于是会出现「已下 5.63 MB / 总量 0.01 MB」。分母永远不许小于分子。
        bd = self.bytes_done
        bt = max(self.bytes_total, bd)
        return {
            "id": self.id, "kind": self.kind, "title": self.title,
            "unit": self.unit, "state": self.state, "message": self.message,
            "paused": self.paused, "error": self.error,
            # 结束时间：前端据此判断"结束的任务留 6 秒再收起来"。
            # 不暴露它的话，前端只能拿 started+elapsed 推算，容易算错。
            "ended": self.ended,
            "started": self.started, "created": self.created,
            "total": self.total, "done": self.done,
            "percent": round(pct, 2),
            "speed": sp, "eta": self.eta(),
            # 进度条的字节维度：已下大小 / 总大小 / 字节速度
            "bytesDone": bd, "bytesTotal": bt,
            "bytesSpeed": self.byte_speed(),
            "itemsTotal": self.items_total, "itemsDone": self.items_done,
            "subTotal": self.sub_total, "subDone": self.sub_done,
            "subLabel": self.sub_label,
            "meta": self.meta,
            "detail": self.detail,
            "created": self.created, "started": self.started,
            "elapsed": (self.ended or time.time()) - (self.started or self.created),
        }


# ---------------------------------------------------------------- 单元占用
def _hold(job: Job, units) -> None:
    with _UNITS_LOCK:
        for u in units or ():
            _UNIT_HOLDERS[str(u)] = job.id


def _release(job: Job) -> None:
    with _UNITS_LOCK:
        for u in [u for u, jid in _UNIT_HOLDERS.items() if jid == job.id]:
            _UNIT_HOLDERS.pop(u, None)


def holders(units) -> list[dict]:
    """这些单元正被哪些**在跑的**任务占着 → [{unit, id, title, kind}]。

    卸载前必须查它：占着就拒绝，否则会把别的任务正在读的文件删掉。
    """
    with _UNITS_LOCK:
        pairs = [(str(u), _UNIT_HOLDERS.get(str(u))) for u in units or ()]
    out, seen = [], set()
    for u, jid in pairs:
        if not jid or (u, jid) in seen:
            continue
        seen.add((u, jid))
        j = get(jid)
        if j and j.state in ("queued", "running"):
            out.append({"unit": u, "id": j.id, "title": j.title, "kind": j.kind})
    return out


# ---------------------------------------------------------------- 管理器
def submit(kind: str, title: str, fn, unit: str = "", holds=(), **meta) -> Job:
    """提交后台任务。``fn(job)`` 在工作线程里跑。

    ``holds`` 是"本任务要用到哪些剧情单元"（如 ``["char_2"]``）。**在返回前同步登记**，
    不是等线程跑起来再登记 —— 否则 submit 与线程启动之间有一小段窗口，刚好被
    "提交导出 → 立刻卸载" 这种连点命中。
    """
    job = Job(kind, title, unit=unit, **meta)
    with _LOCK:
        _JOBS[job.id] = job
        while len(_JOBS) > MAX_KEEP:
            _JOBS.popitem(last=False)
    _hold(job, holds)

    def runner():
        job.start()
        try:
            fn(job)
            if job.cancelled:
                job.state = "cancelled"
            elif not job.ended:
                # fn 里自己调过 finish()/fail() 就别再收一次尾。
                # 以前无条件调，结果是每个任务都在日志里出现两遍 DONE，
                # 失败的任务还会被补写一条 "DONE" 掩盖掉 FAIL。
                job.finish()
        except Exception as e:                                   # noqa: BLE001
            job.fail(e)
            with open(_errlog(), "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%F %T')}] {job.id} {job.title}\n")
                f.write(traceback.format_exc())
        finally:
            _release(job)
            job.ended = job.ended or time.time()

    threading.Thread(target=runner, name=job.id, daemon=True).start()
    return job


def _errlog() -> str:
    from . import paths
    paths.ensure_dirs()
    return f"{paths.LOGS}/errors.log"


def get(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)


def cancel(job_id: str) -> bool:
    job = get(job_id)
    if not job or job.state in ("done", "failed", "cancelled"):
        return False
    job.cancel_event.set()
    _release(job)                      # 取消即视为不再使用这些单元
    job.state = "cancelled"
    job.message = "已取消"
    # 必须记结束时间：前端按 (结束时间 - 现在) 判断"留 6 秒就收起来"。
    # 不写这一行，elapsed 会随当前时间一直变大，任务永远处在"刚结束"的窗口里，
    # 于是取消的任务会一直挂在底部条上（用户看到的就是"点了取消也不消失"）。
    if not job.ended:
        job.ended = time.time()
    return True


def pause(job_id: str) -> bool:
    job = get(job_id)
    if not job or job.state != "running":
        return False
    job.pause()
    return True


def resume(job_id: str) -> bool:
    job = get(job_id)
    if not job or job.state != "running":
        return False
    job.resume()
    job.message = "继续下载…"
    return True


def active() -> list[dict]:
    with _LOCK:
        return [j.snapshot() for j in _JOBS.values()
                if j.state in ("queued", "running")]


def all_jobs() -> list[dict]:
    with _LOCK:
        return [j.snapshot() for j in reversed(_JOBS.values())]


def clear_finished() -> int:
    with _LOCK:
        gone = [k for k, j in _JOBS.items()
                if j.state in ("done", "failed", "cancelled")]
        for k in gone:
            _JOBS.pop(k, None)
        return len(gone)
