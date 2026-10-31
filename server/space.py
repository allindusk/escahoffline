"""磁盘空间统计与"能安全删的东西"。

为什么单独一个模块
------------------
"空间不够"是这个程序最常见的问题（全量素材 20 GB+，超分再来一套），
而用户看到的是几个互不相干的数字：概览页的 bundle 缓存、画质页的 img/img_x2/audio、
导出目录的文件列表……没人能从中算出"到底什么吃掉了 30 GB、我能删哪个"。

所以这里做两件事：
  1. ``report()``：**一次遍历**把整个程序目录按用途分桶（语音 / 原图 / 超分图 /
     剧本 / 资源包缓存 / 导出 / 日志…），给「设置 / 清理空间」页一个完整账本。
     一次遍历很重要：media 下就有 27 万个文件，分桶分别 walk 会慢好几倍。
  2. ``clean()``：按桶清理，每一项都写清"删了会怎样"。

清理安全性的依据
----------------
``userdata/files_*`` 里的资源包只在**解包**时被读（见 cdn/stages.py），
解包完成后播放只读 ``web/media`` + ``web/data``；已装状态由
``web/data/<unit>.js`` 是否存在决定（cdn/install.py: unit_state）。
所以删掉资源包缓存**不影响已装剧情的播放**，要重装那部剧情才会重新下载。
"""

from __future__ import annotations

import os
import shutil
import time

from . import media, paths
from .cdn import config as cdn_cfg

# ---------------------------------------------------------------- 分桶
#: 桶 → (标题, 位置, 说明)。顺序即页面展示顺序。
GROUPS: dict[str, tuple[str, str, str]] = {
    "audio": ("剧情语音", "web/media/audio",
              "台词配音，播放必需；删了对应剧情就没声音"),
    "img": ("剧情图片（原图）", "web/media/img",
            "立绘 / 背景 / CG / 头像，播放必需"),
    "x2": ("超分高清图", "web/media/img_x2",
           "放大后的高清版；删掉会自动回退原图，可重做但很慢"),
    "media_other": ("其他媒体文件", "web/media", "映射表以外的零散文件"),
    "script": ("剧本数据", "web/data",
               "解包出来的剧情脚本与索引，播放必需"),
    "bundles": ("下载缓存（原始压缩包）", "userdata/char_story · files_img · files_audio",
                "下载回来的原始压缩包；装完就用不到，可删"),
    "cdn_cache": ("剧情目录缓存", "userdata/cdn",
                  "官方目录与增量状态，删了下次要重新下载约 5 MB"),
    "trans": ("翻译库", "userdata/trans", "你导入的翻译稿"),
    "export": ("导出成品", "userdata/export", "导出的视频 / CG 压缩包 / 翻译 txt"),
    "logs": ("日志", "userdata/logs", "排错用，可随便删"),
    "backup": ("旧图备份", "userdata/backup", "早期版本遗留，通常为空"),
    "downloads": ("下载残留", "userdata/downloads", "中断的下载与临时上传文件"),
    "userdata_other": ("其他用户数据", "userdata", "配置 / 索引小文件"),
    "program": ("程序本体", "server · tools · web 程序文件",
                "代码与超分引擎，删了要重装程序"),
    "dev": ("程序目录里的其他文件", "_ 开头的目录 · .git",
            "从源码目录运行时才会有的开发残留，正常安装版没有；可自行删除"),
}

ORDER = ("audio", "img", "x2", "script", "bundles", "export",
         "trans", "cdn_cache", "logs", "backup", "downloads",
         "media_other", "userdata_other", "program", "dev")

#: 这些桶的占用可以"一键回收"（进清理清单）
RECLAIMABLE = ("bundles", "export", "logs", "downloads", "backup")

_BUNDLE_DIRS = (cdn_cfg.FILES_STORY, cdn_cfg.FILES_IMG, cdn_cfg.FILES_AUDIO)


