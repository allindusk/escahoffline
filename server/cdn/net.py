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

#: 单次 ``read`` 的空闲超时（秒）：连接建立后多久收不到数据就认为它挂了。
#: **不能**再按"整包超时按体积放大"来给（老实现小包 180 s、大包 400 s+）：
#: 一条被 CDN 挂住、既不吐数据也不 RST 的连接，会让那一路线程干等 3~7 分钟，
#: 期间任务条一个字节都不涨 —— 用户看到的就是"有速度但进度不动"（2026-11-17）。
#: 正常传输时每收到一块都会重置这个计时，所以慢速连接也不会被误杀。
IDLE_TIMEOUT = 60

#: 单次读取块大小。128 KB 在"进度够细"与"syscall 不过多"之间折中。
CHUNK = 128 * 1024

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    """下载进度只写 app.log，**绝不再 print 到启动窗口**。

    以前的实现是 ``print(msg, flush=True)``。Windows 控制台有个特性：一旦进入
    "选择 / 标记"模式（用户随手在窗口里点了一下、框选了文字，甚至只是按住鼠标），
    conhost 会**冻结**所有往 stdout 写的程序 —— ``print`` 阻塞在管道里不返回。
    而 ``fetch_many`` 每 50 个包就在主线程里 _log 一次，于是：

        22:43:22 [job] DONE install「安装 3 部剧情」193s
        22:49:42 [job] [base] 下载音效 505 个包 / 31 MB      ← 空白 6 分 20 秒

    BGM 早就下完了（包数/字节都停在 818 / 736 MB），主线程却卡在这一行 print 上，
    下载线程全空闲（实测只有 3 条无关连接）。用户看到的是"有速度但进度不动"，
    因为当时的速度还是整段平均值（2026-11-17 实测复现）。

    这与 ``stages.run_script(quiet=True)`` 的口径一致：启动窗口里只留横幅，
    运行日志一律进 ``userdata/logs/app.log``。
    """
    try:
        from .. import log as applog
        applog.log(msg, tag="cdn")
    except Exception:                                        # noqa: BLE001
        # 日志系统本身不可用时也不能让下载停摆：静默丢弃这行即可。
        pass


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
                 workers_safe: bool = True,
                 on_progress=None) -> tuple[str, bool, int]:
    """下载单个资源包。返回 ``(name, 是否为本次新下载, 字节数)``。

    ``workers_safe`` 仅用于语义标注，实际重试/退避逻辑始终生效。

    ``on_progress(已收字节)``：**逐块**回调，让任务条能看到单个包内部的进展。
    以前是 ``http_get`` 一次 ``read()`` 把整包读进内存、成功了才写盘 ——
    一个 20 MB 的包在 60 KB/s 的连接上要传 5 分钟，这 5 分钟里界面一个字节都不
    涨（2026-11-17 用户："有速度但是一直卡进度"，实测卡了 6 分钟）。
    回调给的是**该包累计已收字节**（不是增量）：断线重连后数字会回退再涨，
    调用方直接覆盖即可，不用担心重复累加。

    三处为此改动：
    * **分块流式落盘**：边收边写 ``.part``，进度可观测；
    * **空闲超时** ``IDLE_TIMEOUT``：以前是"整包超时按体积放大"（小包 180 s），
      一条被 CDN 挂住、既不吐数据也不断开的连接要干等 180 s×3 次重试才继续，
      这就是"卡在 818/3258 不动"的直接原因；
    * **Range 续传**：断了从 ``.part`` 的大小接着传，不再整包重来
      （老实现重试 3 次 = 同一个 27 MB 包从头下三次）。
    """
    path = dest or bundle_path(name)
    if not need_download(path, size):
        return name, False, 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    url = bundle_url(name)
    part = path + ".part"
    have = 0
    try:
        if os.path.exists(part):
            have = os.path.getsize(part)
            if have > size:                # 比清单还大 → 脏文件（旧版本的包）
                os.remove(part)
                have = 0
    except OSError:
        have = 0
    if have and have == size:
        # 上次已经把字节收齐、只是没来得及改名
        try:
            os.replace(part, path)
            return name, True, size
        except OSError:
            have = 0
    last = None
    for attempt in range(MAX_RETRY + 1):
        try:
            if on_progress is not None:
                on_progress(have)
            have = _download_stream(url, part, size, have, on_progress)
            if have != size:
                raise ValueError(f"size {have} != {size}")
            os.replace(part, path)         # 原子替换，避免半截文件被当成已完成
            return name, True, size
        except Exception as e:             # noqa: BLE001
            last = e
            try:
                have = os.path.getsize(part) if os.path.exists(part) else 0
            except OSError:
                have = 0
            if have > size:
                try:
                    os.remove(part)
                except OSError:
                    pass
                have = 0
            if attempt < MAX_RETRY:
                time.sleep(1 + attempt * 2)    # 退避；下一轮尽量带着 Range 续传
    raise IOError(f"下载失败 {name}: {last}")


