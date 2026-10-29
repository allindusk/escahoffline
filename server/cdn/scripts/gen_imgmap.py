"""生成图片映射表：剧本里的资源 ID -> 实际图片文件路径。"""
import json
import os
import re

WEB = "../web"
IMG = os.path.join(WEB, "media", "img")
X2 = os.path.join(WEB, "media", "img_x2")
# 网页里的相对路径前缀（imgmap.js 的值直接给 <img src> 用）
REL = "media/img"


def rel_p(sub, fname):
    """**只返回原图路径**，绝不指向 media/img_x2。

    为什么不能在这里路由超分图：
      · 出厂映射是随程序分发的，而 img_x2 是**用户本机**跑超分才有的产物；
      · 一旦映射指向 img_x2，没做过超分的机器上会整片裂图 ——
        实测本机 73% 的立绘、88% 的 CG 都指向 img_x2，而新用户一张都没有；
      · 超分的表达方式是 **data/imgmap.x2.js 覆盖层**，播放器的候选链是
        「x2 → base → 原图」，会自动优先用高清图、缺了就回退，不需要改出厂映射。
    """
    return f"{REL}/{sub}/{fname}"

# 实际存在的文件
have = {"stand": set(os.listdir(os.path.join(IMG, "stand"))),
        "back": set(os.listdir(os.path.join(IMG, "back"))),
        "cg": set(os.listdir(os.path.join(IMG, "cg")))}
full = set(os.listdir(os.path.join(IMG, "standfull"))) \
    if os.path.isdir(os.path.join(IMG, "standfull")) else set()

# 剧情数据分两类，结构同构：角色剧本 char_<cid>.js、主线/活动/其他 adv_<kind>_<no>.js。
# 两类引用的图片资源都可能是对方没引用过的（主线有专属背景/标题立绘），必须都扫。
STORY_RE = re.compile(r"(?:char_\d+|adv_(?:main|event|other)_\d+)\.js")

ids = {"stand": set(), "back": set(), "cg": set()}
for fn in os.listdir(os.path.join(WEB, "data")):
    if not STORY_RE.fullmatch(fn):
        continue
    raw = open(os.path.join(WEB, "data", fn), encoding="utf-8").read()
    d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    for p in d["parts"]:
        ids["stand"].update(p.get("stand") or [])
        ids["back"].update(p.get("back") or [])
        ids["cg"].update(p.get("cg") or [])
        # 演出指令里出现的立绘也要收齐（部分 key 不在 parts.stand 列表里）
        for m in p.get("methods") or []:
            if m.get("StandImage"):
                ids["stand"].add(m["StandImage"])

out = {"stand": {}, "back": {}, "cg": {}}
miss = {"stand": 0, "back": 0, "cg": 0}

for s in ids["stand"]:
    m = re.match(r"stand_(\d+)_([0-9A-Fa-f]+)_", s)
    if not m:
        continue
    num, cloth = m.group(1), m.group(2).upper()
    n = int(num)
    # 首选「底图 + 表情差分」合成图：这才是游戏 ADV 的真实画法
    #（身体底图上叠一张同尺寸的表情图层，落点由 compose_stand_all.py 反推）
    if f"{s}.webp" in have["stand"]:
        out["stand"][s] = rel_p("stand", f"{s}.webp")
        continue
    # 其次用 standimages 的完整立绘兜底：只有一张固定表情，差分丢失
    if f"{n}.png" in full:
        out["stand"][s] = rel_p("standfull", f"{n}.png")
        continue
    # 不再回退到分层 base 部件：那是只有身体的底图，会显示成「没有头」。
    miss["stand"] += 1

for s in ids["back"]:
    f = s + ".png"
    if f in have["back"]:
        out["back"][s] = rel_p("back", f)
    else:
        miss["back"] += 1

for s in ids["cg"]:
    f = s + ".jpg"
    if f in have["cg"]:
        out["cg"][s] = rel_p("cg", f)
    else:
        miss["cg"] += 1

with open(os.path.join(WEB, "data", "imgmap.js"), "w", encoding="utf-8") as f:
    f.write("window.__IMGMAP=" + json.dumps(out, ensure_ascii=False,
                                            separators=(",", ":")) + ";")

print("=== 图片映射 ===")
for k in ("stand", "back", "cg"):
    print(f"  {k:<6} 引用 {len(ids[k]):>5}  已映射 {len(out[k]):>5}  "
          f"缺图 {miss[k]:>4}  "
          f"({len(out[k])/max(len(ids[k]),1)*100:.1f}%)")
size = os.path.getsize(os.path.join(WEB, "data", "imgmap.js"))
print(f"\n  imgmap.js {size/1024:.0f} KB")
