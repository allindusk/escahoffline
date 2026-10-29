"""生成角色名索引 data/names.js，重名角色加 ID 后缀区分。"""
import json
from collections import defaultdict

raw = json.load(open("char_names.json", encoding="utf-8"))
by = defaultdict(list)
for cid, v in raw.items():
    by[v["name"]].append(int(cid))

out = {}
dup_n = 0
for cid, v in raw.items():
    cid = int(cid)
    nm = v["name"] or f"角色 {cid}"
    if len(by[nm]) > 1:
        # 同一角色多段剧情：短后缀 ID 便于区分
        out[str(cid)] = f"{nm} ({cid})"
        dup_n += 1
    else:
        out[str(cid)] = nm

js = "window.__NAMES=" + json.dumps(out, ensure_ascii=False,
                                    separators=(",", ":")) + ";"
open("../web/data/names.js", "w", encoding="utf-8").write(js)

import os
print(f"已生成 web/data/names.js  {os.path.getsize('../web/data/names.js')/1024:.0f} KB")
print(f"  角色 {len(out)} 个，其中重名加后缀 {dup_n} 个")
print(f"  唯一名字 {len(by)} 个")
print("\n抽样:")
for cid in list(out)[:6] + ["57", "168", "248"]:
    if cid in out:
        print(f"  {cid:>3}  {out[cid]}")
