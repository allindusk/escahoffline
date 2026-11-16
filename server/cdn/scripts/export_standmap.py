"""扫描 ``web/media/img/stand/`` → 生成立绘画廊索引 ``web/data/standmap.js``。

为什么不复用 ``imgmap.stand``
-----------------------------
``imgmap`` 只收**剧本里实际引用过**的立绘键（9545 条），而磁盘上有 19258 张：
没人用到的表情差分、NPC、敌人全都不在里面。画廊要"把有的都列出来"，
所以直接扫目录。

命名规则（``stages.compose_stands`` 的产物）
-------------------------------------------
``stand_<编号>_<服装HEX>_<表情HEX>.webp``

* **编号的字符串形态有意义**：``stand_36_`` 与 ``stand_036_`` 是两套不同素材
  （鷹守ハルカ 便服 vs 閃忍ハルカ 忍者装），不能 ``int()`` 归一化后合并；
* 编号段：角色是 1–416，NPC 是 4xxx，敌人是 5xxx —— 用来给侧栏分组。

用法（cwd = userdata/）
----------------------
    python export_standmap.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict

WEB = os.path.join("..", "web")
STAND_DIR = os.path.join(WEB, "media", "img", "stand")
OUT_JS = os.path.join(WEB, "data", "standmap.js")

RE_STAND = re.compile(r"^stand_(\d+)_([0-9A-Fa-f]+)_([0-9A-Fa-f]+)\.webp$")


def band(num: str) -> str:
    v = int(num)
    if v >= 5000:
        return "enemy"
    if v >= 4000:
        return "npc"
    return "char"


def main() -> int:
    if not os.path.isdir(STAND_DIR):
        print(f"没有 {STAND_DIR}")
        return 1
    groups = defaultdict(list)
    total = 0
    for fn in os.listdir(STAND_DIR):
        m = RE_STAND.match(fn)
        if not m:
            continue
        total += 1
        num, cloth, _expr = m.group(1), m.group(2).upper(), m.group(3)
        groups[(num, cloth)].append(fn[:-5])          # 去掉 .webp 的键

    out = []
    for (num, cloth), keys in groups.items():
        keys.sort()
        out.append({"n": num, "c": cloth, "b": band(num),
                    "f": keys})
    # 排序：角色（按编号数值大小）→ NPC → 敌人；同编号内按服装
    order = {"char": 0, "npc": 1, "enemy": 2}
    out.sort(key=lambda g: (order[g["b"]], int(g["n"]), g["c"]))

    data = {"v": 1, "groups": out}
    os.makedirs(os.path.dirname(OUT_JS), exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tmp = OUT_JS + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("window.__STANDMAP=" + body + ";")
    os.replace(tmp, OUT_JS)

    sizes = defaultdict(int)
    for g in out:
        sizes[g["b"]] += 1
    print(f"立绘 {total} 张 / {len(out)} 组"
          f"（角色 {sizes['char']} 组 · NPC {sizes['npc']} · 敌人 {sizes['enemy']}）"
          f" → {os.path.normpath(OUT_JS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
