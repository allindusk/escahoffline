"""全量重做「表情差分立绘」合成。

游戏的做法（已逆向确认）：
  底图 base（身上被挖掉脸）  +  表情差分层 face（从同一张原画切下来的矩形）
  → 运行时直接叠放，落点是固定的。

落点怎么求：
  face 里「不是脸」的部分（头发）在 base 里原样存在，
  因此把 face 放到正确位置时，两者重叠像素差 ≈ 0。
  用 FFT 做全位移 SSD 相关，取最小值位置；同一 (角色,服装) 下所有表情变体
  应当给出同一落点，用投票取众数（更稳）。

用法：
  python _scripts/compose_stand_all.py            # 全量
  python _scripts/compose_stand_all.py 0396 0386  # 只做指定角色
"""
import json, os, re, sys, time, glob, collections
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import UnityPy
from PIL import Image

def _find_root(start):
    """向上找到含 web/ 与 server/ 的项目根。

    原来这里用 ``os.path.dirname(os.path.dirname(__file__))`` 假定脚本躺在
    ``_manifest_analysis/_scripts/``，迁进 ``server/cdn/scripts/`` 后那条
    相对关系断了，所以改成"向上探测"，两种布局都能用。
    """
    d = os.path.abspath(start)
    for _ in range(8):
        if os.path.isdir(os.path.join(d, "web")) and \
                os.path.isdir(os.path.join(d, "server")):
            return d
        d = os.path.dirname(d)
    return os.path.dirname(os.path.dirname(os.path.abspath(start)))


ROOT_DIR = _find_root(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT_DIR, "userdata")          # 工作区：files_img 在这里
IMG = os.path.join(BASE, "files_img")
WEBD = os.path.join(ROOT_DIR, "web", "data")
WEBS = os.path.join(ROOT_DIR, "web", "media", "img", "stand")
KEYPAT = re.compile(r"^stand_(\d+)_([0-9A-F]+)_([0-9A-F]+)$")

# 落点质量阈值：重叠区（头发/帽子）平均 RGB 差 > 8 基本就是错位（对齐时实测 0~2）
QWARN = 8.0
# 图集裁边超过这个像素数才算「实质裁边」，需要重做 x2 超分（1~2px 的是抗锯齿级差异）
TRIM_TOL = 2
# 合成逻辑版本：改了落点算法/裁边回补就 +1，强制下一次全量重写（之后恢复幂等）
GEN_VER = 2


# ---------------------------------------------------------------- 资源读取
def _find_pkg(num, cloth: str):
    """找某套衣服的分层包（``num`` 保留剧本 key 里的**原始字符串**）。

    **补零宽度有意义**：``stand_36`` 与 ``stand_036`` 是两套不同素材
    （前者 鷹守ハルカ 便服、后者 閃忍ハルカ 忍者装）。所以先按剧本 key 的
    原始宽度精确查找；只有它不存在时才退回补零写法
    （历史差异：剧本 key 写 ``stand_2``、包名写 ``stand_02``）。
    """
    target = cloth.lower()
    s = str(num)
    p = os.path.join(IMG, f"bundle_standimage_stand_{s}_{target}")
    if os.path.exists(p):
        return p
    n = int(s)
    for pad in (f"{n:02d}", f"{n:03d}", f"{n:04d}", f"{n:05d}", str(n)):
        if pad == s:
            continue
        p = os.path.join(IMG, f"bundle_standimage_stand_{pad}_{target}")
        if os.path.exists(p):
            return p
    return None


