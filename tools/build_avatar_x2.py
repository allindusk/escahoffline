# -*- coding: utf-8 -*-
"""重建「超分头像」：``web/media/img_x2/avatar/<cid>.webp``，并同步 ``imgmap.x2.js``。

为什么需要这个
--------------
播放器 / CG 页取头像走的是两层映射：
``__IMGX2``（超分层，优先）→ ``__IMGMAP``（原图）→ 直接拼 ``media/img/avatar/<id>.png``。
2026-11-01 我重导了 ``media/img/avatar/*.png``（旧的那批本身是错的：13/61/70 字节相同），
**但没有重做 ``media/img_x2/avatar/*.webp``** —— 那一层还是用旧错图做的超分，
于是 CG 页仍旧显示「魔女グリム = エスカ・サファイア」。这条链没变，只是"第一优先级"的缓存是脏的。

做法：直接从 ``bundle_chara_c-panel_<cid>`` 现取原图（170×270），裁顶部正方形，
放大到 256×256 存 webp —— 比"拿 128 的原图再放大"清楚，也能和原图彻底对齐。

用法：
    python tools/build_avatar_x2.py            # 全量（384 个）
    python tools/build_avatar_x2.py 413 13     # 只做指定 cid
    python tools/build_avatar_x2.py --check    # 只报告两层是否一致
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
AVATAR = os.path.join(WEB, "media", "img", "avatar")
X2 = os.path.join(WEB, "media", "img_x2", "avatar")
IMGMAP_X2 = os.path.join(WEB, "data", "imgmap.x2.js")
SEARCH = [
    os.path.join(ROOT, "userdata", "char_story", "files"),
    os.path.join(ROOT, "_manifest_analysis", "char_story", "files"),
]
SIZE = 256


def find_bundle(cid: int) -> str | None:
    for d in SEARCH:
        for n in (f"bundle_chara_c-panel_{cid:03d}",
                  f"bundle_chara_c-panel_{cid:05d}",
                  f"bundle_chara_c-panel_{cid}"):
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return None


def export_one(cid: int) -> bool:
    from PIL import Image
    import UnityPy

    src = find_bundle(cid)
    if not src:
        return False
    try:
        env = UnityPy.load(src)
        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            im = obj.read().image.convert("RGBA")
            w, h = im.size
            side = min(w, h)
            im = im.crop((0, 0, side, side)).resize((SIZE, SIZE), Image.LANCZOS)
            os.makedirs(X2, exist_ok=True)
            im.save(os.path.join(X2, f"{cid}.webp"), "WEBP", quality=92, method=4)
            return True
    except Exception as e:                              # noqa: BLE001
        print(f"  cid {cid} 失败：{e}")
    return False


def sync_imgmap() -> int:
    """把 imgmap.x2.js 的 avatar 段补齐到"磁盘上真实存在的 x2 头像"。"""
    if not os.path.exists(IMGMAP_X2):
        print("没有 imgmap.x2.js，跳过映射同步")
        return 0
    raw = open(IMGMAP_X2, encoding="utf-8-sig").read()
    i = raw.index("=")
    data = json.loads(raw[i + 1:].strip().rstrip(";"))
    have = {}
    if os.path.isdir(X2):
        # 带上时间戳当缓存参数：头像重做过但文件名不变，浏览器会一直用旧图
        # （CG 页就是"魔女グリム 显示成 エスカ・サファイア"的现场）
        import time
        files = [f for f in os.listdir(X2) if f.endswith(".webp")]
        stamp = time.strftime("%m%d%H%M", time.localtime(
            max(os.path.getmtime(os.path.join(X2, f)) for f in files))) if files else "0"
        for f in files:
            have[f[:-5]] = f"media/img_x2/avatar/{f}?v={stamp}"
    old = data.get("avatar") or {}
    data["avatar"] = have
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with open(IMGMAP_X2, "w", encoding="utf-8") as f:
        f.write("window.__IMGX2=" + body + ";")
    print(f"imgmap.x2.js avatar：{len(old)} → {len(have)} 条")
    return len(have)


def check() -> int:
    from PIL import Image
    ids = sorted(int(f[:-4]) for f in os.listdir(AVATAR) if f.endswith(".png"))
    miss = [i for i in ids if not os.path.exists(os.path.join(X2, f"{i}.webp"))]
    print(f"原图 {len(ids)} 个，x2 {len(os.listdir(X2)) if os.path.isdir(X2) else 0} 个，缺 {len(miss)}：{miss[:10]}")
    if ids:
        p = os.path.join(X2, f"{ids[0]}.webp")
        if os.path.exists(p):
            print("x2 尺寸样例：", Image.open(p).size)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", type=int)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        return check()

    ids = a.ids or sorted(int(f[:-4]) for f in os.listdir(AVATAR)
                          if f.endswith(".png"))
    ok = 0
    for i, cid in enumerate(ids, 1):
        if export_one(cid):
            ok += 1
        if i % 60 == 0:
            print(f"  …{i}/{len(ids)}")
    print(f"导出 {ok}/{len(ids)} 张 x2 头像 → {os.path.relpath(X2, ROOT)}")
    sync_imgmap()
    return check()


if __name__ == "__main__":
    sys.exit(main())
