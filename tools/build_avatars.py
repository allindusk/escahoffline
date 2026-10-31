# -*- coding: utf-8 -*-
"""从 ``bundle_chara_c-panel_<cid>`` 重新导出角色头像。

为什么重写
----------
旧的 ``stages.export_avatar``（以及更早的一次性脚本）会把同一张图写到多个 id 上，
实测 ``web/media/img/avatar/13.png`` / ``61.png`` / ``70.png`` 三个文件**字节完全相同**，
而对应三个 c-panel 包里的原图并不一样 —— 于是一个角色的头像被贴到了别的角色头上
（用户看到「魔女グリム 显示成 エスカ・サファイア」就是这个原因）。

现在一律**从包里现取**：先试 ``c-panel_<cid:03d>`` / ``<cid:05d>`` / ``<cid>`` 三种补零，
取第一张 Sprite，裁顶部正方形做头像，并且**每次都重写**（不复用旧文件）。

用法：
    python tools/build_avatars.py              # 全量重建（384 个）
    python tools/build_avatars.py 413 13       # 只重建指定 cid
    python tools/build_avatars.py --check      # 只报告现状（重复/缺失）
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
AVATAR = os.path.join(WEB, "media", "img", "avatar")
#: 剧本/半身像包所在目录（优先用项目里的工作区，退回旧分析目录）
SEARCH = [
    os.path.join(ROOT, "userdata", "char_story", "files"),
    os.path.join(ROOT, "_manifest_analysis", "char_story", "files"),
]
SIZE = 128


def find_bundle(cid: int) -> str | None:
    names = (f"bundle_chara_c-panel_{cid:03d}",
             f"bundle_chara_c-panel_{cid:05d}",
             f"bundle_chara_c-panel_{cid}")
    for d in SEARCH:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return None


def export_one(cid: int, force: bool = True) -> bool:
    from PIL import Image
    import UnityPy

    src = find_bundle(cid)
    if not src:
        return False
    dst = os.path.join(AVATAR, f"{cid}.png")
    if os.path.exists(dst) and not force:
        return True
    try:
        env = UnityPy.load(src)
        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            im = obj.read().image.convert("RGBA")
            # c-panel 是 170x270 的竖图（含胸像）：取**顶部正方形**当头像，
            # 正好是头部+肩，比裁整张好看，也不会把身体挤进小方块。
            w, h = im.size
            side = min(w, h)
            im = im.crop((0, 0, side, side)).resize((SIZE, SIZE), Image.LANCZOS)
            os.makedirs(AVATAR, exist_ok=True)
            im.save(dst, optimize=True)
            return True
    except Exception as e:                              # noqa: BLE001
        print(f"  cid {cid} 失败：{e}")
    return False


def check() -> int:
    """报告重复头像（同一个字节出现在多个 id 上 = 典型错配）。"""
    if not os.path.isdir(AVATAR):
        print("头像目录不存在")
        return 1
    h = {}
    for f in sorted(os.listdir(AVATAR)):
        if not f.endswith(".png"):
            continue
        p = os.path.join(AVATAR, f)
        h.setdefault(hashlib.md5(open(p, "rb").read()).hexdigest(), []).append(f)
    dup = {k: v for k, v in h.items() if len(v) > 1}
    print(f"头像 {sum(len(v) for v in h.values())} 个 / 唯一 {len(h)} 个 / 重复组 {len(dup)}")
    for v in list(dup.values())[:10]:
        print("   ", v)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", type=int)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--keep", action="store_true", help="已存在就跳过（默认全部重写）")
    a = ap.parse_args()
    if a.check:
        return check()

    ids = a.ids or sorted(int(f[5:-3]) for f in os.listdir(os.path.join(WEB, "data"))
                          if f.startswith("char_") and f.endswith(".js"))
    ok = 0
    for i, cid in enumerate(ids, 1):
        if export_one(cid, force=not a.keep):
            ok += 1
        if i % 50 == 0:
            print(f"  …{i}/{len(ids)}")
    print(f"导出 {ok}/{len(ids)} 个头像 → {os.path.relpath(AVATAR, ROOT)}")
    return check()


if __name__ == "__main__":
    sys.exit(main())