def load_pkg(num, cloth):
    """返回 (base, faces, info)
    info = 包内 base_info（游戏官方合成参数）：
      ExpressionCutRectLeft/Top/Width/Height = 表情差分图层的落点与尺寸
      AlphaCutRectLeft/Top/Width/Height     = 底图在 1280x720 画布上的位置

    **表情差分 sprite 是「图集裁边（trim）」过的**：`s.image` 只是 textureRect 那块，
    尺寸常常小于 `s.m_Rect`（= ExpressionCutRect 的宽高）。必须按
    `m_RD.textureRectOffset` 把它补回 m_Rect 大小的透明画布，否则贴上去会整体偏移。
    例：stand_0345_8AEE967B 的表情是 401x244、m_Rect 是 636x244、offset.x=106，
    直接用 401 宽的图贴 (192,8) 就左偏 106px（表情错位到额头上/露头发缺口的元凶）。
    """
    p = _find_pkg(num, cloth)
    if not p:
        return None, None, None, 0
    env = UnityPy.load(p)
    base, faces, info = None, {}, None
    trim_max = 0
    for o in env.objects:
        if o.type.name == "TextAsset":
            raw = o.read().m_Script
            if isinstance(raw, str):
                raw = raw.encode("utf-8", "surrogateescape")
            try:
                j = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            if "ExpressionCutRectLeft" in j:
                info = j
        elif o.type.name == "Sprite":
            s = o.read()
            img = s.image.convert("RGBA")
            # 底图和表情层都可能被图集裁边，统一按 m_Rect 补回（坐标系 = 角色本地画布，
            # base_info 的 AlphaCutRect / ExpressionCutRect 都是相对它的）
            r = s.m_Rect
            w, h = int(round(r.width)), int(round(r.height))
            if (w, h) != img.size and w > 0 and h > 0:
                trim_max = max(trim_max, abs(w - img.width), abs(h - img.height))
                off = s.m_RD.textureRectOffset
                canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                canvas.paste(img, (int(round(off.x)),
                                   int(round(h - off.y - img.height))), img)
                img = canvas
            if s.m_Name.endswith("_base"):
                base = img
            else:
                faces[s.m_Name.rsplit("_", 1)[1]] = img
    return base, faces, info, trim_max


# ---------------------------------------------------------------- SSD 匹配
def _rfft_cache(base):
    """预计算底图的 FFT 复用项"""
    A = np.array(base).astype(np.float32)
    ma = (A[:, :, 3] > 128).astype(np.float32)
    ra = A[:, :, :3].mean(axis=2) * ma
    H, W = ma.shape
    sh = 1 << (H - 1).bit_length()
    sw = 1 << (W - 1).bit_length()
    FA = np.fft.rfft2(a2 := (ra ** 2) * ma, s=(sh, sw))
    FB = np.fft.rfft2(ra, s=(sh, sw))
    FM = np.fft.rfft2(ma, s=(sh, sw))
    return dict(FA=FA, FB=FB, FM=FM, ma=ma, sh=sh, sw=sw)


def ssd_min(base_cache, face):
    """返回 (x, y, ssd_per_px) —— face 放到底图各位置的最优解"""
    B = np.array(face).astype(np.float32)
    mb = (B[:, :, 3] > 128).astype(np.float32)
    rb = B[:, :, :3].mean(axis=2) * mb
    h, w = mb.shape
    H, W = base_cache["ma"].shape
    if h > H or w > W:
        return None
    sh, sw = base_cache["sh"], base_cache["sw"]
    Fmb = np.fft.rfft2(mb, s=(sh, sw))
    Fmb_rb = np.fft.rfft2(mb * rb, s=(sh, sw))
    Fmb_rb2 = np.fft.rfft2(mb * (rb ** 2), s=(sh, sw))
    # SSD = corr(a²·ma, mb) - 2·corr(ma·a, mb·b) + corr(ma, mb·b²)
    t1 = np.fft.irfft2(base_cache["FA"] * np.conj(Fmb), s=(sh, sw))
    t2 = np.fft.irfft2(base_cache["FB"] * np.conj(Fmb_rb), s=(sh, sw))
    t3 = np.fft.irfft2(base_cache["FM"] * np.conj(Fmb_rb2), s=(sh, sw))
    ssd = t1[:H - h + 1, :W - w + 1] - 2 * t2[:H - h + 1, :W - w + 1] + t3[:H - h + 1, :W - w + 1]
    ov = np.fft.irfft2(base_cache["FM"] * np.conj(Fmb), s=(sh, sw))[:H - h + 1, :W - w + 1]
    valid = ov > (0.15 * ov.max())
    if not valid.any():
        return None
    s = np.where(valid, ssd / np.maximum(ov, 1), np.inf)
    i = int(np.argmin(s))
    y, x = divmod(i, s.shape[1])
    return int(x), int(y), float(s[y, x])