def _bucket(rel: str) -> tuple[str, str]:
    """相对路径 → (桶, 图片二级分类)。``rel`` 用正斜杠、无前导斜杠。"""
    # 顶层 _xxx / .git 这类是开发物料（_tools、_TRASH、_manifest_analysis…），
    # 发布包里不存在。单独成桶，免得它把"本程序总占用"撑到看不懂。
    if rel[:1] in ("_", "."):
        return "dev", ""
    if rel.startswith("web/media/img_x2/"):
        return "x2", ""
    if rel.startswith("web/media/img/"):
        rest = rel[len("web/media/img/"):]
        return "img", (rest.split("/", 1)[0] if "/" in rest else "")
    if rel.startswith("web/media/audio/"):
        return "audio", ""
    if rel.startswith("web/media/"):
        return "media_other", ""
    if rel.startswith("web/data/"):
        return "script", ""
    if rel.startswith(cdn_cfg_rel(cdn_cfg.FILES_STORY)) \
            or rel.startswith(cdn_cfg_rel(cdn_cfg.FILES_IMG)) \
            or rel.startswith(cdn_cfg_rel(cdn_cfg.FILES_AUDIO)):
        return "bundles", ""
    if rel.startswith("userdata/cdn/"):
        return "cdn_cache", ""
    if rel.startswith("userdata/trans/"):
        return "trans", ""
    if rel.startswith("userdata/export/"):
        return "export", ""
    if rel.startswith("userdata/logs/"):
        return "logs", ""
    if rel.startswith("userdata/backup/"):
        return "backup", ""
    if rel.startswith("userdata/downloads/"):
        return "downloads", ""
    if rel.startswith("userdata/"):
        return "userdata_other", ""
    return "program", ""


def cdn_cfg_rel(p: str) -> str:
    """绝对路径 → 相对 ROOT 的正斜杠路径（带尾斜杠），用于前缀匹配。"""
    try:
        r = os.path.relpath(p, paths.ROOT).replace("\\", "/")
    except ValueError:
        return "\0"
    return r.rstrip("/") + "/"


def _scan(root: str):
    """一次遍历整个程序目录：桶 → [文件数, 字节数]，以及图片二级分类的文件数。"""
    parts: dict[str, list[int]] = {}
    img_sub: dict[str, int] = {}
    files = total = 0
    stack = [(root, "")]
    while stack:
        d, rel = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    r = rel + e.name
                    if e.is_dir(follow_symlinks=False):
                        stack.append((e.path, r + "/"))
                    elif e.is_file(follow_symlinks=False):
                        sz = e.stat(follow_symlinks=False).st_size
                        key, sub = _bucket(r)
                        rec = parts.setdefault(key, [0, 0])
                        rec[0] += 1
                        rec[1] += sz
                        if sub:
                            img_sub[sub] = img_sub.get(sub, 0) + 1
                        files += 1
                        total += sz
                except OSError:
                    pass
    return parts, img_sub, files, total


# ---------------------------------------------------------------- 统计
_CACHE: dict = {"at": 0.0, "data": None}
_TTL = 120.0


def invalidate() -> None:
    _CACHE.update(at=0.0, data=None)
    try:
        media.invalidate_stats()
    except Exception:                                          # noqa: BLE001
        pass
    try:
        from .cdn import install as _ins
        _ins.invalidate_cache_size()
    except Exception:                                          # noqa: BLE001
        pass


def _upscale_summary() -> dict | None:
    """映射表口径的超分进度（只读映射，不遍历磁盘）。"""
    try:
        im, x2 = media.load_imgmap(), media.load_overlay("x2")
        per, tot, done = {}, 0, 0
        for k in ("stand", "back", "cg"):
            keys, have = im.get(k) or {}, x2.get(k) or {}
            d = len([x for x in keys if x in have])
            per[k] = {"total": len(keys), "done": d}
            tot += len(keys)
            done += d
        return {"total": tot, "done": done, "left": tot - done, "byKind": per}
    except Exception:                                          # noqa: BLE001
        return None


def _originals_stats() -> dict:
    """"已被超分覆盖的原图"到底能腾出多少 —— 逐张 stat，口径与删除逻辑完全一致。

    只统计"超分文件确实存在、且能反查到原图"的那些（`media.src_rel_for`），
    所以这个数字就是点一下「删除」能拿回的空间。
    """
    n = b = 0
    try:
        x2 = media.load_overlay("x2")
        base = media.load_overlay("base")
        im = media.load_imgmap()
        for kind in ("stand", "back", "cg"):
            for key, rel in (x2.get(kind) or {}).items():
                if not os.path.exists(paths.web_path(rel)):
                    continue
                orig = media.src_rel_for(kind, key, im, base)
                if not orig:
                    continue
                try:
                    b += os.path.getsize(paths.web_path(orig))
                    n += 1
                except OSError:
                    pass
    except Exception:                                          # noqa: BLE001
        pass
    return {"files": n, "bytes": b}


