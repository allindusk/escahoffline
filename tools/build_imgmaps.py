#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重新生成图片映射表（原图 / 超分两层）

为什么拆成两层
--------------
改造前 ``imgmap.js`` 只有一张表，值是"当前该用的路径"，里面**混着**原图
（``media/img/back/x.png``）和超分图（``media/img_x2/back/x.webp``）。
一旦超分图缺失（新用户只下了原图包），播放器就直接黑图，没有任何回退。

现在拆成：

    data/imgmap.js      window.__IMGMAP  原图路径（必然存在，资源包只发这些）
    data/imgmap.x2.js   window.__IMGX2   超分路径（可缺，缺了等于没超分）

播放器优先用 ``__IMGX2``，没有就回落到 ``__IMGMAP``。这样：

* 新用户下原图 → 能看
* 本地超分 → 写 ``imgmap.x2.js`` 覆盖层 → 自动变高清
* 删原图 → 只要覆盖层齐了，照样能看
* 上游重新生成数据 → 本脚本可重复跑，输出确定

用法::

    python tools/build_imgmaps.py            # 重建两层映射
    python tools/build_imgmaps.py --dry-run  # 只看统计
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
DATA = os.path.join(WEB, "data")
MEDIA = os.path.join(WEB, "media")

#: 图片类别 → media 下的子目录名（原图 / 超分同名）
KINDS = ["stand", "back", "cg", "standfull"]

#: 同一 key 有多种扩展名时的优先顺序（越小越优先）
EXT_RANK = {".webp": 0, ".png": 1, ".jpg": 2, ".jpeg": 3}


def load_js(path: str):
    with open(path, encoding="utf-8-sig") as f:
        s = f.read()
    i = s.find("=")
    if i < 0:
        raise ValueError(path)
    body = s[i + 1:].strip()
    if body.endswith(";"):
        body = body[:-1]
    return json.loads(body)


def index_dir(base: str, kind: str) -> dict[str, str]:
    """→ {文件基名: 相对路径}，同基名多扩展名按 EXT_RANK 取最优。"""
    d = os.path.join(base, kind)
    out: dict[str, str] = {}
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        if ext not in EXT_RANK:
            continue
        cur = out.get(stem)
        if cur is None or EXT_RANK[ext] < EXT_RANK[os.path.splitext(cur)[1].lower()]:
            out[stem] = f"media/{os.path.basename(base)}/{kind}/{name}"
    return out


def collect_keys() -> dict[str, set]:
    """key 全集：现有 imgmap.js ∪ 剧情数据里实际引用到的。"""
    keys = {k: set() for k in KINDS}
    old = os.path.join(DATA, "imgmap.js")
    if os.path.exists(old):
        d = load_js(old)
        for k in KINDS:
            for key in (d.get(k) or {}):
                keys[k].add(key)
    # 剧情数据里的引用（含 imgmap 遗漏的）
    for name in os.listdir(DATA):
        if not name.endswith(".js") or name.startswith("imgmap"):
            continue
        uid = name[:-3]
        if not (uid.startswith("char_") or uid.startswith("adv_")):
            continue
        try:
            d = load_js(os.path.join(DATA, name))
        except Exception:                                    # noqa: BLE001
            continue
        for p in d.get("parts", []):
            for k, field in (("stand", "stand"), ("back", "back"),
                             ("cg", "cg"), ("standfull", "full")):
                for key in p.get(field) or []:
                    keys[k].add(key)
    return keys


def write_js(path: str, var: str, obj) -> None:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"window.{var}={body};\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重建 imgmap.js / imgmap.x2.js")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    keys = collect_keys()
    base_idx = {k: index_dir(os.path.join(MEDIA, "img"), k) for k in KINDS}
    x2_idx = {k: index_dir(os.path.join(MEDIA, "img_x2"), k) for k in KINDS}

    base_out, x2_out = {}, {}
    lost = []
    for k in KINDS:
        b, x = {}, {}
        for key in sorted(keys[k]):
            if key in base_idx[k]:
                b[key] = base_idx[k][key]
            else:
                lost.append((k, key))
            if key in x2_idx[k]:
                x[key] = x2_idx[k][key]
        base_out[k] = b
        x2_out[k] = x
        print(f"  {k:<10} key {len(keys[k]):>6}  原图 {len(b):>6}  超分 {len(x):>6}")

    if lost:
        print(f"  [X] {len(lost)} 个 key 连原图都找不到（无法播放，需回源）：")
        for k, key in lost[:8]:
            print(f"      {k}/{key}")
    else:
        print("  [OK] 每个 key 都有原图")

    if a.dry_run:
        return 0
    write_js(os.path.join(DATA, "imgmap.js"), "__IMGMAP", base_out)
    write_js(os.path.join(DATA, "imgmap.x2.js"), "__IMGX2", x2_out)
    for f in ("imgmap.js", "imgmap.x2.js"):
        p = os.path.join(DATA, f)
        print(f"  写入 {f}  {os.path.getsize(p) / 1024:.0f} KB")
    return 0 if not lost else 1


if __name__ == "__main__":
    sys.exit(main())