# ---------------------------------------------------------------- 落点质量校验
def overlap_diff(base_arr, face_arr, pos, min_px=300):
    """把 face 贴在 pos 后，与底图**都不透明**的重叠区（= 头发/帽子等没被挖掉的部分）
    的平均 RGB 差。对齐时接近 0（实测 0.3~1），错位时 >10（几十像素的错位会在
    重影区产生巨大差异）。返回 (diff, 重叠像素数)；重叠太少返回 (None, n)。
    """
    x, y = pos
    H, W = base_arr.shape[:2]
    h, w = face_arr.shape[:2]
    if x < 0 or y < 0 or x + w > W or y + h > H:
        return None, 0
    sa = base_arr[y:y + h, x:x + w]
    ov = (face_arr[:, :, 3] > 128) & (sa[:, :, 3] > 128)
    n = int(ov.sum())
    if n < min_px:
        return None, n
    d = np.abs(sa[:, :, :3][ov].astype(np.int16)
               - face_arr[:, :, :3][ov].astype(np.int16)).mean()
    return float(d), n


def group_quality(base_arr, faces, codes, pos, min_px=300):
    """一组表情在给定落点上的质量：重叠像素数加权的平均 RGB 差。"""
    tot, nn = 0.0, 0
    for c in codes:
        f = faces.get(c)
        if f is None:
            continue
        d, n = overlap_diff(base_arr, np.asarray(f), pos, min_px)
        if d is None:
            continue
        tot += d * n
        nn += n
    return (tot / nn if nn else None), nn


def refine_slot(base_arr, faces, codes, pos, rad=16, min_px=300):
    """官方落点附近的局部搜索（±rad）。用于 sprite 尺寸与 ExpressionCutRect 不符的
    少数组（那几个组官方矩形只覆盖部分表情），避免整组退回不可靠的 FFT 反推。"""
    best, best_v = pos, None
    for dy in range(-rad, rad + 1):
        for dx in range(-rad, rad + 1):
            v, _ = group_quality(base_arr, faces, codes, (pos[0] + dx, pos[1] + dy), min_px)
            if v is None:
                continue
            if best_v is None or v < best_v - 1e-6:
                best_v, best = v, (pos[0] + dx, pos[1] + dy)
    return best, best_v


# ---------------------------------------------------------------- 单组处理
def _all_images_exist(num, cloth, codes):
    return all(os.path.exists(os.path.join(WEBS, f"stand_{num}_{cloth}_{c}.webp"))
               for c in codes)