def _count_units() -> int:
    """已解包的剧情部数（以 web/data/<unit>.js 为证据）。"""
    n = 0
    try:
        for name in os.listdir(paths.DATA):
            if name.endswith(".js") and (name.startswith("char_")
                                         or name.startswith("adv_")):
                n += 1
    except OSError:
        pass
    return n


def report(force: bool = False) -> dict:
    if not force and _CACHE["data"] is not None \
            and time.time() - _CACHE["at"] < _TTL:
        return _CACHE["data"]
    parts, img_sub, files, total = _scan(paths.ROOT)
    try:
        du = shutil.disk_usage(paths.ROOT)
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = {"total": 0, "used": 0, "free": paths.disk_free()}
    groups = []
    for key in ORDER:
        rec = parts.get(key) or [0, 0]
        if not rec[1]:
            continue
        label, where, note = GROUPS[key]
        groups.append({"key": key, "label": label, "path": where,
                       "note": note, "files": rec[0], "bytes": rec[1],
                       "reclaimable": key in RECLAIMABLE})
    originals = _originals_stats()
    # 可回收 = 那些"删了不影响播放"的桶 + 已被超分覆盖的原图（删完用高清版照样播）。
    # 超分高清图**不算**在这里：删它是有画质代价的，单独一行让用户自己决定。
    reclaim = sum(g["bytes"] for g in groups if g["reclaimable"]) \
        + originals["bytes"]
    out = {
        "at": time.strftime("%H:%M:%S"),
        "root": {"bytes": total, "files": files},
        "disk": disk,
        "groups": groups,
        "reclaim": reclaim,
        "counts": {
            "units": _count_units(),
            "bundles": (parts.get("bundles") or [0, 0])[0],
            "x2": (parts.get("x2") or [0, 0])[0],
        },
        "upscale": _upscale_summary(),
        "originals": originals,
        "byKindFiles": img_sub,
    }
    _CACHE.update(at=time.time(), data=out)
    return out


