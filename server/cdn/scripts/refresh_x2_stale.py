# -*- coding: utf-8 -*-
"""清理陈旧 x2 超分产物，为按新原图重做 Real-CUGAN 超分做准备。

背景：旧批次错位立绘重写后，``web/media/img_x2/stand`` 与
``_tools/_sr_out/<profile>/img/stand`` 里的超分图仍是从错位原图放大的；
apply_x2 遇到已存在的目标会跳过，不删旧产物就不会重新链接新图。

本脚本：
  1. 读 ``userdata/_archive/x2_stale.json``（重写过且存在 x2 的分组）；
  2. 删除对应 img_x2 与 _sr_out 产物 webp；
  3. 从 sr manifest 的 done 中摘掉对应条目（否则幂等检查会再次跳过）；
  4. 从 ``data/imgmap.x2.js`` 覆盖层摘掉对应 stand 键，避免页面先指向 404 的 x2。

用法（cwd = userdata/）：
  python refresh_x2_stale.py            # 干跑，只统计
  python refresh_x2_stale.py --write    # 实际清理
"""
from __future__ import annotations

import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROFILE = "cugan-se-n2-s2"
X2_STAND = os.path.join(ROOT, "web", "media", "img_x2", "stand")
PROD_STAND = os.path.join(ROOT, "_tools", "_sr_out", PROFILE, "img", "stand")
MANIFEST = os.path.join(ROOT, "_tools", "_sr_out", PROFILE, "_manifest.json")
OVERLAY = os.path.join(ROOT, "web", "data", "imgmap.x2.js")
STALE_JSON = os.path.join(ROOT, "userdata", "_archive", "x2_stale.json")


def group_key(stem: str) -> str:
    parts = stem.split("_")
    return "_".join(parts[1:-1])


def main():
    write = "--write" in sys.argv
    stale = json.load(open(STALE_JSON, encoding="utf-8"))
    groups = {s["key"] for s in stale}

    def stale_stems_in(d: str) -> set[str]:
        out = set()
        if not os.path.isdir(d):
            return out
        for p in glob.glob(os.path.join(d, "*.webp")):
            stem = os.path.splitext(os.path.basename(p))[0]
            if group_key(stem) in groups:
                out.add(stem)
        return out

    x2 = stale_stems_in(X2_STAND)
    prod = stale_stems_in(PROD_STAND)
    print(f"陈旧分组 {len(groups)}；img_x2 {len(x2)}；_sr_out {len(prod)}")

    man = {"done": {}}
    if os.path.exists(MANIFEST):
        man = json.load(open(MANIFEST, encoding="utf-8"))
    done = man.setdefault("done", {})
    stale_man = [k for k in done if group_key(os.path.splitext(os.path.basename(k))[0]) in groups]
    print(f"manifest 陈旧条目 {len(stale_man)}")

    ovr = {k: {} for k in ("stand", "back", "cg", "standfull", "avatar")}
    if os.path.exists(OVERLAY):
        t = open(OVERLAY, encoding="utf-8-sig").read()
        body = t[t.find("=") + 1:].strip().rstrip(";")
        ovr = json.loads(body)
    ovr_stale = [k for k in ovr.get("stand", {}) if group_key(k) in groups]
    print(f"覆盖层陈旧 stand 键 {len(ovr_stale)}")

    if not write:
        print("（干跑，未删除；加 --write 执行）")
        return 0

    n_del = 0
    for stem in x2:
        p = os.path.join(X2_STAND, stem + ".webp")
        if os.path.exists(p):
            os.remove(p)
            n_del += 1
    for stem in prod:
        p = os.path.join(PROD_STAND, stem + ".webp")
        if os.path.exists(p):
            os.remove(p)
            n_del += 1
    for k in stale_man:
        done.pop(k, None)
    for k in ovr_stale:
        ovr["stand"].pop(k, None)

    json.dump(man, open(MANIFEST, "w", encoding="utf-8"), ensure_ascii=False)
    body = json.dumps(ovr, ensure_ascii=False, separators=(",", ":"))
    tmp = OVERLAY + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"window.__IMGX2={body};\n")
    os.replace(tmp, OVERLAY)
    print(f"已删除产物 {n_del} 个，清 manifest {len(stale_man)}，"
          f"清覆盖层 {len(ovr_stale)}；可重跑 sr.py --only stand --only-referenced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
