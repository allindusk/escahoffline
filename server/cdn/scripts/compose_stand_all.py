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
  python _scripts/compose_stand_all.py 0123 0456  # 只做指定角色
"""
import json, os, re, sys, time, glob, collections
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import UnityPy
from PIL import Image

# 控制台/管道是 GBK 时，本脚本打的 ✓ · ⚠ → 会抛 UnicodeEncodeError（2026-11-02
# 74 部剧情全灭就是这一类）。stages.run_script 已强制 UTF-8，这里再兜一层，
# 手动跑 / 别的调用方也不会因为一个符号把整步弄失败。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:                                            # noqa: BLE001
    pass

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
GEN_VER = 3
# alpha 主体阈值：一批包的 DXT5 alpha 被整体压低（实测 204/179/163/126），
# 贴到页面上会与底色做 alpha 混合 → 脸/身体呈现半透明「色块」（2026-11-16 的
# 实测个别组就是这样）。主体 alpha < ALPHA_SOLID 就按 255/mode 整体拉伸。
ALPHA_SOLID = 248.0
ALPHA_FLOOR = 64.0      # 低于它当作真半透明素材（幽灵之类）不动
# 表情层 / 底图 重叠区亮暗比的可接受区间：超出说明两层不同源（脸亮身体暗）
COLOR_TOL_LO = 0.93
COLOR_TOL_HI = 1.06
# 亮暗比「能自动修」的上限：color_match 只处理「表情层更亮」这一段（乘系数提亮底图）。
# 超过它、或者低于 COLOR_TOL_LO（表情层更暗）都不在自动修正能力内 —— 体检不能
# 把它们当「重写就能好」的坏图，否则每次点修复都会重做一遍、结果一模一样。
COLOR_FIX_MAX = 1.8
# 体检每组抽几张表情比对（首/中/尾）：单张偶然性太大，坏脸可能只在某一张上
DEEP_FACES = 3
# 成品与重合成结果的容差：
#   · 轮廓不重合像素数（一边不透明一边全透明）—— 正常成品 0，错位组 6~17 万
#   · 整体亮度偏移（两边不透明处的平均色之差）—— 正常 ≤1.5，乘过系数的 5~29；
#     实测大片渐变的海报式整图（章題之类）webp q86 会有 2.0~2.6 的压缩偏色，
#     所以门槛取 4（落在「压缩噪声」和「整体乘系数」之间）
# 注意别用「alpha 差 >8 的像素数」：正常图边缘抗锯齿就有 3000 左右，会误判。
STALE_ALPHA_PX = 200
STALE_RGB = 4.0


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


def load_pkg(num, cloth, max_faces=None, allowed=None):
    """返回 (base, faces, info)

    ``max_faces``：只取前 N 张表情层（体检时用，纹理解码是这里最贵的一步）。
    ``allowed``：只解码这些表情 code（= ``ImageGroup.ExpressionImages`` 白名单），
    体检时用它，避免把包内的残留精灵算进对齐质量。
    info = 包内 base_info（游戏官方合成参数）：
      ExpressionCutRectLeft/Top/Width/Height = 表情差分图层的落点与尺寸
      AlphaCutRectLeft/Top/Width/Height     = 底图在 1280x720 画布上的位置

    **表情差分 sprite 是「图集裁边（trim）」过的**：`s.image` 只是 textureRect 那块，
    尺寸常常小于 `s.m_Rect`（= ExpressionCutRect 的宽高）。必须按
    `m_RD.textureRectOffset` 把它补回 m_Rect 大小的透明画布，否则贴上去会整体偏移。
    例：某组表情是 401x244、m_Rect 是 636x244、offset.x=106，
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
            _nm = o.peek_name() or ""
            if not _nm.endswith("_base"):
                if allowed is not None and _nm.rsplit("_", 1)[-1] not in allowed:
                    continue                  # 白名单外的残留精灵，不解码
                if max_faces is not None and len(faces) >= max_faces:
                    continue                  # 体检模式：多余的差分图不解码
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


# ---------------------------------------------------------------- alpha 修复
def alpha_mode(img):
    """图里「不透明主体」的 alpha 众数（忽略 <32 的透明背景）。

    正常立绘主体是 255；实测有 13 组包只有 204/179/163（DXT5 的 alpha 端点被
    整体压低），这类图不处理就会在网页上变成半透明色块。
    """
    a = np.array(img.convert("RGBA"))[:, :, 3]
    v = a[a > 32]
    if v.size < 64:
        return 255
    u, c = np.unique(v, return_counts=True)
    return int(u[c.argmax()])