def _apply_source_mtime(path: str, last_modified: str | None) -> None:
    """把**源文件时间**（CDN 的 ``Last-Modified``）写到本地文件的 mtime 上。

    下载器默认把"现在"写成本地时间，于是 ``os.stat`` 只能看到"我哪天下的"，
    跟源文件毫无关系 —— 用户 2026-11-25 问"是不是只能查到本机相关的日期"，
    答案本来是：源文件时间只有 HTTP 头里有，本地这份早就丢了。写回之后，
    以后离线也能看出包的源文件时间（所有人一致）。

    注意：只改 mtime。Windows 的 ctime 是"创建时间"、改不了，所以查源文件时间
    请读 mtime（``os.path.getmtime``）。解析失败就什么都不做。
    """
    from email.utils import parsedate_to_datetime
    if not last_modified:
        return
    try:
        dt = parsedate_to_datetime(last_modified)
        if dt is None:
            return
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except Exception:                                          # noqa: BLE001
        pass


def _download_stream(url: str, part: str, size: int, have: int = 0,
                     on_progress=None) -> int:
    """把 ``url`` 流式写进 ``part``（从 ``have`` 字节处续传），返回最终字节数。

    服务器不认 ``Range``（回 200 而不是 206）时自动退回"从头写"：宁可多传一遍，
    也不能把两段内容拼成一个坏包。
    """
    headers = {"User-Agent": UA}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    lm = ""
    with urllib.request.urlopen(req, timeout=IDLE_TIMEOUT) as r:
        status = getattr(r, "status", 200)
        if have and status != 206:
            have = 0
        lm = r.headers.get("Last-Modified", "") or ""
        with open(part, "r+b" if have else "wb") as f:
            if have:
                f.seek(have)
            while True:
                buf = r.read(CHUNK)
                if not buf:
                    break
                f.write(buf)
                have += len(buf)
                if on_progress is not None:
                    on_progress(have)
    # 源文件时间落到本地 mtime（改名前设置，os.replace 会保留它）
    _apply_source_mtime(part, lm)
    return have


