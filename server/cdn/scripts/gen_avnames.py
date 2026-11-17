"""从剧情脚本的 ShowStand / 话者名自动生成话者→立绘编号表。

输出 ``web/data/avnames.js``:
  * ``gid``：ShowStand 的完整 GroupId → 立绘编号（例如 鷹刃春霞 → 99）
  * ``spk``：短话者名 → 立绘编号；只在所有 GroupId 中唯一命中时才写入，
             避免「ライカ」这种多形态短名被强行套成某一个形态。

用法（cwd = userdata/）：
    python gen_avnames.py
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

WEB = os.path.join("..", "web")
DATA = os.path.join(WEB, "data")
OUT_JS = os.path.join(DATA, "avnames.js")
RE_STAND = re.compile(r"stand_(\d+)_")


def norm(s: str) -> str:
    s = str(s or "").lower()
    return re.sub(r"[\s\u3000・·･\-—–_,，.。、!！?？:：;；/／「」『』（）()［］\[\]【】<>《》]", "", s)


def load_story(path: str):
    with open(path, encoding="utf-8-sig") as f:
        raw = f.read()
    pos = raw.find("=")
    if pos < 0:
        return None
    try:
        return json.loads(raw[pos + 1:].strip().rstrip(";"))
    except Exception:
        return None


def main() -> int:
    gid_ids = defaultdict(Counter)
    speakers = set()
    files = sorted(glob.glob(os.path.join(DATA, "adv_*.js")))
    for path in files:
        d = load_story(path)
        if not d:
            continue
        for part in d.get("parts", []):
            for m in part.get("methods", []):
                method = m.get("Method")
                if method == "ShowStand":
                    gid = str(m.get("GroupId") or "").strip()
                    stand = str(m.get("StandImage") or "")
                    hit = RE_STAND.match(stand)
                    if gid and hit:
                        gid_ids[gid][hit.group(1)] += 1
                elif method == "ShowMessageWindow":
                    nm = str(m.get("Name") or "").strip()
                    if nm:
                        speakers.add(nm)

    # 同一个 GroupId 有多套立绘时取出现次数最多的编号；保留原始前导 0。
    gid = {}
    gid_num = {}
    for k, ids in gid_ids.items():
        # 若同名组对应多个编号，优先保留同一角色最常用的那个；
        # 不把多个编号合并，避免把不同形态混在一个头像 id 里。
        num, _ = ids.most_common(1)[0]
        gid[k] = num
        gid_num.setdefault(norm(k), set()).add(num)

    spk = {}
    ambiguous = []
    for nm in sorted(speakers):
        n = norm(nm)
        if not n:
            continue
        idset = set()
        for group, ids in gid_num.items():
            # 任何包含候选都算歧义：例如「ライカ」同时命中閃忍/聖夜/真夏，
            # 不能因为其中一个名字更短就强行选它；宁可不显示。
            if group == n or n in group:
                idset.update(ids)
        if len(idset) == 1:
            spk[nm] = next(iter(idset))
        elif len(idset) > 1:
            ambiguous.append(nm)

    os.makedirs(os.path.dirname(OUT_JS), exist_ok=True)
    data = {
        "v": 1,
        "gid": gid,
        "spk": spk,
    }
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tmp = OUT_JS + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("window.__AVNAMES=" + body + ";")
    os.replace(tmp, OUT_JS)
    print(f"剧情 {len(files)} 个 / GroupId {len(gid)} / 话者 {len(speakers)}"
          f" → 唯一短名 {len(spk)} / 歧义跳过 {len(ambiguous)} → {os.path.normpath(OUT_JS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
