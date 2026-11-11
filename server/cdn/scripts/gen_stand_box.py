"""生成立绘「画布落点表」 ``web/data/stand_box.js``。

为什么需要它
------------
剧本的 ``ShowStand`` 只给 ``StandImage`` + ``Position``（Left/Center/Right），
**没有坐标也没有缩放**。游戏里的尺寸与位置全部来自资源包自带的
``base_info``：

    AlphaCutRectLeft/Top/Width/Height  = 这张立绘在 1280x720 画布上的落点与尺寸

实测（2026-11-09，真夏のエスカレイヤー 第1段第72句 三人在台，与游戏截图对齐）：

    Center  = AlphaCutRect 原样
    Left    = AlphaCutRect + (-320, 0)
    Right   = AlphaCutRect + (+320, 0)

（1280/4 = 320，即站位锚点 320 / 640 / 960。）

播放器以前没有这张表，只能"贴底居中 + 按人数缩小"，于是立绘比游戏里小一圈、
还低了一截（伞/头顶位置明显违和）。本脚本把官方 AlphaCutRect 落成静态表。

输出
----
``web/data/stand_box.js``： ``window.__STANDBOX = {"21_8AEE967B": [464,120,360,600], ...}``
key = ``<角色id>_<服装hex>``（与 stand_slot.json 同构，不含表情 hex）。

只读 ``base_info``（TextAsset），不解码贴图，所以很快；缺包/缺 base_info 的组
直接跳过——播放器对没有条目的立绘回退旧布局。

用法：
    python server/cdn/scripts/gen_stand_box.py
"""
import json
import os
import re
import sys

import UnityPy

#: 站位锚点相对 AlphaCutRect 的水平偏移（画布 px）
POS_DX = {"Left": -320, "Center": 0, "Right": 320}


def _find_root(start):
    d = os.path.abspath(start)
    for _ in range(8):
        if os.path.isdir(os.path.join(d, "web")) and os.path.isdir(os.path.join(d, "server")):
            return d
        d = os.path.dirname(d)
    return os.path.dirname(os.path.dirname(os.path.abspath(start)))


ROOT_DIR = _find_root(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(ROOT_DIR, "userdata", "files_img")
DATA = os.path.join(ROOT_DIR, "web", "data")
OUT = os.path.join(DATA, "stand_box.js")


def read_box(path):
    """base_info → [L,T,W,H, faceL,faceT,faceW,faceH]。

    前 4 项 = AlphaCutRect（立绘在 1280×720 画布上的落点）。
    后 4 项 = **ExpressionCutRect**（官方的脸部矩形，相对 AlphaCutRect
    左上角的局部坐标）。实测同一角色不同服装虽然 AlphaCutRect 不同，
    但 ``AlphaCutRectLeft + ExpressionCutRectLeft`` 恒为定值（如角色 0100
    恒为 604、Top 之和恒为 200），说明这就是客户端用来挂表情/气泡的锚点。
    以前播放器用 ``0.6*W / 0.05*H`` 之类自己算的系数近似，位置自然违和。
    """
    try:
        env = UnityPy.load(path)
    except Exception:                                             # noqa: BLE001
        return None
    for o in env.objects:
        if o.type.name != "TextAsset":
            continue
        raw = o.read().m_Script
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        try:
            j = json.loads(raw.decode("utf-8"))
        except Exception:                                        # noqa: BLE001
            continue
        if "AlphaCutRectLeft" not in j:
            continue
        try:
            a = [int(round(float(j[k]))) for k in (
                "AlphaCutRectLeft", "AlphaCutRectTop",
                "AlphaCutRectWidth", "AlphaCutRectHeight")]
            e = [int(round(float(j[k]))) for k in (
                "ExpressionCutRectLeft", "ExpressionCutRectTop",
                "ExpressionCutRectWidth", "ExpressionCutRectHeight")]
        except Exception:                                        # noqa: BLE001
            e = [0, 0, 0, 0]
        return a + e
    return None


def main():
    if not os.path.isdir(IMG):
        print(f"找不到资源目录：{IMG}")
        return 2
    # key 必须用**剧本里的写法**（stand_100 / stand_0100 是两套不同素材），
    # 不能拿包名反推：包名常带补零，和剧本 key 不一致。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import compose_stand_all as C                                  # noqa: PLC0415

    groups = C.collect_tasks()
    boxes = {}
    no_pkg = []
    for (num, cloth) in sorted(groups):
        p = C._find_pkg(num, cloth)
        if not p:
            no_pkg.append(f"{num}_{cloth}")
            continue
        b = read_box(p)
        if not b or b[2] <= 0 or b[3] <= 0:
            continue
        boxes[f"{num}_{cloth}"] = b

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("window.__STANDBOX=" + json.dumps(
            boxes, ensure_ascii=False, separators=(",", ":")) + ";\n")

    # 覆盖率：磁盘上已有的合成立绘组里，有多少能拿到官方落点
    webs = os.path.join(ROOT_DIR, "web", "media", "img", "stand")
    have = set()
    if os.path.isdir(webs):
        for fn in os.listdir(webs):
            m = re.match(r"^stand_(\d+)_([0-9A-Fa-f]+)_[0-9A-Fa-f]+\.webp$", fn)
            if m:
                have.add(m.group(1) + "_" + m.group(2))
    miss = have.difference(boxes)
    print(f"剧本引用 {len(groups)} 组 → 落点 {len(boxes)} 组 → {OUT}")
    print(f"合成立绘 {len(have)} 组，其中 {len(have & set(boxes))} 组有官方落点"
          f"（缺 {len(miss)} 组）")
    if miss:
        print("  缺落点示例：" + "、".join(sorted(miss)[:12]))
    if no_pkg:
        print(f"  {len(no_pkg)} 组本地缺资源包（播放器回退旧布局）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