def alpha_gain(base, faces):
    """返回 (gain, mode)：把整组（底图 + 表情层）的 alpha 统一乘 gain 拉回 255。

    用**同一个 gain** 处理底图与表情层，两层之间的相对透明度关系才不变。
    """
    def _valid(m):
        return m is not None and ALPHA_FLOOR <= m < ALPHA_SOLID

    m = alpha_mode(base) if base is not None else None
    if not _valid(m):
        ms = [alpha_mode(f) for f in (faces or {}).values()]
        ms = sorted(x for x in ms if _valid(x))
        m = ms[len(ms) // 2] if ms else None
    if not _valid(m):
        return 1.0, None
    return 255.0 / m, m


def solidify_alpha(img, gain):
    if gain <= 1.001:
        return img
    a = np.array(img.convert("RGBA"))
    a[:, :, 3] = np.clip(a[:, :, 3].astype(np.float32) * gain, 0, 255).astype(np.uint8)
    return Image.fromarray(a, "RGBA")


def _alpha_ok(num, cloth, codes):
    """已生成的图主体 alpha 是否已实心（幂等用：修过一次就不再重写）。

    判定区间必须与 ``scan_disk`` 一致：只有「该修而未修」的
    ``[ALPHA_FLOOR, ALPHA_SOLID)`` 才算不合格。低于 ALPHA_FLOOR 的是真半透明
    素材（幽灵之类），``alpha_gain`` 明确不处理它们，这里要是也判坏，
    每次合成都会重写一遍、永远修不好（幂等永远失效）。
    """
    try:
        for c in codes:
            p = os.path.join(WEBS, f"stand_{num}_{cloth}_{c}.webp")
            with Image.open(p) as im:
                m = alpha_mode(im)
                if ALPHA_FLOOR <= m < ALPHA_SOLID:
                    return False
        return True
    except Exception:                                        # noqa: BLE001
        return False


def overlap_ratio(base, face, slot, min_px=200):
    """表情层与底图在**重叠区**（脸洞外、两边都不透明的那圈头发/帽子）的 RGB 比。

    这两块是同一张原画切出来的，比值必须≈1。偏离说明其中一个被整体乘了系数。
    """
    x, y = slot
    if x < 0 or y < 0 or x + face.width > base.width or y + face.height > base.height:
        return None, 0
    B = np.array(base)[y:y + face.height, x:x + face.width]
    F = np.array(face)
    ov = (F[:, :, 3] > 100) & (B[:, :, 3] > 100)
    n = int(ov.sum())
    if n < min_px:
        return None, n
    fb = F[:, :, :3][ov].astype(np.float32).mean(axis=0)
    bb = B[:, :, :3][ov].astype(np.float32).mean(axis=0)
    return fb / np.maximum(bb, 1), n


def color_match(base, faces, slot):
    """底图偏暗时按重叠区比值把它提亮（表情层/底图不同源的那种包）。

    案例：某一组的底图 RGB 被整体乘了 0.8（三通道等比 = 1.25），
    合成后脸（表情层）亮、身体（底图）暗，肉眼很明显。
    只处理「表情层更亮 6%~80%」这种情况；其余保持原样。
    """
    rs = []
    for f in faces.values():
        r, n = overlap_ratio(base, f, slot)
        if r is not None:
            rs.append(r)
    if not rs:
        return base, 1.0
    r = np.median(np.array(rs), axis=0).astype(np.float32)
    m = float(r.mean())
    if not (1.06 <= m <= 1.8):
        return base, 1.0
    a = np.array(base)
    a[:, :, :3] = np.clip(a[:, :, :3].astype(np.float32) * r[None, None, :],
                          0, 255).astype(np.uint8)
    return Image.fromarray(a, "RGBA"), m


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
    """合成一组表情差分。``task = (num, cloth, faces_wanted[, old_slot[, force]])``。

    ``force=True``（体检修复用）无视一切「文件都在就跳过」的幂等守卫：
    类型 B/C 分支以前只要文件齐全就直接 return，体检发现成品是旧图也修不动。
    老调用方仍传 4 元组，force 默认 False。
    """
    num, cloth = task[0], task[1]
    faces_wanted = task[2]
    old_slot = task[3] if len(task) > 3 else None
    force = bool(task[4]) if len(task) > 4 else False
    base, faces, info, trim = load_pkg(num, cloth)
    if base is None:
        return (num, cloth, None, {}, "缺分层包", "", None, 0)
    # —— 官方 ImageGroup.ExpressionImages 才是允许叠到本底图的精灵清单。
    # 同包内还打包了别的组/残留精灵（尺寸、脸板都对不上），以前把包内所有非 base
    # sprite 都塞进 faces，导致这些清单外精灵被错误合成到本底图（047 的 8BEA、
    # 130 的 8AEC/908C 就是这样出来的坏图：落点 q 27~47，远高于合格阈值 8）。
    # 有清单时只保留清单内精灵；清单为空（旧素材）则维持原行为不动。
    ig = (info or {}).get("ImageGroup") or {}
    expr = ig.get("ExpressionImages") or []
    allowed = {s.rsplit("_", 1)[-1] for s in expr} if expr else None
    if allowed:
        faces = {k: v for k, v in faces.items() if k in allowed}

    # —— alpha 主体修复（整组同一 gain）：部分包的 DXT5 alpha 只有 204/179/163，
    # 不拉回 255 的话合成图是半透明的，网页上脸和身体会透出页面底色（色块）。
    gain, amode = alpha_gain(base, faces)
    if gain > 1.001:
        base = solidify_alpha(base, gain)
        faces = {k: solidify_alpha(v, gain) for k, v in faces.items()}

    ba = np.array(base)[:, :, 3]
    base_opaque = int((ba > 128).sum())

    # —— 类型 B：包里根本没有表情图层，底图本身就是完整立绘
    if not faces:
        if force or gain > 1.001 or not _all_images_exist(num, cloth, faces_wanted):
            for code in faces_wanted:
                fn = f"stand_{num}_{cloth}_{code}.webp"
                base.save(os.path.join(WEBS, fn), "WEBP", quality=86, method=4)
        return (num, cloth, None, {}, "无表情图层-直接用底图", "", None, trim)

    # —— 类型 C：底图是空画布，差分图层本身就是整幅画面（CG/半身图）
    if base_opaque == 0:
        if force or gain > 1.001 or \
                not _all_images_exist(num, cloth, [c for c in faces_wanted if c in faces]):
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

    # —— 底图/表情层不同源时的亮度匹配（脸亮身体暗）
    codes_now = [c for c in faces_wanted if c in faces]
    base, cm = color_match(base, {c: faces[c] for c in codes_now}, slot)
    if cm > 1.001:
        src = f"{src}|色差x{cm:.2f}"

    # —— 落点质量自检（对齐时重叠区差异 ≈0，错位会飙到十几～几十）
    q, qn = group_quality(np.array(base), faces, codes_now, slot)
    if gain > 1.001:
        src = f"{src}|alpha{amode}->255"

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

    # alpha 需要修（gain>1）时，还要确认已生成图真的已经实心，否则重写——
    # 修过一次 _alpha_ok 就为真，之后恢复幂等，不会每次安装都全量重编码。
    # 色差修正（cm>1）无法从成品反查，就每次重算这极少数几组（实测全库 2 组）。
    if not force and old_slot == list(slot) and _all_images_exist(num, cloth, codes_ok) \
            and _size_ok() and (gain <= 1.001 or _alpha_ok(num, cloth, codes_ok)) \
            and cm <= 1.001:
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


# ---------------------------------------------------------------- 全库体检修复
#: 体检逻辑版本（只用于日志留痕；用户是点按钮触发的，不靠版本号跳过）。
#:
#: 重要：这里**不许写任何具体编号/角色名** —— 那等于把"哪些素材有问题"传到 GitHub。
#: 体检全部靠图像特征判定，任何一台装了素材的机器都能自己把坏图找出来。
#:
#: v3：判定口径与合成对齐（白名单 / 邻域精修 / 色差修正都走同一套），并新增
#:     「成品 vs 重合成」忠实度检查、半透明判定加 ALPHA_FLOOR 下限、
#:     源素材层问题记名单不再重复重做。改动判定就 +1（会让旧名单失效、重查一遍）。
STAND_FIX_VER = 3

#: 曾经写死过一份「已知坏组」清单（编号 + 类型），现已删除 —— 见上面的说明。

#: 「源素材层问题」名单：重写也改不好的组（官方矩形/贴图本身对不上底图）。
#: 记下来下次体检跳过，避免每次点按钮都白重做一遍、还连带重做高清图。
SUSPECT_FILE = "stand_src_suspect.json"


def _disk_groups():
    """扫成品目录：``{编号_服装: [表情code, ...]}``（只认 webp）。"""
    out = collections.defaultdict(list)
    try:
        names = os.listdir(WEBS)
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".webp"):
            continue
        parts = fn[:-5].split("_")
        if len(parts) < 4 or parts[0] != "stand":
            continue
        out[f"{parts[1]}_{parts[2]}"].append("_".join(parts[3:]))
    return out


