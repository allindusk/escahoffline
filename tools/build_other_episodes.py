# -*- coding: utf-8 -*-
"""把「其他」拆成"话"，并生成 `web/data/othereps.js`（播放器侧栏用）。

思路
----
**不拆数据文件**：`adv_other_N.js` 原样保留，侧栏的一条"话"指向
「哪个包 + 第几段」，播放时先加载包再跳到那一段。
这样 PACKSTATE 的下载状态、语音/图片映射全都不受影响。

一话的判定与 tools/classify_other.py 一致：
* 1 段包 / 2 段包 → 一话（2 段是前篇+后篇，播放器里就是两个段标签）
* ≥6 段包 → 合集，每段一话

用法：
    python tools/build_other_episodes.py
"""
from __future__ import annotations

import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from classify_other import ENTRIES, episodes, build_class_index, classify  # noqa: E402

DATA = os.path.join(ROOT, "web", "data")


def main() -> int:
    cls = build_class_index()
    rows = episodes()
    for r in rows:
        r["entry"], r["why"] = classify(r, cls)
        r.pop("_text", None)

    # 分组：入口号升序，未分类放最后
    rows.sort(key=lambda r: (r["entry"] is None, r["entry"] or 0, r["pack"], r["ep"]))

    groups = []
    seen = {}
    for r in rows:
        g = r["entry"]
        if g not in seen:
            seen[g] = {"no": g, "name": ENTRIES.get(g, "未分类") if g else "未分类",
                       "eps": []}
            groups.append(seen[g])
        seen[g]["eps"].append({
            # 数据文件始终是整包（adv_other_<N>），播放时再跳到第 i 段
            "k": f"adv_other_{r['pack']}",
            "i": r["ep"],
            "t": r["first"][:28] or f"第{r['ep'] + 1}段",
            "n": r["lines"],
            # 1 = 合集包里的一话（侧栏显示 #N）；0 = 独立一话（2 段包算一话）
            "m": 1 if r.get("_collection") else 0,
            "c": (r["cast"] or [""])[0],
        })

    out = groups
    p = os.path.join(DATA, "othereps.js")
    with open(p, "w", encoding="utf-8") as f:
        f.write("window.__OTHER_EPS=" + json.dumps(out, ensure_ascii=False,
                                                   separators=(",", ":")) + ";")
    total = sum(len(g["eps"]) for g in out)
    print(f"写出 {os.path.relpath(p, ROOT)}：{len(out)} 组 / {total} 话")
    for g in out:
        print(f"  etc_{g['no'] if g['no'] else '?'} {g['name']} —— {len(g['eps'])} 话")
    return 0


if __name__ == "__main__":
    sys.exit(main())
