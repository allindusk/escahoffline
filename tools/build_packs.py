#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成资源包清单 web/data/packs.json

背景
----
播放器的剧情数据（``web/data/char_<id>.js`` / ``adv_<kind>_<no>.js``）里，
每个 part 都自带 ``stand / full / back / cg / music / sound / voices`` 七个
资源键清单，配合 ``imgmap.js``（图片键→原图路径）与 ``sfxmap.js``（音乐键→
路径）可以**精确反查**这部剧到底用到哪些文件，一个不多一个不少。

因此资源包不需要人工规划，全部由本脚本静态求解：

    core          索引 / 章节封面 / 头像（很小，但必须有）
    shared_back   被 ≥SHARE_MIN 部剧情共用的背景图
    shared_art    被共用的立绘 / CG
    shared_sfx    被共用的 BGM / 音效
    char_<id>     该角色剧情正文 + 语音 + 专属立绘
    adv_<kind>_<no> 该主线/活动剧情正文 + 语音 + 专属素材

``shared_*`` 是"软依赖"：不下也能打开剧情，只是缺背景/没声音。打包与前端
都按这个语义处理（前端会默认帮你勾上，但允许取消）。

这样"用户只想看某个角色"时，装 ``core + shared_* + char_X`` 一定能完整
播放，不会出现"少了别人的立绘就黑屏"。

输出
----
``web/data/packs.json`` —— 小体积（约 130 KB），运行时前端读取；**不含**
文件明细。文件明细由 ``tools/make_pack_archives.py`` 在打包时写进 zip。

用法::

    python tools/build_packs.py                 # 生成清单 + 完备性校验
    python tools/build_packs.py --check-only    # 只校验，不写文件
    python tools/build_packs.py --share-min 2   # 调公共素材阈值
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

try:                                     # Windows 控制台默认 GBK，先切 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # noqa: BLE001
    pass

# ---------------------------------------------------------------- 路径常量
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
DATA = os.path.join(WEB, "data")
MEDIA = os.path.join(WEB, "media")

#: 一个媒体文件被多少个剧情单元引用就算"公共素材"
SHARE_MIN = 2

#: 软依赖包（不下也能播，只是降级）
SOFT_DEPOTS = ["shared_back", "shared_art", "shared_sfx"]

#: 索引类数据：很小、所有页面都要 → core
INDEX_FILES = [
    "data/names.js", "data/index.js", "data/advindex.js",
    "data/imgmap.js", "data/imgmap.x2.js", "data/imgmap.base.js", "data/sfxmap.js",
    "data/charmeta.js", "data/cgmap.js",
    "data/stand_layers.json", "data/stand_slot.json",
]

UNIT_RE = r"^(char_\d+|adv_(?:main|event|other|side|tutorial|op)(?:_\d+)?)$"


# ---------------------------------------------------------------- 读 JS 数据
def load_js(path: str):
    """解析 ``window.__X={...};`` 形式的纯数据文件。

    文件不存在时返回空 dict：`sfxmap.js` 是运行时产物（装完音效才生成），
    仓库和 CI 里都没有它，不该让校验直接崩掉。
    """
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        s = f.read()
    i = s.find("=")
    if i < 0:
        raise ValueError(f"{path} 不是 window.__X=... 形式")
    body = s[i + 1:].strip()
    if body.endswith(";"):
        body = body[:-1]
    return json.loads(body)


def index_media_dir(sub: str) -> dict[str, str]:
    """→ {文件基名: 站点相对路径}；资源包只分发 ``media/img/*`` 原始图。"""
    d = os.path.join(MEDIA, "img", sub)
    out = {}
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".webp", ".png", ".jpg", ".jpeg"):
            continue
        out.setdefault(stem, f"media/img/{sub}/{name}")
    return out


def shared_depot_of(path: str) -> str:
    """公共素材按类型分桶，让"先不下音乐也能看剧情"成立。"""
    if path.startswith("media/img/back/"):
        return "shared_back"
    if path.startswith("media/audio/"):
        return "shared_sfx"
    return "shared_art"