def _scan_one(path):
    """读一张成品：``(文件名, 尺寸, 主体 alpha 众数)``。"""
    try:
        with Image.open(path) as im:
            return os.path.basename(path), im.size, alpha_mode(im)
    except Exception:                                        # noqa: BLE001
        return os.path.basename(path), None, None


def scan_disk(workers=None):
    """一遍扫完所有成品图，按组汇总：

    ``{组key: {"alpha": 最差主体alpha, "sizes": {尺寸...}, "codes": [表情code...]}}``

    只读 webp 头 + alpha 通道（1.9 万张 ≈ 5~15 秒）。三项判定都基于它：

      · **半透明**：主体 alpha 落在 ``[ALPHA_FLOOR, ALPHA_SOLID)``。低于 ALPHA_FLOOR
        的是**真半透明素材**（幽灵之类），不是坏图 —— 旧代码只判 ``< ALPHA_SOLID``，
        这类素材每次体检都被判坏、每次都重写一遍还修不好（永远不收敛）。
      · **尺寸不一致**：同组尺寸不同 = 串了另一套素材 / 混进了清单外精灵
        （``stand_36`` 与 ``stand_036`` 是两套不同立绘）。
      · **组内有哪些表情 code**（深度体检与清单外残留判定要用）。
    """
    try:
        files = [os.path.join(WEBS, f) for f in os.listdir(WEBS) if f.endswith(".webp")]
    except OSError:
        return {}
    if not files:
        return {}
    out = {}
    w = workers or min(12, os.cpu_count() or 4)
    with ProcessPoolExecutor(max_workers=w) as ex:
        for name, size, mode in ex.map(_scan_one, files, chunksize=64):
            parts = name[:-5].split("_")
            if len(parts) < 4 or parts[0] != "stand":
                continue           # stand_xxx_base.png 之类的调试残留不算成品
            key = f"{parts[1]}_{parts[2]}"
            rec = out.setdefault(key, {"alpha": None, "sizes": set(), "codes": []})
            rec["codes"].append("_".join(parts[3:]))
            if size:
                rec["sizes"].add(size)
            if mode is not None and (rec["alpha"] is None or mode < rec["alpha"]):
                rec["alpha"] = mode
    return out


