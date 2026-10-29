"""CDN 下载层：UA 伪装、并发、断点续传、size 校验。

复用而非重写的点
----------------
并发数 48–64、按 ``size`` 校验、已存在且大小一致则跳过——这些是旧
``download_char_story.py`` 里实测出来的结论（瓶颈是单连接吞吐与 TLS 握手，
不是带宽），这里保留同样的策略，只是抽成可复用的函数。

**不要给 URL 加缓存破坏参数**（``?x=2510062231`` 之类），那是前端缓存时间戳，
带上只会过几天失效；裸基础路径才是稳定的。
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import UA

DEFAULT_WORKERS = 48
DEFAULT_TIMEOUT = 180
MAX_RETRY = 3

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def head(url: str) -> dict:
    """取响应头（用于判断资源新鲜度）。不抛异常，失败返回空 dict。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return dict(r.headers)
    except Exception:
        return {}


def http_get(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """GET 一个 URL，返回原始字节。失败直接抛出，由调用方决定是否重试。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def bundle_url(name: str) -> str:
    """资源名 -> 完整 CDN URL。

    基础地址每次现取（:func:`config.cdn_base`），这样用户在设置里改了域名
    立刻生效，不用重启服务。没配置时给出能看懂的报错，而不是拼出 "/bundle_xxx"
    这种必然 404 的地址。
    """
    from . import config
    base = config.cdn_base()
    if not base:
        raise RuntimeError(
            "还没有配置资源源地址：请在「设置 → 下载源」填写，"
            "或确认 web/data/packs.links.js 存在且未被改动")
    return base + urllib.parse.quote(name, safe="")


# 图片类包必须落在 files_img：export_images / compose_stand_all.py 都只在那里找。
# 曾经这里一律丢进 char_story/files，导致新 CG 下载成功却"导出失败"，白下一遍。
IMG_PREFIXES = ("bundle_bgimage", "bundle_standimage_", "bundle_standimages_",
                "bundle_eventimage_", "bundle_eventthums_",
                "bundle_image_story_", "bundle_battlebackground_")


def bundle_path(name: str) -> str:
    """资源名 -> 本地存放路径（按包类型分目录，与旧下载脚本一致）。

    - 图片类（背景/立绘/CG）  → ``files_img``
    - 音乐/音效               → ``files_audio``
    - 其余（剧本/语音/头像）   → ``char_story/files``
    """
    from .config import FILES_AUDIO, FILES_IMG, FILES_STORY
    if name.startswith(IMG_PREFIXES):
        return os.path.join(FILES_IMG, name)
    if name.startswith(("bundle_music_", "bundle_sound_")):
        return os.path.join(FILES_AUDIO, name)
    return os.path.join(FILES_STORY, name)


def need_download(path: str, size: int) -> bool:
    """断点续传判据：本地已存在且字节数一致就认为完成。"""
    return not (os.path.exists(path) and os.path.getsize(path) == size)


def fetch_bundle(name: str, size: int, dest: str | None = None,
                 workers_safe: bool = True) -> tuple[str, bool, int]:
    """下载单个资源包。返回 ``(name, 是否为本次新下载, 字节数)``。

    ``workers_safe`` 仅用于语义标注，实际重试/退避逻辑始终生效。

    **超时按体积放大**：CDN 单连接实测只有 60–100 KB/s，固定 180 s 超时意味着
    超过 ~16 MB 的包必然 `IncompleteRead`/超时失败（全量下载时正好卡住 11 个
    10–27 MB 的语音包，日志表现为"速度掉到 0、失败数不再增加"）。
    """
    path = dest or bundle_path(name)
    if not need_download(path, size):
        return name, False, 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    url = bundle_url(name)
    # 60 KB/s 估算 + 120 s 余量，且不低于默认 180 s
    timeout = int(max(DEFAULT_TIMEOUT, 120 + size / 60000))
    last = None
    for attempt in range(MAX_RETRY):
        try:
            blob = http_get(url, timeout=timeout)
            if len(blob) != size:
                raise ValueError(f"size {len(blob)} != {size}")
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)          # 原子替换，避免半截文件被当成已完成
            return name, True, size
        except Exception as e:
            last = e
            time.sleep(1 + attempt * 2)
    raise IOError(f"下载失败 {name}: {last}")


def fetch_many(items, workers: int = DEFAULT_WORKERS, desc: str = "bundle",
               skip_existing: bool = True, pause_event=None,
               job=None, done_offset: int = 0) -> dict:
    """并发下载一批 ``(name, size[, dest])``。

    返回 ``{"ok": n, "new": n, "skip": n, "fail": [(name, err)], "bytes": n}``。
    """
    tasks = []
    for it in items:
        if isinstance(it, (tuple, list)):
            name, size = it[0], int(it[1])
            dest = it[2] if len(it) > 2 else None
        else:                                   # dict（清单条目）
            name, size, dest = it["bundle_name"], int(it["size"]), None
        tasks.append((name, size, dest))

    if skip_existing:
        left = [t for t in tasks if need_download(t[2] or bundle_path(t[0]), t[1])]
    else:
        left = tasks

    stat = {"ok": 0, "new": 0, "skip": len(tasks) - len(left), "fail": [], "bytes": 0}
    total = sum(t[1] for t in left)
    _log(f"[{desc}] 共 {len(tasks)} 个，需下载 {len(left)} 个 / "
         f"{total / 1048576:.1f} MB，并发 {workers}")

    if not left:
        return stat

    t0 = time.time()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        # 暂停检查放在 worker 里：暂停后不再领新包，已经在传的会传完
        # （几 MB、几秒内结束，不会留下半个文件）
        def _worker(nm, sz, ds):
            while pause_event is not None and pause_event.is_set():
                time.sleep(0.25)
            return fetch_bundle(nm, sz, ds)

        futs = {ex.submit(_worker, n, s, d): (n, s) for n, s, d in left}
        # 逐包上报：不报的话前端进度条永远 0%，用户只能看网卡猜
        if job is not None:
            # 已经有总量（公共素材会是"三类之和"）就别覆盖，只接着往上加
            if not job.total:
                job.set_total(done_offset + len(left), "个")
            job.progress(done=done_offset)
        for fut in cf.as_completed(futs):
            n, sz = futs[fut]
            done += 1
            if job is not None:
                job.progress(done=done_offset + done)
            try:
                _, is_new, nbytes = fut.result()
                if job is not None:
                    # 用清单里的包大小累加，而不是 nbytes（本次新下的字节）。
                    # nbytes 在"包已存在、跳过"时是 0，会出现
                    # "601/1323 个但 0.0/771 MB" 这种自相矛盾的进度。
                    job.bytes_done += sz
                stat["ok"] += 1
                stat["bytes"] += nbytes
                if is_new:
                    stat["new"] += 1
            except Exception as e:
                stat["fail"].append((n, str(e)))
            if done % 50 == 0 or done == len(left):
                el = max(time.time() - t0, 1e-6)
                sp = stat["bytes"] / el / 1048576
                _log(f"  [{done}/{len(left)}] {stat['bytes'] / 1048576:.0f} MB "
                     f"{sp:.1f} MB/s  失败 {len(stat['fail'])}")

    el = max(time.time() - t0, 1e-6)
    _log(f"[{desc}] 完成：新下 {stat['new']}，跳过 {stat['skip']}，"
         f"失败 {len(stat['fail'])}，{stat['bytes'] / 1048576:.1f} MB，{el:.0f}s")
    for n, e in stat["fail"][:10]:
        _log(f"   失败 {n}: {e}")
    return stat