def process_group(task):
    num, cloth, faces_wanted, old_slot = task
    base, faces, info, trim = load_pkg(num, cloth)
    if base is None:
        return (num, cloth, None, {}, "缺分层包", "", None, 0)
    ba = np.array(base)[:, :, 3]
    base_opaque = int((ba > 128).sum())

    # —— 类型 B：包里根本没有表情图层，底图本身就是完整立绘
    if not faces:
        if not _all_images_exist(num, cloth, faces_wanted):
            for code in faces_wanted:
                fn = f"stand_{num}_{cloth}_{code}.webp"
                base.save(os.path.join(WEBS, fn), "WEBP", quality=86, method=4)
        return (num, cloth, None, {}, "无表情图层-直接用底图", "", None, trim)

    # —— 类型 C：底图是空画布，差分图层本身就是整幅画面（CG/半身图）
    if base_opaque == 0:
        if not _all_images_exist(num, cloth, [c for c in faces_wanted if c in faces]):
            for code in faces_wanted:
                f = faces.get(code)
                if f is None:
                    continue
                fn = f"stand_{num}_{cloth}_{code}.webp"
                f.save(os.path.join(WEBS, fn), "WEBP", quality=86, method=4)
        return (num, cloth, None, {}, "空底图-差分即整图", "", None, trim)

    # —— 首选：游戏官方 base_info 给出的落点（ExpressionCutRect），零误差
    slot = None
    src = ""
    if info and info.get("ExpressionCutRectWidth") and info.get("ExpressionCutRectHeight"):
        px, py = int(info["ExpressionCutRectLeft"]), int(info["ExpressionCutRectTop"])
        pw, ph = int(info["ExpressionCutRectWidth"]), int(info["ExpressionCutRectHeight"])
        # 表情层已按 m_Rect 补回画布，正常情况下尺寸 == 官方矩形尺寸
        fits = all(
            (faces[c].size == (pw, ph)) for c in faces_wanted if c in faces
        ) and 0 <= px and 0 <= py and px + pw <= base.width and py + ph <= base.height
        if fits:
            slot = (px, py)
            src = "官方base_info"
        else:
            # 极少数组（官方矩形只覆盖部分表情）→ 在官方落点附近 ±16px 局部精修，
            # 而不是整组退回不可靠的 FFT 反推（那才是历史上"总有几张错位"的来源）
            _barr = np.array(base)
            _codes = [c for c in faces_wanted if c in faces]
            slot, qv = refine_slot(_barr, faces, _codes, (px, py))
            if qv is None:
                slot = (px, py)
            src = f"官方邻域精修({px},{py})"

    # —— 退路：没有官方参数时，用像素级 SSD 匹配反推落点
    if slot is None:
        cache = _rfft_cache(base)
        votes = collections.Counter()
        best = {}
        for code in faces_wanted:
            f = faces.get(code)
            if f is None:
                continue
            r = ssd_min(cache, f)
            if r is None:
                continue
            x, y, v = r
            votes[(x, y)] += 1
            best[code] = (x, y, v)
        if not votes:
            # 再退路：底图内容极少 / 比差分图层还小 —— 「差分图层」本身就是整幅画面
            for code in faces_wanted:
                f = faces.get(code)
                if f is None:
                    continue
                fn = f"stand_{num}_{cloth}_{code}.webp"
                f.save(os.path.join(WEBS, fn), "WEBP", quality=86, method=4)
            return (num, cloth, None, {}, "底图过小-差分即整图", "", None, trim)
        # 众数落点；并列时取「该落点上总 SSD 最小」的一组
        top = max(votes.values())
        cands = [k for k, v in votes.items() if v == top]
        if len(cands) > 1:
            cands.sort(key=lambda k: sum(best[c][2] for c in best if best[c][:2] == k))
        slot = cands[0]
        src = "SSD反推"

    # —— 落点质量自检（对齐时重叠区差异 ≈0，错位会飙到十几～几十）
    q, qn = group_quality(np.array(base), faces, [c for c in faces_wanted if c in faces], slot)

    # —— 幂等：落点没变、文件都在**且尺寸对得上** → 不重写。
    # old_slot 传 None 表示合成逻辑升级（GEN_VER 变了），此时强制重写一遍。
    # 尺寸校验是必须的：以前把另一套素材合成到同名位置（stand_36 被 036 的包覆盖），
    # 产出的图比底图大一圈；只看"文件存在 + 落点没变"会一直跳过、永远不修正。
    codes_ok = [c for c in faces_wanted if c in faces]

    def _size_ok():
        try:
            for c in codes_ok:
                p = os.path.join(WEBS, f"stand_{num}_{cloth}_{c}.webp")
                with Image.open(p) as im:
                    if im.size != base.size:
                        return False
            return True
        except Exception:                                    # noqa: BLE001
            return False

    if old_slot == list(slot) and _all_images_exist(num, cloth, codes_ok) \
            and _size_ok():
        out = {c: list(slot) for c in codes_ok}
        return (num, cloth, list(slot), out, "", src, q, trim)

    out = {}
    missing = []
    for code in faces_wanted:
        f = faces.get(code)
        if f is None:
            missing.append(code)
            continue
        comp = base.copy()
        comp.paste(f, slot, f)
        fn = f"stand_{num}_{cloth}_{code}.webp"
        comp.save(os.path.join(WEBS, fn), "WEBP", quality=86, method=4)
        out[code] = list(slot)
    return (num, cloth, list(slot), out,
            (f"缺表情:{','.join(missing)}" if missing else ""), src, q, trim)


