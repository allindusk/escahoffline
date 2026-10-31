# -*- coding: utf-8 -*-
"""从剧本重建 web/data/cgmap.js —— CG 分组按「段 + 剧本标签」，不再靠文件名硬猜。

为什么旧做法会多出一组
----------------------
旧 cgmap 的分组键 = 文件名去掉最后一张号段，于是同一事件的两套命名
（`イベ_0085` 与 `イベ_85`，hex 段一为 303835 一为 3835）被算成两组；
命名里带字的特殊 CG（`イベ_零式出撃`）又多算一组 → "游戏里两组、页面三组"。

剧本里其实写了分组：

    {"Time":"…／…／純愛／００","Method":"HideMessageWindow"}   ← 标签
    {"EventImage":"83438378_30333033_8F8388A4_824F824F",      ← 紧跟的 CG
     "Method":"ShowEventCg"}

所以正确口径是：**CG 归属于它播放时所在的分段 + 标签**：

* 有标签（新角色）→ 组名直接用标签第 3 段（純愛 / 陵辱 …）；
* 无标签（老角色）→ 组名用「イベ_<角色id>」，同一角色有多个时补上段号。

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


def char_no_of(key):
    """CG 资源名里的角色编号（第 2 段，去前导零）。"""
    seg = [dec(s) for s in key.split("_")]
    if len(seg) >= 2 and seg[1].isdigit():
        return str(int(seg[1]))
    return ""


def collect(cid):
    """返回 [(段号, 组名, cg key)]，按分段与出现顺序。

    **文件清单以 parts[].cg（剧本的 EventImages）为准**，因为它列的是这一段
    真正会用到的全部 CG；ShowEventCg 指令只用来判断"这张属于哪个标签"。
    只用 ShowEventCg 会漏图（有的 CG 是被别的方法调起来的，实测 cid 1 会少 1 张）。
    """
    path = os.path.join(DATA, f"char_{cid}.js")
    if not os.path.exists(path):
        return []
    d = read_js(path)
    out = []
    for p in d["parts"]:
        try:
            pno = int(str(p.get("file", "")).rsplit("_", 1)[1])
        except Exception:                                  # noqa: BLE001
            pno = 0
        # ① ShowEventCg 里"标签 → CG"的对应
        # 标签段数不固定：4 段 = 名／名／CG分组／张号，3 段 = 名／名／张号（无分组）。
        # 从右往左解析：最后一段永远是张号，倒数第二段在 ≥4 段时才是 CG 分组。
        key_tag, cur = {}, None
        for m in p["methods"]:
            t = m.get("Time")
            if isinstance(t, str) and SEP in t:
                seg = t.split(SEP)
                if len(seg) >= 2 and re.search(r"[０-９0-9]", seg[-1]):
                    cur = seg
            if m.get("Method") == "ShowEventCg" and m.get("EventImage"):
                tag = (cur[-2] if cur and len(cur) >= 4 else "")
                key_tag.setdefault(m["EventImage"], tag)
        # ② 这一段引用到的全部 CG（顺序 = 剧本给的书目顺序）
        keys = list(p.get("cg") or []) + [k for k in key_tag if k not in (p.get("cg") or [])]
        for key in keys:
            seg = [dec(s) for s in key.split("_")]
            if any(x in PLACEHOLDER for x in seg):
                continue
            tag = key_tag.get(key) or label_of(key)
            out.append((pno, tag, key))
    return out


def build(ids):
    groups = collections.OrderedDict()      # (cid, 组名) -> [key]
    order = []
    for cid in ids:
        rows = collect(cid)
        if not rows:
            continue
        by = collections.OrderedDict()
        for pno, tag, key in rows:
            by.setdefault((pno, tag), [])
            if key not in by[(pno, tag)]:
                by[(pno, tag)].append(key)
        for (pno, tag), keys in by.items():
            gk = (cid, tag, pno) if tag else (cid, "", pno)
            groups[gk] = keys
            order.append(gk)
    # 组名：有标签用标签；无标签用 イベ_<id>，同角色多组时补段号
    per_char = collections.Counter(k[0] for k in order)
    out = []
    for gk in order:
        cid, tag, pno = gk
        keys = groups[gk]
        if tag:
            name = f"イベ_{cid}_{tag}"
        elif per_char[cid] > 1:
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

    for cid in (85, 118, 303, 174, 362, 377):
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
