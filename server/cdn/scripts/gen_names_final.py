"""用 WIKI 名生成 web/data/names.js（重名加 ID 后缀）。

新实装的角色 WIKI 表里还没有（char_names_wiki.json 是人工同步的快照），
过去只读 WIKI 表会让新角色在 names.js 里彻底消失（左侧列表显示"角色 415"）。
所以改为**两层来源**：本地剧本抽出的 char_names.json 打底，WIKI 名覆盖其上。
"""
import json
from collections import Counter, defaultdict

src = json.load(open("char_names_wiki.json", encoding="utf-8"))
local = json.load(open("char_names.json", encoding="utf-8"))
result = {int(k): v["name"] for k, v in local.items()}
wiki_only = 0
for k, v in src.items():
    if result.get(int(k)) != v["name"]:
        wiki_only += 1
    result[int(k)] = v["name"]          # WIKI 优先（人工校对过的）

by = defaultdict(list)
for cid, nm in result.items():
    by[nm].append(cid)

out = {}
dup_n = 0
for cid, nm in result.items():
    if len(by[nm]) > 1:
        out[str(cid)] = f"{nm} ({cid})"
        dup_n += 1
    else:
        out[str(cid)] = nm

js = "window.__NAMES=" + json.dumps(out, ensure_ascii=False,
                                    separators=(",", ":")) + ";"
open("../web/data/names.js", "w", encoding="utf-8").write(js)

import os
print(f"../web/data/names.js {os.path.getsize('../web/data/names.js')/1024:.0f} KB")
print(f"角色 {len(out)}，重名加后缀 {dup_n}，唯一名 {len(by)}")
print("\n=== 抽样 20 个 ===")
for cid in range(1, 21):
    print(f"  {cid:>3}  {out[str(cid)]}")
print("\n=== 关键角色 ===")
for cid in (57, 168, 248, 2, 85):
    print(f"  {cid:>3}  {out[str(cid)]}")
