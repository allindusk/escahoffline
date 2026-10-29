"""从剧本中提取角色名：优先用立绘 GroupId，用说话人 Name 兜底。

判定逻辑：
- 主角 / 常驻角色（跨大量角色剧情出现）排除
- 取「在本角色 6 段中出现段落数最多」的候选
"""
import glob
import json
import os
import re
from collections import Counter, defaultdict

FILES = sorted(glob.glob("../web/data/char_*.js"))
print(f"扫描 {len(FILES)} 个角色剧本")

per_char_gid = {}      # cid -> Counter(GroupId)
per_char_name = {}     # cid -> Counter(Name)
gid_chars = defaultdict(set)   # GroupId -> 出现过的角色集合
name_chars = defaultdict(set)

first_gid = {}        # cid -> 首个出现的 GroupId（登场形态）

for f in FILES:
    cid = int(re.findall(r"\d+", os.path.basename(f))[0])
    raw = open(f, encoding="utf-8").read()
    d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    gids, names = Counter(), Counter()
    seq = []
    for p in d["parts"]:
        for m in p["methods"]:
            M = m.get("Method")
            if M in ("ShowStand", "ShowStandAndSit") and m.get("GroupId"):
                gids[m["GroupId"]] += 1
                seq.append(m["GroupId"])
            elif M == "ShowMessageWindow" and m.get("Name"):
                names[m["Name"]] += 1
    per_char_gid[cid] = gids
    per_char_name[cid] = names
    first_gid[cid] = list(dict.fromkeys(seq))   # 按出现顺序去重
    for g in gids:
        gid_chars[g].add(cid)
    for n in names:
        name_chars[n].add(cid)

print(f"\nGroupId 种类 {len(gid_chars)}，Name 种类 {len(name_chars)}")

# 跨角色高频 = 主角 / 旁白 / 常驻
common_g = {g for g, cs in gid_chars.items() if len(cs) > len(FILES) * 0.5}
common_n = {n for n, cs in name_chars.items() if len(cs) > len(FILES) * 0.5}
print(f"跨角色 >50% 的 GroupId: {sorted(common_g)[:10]}")
print(f"跨角色 >50% 的 Name:    {sorted(common_n)[:10]}")

result = {}
src_stat = Counter()
BAD = {"？？？", "???", ""}
for cid in sorted(per_char_gid):
    # 优先：登场时最早出现的「专属」立绘名（= 变身后的形态名，而非本名）
    # 专属判据：该 GroupId 被 <=3 个角色的剧情用过，排除主角/常驻角色抢占
    seq = first_gid.get(cid) or []
    pick = None
    for limit in (3, 8, 20, 10 ** 6):     # 逐步放宽专属度门槛
        pick = next((g for g in seq
                     if g not in BAD and len(gid_chars[g]) <= limit), None)
        if pick:
            break
    if pick:
        result[cid] = (pick, "登场形态", per_char_gid[cid][pick])
        src_stat["登场形态"] += 1
        continue
    # 其次：出现次数最多的非本名候选
    cand = [(g, c, len(gid_chars[g])) for g, c in per_char_gid[cid].items()
            if g not in common_g and g not in BAD]
    if cand:
        g = max(cand, key=lambda t: (t[1], -t[2]))[0]
        result[cid] = (g, "GroupId", per_char_gid[cid][g])
        src_stat["GroupId"] += 1
        continue
    cand = [(n, c, len(name_chars[n])) for n, c in per_char_name[cid].items()
            if n not in common_n]
    if cand:
        n = max(cand, key=lambda t: (t[1], -t[2]))[0]
        result[cid] = (n, "Name", per_char_name[cid][n])
        src_stat["Name"] += 1
    else:
        result[cid] = ("", "无", 0)
        src_stat["无"] += 1

print(f"\n提取来源统计: {dict(src_stat)}")
print(f"\n=== 抽样（前 20） ===")
for cid in sorted(result)[:20]:
    nm, src, c = result[cid]
    print(f"  角色 {cid:>3}  {nm or '(未取到)':<20} [{src} x{c}]")

print(f"\n=== 角色 57 / 248 / 1 ===")
for cid in (1, 57, 248):
    if cid in result:
        print(f"  角色 {cid}: {result[cid]}")

# 保存
out = {str(cid): {"name": v[0], "src": v[1], "count": v[2]}
       for cid, v in result.items()}
json.dump(out, open("char_names.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(f"\n已保存 char_names.json（{len(out)} 条）")

# 检查重名
names_only = [v[0] for v in result.values() if v[0]]
dup = [n for n, c in Counter(names_only).items() if c > 1]
print(f"重名 {len(dup)} 个: {dup[:10]}")