def collect_tasks():
    ids = set()
    # 角色剧本 char_<cid>.js 与 主线/活动/其他 adv_<kind>_<no>.js 结构同构，都要收
    story_re = re.compile(r"(?:char_\d+|adv_(?:main|event|other)_\d+)\.js")
    for fn in os.listdir(WEBD):
        if not story_re.fullmatch(fn):
            continue
        raw = open(os.path.join(WEBD, fn), encoding="utf-8").read()
        d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        for p in d["parts"]:
            ids.update(p.get("stand") or [])
            for m in p.get("methods") or []:
                if m.get("StandImage"):
                    ids.add(m["StandImage"])
    groups = collections.defaultdict(set)
    for k in ids:
        m = KEYPAT.match(k)
        if m:
            groups[(m.group(1), m.group(2))].add(m.group(3))
    return groups


if __name__ == "__main__":
    os.makedirs(WEBS, exist_ok=True)
    groups = collect_tasks()
    if len(sys.argv) > 1:
        want = set(sys.argv[1:])
        groups = {k: v for k, v in groups.items() if k[0] in want}
    slots = {}
    trims = {}
    done = 0
    warns = []
    special = 0
    srcs = collections.Counter()
    qc = []          # 落点质量自检：差异大的组（错位嫌疑）
    slot_path = os.path.join(WEBD, "stand_slot.json")
    old_slots = {}
    if os.path.exists(slot_path):
        try:
            old_slots = json.load(open(slot_path, encoding="utf-8"))
        except Exception:
            old_slots = {}
    ver_path = os.path.join(BASE, "_archive", "stand_compose_ver.txt")
    force = True
    if os.path.exists(ver_path):
        try:
            force = int(open(ver_path, encoding="utf-8").read().strip()) != GEN_VER
        except Exception:
            force = True
    if force:
        print(f"合成逻辑版本 -> {GEN_VER}：本次强制重写全部合成图", flush=True)
    tasks = [(num, cloth, sorted(faces),
              None if force else old_slots.get(f"{num}_{cloth}"))
             for (num, cloth), faces in groups.items()]
    total_faces = sum(len(t[2]) for t in tasks)
    print(f"分组 {len(tasks)} 个，需合成表情 {total_faces} 张", flush=True)

    # 每组「用哪个分层包」必须留痕：立绘串成另一套（stand_36 被 036 的包覆盖）
    # 这类问题，光看「合成 N 套成功」永远发现不了。
    fell = []
    for (num, cloth) in sorted(groups):
        p = _find_pkg(num, cloth)
        if not p:
            print(f"  ✗ {num}/{cloth} → 找不到分层包"
                  f"（{len(groups[(num, cloth)])} 个表情没有图）", flush=True)
            continue
        b = os.path.basename(p)
        tok = b.split("_")[3] if b.count("_") >= 3 else ""
        if tok and tok != str(num):
            fell.append((num, cloth, tok))
            print(f"  · {num}/{cloth} → {b}  "
                  f"⚠补零宽度不同（要求 {num}，实际 {tok}）", flush=True)
        else:
            print(f"  · {num}/{cloth} → {b}", flush=True)
    if fell:
        print(f"  ⚠ 共 {len(fell)} 组用了不同补零宽度的包："
              + "、".join(f"{n}/{c}->{t}" for n, c, t in fell[:8]), flush=True)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as ex:
        for num, cloth, slot, out, msg, src, q, trim in ex.map(process_group, tasks, chunksize=4):
            done += 1
            if src:
                srcs[re.sub(r"\(.*\)", "", src)] += 1
            if slot is None:
                if msg in ("缺分层包", "无有效表情"):
                    warns.append(f"{num}/{cloth}: {msg}")
                else:
                    special += 1
            else:
                slots[f"{num}_{cloth}"] = slot
                trims[f"{num}_{cloth}"] = trim
                if q is not None and q > QWARN:
                    qc.append((float(q), f"{num}_{cloth}", slot))
            if msg:
                warns.append(f"{num}/{cloth}: {msg}")
            if done % 50 == 0 or done == len(tasks):
                el = time.time() - t0
                print(f"  [{done}/{len(tasks)}] {el:.0f}s  预计剩余 {el/done*(len(tasks)-done):.0f}s", flush=True)
    with open(slot_path, "w", encoding="utf-8") as f:
        json.dump(slots, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\n完成，用时 {time.time()-t0:.0f}s，落点记录 {len(slots)} 组，特殊类型 {special} 组 -> web/data/stand_slot.json")
    print("落点来源:", dict(srcs))
    # 质量自检报告（对齐良好时重叠区差异 ≈0~2；>QWARN 说明表情层与底图在该落点对不上）
    qc.sort(reverse=True)
    rep = os.path.join(BASE, "_archive")
    os.makedirs(rep, exist_ok=True)
    with open(os.path.join(rep, "stand_slot_qc.json"), "w", encoding="utf-8") as f:
        json.dump([{"diff": round(d, 2), "key": k, "slot": s} for d, k, s in qc],
                  f, ensure_ascii=False, indent=1)
    if qc:
        print(f"\n落点质量可疑 {len(qc)} 组（diff > {QWARN}，已写入 _archive/stand_slot_qc.json）：")
        for d, k, s in qc[:15]:
            print(f"   {k} slot={s} diff={d:.1f}")
    if warns:
        print("警告：")
        for w in warns[:20]:
            print("  ", w)

    # ---- x2（超分图）陈旧清单 ----
    # 播放器实际读的是 media/img_x2/stand/*（apply_x2.py 改写的 imgmap）。
    # 本次「落点变了」或「sprite 有实质裁边回补」的组，其超分产物还是旧图，
    # 不重做的话页面上仍是旧的错位立绘——历史上"总有几张没修好"就是这么漏的。
    x2dir = os.path.join(os.path.dirname(WEBD), "media", "img_x2", "stand")
    stale = []
    if os.path.isdir(x2dir):
        for key, sl in slots.items():
            num, cloth = key.split("_", 1)
            changed = old_slots.get(key) != sl
            # 合成逻辑升级时才需要按裁边幅度全量排查；平时只看落点变化
            trimmed = force and trims.get(key, 0) > TRIM_TOL
            if not (changed or trimmed):
                continue
            if glob.glob(os.path.join(x2dir, f"stand_{num}_{cloth}_*.webp")):
                stale.append({"key": key, "slot": sl, "changed": changed,
                              "trim": trims.get(key, 0)})
    stale_path = os.path.join(BASE, "_archive", "x2_stale.json")
    with open(stale_path, "w", encoding="utf-8") as f:
        json.dump(stale, f, ensure_ascii=False, indent=1)
    if stale:
        print(f"\n超分图(x2)需重做 {len(stale)} 组（落点变化或裁边回补，清单 "
              f"-> _archive/x2_stale.json）：")
        for s in stale[:10]:
            print(f"   {s['key']} slot={s['slot']} changed={s['changed']} "
                  f"trim={s['trim']}")
        print("   修复命令：删 x2_stale.json 里各组的 "
              "_tools/_sr_out/*/img/stand/ 与 web/media/img_x2/stand/ 旧文件 "
              "→ python _scripts/sr.py --only stand --only-referenced "
              "→ python _scripts/apply_x2.py")
    with open(ver_path, "w", encoding="utf-8") as f:
        f.write(str(GEN_VER))