# ---------------------------------------------------------------- 清理
def _walk_files(root: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    if not os.path.isdir(root):
        return out
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
                        out.append((e.path, e.stat(follow_symlinks=False).st_size))
                except OSError:
                    pass
    return out


def _remove(files: list[tuple[str, int]], job=None, what: str = "") -> dict:
    """删文件并汇报。单个失败不影响其它（被占用的文件跳过并计数）。"""
    got = removed = skipped = 0
    freed = 0
    if job is not None:
        job.set_total(max(1, len(files)))
        if what:
            job.log(f"{what}：{len(files)} 个文件")
    for i, (p, sz) in enumerate(files, 1):
        if job is not None and job.cancelled:
            break
        try:
            os.remove(p)
            removed += 1
            freed += sz
        except OSError:
            skipped += 1
        got += 1
        if job is not None and i % 50 == 0:
            job.progress(done=got, message=f"删除 {got}/{len(files)}"
                                           f"（已释放 {freed / 1048576:.0f} MB）")
    if job is not None:
        job.progress(done=got)
        job.log(f"完成：删了 {removed} 个文件，释放 {freed / 1048576:.1f} MB"
                + (f"，跳过 {skipped} 个（被占用）" if skipped else ""))
    return {"files": len(files), "deleted": removed, "skipped": skipped,
            "bytes": freed}


def _bundle_files() -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for root in _BUNDLE_DIRS:
        if not os.path.isdir(root):
            continue
        try:
            it = os.scandir(root)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        out.append((e.path, e.stat(follow_symlinks=False).st_size))
                except OSError:
                    pass
    return out


def clean_bundles(job=None, dry: bool = False) -> dict:
    """删除**已完成解包**的资源包缓存。

    安全性：解包后的播放只读 ``web/media`` / ``web/data``，这些包只有
    安装流程会读（见模块头注释）。删掉之后已装剧情照常播放；要重装或
    「补全缺失」某部剧情时才会重新下载对应的包。
    """
    files = _bundle_files()
    total = sum(sz for _p, sz in files)
    if job is not None:
        job.log(f"资源包缓存 {len(files)} 个 / {total / 1048576:.1f} MB"
                + ("（预演，不删）" if dry else ""))
    if dry:
        return {"files": len(files), "deleted": 0, "bytes": total, "dry": True}
    res = _remove(files, job, "删除资源包缓存")
    res["dry"] = False
    invalidate()
    return res


def clean_export(job=None, dry: bool = False) -> dict:
    """清空导出成品目录（视频 / CG 压缩包 / 翻译 txt）。"""
    files = _walk_files(paths.EXPORT)
    total = sum(sz for _p, sz in files)
    if dry:
        return {"files": len(files), "deleted": 0, "bytes": total, "dry": True}
    res = _remove(files, job, "清空导出成品")
    res["dry"] = False
    invalidate()
    return res


def clean_logs(job=None, dry: bool = False) -> dict:
    """清日志。当前正在写的 app.log 会被系统占用，删不掉就跳过（不计为失败）。"""
    files = _walk_files(paths.LOGS)
    total = sum(sz for _p, sz in files)
    if dry:
        return {"files": len(files), "deleted": 0, "bytes": total, "dry": True}
    res = _remove(files, job, "清理日志")
    res["dry"] = False
    invalidate()
    return res


def clean_downloads(job=None, dry: bool = False) -> dict:
    """清下载残留：中断的 .part 与视频导出的临时上传文件。"""
    files = _walk_files(paths.DOWNLOADS)
    total = sum(sz for _p, sz in files)
    if dry:
        return {"files": len(files), "deleted": 0, "bytes": total, "dry": True}
    res = _remove(files, job, "清理下载残留")
    res["dry"] = False
    invalidate()
    return res


def clean_backup(job=None, dry: bool = False) -> dict:
    """清早期版本遗留的备份目录。"""
    files = _walk_files(paths.BACKUP)
    total = sum(sz for _p, sz in files)
    if dry:
        return {"files": len(files), "deleted": 0, "bytes": total, "dry": True}
    res = _remove(files, job, "清理旧备份")
    res["dry"] = False
    invalidate()
    return res


def clean_x2(job=None, dry: bool = False) -> dict:
    """删除全部超分高清图，回到原图播放。

    **可逆但有代价**：超分很慢（每张 0.5–2 秒），删了要重做。
    所以这条只放在「清理空间」里，并且默认不勾选。
    """
    files = _walk_files(paths.IMG_X2)
    total = sum(sz for _p, sz in files)
    if dry:
        return {"files": len(files), "deleted": 0, "bytes": total, "dry": True}
    res = _remove(files, job, "删除超分高清图")
    # 清掉残留的空目录 + 重建覆盖层：映射必须和磁盘一致，否则播放器会去取
    # 已经不存在的图（表现为黑图）。
    for name in os.listdir(paths.IMG_X2) if os.path.isdir(paths.IMG_X2) else []:
        try:
            os.rmdir(os.path.join(paths.IMG_X2, name))
        except OSError:
            pass
    try:
        media.reset_overlay("x2")
        media.rebuild_x2_overlay(job)
    except Exception as e:                                     # noqa: BLE001
        if job is not None:
            job.log(f"⚠ 重建超分索引失败：{e}（到「提高CG画质」点一次重建索引）")
    res["dry"] = False
    invalidate()
    return res


ACTIONS = {
    "bundles": ("删除资源包缓存", clean_bundles),
    "export": ("清空导出成品", clean_export),
    "logs": ("清理日志", clean_logs),
    "downloads": ("清理下载残留", clean_downloads),
    "backup": ("清理旧备份", clean_backup),
    "x2": ("删除超分高清图", clean_x2),
}


def clean(job=None, what: str = "", dry: bool = False) -> dict:
    """统一入口：``what`` 见 :data:`ACTIONS`。"""
    what = (what or "").strip()
    if what not in ACTIONS:
        raise ValueError(f"不知道要清理什么：{what!r}")
    label, fn = ACTIONS[what]
    res = fn(job, dry=dry)
    res["what"] = what
    res["label"] = label
    if job is not None:
        try:
            job.detail["clean"] = res
        except Exception:                                      # noqa: BLE001
            pass
    return res