def fetch_many(items, workers: int = DEFAULT_WORKERS, desc: str = "bundle",
               skip_existing: bool = True, pause_event=None,
               job=None, done_offset: int = 0,
               count_skipped: bool = True) -> dict:
    """并发下载一批 ``(name, size[, dest])``。

    返回 ``{"ok": n, "new": n, "skip": n, "fail": [(name, err)], "bytes": n}``。

    ``count_skipped``：已存在（本次跳过）的包，其字节是否计入 ``job.bytes_done``。
    默认算 —— 任务条上的"已下 / 总量"必须是**全量里的已完成部分**，只算本次新下的
    就会出现「70.9% · 8.40 MB / 893.01 MB」：用户补全过一次，几百个包已在磁盘上，
    一个字节都不用下，却按 893 MB 的分母显示。调用方若已自己预扫过一遍
    （见 ``install.install_base``），传 False 避免重复累加。
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
    skipped_bytes = sum(t[1] for t in tasks) - total
    _log(f"[{desc}] 共 {len(tasks)} 个，需下载 {len(left)} 个 / "
         f"{total / 1048576:.1f} MB，并发 {workers}")

    # 进度上报放在"没有要下的包就返回"之前：全部已存在的阶段（补全时很常见）
    # 也得把跳过的量记进去，否则字节进度会一直停在上一阶段的数字上。
    # sub_mode：顶层进度由上层（多单元安装）按"第 i/N 部"统一驱动，这里把
    # 每一批 bundle 的包计数写进 sub，别去重置顶层 total/done（否则会反复冲到 100%）。
    sub = getattr(job, "sub_mode", False) if job is not None else False
    if job is not None:
        if count_skipped and skipped_bytes:
            job.bytes_done += skipped_bytes
        need_total = done_offset + len(tasks)
        if sub:
            job.sub_progress(done_offset + stat["skip"], need_total)
        else:
            # 总量只增不减：公共素材那边是"五类之和"（一次给全），
            # 单部剧情则是分批 fetch，分母要跟着这批的包数长大，
            # 否则会出现 "done 28 / total 2" 这种分母比分子还小的进度。
            if job.total < need_total:
                job.set_total(need_total, "个")
            # 任务条那句必须跟着批次走：以前只在 install 里 ``job.log`` 一次，
            # 于是换到下一类（BGM → 音效 → 头像）时 message 还停在上一类，
            # 用户看到「下载BGM」却已经在传音效，只会更困惑（2026-11-17）。
            job.progress(done=done_offset + stat["skip"],
                         message=(f"下载{desc}：{len(left)} 个包 / "
                                  f"{total / 1048576:.0f} MB…") if left else None)

    if not left:
        return stat

    t0 = time.time()
    done = 0
    # 字节口径拆成两半：
    #   · 已完成的包 → 按**清单 size** 记账（与分母同口径，不会自相矛盾）；
    #   · 正在传的包 → 逐块覆盖的"已收字节"（大包也有真实进展）。
    # 以前只在整包结束时 ``job.bytes_done += sz``：一个 20 MB 的包传 5 分钟，
    # 这 5 分钟里进度条一格不动，用户只能看到历史平均速度在跳（2026-11-17 反馈）。
    base_bytes = float(getattr(job, "bytes_done", 0.0) or 0.0) if job is not None else 0.0
    finished_bytes = 0
    inflight: dict = {}
    ilock = threading.Lock()
    last_done_at = time.time()
    last_note_at = 0.0

    def _sync_bytes() -> None:
        if job is None:
            return
        with ilock:
            live = sum(inflight.values())
        writer = getattr(job, "bytes_progress", None)
        target = base_bytes + finished_bytes + live
        if writer is not None:
            writer(target)
        elif target > job.bytes_done:          # 兼容没有该方法的旧 Job
            job.bytes_done = target

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        # 暂停检查放在 worker 里：暂停后不再领新包，已经在传的会传完
        # （几 MB、几秒内结束，不会留下半个文件）
        def _worker(nm, sz, ds):
            while pause_event is not None and pause_event.is_set():
                time.sleep(0.25)

            def _rep(nbytes: int) -> None:
                with ilock:
                    inflight[nm] = int(nbytes)

            try:
                return fetch_bundle(nm, sz, ds,
                                    on_progress=_rep if job is not None else None)
            finally:
                with ilock:
                    inflight.pop(nm, None)

        futs = {ex.submit(_worker, n, s, d): (n, s) for n, s, d in left}
        # 逐包上报：不报的话前端进度条永远 0%，用户只能看网卡猜。
        # 用 wait(timeout) 轮询而不是 as_completed：中途要把"在传字节"刷进任务条。
        pending = set(futs)
        while pending:
            finished, pending = cf.wait(pending, timeout=0.5)
            _sync_bytes()
            for fut in finished:
                n, sz = futs[fut]
                done += 1
                if job is not None:
                    if sub:
                        job.sub_progress(done_offset + stat["skip"] + done, need_total)
                    else:
                        job.progress(done=done_offset + stat["skip"] + done)
                try:
                    _, is_new, nbytes = fut.result()
                    # 用清单里的包大小累加，而不是 nbytes（本次新下的字节）。
                    # nbytes 在"包已存在、跳过"时是 0，会出现
                    # "601/1323 个但 0.0/771 MB" 这种自相矛盾的进度。
                    finished_bytes += sz
                    stat["ok"] += 1
                    stat["bytes"] += nbytes
                    if is_new:
                        stat["new"] += 1
                except Exception as e:         # noqa: BLE001
                    stat["fail"].append((n, str(e)))
                last_done_at = time.time()
                if done % 50 == 0 or done == len(left):
                    el = max(time.time() - t0, 1e-6)
                    sp = stat["bytes"] / el / 1048576
                    _log(f"  [{done}/{len(left)}] {stat['bytes'] / 1048576:.0f} MB "
                         f"{sp:.1f} MB/s  失败 {len(stat['fail'])}")
            # 刚完成的包要从"在传字节"转进"已完成字节"：不在这里再同步一次，
            # 最后一个包（尤其是最后一批）的 size 就永远不会进任务条
            # （测试实测：bytes_done 停在 0）。
            _sync_bytes()
            if not pending:
                break
            # 卡住要说清"在等什么"：某些包会挂在一条既不吐数据也不断开的连接上，
            # 这时百分比与包数都不动，界面只剩历史平均速度 —— 必须自己说明白。
            idle = time.time() - last_done_at
            if job is not None and idle >= 20 and time.time() - last_note_at >= 10:
                last_note_at = time.time()
                with ilock:
                    live_n = len(inflight)
                job.progress(message=(
                    f"{desc}：{live_n} 个包在传，已 {int(idle)} 秒没有新包完成"
                    "（网络慢，或连接被挂住正在重试）…"))

    el = max(time.time() - t0, 1e-6)
    _log(f"[{desc}] 完成：新下 {stat['new']}，跳过 {stat['skip']}，"
         f"失败 {len(stat['fail'])}，{stat['bytes'] / 1048576:.1f} MB，{el:.0f}s")
    for n, e in stat["fail"][:10]:
        _log(f"   失败 {n}: {e}")
    return stat


# ================================================================ 失败归因
#: ``(匹配片段, 发生了什么, 该怎么办, 错误码)`` —— 小写匹配 ``str(异常)``。
#:
#: 为什么集中在下载层：以前每个调用点自己拼错误，"装剧情失败"最后只剩一句
#: 「剧本解析失败（包可能损坏）」—— 而真实原因可能是**代理劫持 / 磁盘满 /
#: 清单过期 / 官方撤包 / 目录不可写**，这几件事要用户做的事完全相反。
#: 现在只有这一张表，角色剧情、非角色剧情、公共素材、语音、图片包共用同一套话术，
#: 绝不会把"磁盘满"和"网络超时"说成同一句话。
#:
#: ``错误码``（E-xxx）会写进用户能看到的报错里：用户只截图（不发日志文件）时，
#: 凭这一串就能判断是哪一类问题，不必等我猜。
FAIL_REASONS = (
    # —— 配置缺失 ——
    ("还没有配置资源源地址", "程序里还没有设置下载源",
     "到「设置 → 下载源」填一个地址，或重新解压一份完整的程序包", "E-NOSRC"),
    # —— 清单与源对不上（可自愈：刷新清单）——
    ("size ", "下载到的文件和清单里记的大小不一样",
     "点「更新」刷新资源清单后重试", "E-STALE"),
    ("404", "下载源上已经没有这个包",
     "点「更新」刷新资源清单；仍然失败请换个下载源", "E-STALE"),
    ("not found", "下载源上已经没有这个包",
     "点「更新」刷新资源清单；仍然失败请换个下载源", "E-STALE"),
    ("403", "下载源拒绝提供这个包", "换个下载源试试", "E-SRC"),
    ("407", "代理要求先登录（代理设置有问题）",
     "关掉代理 / VPN，或改用直连后重试", "E-PROXY"),
    # —— 本机写不进去 ——
    ("permission", "本机写不进文件（权限或安全软件）",
     "别把程序放在 Program Files 等受保护目录，并把程序目录加进杀软白名单", "E-PERM"),
    ("denied", "本机写不进文件（权限或安全软件）",
     "别把程序放在 Program Files 等受保护目录，并把程序目录加进杀软白名单", "E-PERM"),
    ("winerror 5", "本机写不进文件（权限或安全软件）",
     "别把程序放在 Program Files 等受保护目录，并把程序目录加进杀软白名单", "E-PERM"),
    ("no space", "磁盘空间不足", "腾出磁盘空间后重试", "E-DISK"),
    ("errno 28", "磁盘空间不足", "腾出磁盘空间后重试", "E-DISK"),
    # —— 网络 / 代理 ——
    ("timed out", "连下载源超时", "关掉代理 / VPN，或换个线路后重试", "E-NET"),
    ("timeout", "连下载源超时", "关掉代理 / VPN，或换个线路后重试", "E-NET"),
    ("refused", "下载源拒绝连接", "检查代理 / VPN 与防火墙后重试", "E-NET"),
    ("reset", "连接被中途切断", "多半是代理 / VPN 或防火墙在拦，关掉后重试", "E-NET"),
    ("certificate", "安全连接校验失败", "代理 / VPN 在改流量，关掉后重试", "E-PROXY"),
    ("ssl", "安全连接失败", "代理 / VPN 在改流量，关掉后重试", "E-PROXY"),
    ("getaddrinfo", "域名解析失败（连不上网或 DNS 被拦）", "检查网络后重试", "E-NET"),
    ("11001", "域名解析失败（连不上网或 DNS 被拦）", "检查网络后重试", "E-NET"),
    ("urlopen error", "连不上下载源", "检查网络 / 代理后重试", "E-NET"),
)

#: 一条不匹配任何规则的失败
FAIL_UNKNOWN = ("下载失败（原因未知）",
                "点「更新」刷新资源清单后重试；仍然不行请重装这部剧，或重启程序",
                "E-UNKNOWN")


def explain_error(err) -> tuple[str, str]:
    """异常 → ``(发生了什么, 该怎么办)``。两条都给人看，不含 URL / 协议细节。"""
    low = str(err or "").lower()
    for key, what, how, _code in FAIL_REASONS:
        if key in low:
            return what, how
    return FAIL_UNKNOWN[0], FAIL_UNKNOWN[1]


def error_code(err) -> str:
    """异常 → 错误码（``E-NET`` / ``E-DISK`` …）。用户截图里的那一串。"""
    low = str(err or "").lower()
    for key, _what, _how, code in FAIL_REASONS:
        if key in low:
            return code
    return FAIL_UNKNOWN[2]


def is_stale_error(err) -> bool:
    """这条失败是不是"本机清单过期"造成的（刷新清单就能好）。"""
    low = str(err or "").lower()
    return "size " in low or "404" in low or "not found" in low


def summarize_fails(fails, limit: int = 400) -> str:
    """把 ``fetch_many`` 的 fail 列表压成**一句按原因归类**的话。

    ``[(包名, 异常), …]`` → ``"6 个包没下下来：4 个 · 下载源上已经没有这个包（如
    bundle_a）；2 个 · 连下载源超时（如 bundle_b）"``
    """
    fails = list(fails or ())
    if not fails:
        return ""
    groups: dict = {}
    for name, err in fails:
        what, how = explain_error(err)
        groups.setdefault((what, how), []).append(str(name))
    bits = []
    for (what, _how), names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        bits.append(f"{len(names)} 个 · {what}（如 {names[0]}）")
    msg = f"{len(fails)} 个包没下下来：" + "；".join(bits)
    return msg if len(msg) <= limit else msg[:limit] + "…"


def hint_for(fails) -> str:
    """这批失败里占比最高的那条"怎么办"（多数情况下一步就够了）。"""
    fails = list(fails or ())
    if not fails:
        return ""
    cnt: dict = {}
    for _n, err in fails:
        how = explain_error(err)[1]
        cnt[how] = cnt.get(how, 0) + 1
    return max(cnt.items(), key=lambda kv: kv[1])[0]
