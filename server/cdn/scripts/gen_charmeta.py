"""生成 web/data/charmeta.js（稀有度 / 类型 / 实装日）。

数据源：公开 Wiki（escalationheroines.wikiru.jp）抓来的 char_meta.json，字段
``r``(稀有度 SSR/SR/R) / ``d``(实装日，官方页缺，多数为空) / ``t``(所属/类型：
戦士/閃忍/神騎/魔女…)。由 tools/scrape_wiki.py 在构建时写入 server/cdn/data/char_meta.json，
或由运行时 sync_wiki 合并进 userdata/char_meta.json。

路径自适应：运行时 cwd=userdata（已种一份 char_meta.json，可能被 wiki 合并过）→
优先读它；构建时 cwd=仓库根、没有 char_meta.json → 回退读 server/cdn/data 出厂表。
names.js / charmeta.js 同理优先用 cwd，否则用仓库 web/data。

**不写中文名**：charmeta.js 随包分发，带上 zh 会让全新安装直接显示中文名
（全量包必须纯日文）。中文名只在 char_meta.json，供 server/names.py 做导出文件名
（用户可自行在 userdata 副本里补）。
"""
import io
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
CDN_DATA = os.path.join(ROOT, "server", "cdn", "data")
WEB_DATA = os.path.join(ROOT, "web", "data")

_cwd = os.getcwd()
if os.path.exists(os.path.join(_cwd, "names.js")):        # 运行时：cwd 即 userdata
    WEB_DATA = _cwd
_cwd_meta = os.path.join(_cwd, "char_meta.json")
META_PATH = _cwd_meta if os.path.exists(_cwd_meta) else os.path.join(CDN_DATA, "char_meta.json")
CHARMAP_OUT = os.path.join(WEB_DATA, "charmeta.js")

meta_by_name = json.load(io.open(META_PATH, encoding="utf-8"))
raw = io.open(os.path.join(WEB_DATA, "names.js"), encoding="utf-8").read()
NAMES = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])

out = {}
miss_meta = 0
for cid, nm in NAMES.items():
    base = re.sub(r"\s*\(\d+\)$", "", nm)          # 去掉重名后缀
    m = meta_by_name.get(base)
    if not m:
        miss_meta += 1
        continue
    # 只放行合法稀有度。「サポーター」是职业不是稀有度，旧表里可能残留这种值
    # （曾在 charmeta 里出现 39 个 r="サポーター"），不能让它随包分发出去。
    _r = m.get("r", "")
    if _r not in ("SSR", "SR", "R", "NPC"):
        _r = ""
    out[cid] = {"r": _r, "d": m.get("d", ""), "t": m.get("t", "")}

js = "window.__CHARMETA=" + json.dumps(out, ensure_ascii=False,
                                       separators=(",", ":")) + ";"
io.open(CHARMAP_OUT, "w", encoding="utf-8").write(js)

print(f"元数据 {len(out)} 条，缺 meta {miss_meta}")
print(f"charmeta.js {os.path.getsize(CHARMAP_OUT) / 1024:.0f} KB")
from collections import Counter
print("稀有度分布:", dict(Counter(v["r"] for v in out.values())))
print("类型(所属)分布:", dict(Counter(v["t"] for v in out.values()).most_common(10)))
