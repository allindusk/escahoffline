# -*- coding: utf-8 -*-
"""「其他」剧情的归类清单（给核对页 other_map.json 用）。

归类口径**不在本文件**，全部来自 `tools/other_taxonomy.py`（按用户在游戏里抄下来的
三张表逐包点名，不再用关键词猜）。本文件只负责把映射结果带上首句/句数/立绘组，
输出成可核对的表。

用法：
    python tools/classify_other.py            # 打印
    python tools/classify_other.py --json     # 另存 _data_analysis/other_map.json
"""
from __future__ import annotations

import argparse
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

OUT = os.path.join(ROOT, "_data_analysis", "other_map.json")


def cast_of(part, k=4):
    c = collections.Counter(
        str(m["GroupId"]) for m in part["methods"]
        if m.get("GroupId") and m.get("Method") == "ShowStand"
        and str(m["GroupId"]) != "タイトルロゴ")
    return [n for n, _ in c.most_common(k)]


def rows():
    eps, parts, bad = verify()
    if bad:
        raise SystemExit("分类表自检失败：\n  " + "\n  ".join(bad))
    cache = {}
    out = []
    for no, i, entry, title in eps:
        if no not in cache:
            cache[no] = read_js(os.path.join(DATA, f"adv_other_{no}.js"))["parts"]
        p = cache[no][i]
        msgs = messages(p)
        out.append({
            "pack": no, "ep": i, "id": f"{no}-{i}", "entry": entry,
            "title": title, "parts": len(cache[no]), "lines": len(msgs),
            "first": " ".join(" ".join(msgs).split())[:60],
            "cast": cast_of(p),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rs = rows()
    print(f"其他：{len({r['pack'] for r in rs})} 包 → {len(rs)} 话\n")
    for entry in ORDER:
        grp = [r for r in rs if r["entry"] == entry]
        if not grp:
            continue
        print(f"== etc_{entry} {ENTRIES[entry]} —— {len(grp)} 话")
        for r in grp:
            print(f"   #{r['id']:<6} {r['lines']:>4}句  {r['title']:<26} {r['first'][:28]}")
        print()

    if a.json:
        json.dump(rs, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("写出", os.path.relpath(OUT, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