def scan_bad_groups():
    """兼容旧调用：返回「主体 alpha 该修而未修」的组 key（``编号_服装``）。"""
    return {k for k, v in scan_disk().items()
            if v["alpha"] is not None and ALPHA_FLOOR <= v["alpha"] < ALPHA_SOLID}


def load_info(num, cloth):
    """只读包内 ``base_info``（TextAsset，快，不解任何贴图）。"""
    p = _find_pkg(num, cloth)
    if not p:
        return None
    try:
        env = UnityPy.load(p)
    except Exception:                                        # noqa: BLE001
        return None
    for o in env.objects:
        if o.type.name != "TextAsset":
            continue
        raw = o.read().m_Script
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        try:
            j = json.loads(raw.decode("utf-8"))
        except Exception:                                    # noqa: BLE001
            continue
        if "ExpressionCutRectLeft" in j:
            return j
    return None


def _expect_paste(base, faces, slot):
    """「按当前逻辑叠出来的那张」——普通组（底图挖空脸 + 表情层贴上去）。"""
    def _one(code):
        f = faces.get(code)
        if f is None:
            return None
        comp = base.copy()
        comp.paste(f, slot, f)
        return comp
    return _one


def _product_check(key, codes, expect, size):
    """把「按当前逻辑重合成出来的那几张」与**磁盘上的成品**逐像素比。

    ``expect(code)`` 给出该表情在当前逻辑下应产出的图（类型 B/C 与普通组不同，
    见 ``_deep_one``）；``size`` 是画布尺寸。

    这是「体检到底有没有真的检查成品」的关键：q / 亮暗比都是从**源包**算出来的，
    源包没问题就永远看不出成品有问题 —— 而页面显示的是成品。旧成品可能是
    旧逻辑做的（没回补图集裁边、混进清单外精灵、没修 alpha），源包数据却是好的。

    返回 ``(轮廓不重合像素数, 整体亮度偏移)``：

      · 轮廓：**一张图不透明、另一张完全透明**的位置数（阈值 200/64）。
        这是「结构不对」的稳妥度量 —— 错位、叠错精灵、漏回补图集裁边，
        都会让轮廓互不重叠；而 webp 边缘的抗锯齿噪声只落在半透明带上，
        两个方向都不计入（实测正常成品为 0，错位组 6~17 万）。
        不能用「alpha 差 > 8 的像素数」：那条实测正常图就有 3000 左右，
        全是边缘抗锯齿噪声，会把好图误判成坏图。
      · 亮度偏移：两边都不透明处的**平均色**之差（不是逐像素差！）。
        逐像素差会被 webp q86 的压缩噪声顶到 7~8，而噪声均值正负抵消 ≈0；
        真正的色差是整体乘了系数，均值必然偏移（实测正常 ≤0.5，
        底图被压暗 0.8 的组是 5~29）。

    读不到成品 / 尺寸不符时返回 ``(None, None)``（尺寸问题由磁盘级检查兜）。
    """
    sil_max, deltas = 0, []
    for code in codes:
        comp = expect(code)
        if comp is None or comp.size != size:
            continue
        p = os.path.join(WEBS, f"stand_{key}_{code}.webp")
        if not os.path.exists(p):
            continue
        try:
            with Image.open(p) as im:
                disk = im.convert("RGBA")
        except Exception:                                    # noqa: BLE001
            continue
        if disk.size != size:
            continue
        C = np.asarray(comp).astype(np.int16)
        D = np.asarray(disk).astype(np.int16)
        ca, da = C[:, :, 3], D[:, :, 3]
        sil = int(((ca > 200) & (da < 64)).sum() + ((ca < 64) & (da > 200)).sum())
        sil_max = max(sil_max, sil)
        both = (ca > 200) & (da > 200)
        if both.sum() >= 100:
            cc = C[:, :, :3][both].astype(np.float64).mean(axis=0)
            dd = D[:, :, :3][both].astype(np.float64).mean(axis=0)
            deltas.append(float(np.abs(cc - dd).max()))
    if sil_max == 0 and not deltas:
        return None, None
    return sil_max, (round(max(deltas), 2) if deltas else None)


