# -*- coding: utf-8 -*-
"""单角色立绘 x2 刷新准备：删除该角色残留的旧 x2 / 产物 / manifest / 覆盖层条目，
并写出仅含该角色 stem 的超分白名单。用法: prep_one_x2.py <编号> [--write]
"""
import json, os, sys, glob

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROFILE = "cugan-se-n2-s2"
num = sys.argv[1]
write = "--write" in sys.argv[2:]

t = open(os.path.join(ROOT, "web", "data", "standmap.js"), encoding="utf-8").read()
sm = json.JSONDecoder().raw_decode(t, t.find("window.__STANDMAP=") + len("window.__STANDMAP="))[0]
stems = sorted({k for g in sm["groups"] if g["n"] == num for k in g["f"]})
print(f"角色 {num} 立绘 stem {len(stems)}")

x2dir = os.path.join(ROOT, "web", "media", "img_x2", "stand")
proddir = os.path.join(ROOT, "_tools", "_sr_out", PROFILE, "img", "stand")
manp = os.path.join(ROOT, "_tools", "_sr_out", PROFILE, "_manifest.json")
ovrp = os.path.join(ROOT, "web", "data", "imgmap.x2.js")

x2 = [s for s in stems if os.path.exists(os.path.join(x2dir, s + ".webp"))]
prod = [s for s in stems if os.path.exists(os.path.join(proddir, s + ".webp"))]
print(f"  现存 img_x2 {len(x2)}，_sr_out 产物 {len(prod)}")

man = json.load(open(manp, encoding="utf-8")) if os.path.exists(manp) else {"done": {}}
done = man.setdefault("done", {})
man_stale = [k for k in done if os.path.splitext(os.path.basename(k))[0] in set(stems)]

ovr = {k: {} for k in ("stand", "back", "cg", "standfull", "avatar")}
if os.path.exists(ovrp):
    tt = open(ovrp, encoding="utf-8-sig").read()
    ovr = json.loads(tt[tt.find("=") + 1:].strip().rstrip(";"))
ovr_stale = [k for k in ovr.get("stand", {}) if k in set(stems)]
print(f"  manifest 条目 {len(man_stale)}，覆盖层条目 {len(ovr_stale)}")

allow = os.path.join(ROOT, "userdata", "_archive", "x2_one.json")

if write:
    for s in x2:
        os.remove(os.path.join(x2dir, s + ".webp"))
    for s in prod:
        os.remove(os.path.join(proddir, s + ".webp"))
    for k in man_stale:
        done.pop(k, None)
    for k in ovr_stale:
        ovr["stand"].pop(k, None)
    json.dump(man, open(manp, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(stems, open(allow, "w", encoding="utf-8"))
    print("  白名单 ->", allow)
    body = json.dumps(ovr, ensure_ascii=False, separators=(",", ":"))
    tmp = ovrp + ".tmp"
    open(tmp, "w", encoding="utf-8", newline="\n").write(f"window.__IMGX2={body};\n")
    os.replace(tmp, ovrp)
    print("  已清理，可跑 sr.py（SR_ALLOWLIST=x2_one.json）")
else:
    print("  （干跑；加 --write 清理）")
