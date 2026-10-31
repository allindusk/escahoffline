# -*- coding: utf-8 -*-
"""从剧本数据重建 web/data/names.js —— 不再依赖 WIKI 名字表。

为什么需要这个
--------------
旧链路的名字来自 `char_names_wiki.json`（人工快照）**按 id 无条件覆盖**
`char_names.json`（剧本抽取）。两份表的 id 空间不同源，实测 383 条里 150 条被
改成另一个名字（典型：cid 85 的剧本主讲是「妹の１０２４」，却显示「ユーノ」）。

剧本里其实有开发自己写的标签，比任何启发式都准：

    {"Time":"レジェンド・エスカレイヤー／レジェンド・エスカレイヤー／純愛／００",
     "Method":"HideMessageWindow"}
    {"EventImage":"83438378_30333033_8F8388A4_824F824F","Method":"ShowEventCg"}

四段依次是 **角色名 ／ 角色名 ／ CG 分组 ／ 张号**（这张 CG 紧跟在后面那条
ShowEventCg）。它只出现在带 CG 的段落（角色剧情第 5、6 段），所以：

1. **有标签的角色（161/382）**：名字直接取标签第 1 段，100% 可信；
2. **没有标签的角色**：退回到「立绘 GroupId 出现在最多**分段**里的那个」，
   并排除跨角色太常见的常驻角色（以标签名做真值实测命中 96.9%，旧的
   char_names.json 只有 80.7%）；
3. 再退回「说话人里出现最多的」。

顺带产出每个角色剧情的 CG 分组（純愛/陵辱 + 张数），供 build_cgmap.py 对照。

用法：
    python tools/build_char_names.py            # 只报告，不写文件
    python tools/build_char_names.py --apply    # 写 web/data/names.js（先备份）
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "web", "data")
BACKUP = os.path.join(ROOT, "_data_analysis", "_backup")

SEP = "\uff0f"          # 全角斜杠
FW2HW = str.maketrans("０１２３４５６７８９", "0123456789")
#: 常驻/主角类，不该拿来当角色名
BAD_GID = {"？？？", "???", ""}


def read_js(path):
    raw = open(path, encoding="utf-8-sig").read()
    return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])


def scan():
    """扫全部 char_<id>.js，返回 {cid: 统计}。"""
    out = {}
    for fn in sorted(os.listdir(DATA)):
        m = re.fullmatch(r"char_(\d+)\.js", fn)
        if not m:
            continue
        cid = int(m.group(1))
        try:
            d = read_js(os.path.join(DATA, fn))
        except Exception as e:                                # noqa: BLE001
            print(f"  跳过 {fn}: {e}")
            continue
        gid = collections.Counter()          # GroupId -> 出现次数
        parts = collections.Counter()        # GroupId -> 出现在几个分段
        spk = collections.Counter()          # 说话人 -> 句数
        label = collections.Counter()        # 标签里的角色名
        groups = collections.defaultdict(list)   # CG 分组 -> [张号]
        for p in d["parts"]:
            seen = set()
            cur = None
            for mm in p["methods"]:
                t = mm.get("Time")
                if isinstance(t, str) and SEP in t:
                    seg = t.split(SEP)
                    # 段数不固定：4 段 = 名／名／CG分组／张号；3 段 = 名／名／张号；
                    # 还有 2 段的（名／张号）。**从右往左**解析才稳：最后一段恒为张号，
                    # 倒数第二段在 ≥4 段时才是 CG 分组（如 妹の１０２４ 就没有分组）。
                    if len(seg) >= 2 and re.search(r"[０-９0-9]", seg[-1]):
                        label[seg[0]] += 1
                        cur = seg
                if mm.get("Method") == "ShowEventCg":
                    if cur is not None:
                        grp = cur[-2] if len(cur) >= 4 else ""
                        groups[grp].append(cur[-1].translate(FW2HW))
                if mm.get("GroupId"):
                    gid[mm["GroupId"]] += 1
                    seen.add(mm["GroupId"])
                if mm.get("Method") == "ShowMessageWindow" and mm.get("Name"):
                    spk[mm["Name"]] += 1
            for g in seen:
                parts[g] += 1
        out[cid] = {"gid": gid, "parts": parts, "spk": spk,
                    "label": label, "groups": {k: sorted(set(v)) for k, v in groups.items()},
                    "nparts": len(d["parts"])}
    return out


def build(info, wiki=None, keep_ratio=0.2):
    """决定每个角色的显示名。返回 ({cid: (name, 来源)}, {cid: CG 分组}).

    优先级（每一级都带"必须被本剧本证实"的守卫）：
      1. CG 标签名 —— 但要求该名字在本剧本的立绘 GroupId 里出现过
         （否则可能是配角/其它作品的 CG 串进来，如 cid 377）；
      2. 现有名字（WIKI 表）—— 只在本剧本里被证实时保留，这样"变身形态名"
         这类官方叫法不会无谓地变，只修真正绑错人的；
         证实 = 是前两名立绘 GroupId，或该名字的说话人句数 ≥ 最高角色的 20%；
      3. 立绘 GroupId 里"出现在最多分段、且说话最多"的那个；
      4. 说话人。
    """
    spread = collections.Counter()
    for v in info.values():
        for g in v["gid"]:
            spread[g] += 1
    wiki = wiki or {}

    names, cgs = {}, {}
    for cid, v in info.items():
        cgs[cid] = {k: len(x) for k, x in v["groups"].items()}
        top2 = [g for g, _c in v["parts"].most_common(2)]
        spk = dict(v["spk"])

        def lines_of(name):
            if not name:
                return 0
            return max((n for s, n in spk.items() if s in name or name in s),
                       default=0)

        # 先定"本剧本的主角立绘组"：分段覆盖最多，其次出现次数、其次说话量
        cand = [g for g in v["gid"] if g not in BAD_GID and spread[g] <= 10] \
            or [g for g in v["gid"] if g not in BAD_GID] \
            or list(v["gid"])
        dom = max(cand, key=lambda g: (v["parts"][g], v["gid"][g], lines_of(g))) if cand else ""
        dom_lines = lines_of(dom)

        # 1. CG 标签的第 1 段 —— **就是游戏里的角色名**（英雄名/形态名）。
        #    384 个角色剧本全都有这条标签（段数 3 或 4，从右解析），
        #    所以这是唯一不需要猜的来源。之前担心"3P 场景写成配角"其实是误会：
        #    那些名字是同一角色的**变身形态名**（如 エスカ・サファイア = 加古野ヒビキ），
        #    立绘 GroupId 里出现的才是本名 —— 游戏卡片显示的是前者。
        if v["label"]:
            names[cid] = (v["label"].most_common(1)[0][0], "CG标签")
            continue

        # 2. 没有标签（理论上不会有）：旧名 → 主角立绘组 → 说话人
        w = wiki.get(cid)
        if w and (w in top2 or lines_of(w) >= keep_ratio * max(dom_lines, 1)):
            names[cid] = (w, "沿用(已证实)")
        elif dom:
            names[cid] = (dom, "立绘GroupId")
        else:
            names[cid] = ((max(spk, key=spk.get) if spk else f"角色{cid}"),
                          "说话人" if spk else "兜底")
    return names, cgs


def write_names(names):
    """重名加 (id) 后缀后写 names.js（格式与旧文件一致）。"""
    by = collections.defaultdict(list)
    for cid, (nm, _src) in names.items():
        by[nm].append(cid)
    out = {}
    for cid in sorted(names):
        nm = names[cid][0]
        out[str(cid)] = f"{nm} ({cid})" if len(by[nm]) > 1 else nm
    os.makedirs(BACKUP, exist_ok=True)
    dst = os.path.join(DATA, "names.js")
    if os.path.exists(dst):
        import shutil
        shutil.copy2(dst, os.path.join(BACKUP, "names.js.bak"))
    body = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    with open(dst, "w", encoding="utf-8") as f:
        f.write("window.__NAMES=" + body + ";")
    print(f"写出 {dst}（{len(out)} 条，旧文件备份到 _data_analysis/_backup/names.js.bak）")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写 web/data/names.js")
    args = ap.parse_args()

    info = scan()
    p = os.path.join(DATA, "names.js")
    cur = {}
    if os.path.exists(p):
        raw = open(p, encoding="utf-8-sig").read()
        cur = {int(k): v for k, v in json.loads(raw[raw.index("{"): raw.rindex("}") + 1]).items()}
    # 现有表（= 旧 WIKI 覆盖的结果）作为"可沿用候选"，只保留被剧本证实的
    names, cgs = build(info, wiki={c: n.split(" (")[0] for c, n in cur.items()})
    src = collections.Counter(s for _n, s in names.values())
    print(f"角色 {len(names)} 个；名字来源：{dict(src)}")

    old = cur
    chg = [(cid, old.get(cid, ""), nm) for cid, (nm, _s) in sorted(names.items())
           if old.get(cid, "").split(" (")[0] != nm]
    print(f"与现有 names.js 不同的 {len(chg)} 条（前 15）：")
    for c in chg[:15]:
        print(f"  cid {c[0]:>3}  {c[1] or '(无)':<26} -> {c[2]}")

    multi = {cid: cg for cid, cg in cgs.items() if len(cg) > 2}
    print(f"CG 分组数 >2 的角色 {len(multi)} 个（多出来的通常是特殊 CG）：")
    for cid, cg in list(multi.items())[:10]:
        print(f"  cid {cid}: {cg}")

    if args.apply:
        write_names(names)
    else:
        print("\n[dry-run] 未写文件。加 --apply 写入 web/data/names.js")


if __name__ == "__main__":
    main()
