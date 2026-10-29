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
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque

MAX_KEEP = 40
_LOCK = threading.RLock()
_JOBS: "OrderedDict[str, Job]" = OrderedDict()
_SEQ = itertools.count(1)


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
        self.error = ""
        self.total = 0.0
        self.done = 0.0
        self.bytes_total = 0.0
        self.bytes_done = 0.0
        self.items_total = 0
        self.items_done = 0
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
        self.total = float(n)
        if unit:
            # snapshot() 读的是 self.unit —— 以前写进了 detail["unit"]，
            # 前端永远拿不到，于是"505 个包"被按字节显示成 "470 B/505 B"。
            self.unit = unit
        self._sample()

    def set_items(self, n: int) -> None:
        self.items_total = int(n)

    def progress(self, done: float | None = None, add: float = 0.0,
                 message: str | None = None, **detail) -> None:
        if done is not None:
            self.done = float(done)
        if add:
            self.done += float(add)
            self.items_done += 1
        if message is not None:
            self.message = message
        if detail:
            self.detail.update(detail)
        self._sample()

    def log(self, message: str) -> None:
        self.message = message
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
        self.state = "failed"
        self.error = str(err)[:800]
        self.ended = time.time()
        # 失败必须留完整堆栈：只留一句 str(err) 往往看不出是哪一行炸的
        try:
            import traceback
            self.detail["traceback"] = traceback.format_exc()[-3000:]
        except Exception:                                      # noqa: BLE001
            pass
        self._audit("FAIL", self.error)

    def finish(self, message: str = "") -> None:
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
        dt = (self.ended or time.time()) - (self.started or self.created)
        if dt < 1 or self.bytes_done <= 0:
            return 0.0
        sp = self.bytes_done / dt
        return sp if 0 < sp < 1e12 else 0.0

    def eta(self) -> float:
        """剩余秒数。有字节总量时按字节算 —— 按"包个数"算会严重失真
        （音效包 60 KB、BGM 包 2.8 MB，混在一起时个数速度毫无意义）。"""
        if self.bytes_total and self.bytes_done and self.bytes_done < self.bytes_total:
            sp = self.byte_speed()
            if sp > 0:
                return max(0.0, (self.bytes_total - self.bytes_done) / sp)
        sp = self.speed()
        if sp <= 0 or not self.total or self.done >= self.total:
            return 0.0
        return max(0.0, (self.total - self.done) / sp)

    def snapshot(self) -> dict:
        sp = self.speed()
        pct = (100.0 * self.done / self.total) if self.total else 0.0
        return {
            "id": self.id, "kind": self.kind, "title": self.title,
            "unit": self.unit, "state": self.state, "message": self.message,
            "paused": self.paused, "error": self.error,
            # 结束时间：前端据此判断"结束的任务留 6 秒再收起来"。
            # 不暴露它的话，前端只能拿 started+elapsed 推算，容易算错。
            "ended": self.ended,
            "started": self.started, "created": self.created,
            "total": self.total, "done": self.done,
            "percent": round(min(100.0, max(0.0, pct)), 2),
            "speed": sp, "eta": self.eta(),
            # 进度条的字节维度：已下大小 / 总大小 / 字节速度
            "bytesDone": self.bytes_done, "bytesTotal": self.bytes_total,
            "bytesSpeed": self.byte_speed(),
            "bytesTotal": self.bytes_total, "bytesDone": self.bytes_done,
            "itemsTotal": self.items_total, "itemsDone": self.items_done,
            "detail": self.detail,
            "created": self.created, "started": self.started,
            "elapsed": (self.ended or time.time()) - (self.started or self.created),
        }


# ---------------------------------------------------------------- 管理器
def submit(kind: str, title: str, fn, unit: str = "", **meta) -> Job:
    """提交后台任务。``fn(job)`` 在工作线程里跑。"""
    job = Job(kind, title, unit=unit, **meta)
    with _LOCK:
        _JOBS[job.id] = job
        while len(_JOBS) > MAX_KEEP:
            _JOBS.popitem(last=False)

    def runner():
        job.start()
        try:
            fn(job)
            if job.cancelled:
                job.state = "cancelled"
            else:
                job.finish()
        except Exception as e:                                   # noqa: BLE001
            job.fail(e)
            with open(_errlog(), "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%F %T')}] {job.id} {job.title}\n")
                f.write(traceback.format_exc())
        finally:
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