# ---------------------------------------------------------------- 单元解析
def scan_units():
    """→ {unit_id: {'kind','no','media':set(...),'data':路径}}"""
    import re
    unit_re = re.compile(UNIT_RE)
    units = {}
    for name in sorted(os.listdir(DATA)):
        if not name.endswith(".js"):
            continue
        uid = name[:-3]
        if not unit_re.match(uid):
            continue
        try:
            d = load_js(os.path.join(DATA, name))
        except Exception as e:                                   # noqa: BLE001
            print(f"  ! 跳过 {name}：{e}")
            continue

        kind = "char" if uid.startswith("char_") else "adv"
        media = set()
        # 语音目录：角色用角色 id，主线/活动用单元 key（与 player.js 一致）
        voice_dir = str(d.get("id")) if kind == "char" else uid
        for p in d.get("parts", []):
            for k in ("stand", "back", "cg"):
                for key in p.get(k) or []:
                    path = IMGMAP.get(k, {}).get(key)
                    if path:
                        media.add(path)
            for key in p.get("full") or []:            # 全身立绘（standfull）
                path = STANDFULL.get(key)
                if path:
                    media.add(path)
            for key in (p.get("music") or []) + (p.get("sound") or []):
                path = SFXMAP.get(key)
                if path:
                    media.add(path)
            for v in p.get("voices") or []:
                media.add(f"media/audio/{voice_dir}/{v}.m4a")
        units[uid] = {"kind": kind, "no": d.get("no"),
                      "media": media, "data": f"data/{uid}.js"}
    return units


# ---------------------------------------------------------------- 主流程
def build(share_min: int):
    units = scan_units()
    print(f"扫描到 {len(units)} 个剧情单元")

    # ---- 引用计数
    refcnt = defaultdict(int)
    owner = {}
    for uid, u in units.items():
        for f in u["media"]:
            refcnt[f] += 1
            owner.setdefault(f, uid)

    depot_files = defaultdict(set)

    # ---- core：索引 + 封面 + 头像
    for f in INDEX_FILES:
        if os.path.exists(os.path.join(WEB, f)):
            depot_files["core"].add(f)
    for sub in ("story", "avatar"):
        d = os.path.join(MEDIA, "img", sub)
        if os.path.isdir(d):
            for name in os.listdir(d):
                depot_files["core"].add(f"media/img/{sub}/{name}")

    # ---- 源资源本身就缺的（游戏侧没抓到）→ 剔除并记录
    missing_src = sorted(f for f in refcnt
                         if not os.path.exists(os.path.join(WEB, f)))
    for f in missing_src:
        refcnt.pop(f, None)
        owner.pop(f, None)

    # ---- 每个单元一定有自己的一包（至少含剧情正文）
    for uid in units:
        depot_files[uid].add(units[uid]["data"])

    # ---- 媒体：共享 ≥share_min 进 shared_*，否则归独占它的单元
    for f, n in refcnt.items():
        if n >= share_min:
            depot_files[shared_depot_of(f)].add(f)
        else:
            depot_files[owner[f]].add(f)

    # ---- 全身立绘 standfull：播放器暂未引用，按 charId 归角色包
    assigned = set()
    for fs in depot_files.values():
        assigned |= fs
    for stem, path in STANDFULL.items():
        if path in assigned:
            continue
        did = f"char_{stem}" if f"char_{stem}" in units else "core"
        depot_files[did].add(path)
        assigned.add(path)

    # ---- 体积
    def size_of(files):
        total = 0
        for f in files:
            try:
                total += os.path.getsize(os.path.join(WEB, f))
            except OSError:
                pass
        return total

    names = _load_optional("data/names.js") or {}
    advs = _load_optional("data/advindex.js") or []
    adv_by_key = {a.get("key"): a for a in advs}

    TITLE = {
        "core": "核心索引（剧情目录 / 图片映射 / 章节封面 / 头像）",
        "shared_back": "公共背景（多部剧情共用的场景图）",
        "shared_art": "公共立绘与 CG（多部剧情共用的角色图）",
        "shared_sfx": "公共音乐与音效（BGM / SE）",
    }

    depots = []
    for did in sorted(depot_files):
        files = depot_files[did]
        if did in TITLE:
            kind, title = did.split("_")[0] if "_" in did else did, TITLE[did]
            kind = "core" if did == "core" else "shared"
            requires, soft = [], []
        elif did.startswith("char_"):
            kind = "char"
            title = f"角色 {did[5:]} · {names.get(did[5:]) or '未命名'}"
            requires, soft = ["core"], list(SOFT_DEPOTS)
        else:
            kind = "adv"
            a = adv_by_key.get(did) or {}
            title = _adv_title(did, a)
            requires, soft = ["core"], list(SOFT_DEPOTS)
        depots.append({
            "id": did,
            "kind": kind,
            "title": title,
            "requires": requires,
            "soft": soft,
            "files": len(files),
            "bytes": size_of(files),
        })

    # ---- 单元 → 依赖
    units_out = {}
    for uid, u in units.items():
        hard = [d for d in (uid, "core") if d in depot_files]
        units_out[uid] = {
            "kind": u["kind"],
            "no": u.get("no"),
            "depots": hard,
            "soft": [d for d in SOFT_DEPOTS if d in depot_files],
            "bytes": sum(size_of(depot_files[d]) for d in hard),
            "bytesFull": sum(size_of(depot_files[d])
                             for d in hard + SOFT_DEPOTS if d in depot_files),
        }

    total_files = set()
    for fs in depot_files.values():
        total_files |= fs
    manifest = {
        "schema": 1,
        "generated": _today(),
        "share_min": share_min,
        "totals": {
            "units": len(units),
            "depots": len(depots),
            "files": len(total_files),
            "bytes": size_of(total_files),
        },
        "missing": missing_src[:200],
        "missingCount": len(missing_src),
        "depots": depots,
        "units": units_out,
    }
    return manifest, depot_files, units


