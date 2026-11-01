# -*- coding: utf-8 -*-
"""生成 `web/data/othereps.js`（播放器侧栏「其他」的分组清单）。

分组与话名**不再靠关键词猜**，全部来自 tools/other_taxonomy.py 的硬编码映射
（依据用户在游戏里抄下来的三张表，见那个模块的文档）。

**不拆数据文件**：`adv_other_N.js` 原样保留，侧栏的一条"话"指向
「哪个包 + 第几段」，播放时先加载包再跳到那一段。
这样 PACKSTATE 的下载状态、语音/图片映射全都不受影响。

用法：
    python tools/build_other_episodes.py
"""
from __future__ import annotations

import collections
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from other_taxonomy import (  # noqa: E402
    DATA, ENTRIES, ORDER, messages, plan, read_js, verify,
)

CACHE = {}


def pack(no):
    if no not in CACHE:
        CACHE[no] = read_js(os.path.join(DATA, f"adv_other_{no}.js"))["parts"]
    return CACHE[no]


def cast_of(part, k=3):
    """该段出场最多的立绘组（用来做悬停提示里的「主要人物」）。"""
    c = collections.Counter(
        str(m["GroupId"]) for m in part["methods"]
        if m.get("GroupId") and m.get("Method") == "ShowStand"
        and str(m["GroupId"]) != "タイトルロゴ")
    return [n for n, _ in c.most_common(k)]


def main() -> int:
    rows, parts, bad = verify()
    if bad:
        print("分类表自检失败，已中止：")
        for b in bad:
            print("  -", b)
        return 1

    groups = []
    for entry in ORDER:
        rs = [r for r in rows if r[2] == entry]
        if not rs:
            continue
        eps = []
        for no, i, _e, title in rs:
            p = pack(no)[i]
            eps.append({
                # 数据文件始终是整包（adv_other_<N>），播放时再跳到第 i 段
                "k": f"adv_other_{no}",
                "i": i,
                "t": title,
                "n": len(messages(p)),
                "c": (cast_of(p) or [""])[0],
            })
        groups.append({"no": entry, "name": ENTRIES[entry], "eps": eps})

    p = os.path.join(DATA, "othereps.js")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("window.__OTHER_EPS=" + json.dumps(groups, ensure_ascii=False,
                                                  separators=(",", ":")) + ";")

    total = sum(len(g["eps"]) for g in groups)
    print(f"写出 {os.path.relpath(p, ROOT)}：{len(groups)} 组 / {total} 话")
    for g in groups:
        print(f"  etc_{g['no']:<4} {g['name']:<26} {len(g['eps']):>3} 话"
              f"  （首话：{g['eps'][0]['t']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
