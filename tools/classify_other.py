# -*- coding: utf-8 -*-
"""把「其他」拆成话（2 段包=前/后篇合一话），并按 16 个真实入口归类。

16 个入口（逐张看 topic_etc_<num>.webp 的标题确认）
----------------------------------------------------
| 编号 | 标题 |
| 100  | RAID BOSS BATTLE ゴールデンハニー |
| 101  | RAID BOSS BATTLE グナガン |
| 102  | RAID BOSS BATTLE ウールタール |
| 103  | RAID BOSS BATTLE 堕威大仏 |
| 104  | RAID BOSS BATTLE 大往嬢 |
| 105  | RAID BOSS BATTLE 狂王 |
| 107  | RAID BOSS BATTLE ヴリトラ |
| 200  | 拡域戦 戦士 |
| 210  | 拡域戦 閃忍 |
| 220  | 拡域戦 神騎 |
| 230  | 拡域戦 魔女 |
| 240  | 拡域戦 その他 |
| 800  | 殲滅戦 -STAMPEDE- |
| 1000 | 記念イベント（周年） |
| 7000 | エイプリルフール |
| 9000 | メインストーリー Hシーン集 |

归类规则（按优先级）：
1. 命中 BOSS 名 → 对应 RAID 入口；
2. 命中「拡域」→ 按台词里出现的职种 / 主要立绘组的所属（戦士・閃忍・神騎・魔女）分到 200/210/220/230，分不出 → 240 その他；
3. 「殲滅 / スタンピード」→ 800；
4. 「周年 / アニバーサリー」→ 1000；
5. 「４月１日 / エイプリル」→ 7000；
6. 其余 → 未分类（H シーン集 这类合集在 bundle 里没有独立剧本，通常会是 0 话）。

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
DATA = os.path.join(ROOT, "web", "data")
OUT = os.path.join(ROOT, "_data_analysis", "other_map.json")

#: 编号 → 标题（读横幅图确认）
ENTRIES = {
    100: "RAID BOSS ゴールデンハニー",
    101: "RAID BOSS グナガン",
    102: "RAID BOSS ウールタール",
    103: "RAID BOSS 堕威大仏",
    104: "RAID BOSS 大往嬢",
    105: "RAID BOSS 狂王",
    107: "RAID BOSS ヴリトラ",
    200: "拡域戦 戦士",
    210: "拡域戦 閃忍",
    220: "拡域戦 神騎",
    230: "拡域戦 魔女",
    240: "拡域戦 その他",
    800: "殲滅戦 -STAMPEDE-",
    1000: "記念イベント（周年）",
    7000: "エイプリルフール",
    9000: "メインストーリー Hシーン集",
}

#: BOSS 关键词 → 入口（顺序无关，命中即算）
BOSS = [
    (100, ["ゴールデンハニー", "Ｇハニー", "Gハニー", "ゴールデン・ハニー"]),
    (101, ["グナガン"]),
    (102, ["ウールタール"]),
    (103, ["堕威大仏"]),
    (104, ["大往嬢"]),
    (105, ["狂王"]),
    (107, ["ヴリトラ"]),
]

#: 职种 → 拡域戦入口
CLASS_ENTRY = {"戦士": 200, "閃忍": 210, "神騎": 220, "魔女": 230}


def read_js(p):
    raw = open(p, encoding="utf-8-sig").read()
    i = raw.find("{")
    return json.loads(raw[i:raw.rindex("}") + 1])


def build_class_index():
    """角色名 → 所属（戦士/閃忍/神騎/魔女），用来给「拡域戦」分类。"""
    names = read_js(os.path.join(DATA, "names.js"))
    meta = read_js(os.path.join(DATA, "charmeta.js"))
    out = {}
    for cid, nm in names.items():
        t = (meta.get(cid) or {}).get("t") or ""
        if t:
            out[str(nm).split(" (")[0]] = t
    return out


def part_text(part):
    return "\n".join(str(m.get("Message") or "") for m in part["methods"]
                     if m.get("Method") == "ShowMessage")


def part_lines(part):
    return sum(1 for m in part["methods"] if m.get("Method") == "ShowMessage")


def cast_of(parts):
    c = collections.Counter()
    for p in parts:
        for m in p["methods"]:
            if m.get("GroupId"):
                c[m["GroupId"]] += 1
    return [k for k, _ in c.most_common(4)]


def episodes():
    rows = []
    packs = sorted((f for f in os.listdir(DATA)
                    if f.startswith("adv_other_") and f.endswith(".js")),
                   key=lambda s: int(s[:-3].rsplit("_", 1)[1]))
    for fn in packs:
        key = fn[:-3]
        no = int(key.rsplit("_", 1)[1])
        parts = read_js(os.path.join(DATA, fn))["parts"]
        groups = [parts] if len(parts) <= 2 else [[p] for p in parts]
        for i, grp in enumerate(groups):
            lines = sum(part_lines(p) for p in grp)
            if lines == 0:
                continue
            text = "\n".join(part_text(p) for p in grp)
            rows.append({
                "pack": no, "ep": i,
                "id": f"{no}-{i}",
                "key": key if len(parts) <= 2 else f"{key}_{i}",
                "parts": len(grp), "lines": lines,
                "cast": cast_of(grp),
                "first": " ".join(text.split())[:40],
                # 多段包 = 合集（每段一话），单/双段包 = 独立一话
                "_collection": len(parts) >= 6,
                "_text": text,
            })
    return rows


def classify(row, cls):
    text = row["_text"]
    # 合集包（≥6 段）整包都属于同一个合集入口，先判 —— 否则里面顺手提到某个 BOSS
    # 的那一话会被错分到 RAID。
    if row.get("_collection"):
        return 9000, "合集包"
    # BOSS 名可能在一话里出现好几个（战斗里连着打），按**最先出现**的算，
    # 因为开场那句通常决定了这一话属于哪个模式。
    best = None
    for num, keys in BOSS:
        for k in keys:
            pos = text.find(k)
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, num, k)
    if best:
        return best[1], f"BOSS:{best[2]}"

    if "拡域" in text:
        # 先看台词里的职种词，再看主要立绘组的所属
        for word, num in CLASS_ENTRY.items():
            if f"拡域戦 {word}" in text or f"拡域・{word}" in text:
                return num, f"拡域+{word}"
        for c in row["cast"]:
            t = cls.get(c)
            if t in CLASS_ENTRY:
                return CLASS_ENTRY[t], f"拡域+cast:{t}"
        return 240, "拡域（职种不明）"

    if "殲滅" in text or "スタンピード" in text or "STAMPEDE" in text:
        return 800, "殲滅"
    if "周年" in text or "アニバーサリー" in text:
        return 1000, "周年"
    # 8/2 是开服日，周年话里常写「今日は八月二日」而不是「周年」
    if "八月二日" in text or "8月2日" in text or "８月２日" in text:
        return 1000, "开服日 8/2（周年）"
    if "４月１日" in text or "4月1日" in text or "エイプリル" in text:
        return 7000, "エイプリル"
    # 「大往嬢」的读音梗（だいおうじょう）
    if "おうじょう" in text:
        return 104, "大往嬢（おうじょう）"
    # 兜底：只提到「ハニー」的日常短編基本都挂在 ゴールデンハニー 入口下
    if "ハニー" in text:
        return 100, "ハニー（弱）"
    return None, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    cls = build_class_index()
    rows = episodes()
    for r in rows:
        r["entry"], r["why"] = classify(r, cls)
        r.pop("_text", None)

    print(f"其他：{len({r['pack'] for r in rows})} 包 → {len(rows)} 话\n")
    by = collections.defaultdict(list)
    for r in rows:
        by[r["entry"]].append(r)
    for entry in sorted(by, key=lambda x: (x is None, x or 0)):
        name = ENTRIES.get(entry, "未分类") if entry else "未分类（不在 16 个入口里）"
        print(f"== etc_{entry if entry else '?'} {name} —— {len(by[entry])} 话")
        for r in by[entry][:6]:
            print(f"   #{r['id']:<6} {r['lines']:>4}句  {r['first']}")
        if len(by[entry]) > 6:
            print(f"   …… 还有 {len(by[entry]) - 6} 话")
        print()

    if a.json:
        json.dump(rows, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("写出", os.path.relpath(OUT, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
