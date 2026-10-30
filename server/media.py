"""解包 → 格式转换 → 超分 → 应用（覆盖层）。

三个动作是解耦的（下载归 net/packs，这里只处理"已到本地的文件"）：

    extract()        解 zip 到站点根，可选顺手转 webp
    upscale()        对 4.4 GB 原图做超分，产出 media/img_x2（可中断续跑）
    apply_overlays() 把"本地实际文件路径"写成覆盖层 js，播放器据此取图

覆盖层为什么必要
----------------
出厂 ``imgmap.js`` 记录的是**原始文件名**（``x.png``/``x.jpg``）。一旦用户
把图转成 webp 或删掉原图，名字就变了。我们不修改出厂映射（它可被上游重新
生成），而是另写一层：

    data/imgmap.base.js  window.__IMGBASE  转码后的原图路径（覆盖 __IMGMAP）
    data/imgmap.x2.js    window.__IMGX2    超分图路径（优先级最高）

播放器取图顺序：``__IMGX2``（且用户开了超分）→ ``__IMGBASE`` → ``__IMGMAP``，
``<img onerror>`` 再逐级回退，任何一层缺失都只是画质降级，不会黑图。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import zipfile

from . import paths

RASTER = (".png", ".jpg", ".jpeg", ".bmp")

#: kind 目录（与 imgmap.js 的段名一致）
IMG_KINDS = ("stand", "back", "cg", "standfull", "story", "avatar")


# ---------------------------------------------------------------- Pillow
def pillow():
    try:
        from PIL import Image, ImageFilter                # noqa: F401
        return Image, ImageFilter
    except ImportError:
        return None, None


def have_pillow() -> bool:
    return pillow()[0] is not None


# ---------------------------------------------------------------- 覆盖层
def _ovr_file(kind: str) -> str:
    return os.path.join(paths.DATA, f"imgmap.{kind}.js")


def load_overlay(kind: str) -> dict:
    p = _ovr_file(kind)
    if not os.path.exists(p):
        return {k: {} for k in IMG_KINDS}
    try:
        with open(p, encoding="utf-8-sig") as f:
            s = f.read()
        i = s.find("=")
        body = s[i + 1:].strip().rstrip(";")
        return json.loads(body) or {k: {} for k in IMG_KINDS}
    except Exception:                                            # noqa: BLE001
        return {k: {} for k in IMG_KINDS}


def save_overlay(kind: str, data: dict) -> None:
    p = _ovr_file(kind)
    for k in IMG_KINDS:
        data.setdefault(k, {})
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"window.__IMG{kind.upper()}={body};\n")
    os.replace(tmp, p)


def overlay_path(kind: str, img_kind: str, key: str) -> str:
    return f"media/{'img_x2' if kind == 'x2' else 'img'}/{img_kind}/{key}"


# ---------------------------------------------------------------- 解包
def _safe_join(root: str, name: str) -> str | None:
    """防 zip 路径穿越。返回 None 表示非法条目。"""
    name = name.replace("\\", "/")
    if name.startswith("/") or ".." in name.split("/"):
        return None
    dst = os.path.abspath(os.path.join(root, name.replace("/", os.sep)))
    if not dst.startswith(os.path.abspath(root) + os.sep):
        return None
    return dst


def _to_webp(src: str, quality: int = 88) -> str | None:
    """把单张图转成同名 .webp，成功后删掉原文件，返回新的站点相对路径。"""
    Image, _ = pillow()
    if Image is None:
        return None
    dst = os.path.splitext(src)[0] + ".webp"
    try:
        with Image.open(src) as im:
            im = im.convert("RGBA" if _has_alpha(im) else "RGB")
            im.save(dst, "WEBP", quality=quality, method=4)
        if os.path.abspath(dst) != os.path.abspath(src):
            os.remove(src)
        return dst
    except Exception:                                            # noqa: BLE001
        try:
            if os.path.exists(dst) and os.path.getsize(dst) == 0:
                os.remove(dst)
        except OSError:
            pass
        return None


def _has_alpha(im) -> bool:
    return im.mode in ("RGBA", "LA") or (
        im.mode == "P" and "transparency" in im.info)


def _rel_of(abs_path: str) -> str:
    return os.path.relpath(abs_path, paths.WEB).replace("\\", "/")


def extract(zip_path: str, job=None, convert: bool = False,
            quality: int = 88, overwrite: bool = True) -> dict:
    """解包到站点根。返回 {files, bytes, converted, failed}。"""
    stats = {"files": 0, "bytes": 0, "converted": 0, "failed": 0}
    with zipfile.ZipFile(zip_path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        total = sum(i.file_size for i in infos) or 1
        if job is not None:
            job.set_total(total)
            job.set_items(len(infos))
        base_map = {}
        for info in infos:
            if job is not None and job.cancelled:
                break
            dst = _safe_join(paths.WEB, info.filename)
            if dst is None:
                stats["failed"] += 1
                continue
            exists = os.path.exists(dst)
            if exists and not overwrite:
                if job is not None:
                    job.progress(add=info.file_size)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                with z.open(info) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 256)
            except Exception:                                # noqa: BLE001
                stats["failed"] += 1
                continue
            stats["files"] += 1
            stats["bytes"] += info.file_size

            # 图像转码（只改 media/img 下的位图，不动 img_x2 与 ui 素材）
            rel = _rel_of(dst)
            if convert and rel.startswith("media/img/") and rel.lower().endswith(RASTER):
                new = _to_webp(dst, quality)
                if new:
                    stats["converted"] += 1
                    parts = _rel_of(new).split("/")
                    if len(parts) == 4:            # media/img/<kind>/<key>.webp
                        key = os.path.splitext(parts[3])[0]
                        base_map.setdefault(parts[2], {})[key] = _rel_of(new)
            if job is not None:
                job.progress(add=info.file_size,
                             message=f"{stats['files']}/{len(infos)} 个文件")
        if base_map:
            ovr = load_overlay("base")
            for k, v in base_map.items():
                ovr.setdefault(k, {}).update(v)
            save_overlay("base", ovr)
    return stats


# ---------------------------------------------------------------- 超分
# 引擎固定为 **Real-CUGAN**（项目自带的 `_tools/realcugan`；全量包在
# `tools/realcugan/` 下）。参数与本地 `_manifest_analysis/_scripts/sr.py` 完全一致：
#   realcugan-ncnn-vulkan.exe -i <in> -o <out> -n 2 -s 2 -m models-se -f png -j 2:2:2
#   → Pillow 转 WebP（q95）
# 以前这里探测的是 realesrgan-ncnn-vulkan.exe —— 项目里根本没有那个引擎，
# 于是永远回落到"内置 Lanczos 插值"，看着自然不清晰。不要再用 Lanczos 顶替。
_CUGAN_EXE = "realcugan-ncnn-vulkan.exe"
_CUGAN_MODEL = "models-se"      # models-se / models-pro / models-nose
_CUGAN_DENOISE = 2              # -n：-1 保守 / 0 不降噪 / 1-3 降噪强度
_CUGAN_THREADS = "2:2:2"        # -j：load:proc:save
_CUGAN_LAST_ERR = ""


def find_engine() -> dict:
    """探测超分引擎（只认 Real-CUGAN）。

    → ``{'kind': 'cugan'|'none', 'path': exe, 'dir': 工作目录, 'note': 说明}``

    搜索顺序：``<程序根>/tools/realcugan``（发布包布局）、
    ``<项目根>/_tools/realcugan``（本地开发布局），两种都兼容套一层
    ``xxx-windows/`` 的解压结构。
    """
    for root in (paths.TOOLS, os.path.join(paths.ROOT, "_tools")):
        base = os.path.join(root, "realcugan")
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            if _CUGAN_EXE in files:
                return {"kind": "cugan",
                        "path": os.path.join(dirpath, _CUGAN_EXE),
                        "dir": dirpath,
                        "note": f"Real-CUGAN（ncnn/Vulkan · {_CUGAN_MODEL}）"}
    return {"kind": "none", "path": "", "dir": "",
            "note": "缺少 Real-CUGAN 超分引擎：请把 realcugan-ncnn-vulkan 解压到 "
                    "tools/realcugan/ 下（全量包自带这一份）"}


def _upscale_one_cugan(exe: str, workdir: str, src: str, dst: str,
                       scale: int, denoise: int = _CUGAN_DENOISE) -> bool:
    """调 Real-CUGAN 放大一张图，再转 WebP（q95，与本地 sr.py 一致）。"""
    global _CUGAN_LAST_ERR
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = os.path.splitext(dst)[0] + ".png"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        cmd = [exe, "-i", src, "-o", tmp,
               "-n", str(int(denoise)), "-s", str(int(scale)),
               "-m", _CUGAN_MODEL, "-f", "png", "-j", _CUGAN_THREADS]
        r = subprocess.run(cmd, cwd=workdir or None, capture_output=True,
                           timeout=900)
        if r.returncode != 0 or not os.path.exists(tmp):
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            _CUGAN_LAST_ERR = f"rc={r.returncode} {err[:300]}"
            return False
        Image, _ = pillow()
        if Image is None:
            os.replace(tmp, dst)
            return True
        with Image.open(tmp) as im:
            im = im.convert("RGBA" if _has_alpha(im) else "RGB")
            im.save(dst, "WEBP", quality=95, method=4)
        os.remove(tmp)
        _CUGAN_LAST_ERR = ""
        return True
    except Exception as e:                                       # noqa: BLE001
        _CUGAN_LAST_ERR = f"{type(e).__name__}: {e}"
        return False


def load_imgmap() -> dict:
    """出厂图片映射（原图路径）。"""
    try:
        with open(os.path.join(paths.DATA, "imgmap.js"), encoding="utf-8-sig") as f:
            s = f.read()
        return json.loads(s[s.find("=") + 1:].strip().rstrip(";"))
    except Exception:                                            # noqa: BLE001
        return {}


def src_rel_for(kind: str, key: str, imgmap: dict | None = None,
                base: dict | None = None) -> str | None:
    """某个图片 key 当前在磁盘上的真实路径（转码过的优先）。"""
    base = base if base is not None else load_overlay("base")
    imgmap = imgmap if imgmap is not None else load_imgmap()
    cand = (base.get(kind) or {}).get(key) or (imgmap.get(kind) or {}).get(key)
    return cand if cand and os.path.exists(paths.web_path(cand)) else None


def collect_upscale_targets(kinds=("stand", "back", "cg"),
                            only_missing: bool = True,
                            only: set | None = None) -> list[tuple]:
    """→ [(img_kind, key, 原图相对路径)]；已有超分的跳过。

    ``only`` 给了就只处理这些 ``(kind, key)`` —— "安装后顺手超分这部剧的图"
    用它，避免把全站几千张图都跑一遍。
    """
    base = load_overlay("base")
    x2 = load_overlay("x2")
    imgmap = load_imgmap()
    out = []
    for kind in kinds:
        for key in (imgmap.get(kind) or {}):
            if only is not None and (kind, key) not in only:
                continue
            if only_missing and key in (x2.get(kind) or {}):
                continue
            rel = src_rel_for(kind, key, imgmap, base)
            if rel:
                out.append((kind, key, rel))
    return out


def _human_sec(s: float) -> str:
    s = int(round(s))
    if s < 60:
        return f"{s} 秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {sec} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分"


def _per_image_sec(mp: float, engine: str, scale: int = 2) -> float:
    """单张耗时估算（秒）。**按输出像素算，不是原图像素**。

    这点很关键：1280×720 放大 2 倍 → 2560×1440（面积 ×4），放大 4 倍 → ×16。
    如果只按原图算，选 4 倍时预估会和 2 倍一模一样（实际慢 4 倍）。

    系数来自实测：Real-CUGAN（AMD 780M 核显，models-se/-n 2/-s 2）
    把 0.92 MP 放大到 3.7 MP 用 **3.0 秒**（含 PNG 中转与 WebP 写盘）。
    以前这里写的是插值算法的系数（0.25/MP），会把 3 小时说成半小时，
    等于骗人 —— 宁可估高一点。
    """
    out_mp = max(0.01, mp * (scale ** 2))
    return 0.85 * out_mp + 0.5


def upscale_plan(kinds=("stand", "back", "cg"),
                 scale: int = 2) -> dict:
    """超分前的预估：多少张、单张多大、要多久。

    为什么需要：超分是最容易让用户后悔的操作（几分钟、CPU 满载）。
    光写"很慢"没有说服力，得给出"这次 87 张、原图 1280×720、
    放大后 2560×1440、预计 2 分 10 秒"，用户才好决定。
    """
    targets = collect_upscale_targets(tuple(kinds))
    by: dict[str, int] = {}
    for kind, _key, _rel in targets:
        by[kind] = by.get(kind, 0) + 1

    eng = find_engine()
    mode = eng["kind"]

    sample = None
    if targets:
        _k, _key, rel = targets[0]
        try:
            Image, _ = pillow()
            with Image.open(paths.web_path(rel)) as im:
                w, h = im.size
            sample = {"w": w, "h": h,
                      "outW": int(w * scale), "outH": int(h * scale),
                      "mp": round(w * h / 1e6, 2)}
        except Exception:                                      # noqa: BLE001
            sample = None

    # 逐类取样本：立绘多是 300×628 这种小图，背景/CG 是 1280×720 ——
    # 只拿第一张当样本，既会把尺寸说错（用户会以为所有图都是 300×628），
    # 也会让 ETA 差几倍（说过"半小时"结果跑 3 小时）。
    def _first_size(kind: str):
        try:
            Image, _ = pillow()
        except Exception:                                      # noqa: BLE001
            return None
        for k, _key, rel in targets:
            if k != kind:
                continue
            try:
                with Image.open(paths.web_path(rel)) as im:
                    return im.width, im.height
            except Exception:                                  # noqa: BLE001
                continue
        return None

    by_kind_size: dict = {}
    sum_out_mp = 0.0
    for kind, cnt in by.items():
        wh = _first_size(kind)
        if not wh:
            continue
        w, h = wh
        by_kind_size[kind] = {"n": cnt, "w": w, "h": h,
                              "outW": int(w * scale), "outH": int(h * scale)}
        sum_out_mp += cnt * (w * h / 1e6) * (scale ** 2)
    total = len(targets)
    # 总耗时 = 像素项 + 每张固定开销
    eta = 0.85 * sum_out_mp + 0.5 * total
    per = (eta / total) if total else 0.0

    # total==0 有两种截然不同的原因，不能都说成"都已做过"：
    #   ① 一张原图都没有（刚装好程序、还没装剧情）
    #   ② 有原图，但全部超分过了
    # 以前一律返回"没有需要超分的图（都已做过）"，全新安装时会让用户
    # 以为程序搞错了 —— 明明什么都没做过。
    if total == 0:
        all_n = len(collect_upscale_targets(tuple(kinds), only_missing=False))
        hint = ("还没有图片可超分 —— 先去「剧情安装」装一部剧情"
                if all_n == 0 else f"没有需要超分的图（{all_n} 张都已做过）")
    else:
        hint = ""

    return {
        "total": total,
        "byKind": by,
        "scale": scale,
        "engine": mode,
        "engineNote": eng.get("note", ""),
        "sample": sample,
        "byKindSize": by_kind_size,
        "perImageSec": round(per, 2),
        "etaSec": round(eta, 1),
        "etaText": _human_sec(eta),
        "hint": hint,
    }


def upscale(job, kinds=("stand", "back", "cg"), scale: int = 2,
            engine: str = "cugan", limit: int = 0,
            only: set | None = None) -> dict:
    """批量超分，逐张写覆盖层（中断也能续跑）。

    ``engine`` 参数只为兼容旧调用保留 —— 现在**只有 Real-CUGAN 一种引擎**，
    没有引擎就直接报错，不再用 Lanczos 插值顶替（那只是像素变多、不增细节）。
    """
    eng = find_engine()
    if eng["kind"] == "none":
        raise RuntimeError(eng["note"])
    mode = eng["kind"]

    targets = collect_upscale_targets(kinds, only=only)
    if limit:
        targets = targets[:limit]
    if not targets:
        return {"done": 0, "failed": 0, "skipped": 0, "note": "没有需要超分的图"}

    job.set_total(len(targets))
    job.set_items(len(targets))
    job.log(f"引擎：{eng['note']}；共 {len(targets)} 张")
    x2 = load_overlay("x2")
    ok = fail = 0
    t0 = time.time()
    for kind, key, rel in targets:
        if job.cancelled:
            break
        src = paths.web_path(rel)
        # 注意：imgmap 的 key 是**不带扩展名**的 stem，但落盘与映射的值必须带
        # `.webp` —— 以前这里漏了扩展名，产出 `img_x2/stand/<key>` 这种无后缀
        # 文件，重建覆盖层时被"扩展名不认识"整批跳过，页面/导出永远拿不到超分图。
        dst_rel = overlay_path("x2", kind, key) + ".webp"
        dst = paths.web_path(dst_rel)
        good = _upscale_one_cugan(eng["path"], eng.get("dir") or "",
                                  src, dst, scale)
        if good:
            ok += 1
            x2.setdefault(kind, {})[key] = dst_rel
            if ok % 25 == 0:               # 每 25 张落一次盘，崩溃不至于全丢
                save_overlay("x2", x2)
        else:
            fail += 1
            if fail == 1:
                # 引擎可能因为「缺模型 / 没有 Vulkan / 参数不合法」整批失败，
                # 必须把原因写进日志，否则用户只看到"全部失败"。
                job.log("  超分失败示例：" + (_CUGAN_LAST_ERR[:300] or "（无输出）"))
        sp = (ok + fail) / max(0.001, time.time() - t0)
        job.progress(add=1, message=f"超分 {ok + fail}/{len(targets)}"
                                   f"（{sp:.1f} 张/秒，失败 {fail}）")
    save_overlay("x2", x2)
    return {"done": ok, "failed": fail, "skipped": 0}


# ---------------------------------------------------------------- 批量转码
def convert_existing(job, quality: int = 88,
                     kinds=("stand", "back", "cg")) -> dict:
    """把磁盘上已有的 PNG/JPG 原图批量转成 WebP（跳过已经是 webp 的）。"""
    Image, _ = pillow()
    if Image is None:
        raise RuntimeError("缺少 Pillow，无法转码：pip install pillow")
    imgmap = load_imgmap()
    base = load_overlay("base")
    targets = []
    for kind in kinds:
        for key in (imgmap.get(kind) or {}):
            cur = (base.get(kind) or {}).get(key) or imgmap[kind][key]
            if cur.lower().endswith(".webp"):
                continue
            p = paths.web_path(cur)
            if os.path.exists(p):
                targets.append((kind, key, p))
    if not targets:
        return {"done": 0, "failed": 0, "note": "没有需要转码的图"}
    job.set_total(len(targets))
    job.set_items(len(targets))
    buf: dict = {}
    ok = fail = 0
    saved = 0
    t0 = time.time()
    for kind, key, p in targets:
        if job.cancelled:
            break
        try:
            before = os.path.getsize(p)
        except OSError:
            before = 0
        new = _to_webp(p, quality)
        if new:
            ok += 1
            buf.setdefault(kind, {})[key] = _rel_of(new)
            try:
                saved += max(0, before - os.path.getsize(new))
            except OSError:
                pass
            if ok % 50 == 0:                # 增量落盘
                ovr = load_overlay("base")
                for k, v in buf.items():
                    ovr.setdefault(k, {}).update(v)
                save_overlay("base", ovr)
        else:
            fail += 1
        job.progress(add=1, message=f"转码 {ok + fail}/{len(targets)}"
                                   f"（已省 {saved / 1024 / 1024:.1f} MB，"
                                   f"{(ok + fail) / max(0.001, time.time() - t0):.1f} 张/秒）")
    ovr = load_overlay("base")
    for k, v in buf.items():
        ovr.setdefault(k, {}).update(v)
    save_overlay("base", ovr)
    return {"done": ok, "failed": fail, "saved": saved}


# ---------------------------------------------------------------- 清理
def delete_originals(job=None, kinds=("stand", "back", "cg")) -> dict:
    """删除已被超分覆盖的原图（安全：只删 x2 层已有对应项的文件）。"""
    x2 = load_overlay("x2")
    base = load_overlay("base")
    imgmap = load_imgmap()
    victims = []
    for kind in kinds:
        for key, x2rel in (x2.get(kind) or {}).items():
            if not os.path.exists(paths.web_path(x2rel)):
                continue                    # 超分文件没了就别删原图
            orig = src_rel_for(kind, key, imgmap, base)
            if not orig:
                continue
            victims.append(paths.web_path(orig))
    freed = 0
    if job is not None:
        job.set_total(len(victims))
    for p in victims:
        if job is not None and job.cancelled:
            break
        try:
            freed += os.path.getsize(p)
            os.remove(p)
        except OSError:
            pass
        if job is not None:
            job.progress(add=1)
    return {"deleted": len(victims), "freed": freed}


_STATS_CACHE = {"at": 0.0, "data": None}
_STATS_TTL = 30.0


def _walk_size(root: str) -> tuple[int, int]:
    """→ (文件数, 字节数)。

    media 下有 24 万个文件，Windows 上逐次 stat 要 1–2 秒，
    所以调用方一律走缓存；这里用 scandir 而不是 walk + getsize，
    少一次路径拼接与一次额外的 stat 系统调用。
    """
    n = b = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        b += e.stat(follow_symlinks=False).st_size
                        n += 1
                except OSError:
                    pass
    return n, b


def img_stats(force: bool = False) -> dict:
    """磁盘占用统计（给设置页显示）。带 30 秒缓存 —— 遍历 24 万文件太贵。"""
    import time
    if not force and _STATS_CACHE["data"] is not None \
            and time.time() - _STATS_CACHE["at"] < _STATS_TTL:
        return _STATS_CACHE["data"]
    out = {}
    for name, p in (("img", paths.IMG), ("img_x2", paths.IMG_X2),
                    ("audio", paths.AUDIO)):
        n, b = _walk_size(p)
        out[name] = {"files": n, "bytes": b}
    out["free"] = paths.disk_free()
    out["at"] = time.strftime("%H:%M:%S")
    # 「磁盘文件数」和「待超分张数」是两个口径，页面必须能解释差异：
    #   img/  = 解出来的**所有**图片（含没人引用的立绘、剧情插图、头像…）
    #   待超分 = 只算 imgmap 里登记过的 stand/back/cg
    try:
        im, x2 = load_imgmap(), load_overlay("x2")
        per, tot, done = {}, 0, 0
        for k in ("stand", "back", "cg"):
            keys = im.get(k) or {}
            have = x2.get(k) or {}
            d = len([x for x in keys if x in have])
            per[k] = {"total": len(keys), "done": d}
            tot += len(keys)
            done += d
        out["upscalable"] = {"total": tot, "done": done,
                             "left": tot - done, "byKind": per}
        # 各类目录的实际文件数，用来解释"其余那些是什么"
        out["byKindFiles"] = {}
        for k in ("stand", "back", "cg", "avatar", "standfull", "story"):
            n, _b = _walk_size(os.path.join(paths.IMG, k))
            out["byKindFiles"][k] = n
    except Exception:                                          # noqa: BLE001
        pass
    _STATS_CACHE.update(at=time.time(), data=out)
    return out


def reset_overlay(kind: str) -> bool:
    p = _ovr_file(kind)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def rebuild_x2_overlay(job=None) -> dict:
    """按磁盘实际存在的 img_x2 文件重建 __IMGX2（比 state 更可信）。

    顺带自愈历史 bug：早期 ``upscale()`` 落盘时漏了扩展名，产出
    ``img_x2/stand/<key>`` 这种无后缀文件；这里把它们补成 ``.webp``，
    否则重建时会因"扩展名不认识"整批跳过（页面和导出都拿不到超分图）。
    """
    data = {k: {} for k in IMG_KINDS}
    n = fixed = 0
    for kind in IMG_KINDS:
        d = os.path.join(paths.IMG_X2, kind)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            stem, ext = os.path.splitext(name)
            if not ext:
                # 历史无后缀文件：内容其实是 webp，补上扩展名
                try:
                    os.replace(os.path.join(d, name),
                               os.path.join(d, name + ".webp"))
                    name, ext = name + ".webp", ".webp"
                    fixed += 1
                except OSError:
                    continue
            if ext.lower() not in (".webp", ".png", ".jpg"):
                continue
            # 映射的**值**要带扩展名（key 仍是 stem，和 imgmap 对齐）
            data[kind][stem] = overlay_path("x2", kind, stem) + ext.lower()
            n += 1
    save_overlay("x2", data)
    if job is not None:
        msg = f"重建覆盖层：{n} 张超分图"
        if fixed:
            msg += f"（顺手补全 {fixed} 个缺扩展名的旧文件）"
        job.log(msg)
    return {"count": n, "fixed": fixed}
