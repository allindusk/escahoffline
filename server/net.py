"""多线程分段下载（断点续传 / 重试 / 取消 / 限速观测）。

为什么不用第三方库
------------------
项目要"下载便携包即用"，不能要求用户装 pip 包。标准库 ``urllib`` 足够：
我们只需要 Range 请求 + 自己拼段。

分段策略
--------
1. 先发 ``Range: bytes=0-0`` 探测总长度与是否支持 Range
2. 不支持 Range / 文件小于 4 MB / 线程数 1 → 单线程整块下载（最稳）
3. 否则按线程数切成等长段，各线程独立 Range 请求，seek+write 到同一个
   ``.part`` 文件（写入用一把锁，段之间不重叠所以不会互相覆盖）
4. 断点续传靠 ``.part.json`` 记录每段已完成字节；中断后重跑从断点继续

注意：服务器必须真的支持 Range，很多网盘直链是支持的；如果实测返回 200
（整块），会自动退化成单线程，不会写出错位的文件。
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CHUNK = 256 * 1024
MIN_SEG_BYTES = 4 * 1024 * 1024       # 小于这个就不分段了，收益不如风险大


class DownloadError(RuntimeError):
    pass


def _open(url: str, headers: dict | None = None, timeout: float = 30):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=timeout)


def probe(url: str, headers: dict | None = None) -> tuple[int, bool]:
    """→ (总字节数, 是否支持 Range)。探测失败抛 DownloadError。"""
    try:
        h = {"Range": "bytes=0-0"}
        if headers:
            h.update(headers)
        with _open(url, h) as r:
            code = getattr(r, "status", r.getcode())
            cr = r.headers.get("Content-Range", "")
            m = re.search(r"/(\d+)", cr or "")
            if code == 206 and m:
                return int(m.group(1)), True
            cl = int(r.headers.get("Content-Length") or 0)
            return cl, False
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise DownloadError(f"HTTP {e.code}：链接无效或已过期") from e
        raise DownloadError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise DownloadError(f"网络不可用：{e}") from e


def _load_part_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_part_state(path: str, st: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    try:
        os.replace(tmp, path)
    except OSError:
        pass


def download(url: str, dest: str, threads: int = 4, job=None,
             timeout: float = 30, headers: dict | None = None) -> str:
    """下载到 ``dest``（原子替换）。返回 dest。

    ``job`` 需提供 ``cancelled``、``progress(bytes_done, message)``。
    """
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    size, ranges = probe(url, headers)
    if job is not None:
        job.set_total(size or 0)
        job.bytes_total = size or 0
        job.progress(0, message=f"开始下载 {size / 1024 ** 2:.1f} MB")

    part = dest + ".part"
    stf = dest + ".part.json"
    st = _load_part_state(stf)
    fresh = st.get("url") != url or st.get("size") != size
    segs = [(0, max(0, size - 1))] if (not ranges or threads <= 1
                                       or size < MIN_SEG_BYTES) else _split(size, threads)
    if fresh:
        st = {"url": url, "size": size, "done": [0] * len(segs), "segs": segs}
        _save_part_state(stf, st)
        with open(part, "wb") as f:      # 预分配，避免边写边扩容在机械盘上抖动
            if size:
                f.truncate(size)
    else:
        segs = [tuple(s) for s in st.get("segs", segs)]
        if len(st.get("done", [])) != len(segs):
            st["done"] = [0] * len(segs)

    lock = threading.Lock()
    stop = threading.Event()
    errs: list[str] = []
    progress = {"bytes": sum(st["done"])}

    def _report(msg: str = "") -> None:
        if job is None:
            return
        with lock:
            b = progress["bytes"]
        job.bytes_done = b
        job.progress(min(b, size or b), message=msg or
                     f"{b / 1024 ** 2:.1f} / {size / 1024 ** 2:.1f} MB")

    def _fetch(i: int, start: int, end: int) -> None:
        pos = start + st["done"][i]
        for attempt in range(3):
            if stop.is_set() or (job is not None and job.cancelled):
                return
            try:
                h = {"Range": f"bytes={pos}-{end}"}
                if headers:
                    h.update(headers)
                with _open(url, h, timeout) as r, open(part, "r+b") as out:
                    if getattr(r, "status", 200) != 206 and pos != 0:
                        raise DownloadError("服务器不支持断点续传")
                    while pos <= end:
                        if stop.is_set() or (job is not None and job.cancelled):
                            return
                        buf = r.read(min(CHUNK, end - pos + 1))
                        if not buf:
                            break
                        with lock:
                            out.seek(pos)
                            out.write(buf)
                            st["done"][i] = pos + len(buf) - start
                            progress["bytes"] += len(buf)
                        pos += len(buf)
                        _report()
                if pos > end:
                    return
            except Exception as e:                               # noqa: BLE001
                errs.append(str(e))
                time.sleep(1.2 * (attempt + 1))
        errs.append(f"分段 {i} 下载失败")

    t0 = time.time()
    ths = [threading.Thread(target=_fetch, args=(i, s, e), daemon=True)
           for i, (s, e) in enumerate(segs)]
    for t in ths:
        t.start()
    try:
        while any(t.is_alive() for t in ths):
            for t in ths:
                t.join(0.5)
            if job is not None and job.cancelled:
                stop.set()
                raise DownloadError("已取消")
            _save_part_state(stf, st)
        for t in ths:
            t.join()
    finally:
        _save_part_state(stf, st)

    if job is not None and job.cancelled:
        raise DownloadError("已取消")
    if size and os.path.getsize(part) < size:
        raise DownloadError(f"下载不完整：{os.path.getsize(part)}/{size}"
                            f"（{errs[-1] if errs else '未知原因'}）")
    os.replace(part, dest)
    for f in (stf,):
        try:
            os.remove(f)
        except OSError:
            pass
    if job is not None:
        job.progress(size or os.path.getsize(dest),
                     message=f"下载完成，用时 {time.time() - t0:.0f}s")
    return dest


def _split(size: int, n: int) -> list[tuple[int, int]]:
    n = max(1, min(n, 16))
    seg = size // n
    out = []
    for i in range(n):
        s = i * seg
        e = size - 1 if i == n - 1 else (i + 1) * seg - 1
        out.append((s, e))
    return out


def fetch_text(url: str, timeout: float = 20, headers: dict | None = None) -> str:
    with _open(url, headers, timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_json(url: str, timeout: float = 20, headers: dict | None = None):
    h = {"Accept": "application/vnd.github+json"}
    if headers:
        h.update(headers)
    return json.loads(fetch_text(url, timeout, h))