def _deep_one(task):
    """单组深度体检：解一次源包，把四项判定一起算出来。

    参数 ``task = (key, 成品里的表情 code 列表)``；返回
    ``(key, 亮暗比, 对齐色差, 轮廓不重合像素, 整体亮度偏移, 底图尺寸, 清单外残留code)``。

    **判定必须与 ``process_group`` 走同一套流程**（ExpressionImages 白名单 →
    alpha 拉伸 → 官方落点 / 尺寸不符时邻域精修 → 色差修正）。旧代码直接拿官方
    ``ExpressionCutRect`` 算 q，而合成在尺寸不符时会邻域精修 —— 两边口径不一致，
    结果就是「体检判坏 → 重写 → 还是判坏」的死循环。
    """
    key, codes = task
    num, cloth = key.split("_", 1)
    info = load_info(num, cloth)
    if not info or not info.get("ExpressionCutRectWidth"):
        return key, None, None, None, None, None, []
    ig = info.get("ImageGroup") or {}
    expr = ig.get("ExpressionImages") or []
    allowed = {s.rsplit("_", 1)[-1] for s in expr} if expr else None
    on_disk = [c for c in codes if allowed is None or c in allowed]
    orphan = [c for c in codes if allowed is not None and c not in allowed]
    if not on_disk:
        return key, None, None, None, None, None, orphan
    # 抽 3 张（首/中/尾）而不是 1 张：单张偶然性太大，同一组里坏脸可能只在某一张。
    sample = on_disk if len(on_disk) <= DEEP_FACES else \
        [on_disk[0], on_disk[len(on_disk) // 2], on_disk[-1]]
    sample = sorted(set(sample))
    try:
        base, faces, info, _trim = load_pkg(num, cloth,
                                            max_faces=len(sample), allowed=set(sample))
    except Exception:                                        # noqa: BLE001
        return key, None, None, None, None, None, orphan
    if base is None or not info:
        return key, None, None, None, None, None, orphan
    faces = {k: v for k, v in faces.items() if k in sample}

    # ① alpha 主体拉伸（与合成一致：整组同一个 gain）
    gain, _am = alpha_gain(base, faces)
    if gain > 1.001:
        base = solidify_alpha(base, gain)
        faces = {k: solidify_alpha(v, gain) for k, v in faces.items()}

    # 类型 B：包里没有表情层，成品应当就是底图本身 —— 同样要检查成品忠实度，
    # 否则「底图即整图」那批旧图（2026-10-25 那批 570 张）永远没人管。
    if not faces:
        code = on_disk[0]
        sil, delta = _product_check(key, [code], lambda c: base, base.size)
        return key, None, None, sil, delta, base.size, orphan

    # ② 落点（与合成一致：尺寸全对才用官方，否则邻域精修）
    px, py = int(info["ExpressionCutRectLeft"]), int(info["ExpressionCutRectTop"])
    pw, ph = int(info["ExpressionCutRectWidth"]), int(info["ExpressionCutRectHeight"])
    if all(f.size == (pw, ph) for f in faces.values()):
        slot = (px, py)
    else:
        slot, qv = refine_slot(np.array(base), faces, sorted(faces), (px, py))
        if qv is None:
            slot = (px, py)

    # ③ 亮暗比 + 色差修正（与合成一致）。q 必须在**修正后的底图**上算：
    # 底图被整体压暗时，原始 q 是几十（看着像严重错位），修正后其实是 1~2。
    # 旧判定没做这一步，把「脸亮身体暗」误当成「表情错位」，于是那些组
    # 每次都被判坏、每次重写完还是判坏。
    rs = []
    for f in faces.values():
        r, _n = overlap_ratio(base, f, slot)
        if r is not None:
            rs.append(r)
    m = float(np.median(np.array(rs), axis=0).mean()) if rs else None
    base, _cm = color_match(base, faces, slot)
    q, _qn = group_quality(np.array(base), faces, sorted(faces), slot)

    # ④ 成品忠实度。类型 C（底图是空画布）时 process_group 直接存差分图本身、
    # **不做叠放** —— 期望图必须跟着一致，否则叠到透明画布上会被 alpha 预乘变暗，
    # 凭空多出 2~4 的亮度差，修完还判坏。
    base_op = int((np.asarray(base)[:, :, 3] > 128).sum())
    if base_op == 0:
        expect = (lambda c: faces.get(c))
    else:
        expect = _expect_paste(base, faces, slot)
    a_miss, rgb_miss = _product_check(key, sorted(faces), expect, base.size)
    return (key,
            round(m, 3) if m is not None else None,
            round(q, 1) if q is not None else None,
            a_miss, (round(rgb_miss, 2) if rgb_miss is not None else None),
            base.size, orphan)


def scan_bad_deep(tasks, workers=None):
    """深度体检（慢，要解包）。返回 ``(det, orphans)``：

    det[key] = ``{"m": 亮暗比, "q": 对齐色差, "a": 成品alpha差异, "rgb": 成品RGB差异,
                  "size": 底图尺寸}``（全部组，不只是可疑的 —— 调用方要分类）
    orphans[key] = 成品里存在、但包内 ``ImageGroup.ExpressionImages`` 没列的表情 code

    四项在同一次解包里算完，几乎不额外花时间。**任何一项都不许写成
    「< 某阈值就算坏」的一刀切**：低于 ALPHA_FLOOR 的半透明是真素材，
    q 偏大往往是源素材/官方矩形的问题（离线无论重写多少遍都是同一张图）。
    """
    tasks = sorted(tasks)
    det, orphans = {}, {}
    if not tasks:
        return det, orphans
    w = workers or min(8, os.cpu_count() or 4)
    with ProcessPoolExecutor(max_workers=w) as ex:
        for i, (key, m, q, a, rgb, size, orp) in enumerate(
                ex.map(_deep_one, tasks, chunksize=4)):
            det[key] = {"m": m, "q": q, "a": a, "rgb": rgb, "size": size}
            if orp:
                orphans[key] = sorted(orp)
            if (i + 1) % 200 == 0:
                print(f"  深度体检 {i + 1}/{len(tasks)}…", flush=True)
    return det, orphans


def _load_suspect():
    """读「源素材层问题」名单；判定口径变过（STAND_FIX_VER）就整份作废重查。"""
    p = os.path.join(BASE, "_archive", SUSPECT_FILE)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("ver") == STAND_FIX_VER \
                and isinstance(d.get("keys"), dict):
            return d
    except Exception:                                        # noqa: BLE001
        pass
    return {"ver": STAND_FIX_VER, "keys": {}}


def _same_finding(rec, cur):
    """上次记的和这次算的是不是同一件事。

    差得多就说明素材被换过/重装过，必须重查一遍，不能拿旧结论跳过。
    """
    for f, tol in (("q", 0.5), ("m", 0.02), ("a", 200)):
        a, b = rec.get(f), cur.get(f)
        if a is None or b is None:
            continue
        if abs(float(a) - float(b)) > max(tol, abs(float(a)) * 0.2):
            return False
    return True


def _stale_kind(d):
    """成品是不是「旧逻辑 / 坏图」留下的（按当前逻辑重合成一遍就能看出差别）。

    q 和亮暗比都是从**源包**算的，源包没问题就永远看不出成品有问题 —— 而页面
    显示的是成品。这一项才是真正「检查已经装好的图」。
    """
    a, rgb = d.get("a"), d.get("rgb")
    if a is not None and a > STALE_ALPHA_PX:
        return f"成品轮廓与重做结果差 {a} 像素（旧图/错位）"
    if rgb is not None and rgb > STALE_RGB:
        return f"成品整体色差 {rgb}（旧图/亮度不对）"
    return ""


def repair(force=True, dry=False):
    """全库体检 + 修复（资源管理页「检查并修复立绘」按钮调这个）。

    **不依赖任何写死的编号清单**，四项检查全部从图像特征判定：

      ① **半透明**：成品主体 alpha 落在 ``[ALPHA_FLOOR, ALPHA_SOLID)``（该修而未修）。
         低于 ALPHA_FLOOR 的是真半透明素材，不是坏图 —— 判坏就会永远修不好。
      ② **同组尺寸不一致**：串了另一套素材 / 混进清单外精灵（36 与 036 是两套）。
      ③ **成品与重做结果不一致**：旧逻辑（未回补裁边 / 叠错精灵 / 没修 alpha）
         留下的成品。这是唯一能看见「源包没毛病但页面上的图是错的」的检查。
      ④ **亮暗比 / 对齐色差**：两层不同源或表情与底图挖空区对不上。

    ④ 里的「对齐色差偏大」往往重写一百遍也是同一张图（源素材 / 官方矩形问题），
    因此记进「源素材层问题」名单，第二次起直接跳过，不再白重做、也不再连带
    重做高清图 —— 旧代码就是每次都重做，用户看到「一直在修却还是那几张」。

    ``dry=True`` 只出报告不写文件（``--check``）。
    """
    t0 = time.time()
    disk = scan_disk()
    if not disk:
        print("没有已合成的立绘，跳过", flush=True)
        return 0
    groups = {k: sorted(v["codes"]) for k, v in disk.items()}
    print(f"立绘体检：已合成 {len(groups)} 组", flush=True)

    # —— 闸门：合成逻辑升级过就全量重做一遍。
    # 老成品是旧逻辑做的，而体检是从源包重算的；源包没问题就看不出成品有问题。
    # 只有版本号能兜这一刀（对应「改了落点/裁边/白名单必须升 GEN_VER」这条规则）。
    verp = os.path.join(BASE, "_archive", "stand_compose_ver.txt")
    try:
        cur_ver = int(open(verp, encoding="utf-8").read().strip())
    except Exception:                                        # noqa: BLE001
        cur_ver = None
    force_all = cur_ver != GEN_VER
    if force_all:
        print(f"合成逻辑版本 {cur_ver} -> {GEN_VER}：本次全量重做", flush=True)

    # —— ① ② 磁盘级（便宜，只读 webp 头 + alpha）
    alpha_bad = {k for k, v in disk.items()
                 if v["alpha"] is not None and ALPHA_FLOOR <= v["alpha"] < ALPHA_SOLID}
    size_bad = {k for k, v in disk.items() if len(v["sizes"]) > 1}
    print(f"① 半透明 {len(alpha_bad)} 组、② 同组尺寸不一致 {len(size_bad)} 组",
          flush=True)

    # —— ③ ④ 源包级 + 成品忠实度
    with_pkg = [k for k in sorted(groups) if _find_pkg(*k.split("_", 1))]
    pkg_warn = []
    for k in with_pkg:                       # 补零宽度串包审计（只报告）
        num, cloth = k.split("_", 1)
        b = os.path.basename(_find_pkg(num, cloth) or "")
        tok = b.split("_")[3] if b.count("_") >= 3 else ""
        if tok and tok != num:
            pkg_warn.append(f"{k} 用的是 stand_{tok} 的包")
    print("③④ 检查成品与源包（要解包，慢一些）…", flush=True)
    det, orphans = scan_bad_deep([(k, groups[k]) for k in with_pkg])

    x2keys = set()
    x2dir = os.path.join(os.path.dirname(WEBD), "media", "img_x2", "stand")
    if os.path.isdir(x2dir):
        for f in os.listdir(x2dir):
            if f.endswith(".webp") and f.startswith("stand_"):
                p = f[:-5].split("_")
                if len(p) >= 4:
                    x2keys.add(f"{p[1]}_{p[2]}")
    # 超分待办必须**累积**：只交「本次重写」的那批，下一次体检的报告会把上一批
    # 覆盖掉 —— 上一批要是没跑完（用户关页面 / 超分引擎缺失 / 中途崩），对应的高清图
    # 就永远停在旧版，而播放器优先读高清图，用户看到的就是「修了没效果」。
    # 由 api 处理完（重做或删掉）后清空 userdata/_archive/x2_pending.json。
    pend_p = os.path.join(BASE, "_archive", "x2_pending.json")
    try:
        with open(pend_p, encoding="utf-8") as f:
            pending = [k for k in json.load(f) if isinstance(k, str)]
    except Exception:                                        # noqa: BLE001
        pending = []

    sus = _load_suspect()
    todo, skipped, notes = [], [], []
    kind = {}                    # key -> 本次要重写的理由（日志/报告用）
    content = set()              # 内容确实会变的组（只有这些需要重做高清图）
    for key in with_pkg:
        d = det.get(key) or {}
        m, q = d.get("m"), d.get("q")
        stale = _stale_kind(d)
        size_deep = d.get("size") is not None and any(
            s != d["size"] for s in disk[key]["sizes"])
        # 「能重写修好」的只有三类：成品本身有问题（半透明 / 尺寸不对 / 与当前
        # 逻辑重做结果不一致）。**亮暗比偏大不算**——它和 q 一样是从源包算的，
        # 修正后的成品一旦忠实，这一项永远为真；把它当触发条件会每次重做一遍。
        # 真有亮度问题的话，成品与「修正后合成」必然对不上，由 stale 兜住。
        hard = key in alpha_bad or key in size_bad or size_deep or bool(stale)
        align = q is not None and q > QWARN
        color_odd = m is not None and (m < COLOR_TOL_LO or m > COLOR_FIX_MAX)
        if not (force_all or hard or align or color_odd):
            continue
        if force_all or hard:
            todo.append(key)
            if hard:
                content.add(key)
            if key in alpha_bad:
                kind[key] = "半透明躯体"
            elif stale:
                kind[key] = stale
            elif key in size_bad or size_deep:
                kind[key] = "尺寸不一致（串包/清单外精灵）"
            else:
                kind[key] = "逻辑升级全量重做"
            continue
        # 只剩「对齐色差偏大 / 亮暗比超出可自动修正范围」：源素材 / 官方矩形层面
        # 的缺陷，重写一百遍也是同一张图。第一次仍重写一遍确认（也许是旧成品
        # 造成的），之后按名单跳过，不再白重做、也不再连带重做高清图。
        why = (f"对齐色差 {q}（表情与底图挖空区对不上）" if align
               else f"亮暗比 {m}（超出可自动修正范围）")
        cur = {"q": q, "m": m, "a": d.get("a")}
        rec = sus["keys"].get(key)
        if rec and _same_finding(rec, cur):
            skipped.append(key)
            continue
        sus["keys"][key] = dict(cur, why=why, at=time.strftime("%Y-%m-%d %H:%M"))
        notes.append(f"{key}  {why}")
        todo.append(key)
        kind[key] = why + "（源素材嫌疑，重写确认）"

    # 名单里已经没有问题的组要清掉，别让它永远挂着
    for key in list(sus["keys"]):
        d = det.get(key) or {}
        q, m = d.get("q"), d.get("m")
        keep = ((q is not None and q > QWARN)
                or (m is not None and (m < COLOR_TOL_LO or m > COLOR_FIX_MAX)))
        if not keep:
            sus["keys"].pop(key, None)

    print(f"   要重做 {len(todo)} 组、按名单跳过 {len(skipped)} 组、"
          f"源素材层新记 {len(notes)} 组", flush=True)
    if orphans:
        n = sum(len(v) for v in orphans.values())
        print(f"   提示：{len(orphans)} 组里有 {n} 张清单外残留表情（不在官方"
              f" ExpressionImages 里；只报告不删，可能仍被剧本引用）", flush=True)

    fixed_keys, x2_stale, failed = [], [], 0
    slots = {}
    if not dry:
        for i, key in enumerate(todo, 1):
            num, cloth = key.split("_", 1)
            codes = groups.get(key) or []
            if not codes:
                continue
            try:
                # force=True：体检已经确认成品是旧图/坏图，不能再让「文件都在就跳过」
                # 的幂等守卫把它们挡回去（类型 B/C 分支以前就这么跳过，修了等于没修）
                r = process_group((num, cloth, codes, None, True))
            except Exception as e:                            # noqa: BLE001
                failed += 1
                print(f"  ✗ {key}: {e}", flush=True)
                continue
            if r[2]:
                slots[key] = r[2]
            fixed_keys.append(key)
            print(f"  · [{i}/{len(todo)}] {key} 重做 {len(codes)} 张  "
                  f"{kind.get(key, '')}  {r[5]}", flush=True)
            # 超分产物（img_x2）是另一份文件、播放器优先读它；这里只登记哪些组
            # 有旧高清图，由 server 侧（api 的 /api/stand/repair）决定重做还是丢掉。
            # 只有「内容确实变了」的才需要重做 —— 对齐色差那类重写后是同一张图，
            # 跟着重做一次高清图要多花好几分钟却没有任何变化。
            if key in x2keys and (key in content or force_all):
                x2_stale.append(key)
                print(f"    · {key} 有旧的高清图，稍后一并重做", flush=True)
    else:
        for key in todo:
            print(f"  (dry) {key}  {kind.get(key, '')}", flush=True)

    x2_stale = sorted(set(pending) | set(x2_stale))
    try:
        arch = os.path.join(BASE, "_archive")
        os.makedirs(arch, exist_ok=True)
        if dry:
            print(f"[dry] 用时 {time.time() - t0:.0f}s（未写入任何文件）", flush=True)
            return 0
        if x2_stale:
            with open(pend_p, "w", encoding="utf-8") as f:
                json.dump(x2_stale, f, ensure_ascii=False, indent=1)
        if slots:
            slot_path = os.path.join(WEBD, "stand_slot.json")
            old = {}
            if os.path.exists(slot_path):
                try:
                    old = json.load(open(slot_path, encoding="utf-8"))
                except Exception:                            # noqa: BLE001
                    old = {}
            old.update(slots)
            with open(slot_path, "w", encoding="utf-8") as f:
                json.dump(old, f, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(arch, "stand_fix_ver.txt"), "w", encoding="utf-8") as f:
            f.write(str(STAND_FIX_VER))
        with open(os.path.join(arch, SUSPECT_FILE), "w", encoding="utf-8") as f:
            json.dump(sus, f, ensure_ascii=False, indent=1)
        with open(os.path.join(arch, "stand_orphans.json"), "w", encoding="utf-8") as f:
            json.dump({"note": "成品里有、但包内 ImageGroup.ExpressionImages 没列的"
                              "表情（可能来自旧逻辑/串包；只报告不删）",
                       "groups": orphans}, f, ensure_ascii=False, indent=1)
        # 全量重做跑完才算「版本对齐」，否则下次还得再全量来一遍
        if force_all and not failed:
            with open(verp, "w", encoding="utf-8") as f:
                f.write(str(GEN_VER))
        # 本次修了哪些组 / 哪些组还有旧高清图：server 侧据此重做超分
        with open(os.path.join(arch, "stand_repair_last.json"), "w", encoding="utf-8") as f:
            json.dump({"fixed": fixed_keys, "x2_stale": x2_stale,
                       "src_suspect": sorted(sus["keys"]),
                       "skipped": skipped,
                       "new_suspect": notes,
                       "orphans": sum(len(v) for v in orphans.values()),
                       "pkg_warn": pkg_warn,
                       "full_rewrite": force_all,
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    print(f"立绘体检完成：修好 {len(fixed_keys)} 组，失败 {failed} 组，"
          f"源素材层跳过 {len(skipped)} 组，用时 {time.time() - t0:.0f}s", flush=True)
    return len(fixed_keys)


if __name__ == "__main__":
    if "--check" in sys.argv:
        repair(dry=True)          # 只体检出报告，不写任何文件（排查/验证用）
        sys.exit(0)
    if "--repair" in sys.argv:
        repair()
        sys.exit(0)
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
