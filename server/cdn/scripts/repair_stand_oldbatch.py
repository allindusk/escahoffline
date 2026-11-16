# -*- coding: utf-8 -*-
"""一次性修复：重写早期旧批次（2026-10-28 之前）合成的立绘。

背景
----
早期原型批次（mtime < 2026-10-28）产出的部分立绘表情差分落点/裁边回补有误，
出现头身上移、颈胸黑缝、十字接缝等错位。当前 ``compose_stand_all.py`` 的落点
逻辑（官方 base_info 优先 → 邻域精修 → 裁边回补）已验证正确，但幂等检查
（落点没变 + 文件存在 + 尺寸对得上）会把旧产物一直保留下来，不会自动重写。

本脚本：
  1. 扫 ``standmap.js``，按文件 mtime 找出仍属旧批次的分组；
  2. 以 ``old_slot=None`` 强制走当前合成逻辑重写这些分组的全部表情；
  3. 合并落点记录到 ``web/data/stand_slot.json``，写 QC 报告与 x2 陈旧清单。

用法（cwd = userdata/）：
  python repair_stand_oldbatch.py            # 干跑，只统计
  python repair_stand_oldbatch.py --write    # 实际重写
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import glob
import time

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose_stand_all as C  # noqa: E402

CUTOFF = datetime.datetime(2026, 10, 28)


def load_standmap():
    t = open(C.WEBD + os.sep + "standmap.js", encoding="utf-8").read()
    return json.JSONDecoder().raw_decode(
        t, t.find("window.__STANDMAP=") + len("window.__STANDMAP="))[0]


def old_groups(sm):
    """返回 {(num, cloth): [表情HEX]}，只要组内有一张旧批次文件就算。"""
    old = {}
    for g in sm["groups"]:
        num, cloth, keys = g["n"], g["c"], g["f"]
        for k in keys:
            p = os.path.join(C.WEBS, k + ".webp")
            if os.path.exists(p) and \
                    datetime.datetime.fromtimestamp(os.path.getmtime(p)) < CUTOFF:
                codes = [k.split("_")[-1] for k in keys]
                old.setdefault((num, cloth), codes)
                break
    return old


def main():
    write = "--write" in sys.argv
    sm = load_standmap()
    old = old_groups(sm)
    tasks = [(num, cloth, sorted(keys), None)
             for (num, cloth), keys in sorted(old.items())]
    total_faces = sum(len(t[2]) for t in tasks)
    print(f"旧批次分组 {len(tasks)} 个，需重写表情 {total_faces} 张"
          f"（{'写入' if write else '干跑'}）", flush=True)
    if not write:
        return 0

    slots, trims, qc, warns, srcs = {}, {}, [], [], {}
    done = 0
    t0 = time.time()
    # 串行执行：Windows spawn 子进程会重跑入口脚本，且沙箱子解释器未必带
    # numpy/UnityPy；这里组级负载不重，串行稳定优先（避免 BrokenProcessPool）。
    for task in tasks:
        num, cloth, slot, out, msg, src, q, trim = C.process_group(task)
        done += 1
        if src:
            srcs[src.split("(")[0]] = srcs.get(src.split("(")[0], 0) + 1
        if slot is None:
            if msg:
                warns.append(f"{num}/{cloth}: {msg}")
        else:
            slots[f"{num}_{cloth}"] = slot
            trims[f"{num}_{cloth}"] = trim
            if q is not None and q > C.QWARN:
                qc.append((float(q), f"{num}_{cloth}", slot))
        if msg:
            warns.append(f"{num}/{cloth}: {msg}")
        if done % 50 == 0 or done == len(tasks):
            el = time.time() - t0
            print(f"  [{done}/{len(tasks)}] {el:.0f}s "
                  f"预计剩余 {el / done * (len(tasks) - done):.0f}s",
                  flush=True)

    # 合并落点记录（保留其它分组已有记录）
    slot_path = os.path.join(C.WEBD, "stand_slot.json")
    old_slots = {}
    if os.path.exists(slot_path):
        try:
            old_slots = json.load(open(slot_path, encoding="utf-8"))
        except Exception:
            old_slots = {}
    old_slots.update(slots)
    with open(slot_path, "w", encoding="utf-8") as f:
        json.dump(old_slots, f, ensure_ascii=False, separators=(",", ":"))

    qc.sort(reverse=True)
    rep = os.path.join(C.BASE, "_archive")
    os.makedirs(rep, exist_ok=True)
    with open(os.path.join(rep, "stand_slot_qc.json"), "w", encoding="utf-8") as f:
        json.dump([{"diff": round(d, 2), "key": k, "slot": s}
                   for d, k, s in qc], f, ensure_ascii=False, indent=1)

    # x2 陈旧清单：本次重写过、且磁盘上存在 x2 产物的分组
    x2dir = os.path.join(os.path.dirname(C.WEBD), "media", "img_x2", "stand")
    stale = []
    if os.path.isdir(x2dir):
        for key, sl in slots.items():
            num, cloth = key.split("_", 1)
            if glob.glob(os.path.join(x2dir, f"stand_{num}_{cloth}_*.webp")):
                stale.append({"key": key, "slot": sl})
    with open(os.path.join(rep, "x2_stale.json"), "w", encoding="utf-8") as f:
        json.dump(stale, f, ensure_ascii=False, indent=1)

    print(f"\n完成，用时 {time.time() - t0:.0f}s，重写分组 {len(slots)}，"
          f"特殊类型 {len(warns)} 组")
    print("落点来源:", srcs)
    if qc:
        print(f"QC 可疑 {len(qc)} 组（diff > {C.QWARN}）：")
        for d, k, s in qc[:20]:
            print(f"   {k} slot={s} diff={d:.1f}")
    if warns:
        print("警告：")
        for w in warns[:20]:
            print("  ", w)
    print(f"x2 需重做 {len(stale)} 组 -> _archive/x2_stale.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
