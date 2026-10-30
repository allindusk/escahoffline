"""生成 web/data/charmeta.js（稀有度/实装日期），并把 WIKI 头像转为 {id}.png。

**不写中文名**：charmeta.js 是随包分发的出厂索引，带上 zh 会让全新安装的
CG 页 / 侧栏直接显示中文名（全量包必须纯日文）。中文名只留在
``server/cdn/data/char_meta.json``，由 ``server/names.py`` 取来做导出文件名。
"""
import io
import json
import os
import re

from PIL import Image

SITE = r"D:\D11_DeveloperProject\150_HTML_Project\escalation_heroines\escah\site"
AVATAR_OUT = "../web/media/img/avatar"
os.makedirs(AVATAR_OUT, exist_ok=True)

meta_by_name = json.load(open("char_meta.json", encoding="utf-8"))

# 读 names.js
raw = open("../web/data/names.js", encoding="utf-8").read()
NAMES = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])

out = {}
ok = miss_meta = miss_icon = 0
for cid, nm in NAMES.items():
    base = re.sub(r"\s*\(\d+\)$", "", nm)          # 去掉重名后缀
    m = meta_by_name.get(base)
    if not m:
        miss_meta += 1
        continue
    out[cid] = {"r": m["r"], "d": m["d"]}       # 刻意不带 zh，见文件头说明
    if m.get("icon"):
        src = os.path.join(SITE, "public", m["icon"].replace("/", os.sep))
        dst = os.path.join(AVATAR_OUT, f"{cid}.png")
        if os.path.exists(src):
            try:
                im = Image.open(src).convert("RGBA")
                w, h = im.size
                s = min(w, h)
                im = im.crop(((w - s) // 2, (h - s) // 2,
                              (w + s) // 2, (h + s) // 2))
                im.resize((96, 96), Image.LANCZOS).save(dst, optimize=True)
                ok += 1
            except Exception as e:
                miss_icon += 1
        else:
            miss_icon += 1

js = "window.__CHARMETA=" + json.dumps(out, ensure_ascii=False,
                                       separators=(",", ":")) + ";"
open("../web/data/charmeta.js", "w", encoding="utf-8").write(js)

print(f"元数据 {len(out)} 条，缺 meta {miss_meta}")
print(f"头像转换成功 {ok}，失败/缺失 {miss_icon}")
print(f"charmeta.js {os.path.getsize('../web/data/charmeta.js')/1024:.0f} KB")

from collections import Counter
print("\n稀有度分布（有剧情的 383 个角色）:")
for k, v in Counter(v["r"] for v in out.values()).most_common():
    print(f"  {k:<12}{v:>4}")

print("\n抽样:")
for cid in ("1", "2", "57", "168", "248"):
    if cid in out:
        zh = (meta_by_name.get(re.sub(r"\s*\(\d+\)$", "", NAMES[cid])) or {}).get("zh", "")
        print(f"  {cid:>3}  {NAMES[cid]:<22} {out[cid]['r']:<5} "
              f"{out[cid]['d']:<12} （中文名 {zh or '无'}，仅用于导出文件名）")
