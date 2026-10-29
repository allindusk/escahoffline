"""导出：角色 CG 打包、剧情视频落盘、译文导入 / 导出。

为什么 CG 用后端打包
--------------------
浏览器里没有 zip 能力（不想为了打包再引第三方库），而"选 10 个角色、几百张
CG"通过 HTTP 逐张下载再存盘对用户是灾难。后端直接读本地文件打包最省事。

视频为什么不在这里渲染
----------------------
``web/assets/js/recorder.js`` 用 canvas 逐帧重绘 + MediaRecorder 录制（浏览器
本身就有编解码器），录完把 blob POST 上来，这里只负责落盘和可选的 ffmpeg 转码。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile

from . import media, paths

TRANS_DIR = os.path.join(paths.WEB, "assets", "i18n", "zh-Hans")

TRANS_HEADER = """# ===== ESCAH 离线阅读器 · 剧情译文文件 =====
# 剧情标识 : {unit}
# 剧情名称 : {title}
# 段落/句数: {parts} 段 / {lines} 句
# 导出时间 : {at}
#
# 使用说明（给人和 AI 都看得懂）：
#   1. 每行一句，格式为：   日文原文 ||| 中文译文
#   2. 分隔符固定是三个竖线，两侧各一空格：空格|||空格
#   3. 原文里的换行在导出时写成了 \\n，请不要改动它，也不要改动原文
#   4. 只翻译右侧；左侧原文一个字都不能动，否则导入时会匹配不上
#   5. 想跳过某句就把右侧留空（该句将回退显示日文）
#   6. 以 # 开头的行是注释，导入时忽略
#   7. 改完保存为 UTF-8，回到页面的「译文导入」里选中本文件即可
#
"""


# ---------------------------------------------------------------- 基础
def _load_js(p: str):
    with open(p, encoding="utf-8-sig") as f:
        s = f.read()
    return json.loads(s[s.find("=") + 1:].strip().rstrip(";"))


def unit_data(unit: str) -> dict | None:
    p = os.path.join(paths.DATA, f"{unit}.js")
    if not os.path.exists(p):
        return None
    try:
        return _load_js(p)
    except Exception:                                            # noqa: BLE001
        return None


def unit_title(unit: str) -> str:
    try:
        if unit.startswith("char_"):
            names = _load_js(os.path.join(paths.DATA, "names.js"))
            return f"角色 {unit[5:]} · {names.get(unit[5:], '')}"
        advs = _load_js(os.path.join(paths.DATA, "advindex.js"))
        for a in advs:
            if a.get("key") == unit:
                return f"{a.get('kind')} 第 {a.get('no')} 话"
    except Exception:                                            # noqa: BLE001
        pass
    return unit


def _unique_path(base: str, ext: str = ".zip") -> str:
    os.makedirs(paths.EXPORT, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    p = os.path.join(paths.EXPORT, f"{base}_{stamp}{ext}")
    i = 2
    while os.path.exists(p):
        p = os.path.join(paths.EXPORT, f"{base}_{stamp}_{i}{ext}")
        i += 1
    return p


# ---------------------------------------------------------------- CG 导出
def export_cg(job, chars: list[int] | None = None,
              groups: list[list[int]] | None = None,
              whole: bool = False) -> dict:
    """导出角色 CG 为 zip。

    ``chars``  角色 id 列表（None = 全部有 CG 的角色）
    ``groups`` 精确到事件组：[charId, groupIndex] 的列表（可选）
    ``whole``  True 表示忽略筛选导出全部
    """
    cgmap = _load_js(os.path.join(paths.DATA, "cgmap.js"))
    imgmap = media.load_imgmap()
    base = media.load_overlay("base")
    x2 = media.load_overlay("x2")

    picked: list[tuple[int, str, list[str]]] = []
    for gi, g in enumerate(cgmap):
        cid = g.get("c")
        if groups and not whole:
            if [cid, gi] not in groups:
                continue
        elif chars and not whole and cid not in chars:
            continue
        picked.append((cid, g.get("k") or f"group{gi}", g.get("f") or []))

    total = sum(len(f) for _, _, f in picked)
    if job is not None:
        job.set_total(total)
        job.set_items(total)
    if not total:
        return {"path": "", "files": 0, "note": "没有匹配到 CG"}

    # zip 名字用角色名，别再用 cg_20261029_xxx 这种看不出是谁的
    from . import names as nameutil
    ids = sorted({cid for cid, _k, _f in picked})
    if len(ids) == 1:
        tag = nameutil.of(ids[0]) + "_CG"
    elif len(ids) <= 3:
        tag = "、".join(nameutil.of(c) for c in ids) + "_CG"
    else:
        tag = f"{nameutil.of(ids[0])}等{len(ids)}个_CG"
    out = _unique_path(nameutil.safe(tag, 70))

    written = missing = 0
    names = _safe_names(cgmap)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as z:
        for gi, (cid, gname, files) in enumerate(picked):
            if job is not None and job.cancelled:
                break
            base_nm = nameutil.safe(names.get(cid) or nameutil.of(cid))
            # 只有真的两个角色同名时才退回 id 区分（正常情况直接用名字）
            if len(ids) > 1 and \
                    sum(1 for c in ids if nameutil.of(c) == nameutil.of(cid)) > 1:
                base_nm += f"_{cid}"
            folder = f"{base_nm}/{_san(gname)}"
            for f in files:
                if job is not None and job.cancelled:
                    break
                key = os.path.splitext(f)[0]
                rel = ((x2.get("cg") or {}).get(key)
                       or (base.get("cg") or {}).get(key)
                       or (imgmap.get("cg") or {}).get(key))
                src = paths.web_path(rel) if rel else None
                if not src or not os.path.exists(src):
                    missing += 1
                    if job is not None:
                        job.progress(add=1)
                    continue
                z.write(src, f"{folder}/{os.path.basename(src)}")
                written += 1
                if job is not None:
                    job.progress(add=1, message=f"打包 {written}/{total} 张")
    if job is not None:
        job.finish(f"已导出 {written} 张 CG")
    return {"path": paths.rel(out) if out.startswith(paths.WEB) else out,
            "abs": out, "files": written, "missing": missing}


def cg_list() -> dict:
    """CG 清单（给导出页做选择）：{chars:[{id,name,groups:[{i,k,n}],total}], total}"""
    try:
        cgmap = _load_js(os.path.join(paths.DATA, "cgmap.js"))
    except Exception:                                            # noqa: BLE001
        return {"chars": [], "total": 0}
    names = _safe_names(cgmap)
    by: dict[int, dict] = {}
    for gi, g in enumerate(cgmap):
        cid = int(g.get("c") or 0)
        item = by.setdefault(cid, {"id": cid, "name": names.get(cid, str(cid)),
                                   "groups": [], "total": 0})
        n = len(g.get("f") or [])
        item["groups"].append({"i": gi, "k": g.get("k") or "", "n": n})
        item["total"] += n
    chars = sorted(by.values(), key=lambda x: -x["total"])
    return {"chars": chars, "total": sum(c["total"] for c in chars)}


def _safe_names(cgmap) -> dict:
    """``{cid: 角色名}``。命名规则统一由 :mod:`server.names` 决定（中文优先）。"""
    from . import names as nameutil
    out = {}
    for g in cgmap:
        cid = g.get("c")
        if cid is None:
            continue
        out[cid] = nameutil.of(cid)
    return out


def _san(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s)).strip()
    return (s or "x")[:60]


# ---------------------------------------------------------------- 视频落盘
def save_video(blob_path: str, name: str = "", convert_mp4: bool = False) -> dict:
    """把浏览器上传的录制文件搬到 export 目录，可选 ffmpeg 转 mp4。"""
    os.makedirs(paths.EXPORT, exist_ok=True)
    ext = os.path.splitext(blob_path)[1] or ".webm"
    dst = _unique_path(_san(name or "story_video"), ext)
    shutil.move(blob_path, dst)
    info = {"path": dst, "name": os.path.basename(dst),
            "bytes": os.path.getsize(dst), "mp4": ""}
    if convert_mp4:
        ff = shutil.which("ffmpeg")
        if ff:
            mp4 = os.path.splitext(dst)[0] + ".mp4"
            import subprocess
            try:
                subprocess.run([ff, "-y", "-i", dst, "-c:v", "libx264",
                                "-preset", "veryfast", "-crf", "20",
                                "-c:a", "aac", "-b:a", "160k", mp4],
                               capture_output=True, timeout=3600, check=False)
                if os.path.exists(mp4):
                    info["mp4"] = mp4
            except Exception:                                # noqa: BLE001
                pass
    return info


def list_exports() -> list[dict]:
    out = []
    if not os.path.isdir(paths.EXPORT):
        return out
    for name in sorted(os.listdir(paths.EXPORT), reverse=True)[:100]:
        p = os.path.join(paths.EXPORT, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": name, "bytes": st.st_size,
                    "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


# ---------------------------------------------------------------- 译文导入导出
_LINE = re.compile(r"^(.*?) \|\|\| ?(.*)$")


def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n")


def _unesc(s: str) -> str:
    return s.replace("\\n", "\n").replace("\\\\", "\\")


def export_trans(unit: str) -> str:
    """生成某部剧情的双语对照 txt（可交给 AI 翻译后再导回）。"""
    d = unit_data(unit)
    if not d:
        raise FileNotFoundError(f"没有剧情数据：data/{unit}.js（该剧情的资源包还没下载）")
    seen = set()
    rows = []
    for i, p in enumerate(d.get("parts") or []):
        for m in p.get("methods") or []:
            if m.get("Method") != "ShowMessage":
                continue
            ja = m.get("Message") or ""
            if not ja.strip() or ja in seen:
                continue
            seen.add(ja)
            rows.append(ja)
    cur = {}
    fp = os.path.join(TRANS_DIR, f"{unit}.json")
    if os.path.exists(fp):
        cur = (paths.load_json(fp, {}) or {}).get("messages", {}) or {}
    head = TRANS_HEADER.format(unit=unit, title=unit_title(unit),
                              parts=len(d.get("parts") or []),
                              lines=len(rows),
                              at=time.strftime("%Y-%m-%d %H:%M:%S"))
    body = "\n".join(f"{_esc(ja)} ||| {_esc(cur.get(ja, ''))}" for ja in rows)
    return head + body + "\n"


def import_trans(unit: str, text: str, mode: str = "merge") -> dict:
    """解析并合并译文。→ {added, updated, skipped, total}"""
    fp = os.path.join(TRANS_DIR, f"{unit}.json")
    cur = {}
    if os.path.exists(fp) and mode != "replace":
        cur = dict((paths.load_json(fp, {}) or {}).get("messages", {}) or {})

    added = updated = skipped = bad = 0
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            bad += 1
            continue
        ja, zh = _unesc(m.group(1)), _unesc(m.group(2))
        if not ja.strip():
            bad += 1
            continue
        if not zh.strip():
            skipped += 1
            continue
        if ja in cur and cur[ja] == zh:
            skipped += 1
            continue
        if ja in cur:
            updated += 1
        else:
            added += 1
        cur[ja] = zh

    os.makedirs(TRANS_DIR, exist_ok=True)
    paths.save_json(fp, {"unit": unit, "lang": "zh-Hans",
                         "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "messages": cur})
    _update_trans_index(unit, len(cur))
    return {"added": added, "updated": updated, "skipped": skipped,
            "bad": bad, "total": len(cur), "path": paths.rel(fp)}


def _update_trans_index(unit: str, n: int) -> None:
    idx_path = os.path.join(paths.WEB, "assets", "i18n", "index.json")
    idx = paths.load_json(idx_path, {}) or {}
    idx.setdefault("chars", {})
    cid = unit[5:] if unit.startswith("char_") else unit
    idx["chars"].setdefault(cid, {})
    idx["chars"][cid]["zh-Hans"] = n
    paths.save_json(idx_path, idx)


def trans_stats() -> dict:
    out = {"units": 0, "messages": 0}
    if not os.path.isdir(TRANS_DIR):
        return out
    for name in os.listdir(TRANS_DIR):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        d = paths.load_json(os.path.join(TRANS_DIR, name), {}) or {}
        n = len(d.get("messages") or {})
        if n:
            out["units"] += 1
            out["messages"] += n
    return out
