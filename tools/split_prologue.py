# -*- coding: utf-8 -*-
"""把 ``bundle_scenarioscript_op`` 里的「部プロローグ」拆成三部序章。

背景
----
游戏里主线三部各有一张「第N部プロローグ」横幅（``topic_chapter_000/000b/000c`` →
``main_0/main_0b/main_0c.webp``），但**没有独立的序章剧本包** —— 611 个剧本包里
只有 main 1~61、event、other、char、tutorial、op。

序章剧情实际藏在 ``bundle_scenarioscript_op``（5 段）里。用户在游戏里核对过首句：

* 第1部序章 = 「トキサダ……トキサダ……」            → op_1
* 第2部序章 = 「はぁっ、はぁっ、はぁつ……!」        → op_3
* 第3部序章 = 「それは、あの激しい戦いから数ヶ月後のこと。」→ op_4

所以这里把 op 包按段拆成三个独立剧情（各带封面 / 标题 / 排序），
剩下没归属的段（op_2 / op_5）继续留在 ``adv_op`` 里。

用法：
    python tools/split_prologue.py            # 拆分并重写 advindex.js
    python tools/split_prologue.py --dry      # 只看会拆成什么
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "web", "data")
BACKUP = os.path.join(ROOT, "_data_analysis", "_backup")

#: (首句特征, 目标 key, no, order, 标题)
#: order 用小数把序章插到部与部之间：0 → 部1序章，23.5 → 部2序章（在 23 与 24 之间）
TARGETS = [
    ("トキサダ……トキサダ", "adv_main_0", 0, 0.0, "第1部 序章"),
    ("はぁっ、はぁっ", "adv_main_0b", "0b", 23.5, "第2部 序章"),
    ("あの激しい戦いから", "adv_main_0c", "0c", 44.5, "第3部 序章"),
]


def read_js(path):
    raw = open(path, encoding="utf-8-sig").read()
    i = raw.index("=")
    return json.loads(raw[i + 1:].strip().rstrip(";"))


def write_js(name, obj):
    p = os.path.join(DATA, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("window.__STORY=" + json.dumps(obj, ensure_ascii=False,
                                               separators=(",", ":")) + ";")
    return p


def first_msg(part):
    for m in part["methods"]:
        if m.get("Method") == "ShowMessage":
            return " ".join(str(m.get("Message") or "").split())
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    src = os.path.join(DATA, "adv_op.js")
    if not os.path.exists(src):
        print("找不到 adv_op.js，先接入 op 包")
        return 1
    op = read_js(src)
    parts = op["parts"]
    print(f"op 包 {len(parts)} 段：")
    for p in parts:
        print(f"  {p['file']}: {first_msg(p)[:40]}")

    taken, units = set(), []
    for needle, key, no, order, title in TARGETS:
        hit = None
        for p in parts:
            if p["file"] in taken:
                continue
            if needle in first_msg(p):
                hit = p
                break
        if not hit:
            print(f"  !! 没找到「{needle}」→ {key} 跳过")
            continue
        taken.add(hit["file"])
        units.append((key, {"id": key, "kind": "main", "no": no,
                            "order": order, "title": title,
                            "parts": [hit]}))
        print(f"  {hit['file']} → {key}（{title}，{sum(1 for m in hit['methods'] if m.get('Method') == 'ShowMessage')} 句）")

    rest = [p for p in parts if p["file"] not in taken]
    print(f"剩余留在 adv_op：{[p['file'] for p in rest]}")

    if a.dry:
        return 0

    os.makedirs(BACKUP, exist_ok=True)
    shutil.copy2(src, os.path.join(BACKUP, "adv_op.js.bak"))
    for key, data in units:
        write_js(key + ".js", data)
        print("  写出", key + ".js")

    if rest:
        left = dict(op)
        left["parts"] = rest
        left.setdefault("title", "")
        write_js("adv_op.js", left)
        print(f"  adv_op.js 保留 {len(rest)} 段")
    else:
        os.remove(src)
        print("  adv_op.js 已全部拆走，删除")

    # 重新汇总 advindex.js（侧栏数据源）
    sys.path.insert(0, ROOT)
    from server.cdn import stages                                  # noqa: E402
    stages.sync_advindex()
    return 0


if __name__ == "__main__":
    sys.exit(main())
