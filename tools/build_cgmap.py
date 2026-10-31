# -*- coding: utf-8 -*-
"""从剧本重建 web/data/cgmap.js —— CG 分组按「CG 归属 + 剧本标签」，不再靠文件名硬猜。

剧本里其实写了分组：

    {"Time":"…／…／純愛／００","Method":"HideMessageWindow"}   ← 标签
    {"EventImage":"83438378_30333033_8F8388A4_824F824F",      ← 紧跟的 CG
     "Method":"ShowEventCg"}

``Time`` 的格式是 ``名前／名前／[CG分组]／张号``（张号那段的方框数字是必需的），
倒数第二段才是事件名：4 段时是 CG 分组（純愛 / 陵辱），3 段时是**这一批 CG 的
角色形态**（神騎プラチネル／白金エレン 这种英雄名与素颜名）。两种都是组名。

三条判定（2026-11-01 改，之前的三处错都在这里）：

1. **CG 归属**：`イベ_<号>_…` 里的号就是 CG 所属角色。角色页只收自己名下的图，
   别人的图（同一部剧情里的联动 CG）和**不带号的美术字图**（`イベ_狂王`、
   `イベ_超昂神騎`、`イベ_零式出撃` 这类 2048×1152 宣传画）不算它的 CG。
   这些图在「主线/活动/其他」桶里照样能看，不会丢。
2. **组 = 同一批 CG**：`(标签, 号段原文)` 相同就是同一批。以前按「段号」切分，
   于是同一批 CG 被剧情在第 6 段重播一次就变成第二组（299 的 純愛 6+1、
   396 的 6+8）。
3. **组名**：有标签用标签；没有标签（老角色）才用「イベ_<id>_第N段」。

实测结果：384 个角色**每个都是 2 组**（純愛/陵辱，或英雄名/素颜名），
这正是游戏画廊的结构；改之前有 2 个角色 4 组、6 个角色 3 组。

占位转场图（暗転 / 黒画面 / 黒）不进画廊。

用法：
    python tools/build_cgmap.py            # 只报告
    python tools/build_cgmap.py --apply    # 写 web/data/cgmap.js（先备份）
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "web", "data")
BACKUP = os.path.join(ROOT, "_data_analysis", "_backup")

SEP = "\uff0f"
FW2HW = str.maketrans("０１２３４５６７８９", "0123456789")
PLACEHOLDER = {"暗転", "黒画面", "黒", "暗転（黒）"}


def read_js(path):
    raw = open(path, encoding="utf-8-sig").read()
    i = raw.index("=")
    return json.loads(raw[i + 1:].strip().rstrip(";"))


def dec(h):
    try:
        return bytes.fromhex(h).decode("cp932")
    except Exception:                                      # noqa: BLE001
        return h


def label_of(key):
    """CG 资源名里的分组标签（第 3 段），没有就是空串。

    `83438378_30333033_8F8388A4_824F824F` -> 純愛
    `83438378_303835_824F824F`            -> ''（只有类型/角色/张号）
    """
    seg = [dec(s) for s in key.split("_")]
    if len(seg) >= 4:
        return seg[2]
    return ""


def owner_of(key):
    """CG 归属的角色号（第 2 段，去前导零）；第 2 段不是数字就返回 ''。

    `83438378_30333936_824F824F` -> '396'（带前导零的同一批）
    `83438378_8BB689A4`          -> ''（美术字宣传图，不属于任何角色）
    """
    seg = [dec(s) for s in key.split("_")]
    if len(seg) >= 2 and seg[1].isdigit():
        return str(int(seg[1]))
    return ""


def num_seg_of(key):
    """CG 资源名里第 2 段的**原文**（前导零也算区别，0396 与 396 是两批不同的图）。"""
    seg = [dec(s) for s in key.split("_")]
    return seg[1] if len(seg) >= 2 else ""


def collect(cid):
    """返回 [(段号, 组名, cg key)]，按分段与出现顺序。只收**这个角色名下**的图。

    **文件清单以 parts[].cg（剧本的 EventImages）为准**，因为它列的是这一段
    真正会用到的全部 CG；ShowEventCg 指令只用来判断"这张属于哪个标签"。
    只用 ShowEventCg 会漏图（有的 CG 是被别的方法调起来的，实测 cid 1 会少 1 张）。
    """
    path = os.path.join(DATA, f"char_{cid}.js")
    if not os.path.exists(path):
        return []
    d = read_js(path)
    own, other = [], []
    for p in d["parts"]:
        try:
            pno = int(str(p.get("file", "")).rsplit("_", 1)[1])
        except Exception:                                  # noqa: BLE001
            pno = 0
        # ① ShowEventCg 里"标签 → CG"的对应
        # 标签段数不固定：4 段 = 名／名／CG分组／张号，3 段 = 名／名／张号（无分组）。
        # 从右往左解析：最后一段永远是张号，倒数第二段是事件名（3 段也有）。
        key_tag, cur = {}, None
        for m in p["methods"]:
            t = m.get("Time")
            if isinstance(t, str) and SEP in t:
                seg = t.split(SEP)
                if len(seg) >= 2 and re.search(r"[０-９0-9]", seg[-1]):
                    cur = seg
            if m.get("Method") == "ShowEventCg" and m.get("EventImage"):
                tag = (cur[-2] if cur and len(cur) >= 3 else "")
                key_tag.setdefault(m["EventImage"], tag)
        # 同一段里同号 CG 的标签唯一时，用它补上"没被 ShowEventCg 点过"的那几张
        # （否则一批 6 张会被拆成 5+1）
        bynum = collections.defaultdict(set)
        for k, t in key_tag.items():
            if t:
                bynum[num_seg_of(k)].add(t)
        # ② 这一段引用到的全部 CG（顺序 = 剧本给的书目顺序）
        keys = list(p.get("cg") or []) + [k for k in key_tag if k not in (p.get("cg") or [])]
        for key in keys:
            seg = [dec(s) for s in key.split("_")]
            if any(x in PLACEHOLDER for x in seg):
                continue
            tag = key_tag.get(key) or label_of(key)
            if not tag:
                cand = bynum.get(num_seg_of(key)) or set()
                if len(cand) == 1:
                    tag = next(iter(cand))
            row = (pno, tag, key)
            if owner_of(key) == str(cid):
                own.append(row)
            else:
                other.append(row)
    # 兜底：整个角色的图都不带头号（老数据）时退回不筛，别把角色页清空
    return own or other


def build(ids):
    out = []
    for cid in ids:
        rows = collect(cid)
        if not rows:
            continue
        # 组 = (标签, 号段原文)；无标签的才退回按段号切
        by = collections.OrderedDict()
        for pno, tag, key in rows:
            gk = (tag, num_seg_of(key)) if tag else (None, pno)
            slot = by.setdefault(gk, {"tag": tag, "pno": pno, "keys": []})
            if key not in slot["keys"]:
                slot["keys"].append(key)
            slot["pno"] = min(slot["pno"], pno)
        n = len(by)
        for slot in by.values():
            tag, pno, keys = slot["tag"], slot["pno"], slot["keys"]
            if tag:
                name = f"イベ_{cid}_{tag}"
            elif n > 1:
                name = f"イベ_{cid}_第{pno + 1}段"
            else:
                name = f"イベ_{cid}"
            out.append({"c": cid, "k": name, "f": [k + ".jpg" for k in keys],
                        "n": len(keys), "p": pno, "g": tag})
    out.sort(key=lambda x: (x["c"], x["p"], x["k"]))
    return out


# ============================================================ 主线/活动/其他 CG

#: 把非角色剧情塞进同一个 cgmap.js，用**负数假 id** 当分类桶：
#: -1 主线 / -2 活动 / -3 其他（含 tutorial / op）。
#: 这样 CG 页不用改数据格式，只要认识这三个 id 就能按类别翻（cg.js 里有 CATS）。
CAT_ID = {"main": -1, "event": -2, "other": -3, "tutorial": -3, "op": -3}
CAT_NAME = {-1: "主线剧情 CG", -2: "活动剧情 CG", -3: "其他剧情 CG"}


def _label_scene(seg):
    """非角色剧情的标签第一段是**场景名**（病床のハルナ／００），不是人名。"""
    return seg[0] if seg else ""


def collect_adv(key):
    """返回 [(段号, 场景名, cg key)]。"""
    path = os.path.join(DATA, key + ".js")
    if not os.path.exists(path):
        return []
    d = read_js(path)
    out = []
    for p in d["parts"]:
        try:
            pno = int(str(p.get("file", "")).rsplit("_", 1)[1])
        except Exception:                                  # noqa: BLE001
            pno = 0
        key_scene, cur = {}, None
        for m in p["methods"]:
            t = m.get("Time")
            if isinstance(t, str) and SEP in t:
                seg = t.split(SEP)
                if len(seg) >= 2 and re.search(r"[０-９0-9]", seg[-1]):
                    cur = seg
            if m.get("Method") == "ShowEventCg" and m.get("EventImage"):
                key_scene.setdefault(m["EventImage"], _label_scene(cur) if cur else "")
        keys = list(p.get("cg") or []) + [k for k in key_scene
                                          if k not in (p.get("cg") or [])]
        for k in keys:
            seg = [dec(s) for s in k.split("_")]
            if any(x in PLACEHOLDER for x in seg):
                continue
            out.append((pno, key_scene.get(k, ""), k))
    return out


def build_adv(keys):
    """非角色剧情的 CG 组（c = 分类桶负数）。"""
    out = []
    for key in keys:
        kind = key.split("_")[1]
        cat = CAT_ID.get(kind, -3)
        by = collections.OrderedDict()
        for pno, scene, cg in collect_adv(key):
            gk = (pno, scene)
            by.setdefault(gk, [])
            if cg not in by[gk]:
                by[gk].append(cg)
        for (pno, scene), files in by.items():
            name = f"第{pno + 1}段" + (f" · {scene}" if scene else "")
            out.append({"c": cat, "k": name, "f": [x + ".jpg" for x in files],
                        "n": len(files), "p": pno, "g": scene,
                        "u": key, "t": kind})
    out.sort(key=lambda x: (x["c"], x["u"], x["p"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    ids = sorted(int(m.group(1)) for f in os.listdir(DATA)
                 if (m := re.fullmatch(r"char_(\d+)\.js", f)))
    chars_g = build(ids)
    adv_keys = sorted(f[:-3] for f in os.listdir(DATA)
                      if re.fullmatch(r"adv_\w+\.js", f))
    adv_g = build_adv(adv_keys)
    new = chars_g + adv_g
    pics = sum(x["n"] for x in new)
    print(f"角色 {len(ids)} 个 → CG 组 {len(chars_g)} / 张 {sum(x['n'] for x in chars_g)}")
    print(f"主线/活动/其他 {len(adv_keys)} 部 → CG 组 {len(adv_g)} / "
          f"张 {sum(x['n'] for x in adv_g)}")
    print(f"合计 {len(new)} 组 / {pics} 张")

    dist = collections.Counter()
    per = collections.Counter(x["c"] for x in chars_g)
    for c, n in per.items():
        dist[n] += 1
    print("每角色组数分布：", dict(sorted(dist.items())))
    cat = collections.Counter()
    for x in adv_g:
        cat[CAT_NAME[x["c"]]] += 1
    print("剧情 CG 分类：", dict(cat))

    old = []
    p = os.path.join(DATA, "cgmap.js")
    if os.path.exists(p):
        old = read_js(p)
        print(f"旧表 {len(old)} 组（旧分组键按文件名，同一事件会被拆开）")
    oldset = {(g["c"], g["k"]) for g in old}
    newset = {(g["c"], g["k"]) for g in new}
    print(f"新增 {len(newset - oldset)} 组，消失 {len(oldset - newset)} 组（旧表）")

    # 每个角色的组数应当恒为 2（純愛/陵辱，或英雄名/素颜名）；不是 2 就该查剧本
    bad = [(c, n) for c, n in sorted(per.items()) if n != 2]
    print("组数不是 2 的角色：", bad or "无")

    for cid in (396, 381, 382, 377, 283, 299):
        print(f"  cid {cid}: " + " · ".join(
            f"{x['k']}({x['n']})" for x in new if x["c"] == cid)[:160])

    if args.apply:
        os.makedirs(BACKUP, exist_ok=True)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(BACKUP, "cgmap.js.bak"))
        with open(p, "w", encoding="utf-8") as f:
            f.write("window.__CGMAP=" + json.dumps(new, ensure_ascii=False,
                                                   separators=(",", ":")) + ";")
        print(f"写出 {p}（旧文件备份到 _data_analysis/_backup/cgmap.js.bak）")
    else:
        print("\n[dry-run] 未写文件。加 --apply 写入 web/data/cgmap.js")


if __name__ == "__main__":
    main()