def _adv_title(uid: str, a: dict) -> str:
    label = {"main": "主线", "event": "活动", "other": "其他",
             "side": "外传", "tutorial": "新手教程", "op": "开场动画"}
    kind = (uid.split("_") + [""])[1]
    no = a.get("no") or uid.rsplit("_", 1)[-1]
    name = label.get(kind, kind)
    return f"{name} 第 {no} 话" if str(no).isdigit() else f"{name} {uid}"


def _load_optional(relpath: str):
    p = os.path.join(WEB, relpath)
    if not os.path.exists(p):
        return None
    try:
        return load_js(p)
    except Exception:                                            # noqa: BLE001
        return None


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------- 校验
def verify(depot_files, units) -> int:
    """唯一归属 / 存在性 / 单元覆盖 三项校验，返回错误数。"""
    errs = 0
    seen = defaultdict(list)
    for did, files in depot_files.items():
        for f in files:
            seen[f].append(did)

    dup = {f: d for f, d in seen.items() if len(d) > 1}
    if dup:
        print(f"  [X] {len(dup)} 个文件被重复分配")
        for f, d in list(dup.items())[:5]:
            print(f"      {f} -> {d}")
        errs += 1
    else:
        print("  [OK] 文件唯一归属：无重复")

    missing = 0
    for f in seen:
        if not os.path.exists(os.path.join(WEB, f)):
            missing += 1
            if missing <= 5:
                print(f"  [X] 清单里的文件不存在：{f}")
    print(f"  {'[OK]' if not missing else '[X]'} 文件存在性："
          f"{len(seen):,} 个文件，缺失 {missing}")
    errs += 1 if missing else 0

    uncovered = src_missing = 0
    for uid, u in units.items():
        for f in u["media"]:
            if f in seen:
                continue
            if not os.path.exists(os.path.join(WEB, f)):
                src_missing += 1        # 源资源本身就没有，不算清单错误
                continue
            uncovered += 1
            if uncovered <= 5:
                print(f"  [X] {uid} 需要的文件没进任何包：{f}")
    print(f"  {'[OK]' if not uncovered else '[X]'} 单元覆盖：{len(units)} 个单元，"
          f"漏 {uncovered}（另有 {src_missing} 处源资源缺失，已剔除）")
    errs += 1 if uncovered else 0
    return errs


def main(argv=None) -> int:
    global IMGMAP, SFXMAP, STANDFULL
    ap = argparse.ArgumentParser(description="生成资源包清单 packs.json")
    ap.add_argument("--share-min", type=int, default=SHARE_MIN,
                    help="被多少个单元引用算公共素材（默认 2）")
    ap.add_argument("--check-only", action="store_true", help="只校验不写文件")
    a = ap.parse_args(argv)

    IMGMAP = load_js(os.path.join(DATA, "imgmap.js"))
    SFXMAP = load_js(os.path.join(DATA, "sfxmap.js"))
    STANDFULL = index_media_dir("standfull")

    manifest, depot_files, units = build(a.share_min)
    errs = verify(depot_files, units)

    t = manifest["totals"]
    print(f"\n总计：{t['units']} 单元 / {t['depots']} 包 / "
          f"{t['files']:,} 文件 / {t['bytes'] / 1024 ** 3:.2f} GB")
    size = {d["id"]: d["bytes"] for d in manifest["depots"]}
    core = size.get("core", 0)
    soft = sum(size.get(d, 0) for d in SOFT_DEPOTS)
    print(f"  首次安装 core = {core / 1024 ** 2:.0f} MB；"
          f"core+公共素材 = {(core + soft) / 1024 ** 2:.0f} MB")
    for d in sorted(manifest["depots"], key=lambda x: -x["bytes"])[:6]:
        print(f"    {d['id']:<16}{d['bytes'] / 1024 ** 2:9.1f} MB  "
              f"{d['files']:>7,} 文件  {d['title'][:34]}")

    if a.check_only:
        return 1 if errs else 0
    out = os.path.join(DATA, "packs.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\n已写入 {os.path.relpath(out, ROOT)} "
          f"（{os.path.getsize(out) / 1024:.1f} KB）")
    return 1 if errs else 0


IMGMAP: dict = {}
SFXMAP: dict = {}
STANDFULL: dict = {}

if __name__ == "__main__":
    sys.exit(main())
