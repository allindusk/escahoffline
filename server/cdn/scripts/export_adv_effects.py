"""导出 ADV 演出特效：表情符号（da_mark_*）+ 图集，给播放器做真帧动画。

为什么需要它
------------
游戏里 ``{"Name":"汗","Method":"ShowStandEffect"}`` 只是「放哪个表情」，
真正的样子（白色气泡框 + 水滴在框里跳 4 次、出现/循环/收起三段节奏）
全在 ``bundle_image_adv`` 里的 12 个 ``da_mark_*`` 数据结构里：
``TableParts`` 是部件树，``TableAnimation`` 每段带 fps/总帧数，
每个部件又是逐帧的 position / scaling / opacity 曲线（TableCode 低 15 位 = 帧号）。
剧本里没有任何时长或次数信息，所以靠 CSS/emoji 猜永远还原不了。

产物：

    web/media/img/advfx/mark.webp   表情符号图集（512×512，无损 webp，含透明通道）
    web/data/advfx.js               window.__ADVFX = { v, atlas, marks }

数据约定
--------
* ``atlas.mark = {src, w, h}``：图集地址与尺寸；
* ``marks.sweat.anims[]`` 每段 = ``{n, fps, cx, parts[]}``；
* ``parts[i]`` = ``{cell:[x,y,w,h,px,py], col:染色色值|null, k:逐帧数组}``；
* ``k[f] = [x, y, scale, opacity]``，**x/y 是 320 宽设计画布里的世界坐标**
  （父级的位置/缩放已经乘进去），前端按 1280/320 = 4 倍放大到舞台。
* 播放顺序 = ``anims`` 顺序（例：da_mark_sweat = first 出现并循环 → second 收起）。

用法
----
    python server/cdn/scripts/export_adv_effects.py          # cwd = userdata/
    python server/cdn/scripts/export_adv_effects.py --root . # cwd = 项目根
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

import UnityPy

MARK_PREFIX = "da_mark_"

# 剧本 12 种 ShowStandEffect.Name 与包里 12 个 da_mark_* 是**一一对应**的：
#   da_mark_leader      27×27 圆点、41 帧 → 点々（省略号）
#   da_mark_troubled    90×101 乱线团    → 困り
#   da_mark_exclamation 感叹号           → ビックリ
#   da_mark_surprise    「！？」         → ビックリはてな
# 下面两个常量是早期"点々 没有独立结构"的错误假设留下的，导出流程已不使用；
# 保留仅为兼容旧调用，正确映射见 player.js 的 MARK_ALIAS。
MARK_FALLBACK = {"dots": "leader"}
MARK_EXTRA = {"ビックリはてな": ["surprise"]}


def _n(v, d=0.0):
    """取数值字段，只在**缺失/None**时用默认值。

    以前一律写 `float(x or d)`，于是 0 被当成"没填"：
    冷気的冷蓝 b=0.38 没问题，但 r=0 → 1；煙的橙 (1,0.25,0) → 洋红 (1,0.25,1)；
    冷気雾 ColorVertex.Main.a=0（每个粒子随机透明度）→ 恒 1（不透明）。
    """
    if v is None or v == "":
        return float(d)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(d)


def _lerp(a, b, t):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a + (b - a) * t
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: _lerp(a.get(k, 0) or 0, b.get(k, 0) or 0, t) for k in set(a) | set(b)}
    return a if t < 0.5 else b


def _frames(codes, vals, total):
    """(TableCode, TableValue) → 每帧一个值。

    ``TableCode`` 是打包整数：低 15 位 = 帧号，高位是插值标志（与帧号无关）。
    只有一个值 = 整段常数；多个值按帧号线性插值铺满 total 帧。
    """
    if not vals:
        return None
    if len(vals) == 1 or not codes:
        return [vals[0]] * total
    pts = []
    for i, c in enumerate(codes):
        if i >= len(vals):
            break
        f = int(c) & 0x7FFF
        pts.append((min(f, total - 1), vals[i]))
    pts.sort(key=lambda t: t[0])
    out, j = [], 0
    for f in range(total):
        while j + 1 < len(pts) and pts[j + 1][0] <= f:
            j += 1
        f0, v0 = pts[j]
        if j + 1 < len(pts):
            f1, v1 = pts[j + 1]
            t = 0.0 if f1 == f0 else (f - f0) / (f1 - f0)
            out.append(_lerp(v0, v1, max(0.0, min(1.0, t))))
        else:
            out.append(v0)
    return out


def _track(ap, key, total):
    """取一条轨道并展开成逐帧数组。

    ``TableCodeValue`` 是 ``[{TableCode:[打包帧码…]}]``（一段或多段），
    把它摊平后与 ``TableValue`` 一一对应。
    """
    box = ap.get(key) or {}
    codes = []
    for cv in box.get("TableCodeValue") or []:
        codes.extend(cv.get("TableCode") or [])
    return _frames(codes, box.get("TableValue") or [], total)


def _cell_track(ap, total):
    """Cell 轨道 → 每帧 [IndexCellMap, IndexCell]，**逐帧跳变**（不插值）。

    电磁ウェブ（electricity / electricity_yellow）的 006/001 部件把 13 张闪电
    格子按帧号切换（TableCode 低 15 位 = 0,2,5,8…，每 2~3 帧换一格），
    这就是"电弧在跳"的全部来源。以前只取 TableValue[0] 当唯一格子，
    整段就变成一张静止的闪电图。
    """
    box = ap.get("Cell") or {}
    vals = box.get("TableValue") or []
    if not vals:
        return None
    def pair(v):
        if not isinstance(v, dict):
            return None
        ci = v.get("IndexCell")
        mi = v.get("IndexCellMap")
        return [-1 if ci is None else int(ci), -1 if mi is None else int(mi)]
    if len(vals) == 1:
        return [pair(vals[0])] * total
    codes = []
    for cv in box.get("TableCodeValue") or []:
        codes.extend(cv.get("TableCode") or [])
    if not codes:
        return [pair(vals[0])] * total
    pts = []
    for i, c in enumerate(codes):
        if i >= len(vals):
            break
        pts.append((min(int(c) & 0x7FFF, total - 1), pair(vals[i])))
    pts.sort(key=lambda t: t[0])
    out, j = [], 0
    for f in range(total):
        while j + 1 < len(pts) and pts[j + 1][0] <= f:
            j += 1
        out.append(pts[j][1])
    return out


def _argb(v):
    """LabelColor 的 rgba 是 ARGB 打包；0 = 不染色（保持原图颜色）。"""
    v = int(v or 0)
    if not v:
        return None
    a = (v >> 24) & 0xFF
    if not a:
        return None
    return "#%02x%02x%02x" % ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)


def read_scene_cells(env):
    """dc_scene_adv.TableCellMap → {图集名: {w,h,cells}}，cell 带矩形与 pivot。"""
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            if str(obj.read().m_Name) != "dc_scene_adv":
                continue
            t = obj.read_typetree()
        except Exception:                                      # noqa: BLE001
            continue
        out = {}
        for cm in t.get("TableCellMap") or []:
            name = str(cm.get("Name") or "")
            sz = cm.get("SizeOriginal") or {}
            cells = []
            for c in cm.get("TableCell") or []:
                r = c.get("Rectangle") or {}
                p = c.get("Pivot") or {}
                cells.append([
                    str(c.get("Name") or ""),
                    round(float(r.get("x") or 0), 1), round(float(r.get("y") or 0), 1),
                    round(float(r.get("width") or 0), 1), round(float(r.get("height") or 0), 1),
                    round(float(p.get("x") or 0), 1), round(float(p.get("y") or 0), 1),
                ])
            out[name] = {"w": round(float(sz.get("x") or 0), 1),
                         "h": round(float(sz.get("y") or 0), 1), "cells": cells}
        return out
    return {}


def export_atlas(env, tex_name, root, out_name):
    """把一张 Texture2D 存成无损 webp，返回 {src,w,h,bytes}。"""
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            d = obj.read()
        except Exception:                                      # noqa: BLE001
            continue
        if str(d.m_Name) != tex_name:
            continue
        out_dir = os.path.join(root, "web", "media", "img", "advfx")
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, out_name + ".webp")
        img = d.image
        img.save(p, "WEBP", lossless=True, quality=100)
        return {"src": f"media/img/advfx/{out_name}.webp",
                "w": img.width, "h": img.height,
                "bytes": os.path.getsize(p)}
    return None


def collect_marks(env):
    """抓出全部 da_mark_* 的 typetree。"""
    out = {}
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            name = str(obj.read().m_Name or "")
        except Exception:                                      # noqa: BLE001
            continue
        if not name.startswith(MARK_PREFIX):
            continue
        try:
            out[name[len(MARK_PREFIX):]] = obj.read_typetree()
        except Exception as e:                                 # noqa: BLE001
            print(f"  {name} typetree 读取失败：{e}")
    return out


def _status_vis(ap, total):
    """Status.Flags 的 **bit29 (0x20000000)** 是「隐藏」位。

    da_mark_leader 的三个圆点靠这条轨道依次出现：
      leader    keyframes 0(隐藏) → 20(显示) → 30
      leader_1  keyframes 0(隐藏) → 30(显示) → 40
      leader_2  keyframes 0(隐藏) → 40(显示)
    以前 build_mark 完全没读 Status，三个点从第 0 帧就全画出来
    （2026-11-10 用户：游戏里是依次出现，播放器一下子全出现）。
    TableCode 的低 13 位是帧号，与 TableValue 一一对应时按帧展开；
    长度不等（da_mark_troubled）保守跳过，避免错解。
    """
    st = ap.get("Status")
    if not isinstance(st, dict):
        return [1.0] * total
    vals = st.get("TableValue") or []
    codes = ((st.get("TableCodeValue") or [{}])[0].get("TableCode")) or []
    vis = [1.0] * total
    if not vals or len(codes) != len(vals):
        return vis
    keys = []
    for i, v in enumerate(vals):
        try:
            fr = int(codes[i]) & 0x1FFF
            fl = int((v or {}).get("Flags") or 0)
        except (TypeError, ValueError, IndexError):
            continue
        keys.append((max(0, min(fr, total - 1)), 0.0 if fl & 0x20000000 else 1.0))
    for ki, (fr, val) in enumerate(keys):
        end = keys[ki + 1][0] if ki + 1 < len(keys) else total
        for f in range(fr, min(end, total)):
            vis[f] = val
    return vis


def build_mark(tree, mark_cells, log, name):
    """把一个 da_mark_* 结构摊平成逐帧数据。

    坐标要在 Python 里算成世界坐标：部件的 Position/Scaling 是**相对父部件**的
    （impatience01 挂在 base_01 下面，父级做 0→1 的弹出缩放，子级必须跟着缩），
    前端只该做「按帧画图」这一件事，不该再实现一遍层级变换。
    """
    parts = tree.get("TableParts") or []
    # 同理：IDParent=0 就是挂在 root 上，`or -1` 会把它当成"没有父级"
    parent_of = {}
    for p in parts:
        pid = p.get("ID")
        par = p.get("IDParent")
        parent_of[-1 if pid is None else int(pid)] = -1 if par is None else int(par)
    anims = []
    for a in tree.get("TableAnimation") or []:
        total = int(a.get("CountFrame") or 0)
        if total <= 0:
            continue
        fps = int(a.get("FramePerSecond") or 30) or 30
        a_parts = a.get("TableParts") or []
        # 第一遍：取每个部件自己的逐帧局部值
        local = {}
        for pi, ap in enumerate(a_parts):
            bp = parts[pi] if pi < len(parts) else {}
            pid = int(bp.get("ID") or pi)
            pos = _track(ap, "Position", total) or [{"x": 0, "y": 0}] * total
            scl = _track(ap, "Scaling", total) or [{"x": 1, "y": 1}] * total
            opa = _track(ap, "RateOpacity", total) or [1.0] * total
            cvals = (ap.get("Cell") or {}).get("TableValue") or []
            cell = None
            if cvals:
                # 注意不能用 `IndexCell or -1`：**0 是合法 cell 号**（blush 就在 0），
                # `or` 会把它当成缺省值 → cell=None → 气泡里那张图整个消失。
                ci = cvals[0].get("IndexCell")
                ci = -1 if ci is None else int(ci)
                if 0 <= ci < len(mark_cells):
                    c = mark_cells[ci]
                    cell = c[1:7]
            # Priority 决定绘制先后（小 → 大，即先画底层）：sweat 的白气泡是 0、
            # 网格描边是 2、水滴是 7。不按它排序就会把白色底片盖在水滴上面。
            prv = (ap.get("Priority") or {}).get("TableValue") or []
            local[pid] = {
                "pid": pid,
                # 部件名（base_01 / base_00 / heart / impatience01 …）要带给前端：
                # 只有 base_* 这种"白底/描边"需要按 LabelColor 染色，
                # 内容部件（心/水滴/斜线）的图在 atlas 里**已经是成品颜色**，
                # 再乘一次顶点色会把红心染成紫心（2026-11-09 用户反馈）。
                "name": str(bp.get("Name") or ""),
                "pos": pos, "scl": scl, "opa": opa,
                "vis": _status_vis(ap, total),
                "cell": cell,
                "pr": int(prv[0]) if prv else 0,
                "col": _argb((bp.get("LabelColor") or {}).get("Color", {}).get("rgba")),
            }
        # 第二遍：逐帧合成世界坐标（parent 先算，链长一般 ≤ 2 层）
        cache = {}

        def world_of(pid, f):
            key = (pid, f)
            if key in cache:
                return cache[key]
            it = local[pid]
            lx = float(it["pos"][f].get("x") or 0)
            ly = float(it["pos"][f].get("y") or 0)
            ls = float(it["scl"][f].get("x") or 1)
            lo = float(it["opa"][f] if f < len(it["opa"]) else 1)
            # Status 隐藏位：父级隐藏会通过 po 传给子级
            lo *= (it["vis"][f] if f < len(it["vis"]) else 1)
            par = parent_of.get(pid, -1)
            if par in local and par != pid:
                px, py, ps, po = world_of(par, f)
                lx, ly = lx * ps + px, ly * ps + py
                ls *= ps
                lo *= po
            cache[key] = (lx, ly, ls, lo)
            return cache[key]

        # 绘制顺序：Priority 小的先画（底），相同再按部件 ID
        order = sorted(local, key=lambda i: (local[i]["pr"], i))
        a_parts_out = []
        for pid in order:
            it = local[pid]
            k = []
            for f in range(total):
                x, y, s, o = world_of(pid, f)
                k.append([round(x, 1), round(y, 1), round(s, 2), round(o, 2)])
            a_parts_out.append({"name": it["name"], "cell": it["cell"],
                                "col": it["col"], "k": k})
        anims.append({"name": str(a.get("Name") or ""), "fps": fps,
                      "n": total, "cx": 320.0, "parts": a_parts_out})
    if not anims:
        log(f"  {name} 没有时间轴，跳过")
    return {"anims": anims} if anims else None


def _depth(pid, parent_of, guard=8):
    d = 0
    while parent_of.get(pid, -1) >= 0 and d < guard:
        pid = parent_of[pid]
        d += 1
    return d


#: 画面特效（ShowScreenEffect）要用的贴图：导出时的键 → 包里的 Texture2D 名。
#:
#: 为什么只挑这几张：包里 81 张 Texture2D 有一半是同一 effect 的多级 mip（同名多份），
#: 真正进入画面的是这十几张 —— 依据是把每个 ``da_adv_effect_*`` 的
#: TableMaterial 追到 ``_MainTex`` 得到的（见 tools/_probe_effect_mats.py）。
#: 集中线四方向共用同一张白色三角形图，方向靠旋转实现，所以只导 up。
SCREFF_TEX = {
    # 集中線的真身：一根白色长三棱（上宽下尖），游戏里实例化几十根围成一圈、
    # 尖端朝中心。前端按这个拼，粗细才够（铺那张 1024² 的细线版几乎看不见）。
    "line": "adv_effect_concentration line_up_0",
    "lineWide": "ima_line",
    "illum1": "ima_illumination_01",   # 实心圆（整体发光底 / 回復）
    "illum2": "ima_illumination_02",   # 四芒星（光る / キラキラ）
    "illum3": "ima_illumination_03",   # 方形柔光（全体光晕）
    "illum4": "ima_illumination_04",   # 细十字星芒（斩击/刺突的闪光）
}


#: 帧图集 → 切一格导出。
#:
#: 这些贴图不是单张图，而是一张 PNG 里排着几十帧（爆炸 8×8、火柱 6×3…），
#: 整张贴上去画面会变成"一片小格子"。网格数是按 alpha 投影自动量出来的
#: （tools/_probe_atlas_grid.py），爆炸那张帧之间没有全透明间隔、自动量不准，
#: 按图目测是 8×8。
#:
#: 导出的不是动画序列而是**能量最高的一格**（alpha 总和最大 = 图最饱满的那帧）：
#: 播放帧序列要额外搞定时序，收益不大，而"图案正确"已经能解决 90% 的观感差。
SLICE_TEX = {
    "burst": ("adv_effect_attack_explosion_0", 8, 8),      # 爆発（64² 的白烟团）
    "hit": ("ef_hit_ima", 5, 2),                           # 打撃 / 斬撃（白色刀光）
    "smoke": ("adv_effect_cold_ima", 2, 2),                # 煙 / 冷気
    "snow": ("adv_effect_snow_loop_0", 1, 1),              # 雪（白点，平铺）
    "music": ("adv_effect_music_ima", 4, 2),               # 音楽（音符，平铺）
    "butterfly": ("adv_effect_butterfly_0", 4, 2),         # 蝶（平铺）
}

#: 一张图集拆成多个**互不相干的图案**（不是帧序列，不能只取一格）。
#: defo_effect 有 28 张同名贴图，内容几乎一样，都是 2×2 的通用光效：
#: 左上四芒星 / 右上是三点+锥形闪光 / 左下实心圆 / 右下放射光环。
#: 回復 用光环、キラキラ 用星芒、水しぶき 用实心圆。
MULTI_SLICE = {
    "defo_effect": (2, 2, ("star", "flash", "disc", "ring")),
}


def export_slices(env, root):
    """把帧图集切成单片（取 alpha 能量最高的一格）导成 webp。"""
    out_dir = os.path.join(root, "web", "media", "img", "advfx")
    os.makedirs(out_dir, exist_ok=True)
    want = {v[0]: (k, v[1], v[2]) for k, v in SLICE_TEX.items()}
    best = {}
    multi = {}
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            d = obj.read()
        except Exception:                                      # noqa: BLE001
            continue
        name = str(d.m_Name)
        if name in MULTI_SLICE:
            cols, rows, keys = MULTI_SLICE[name]
            if name not in multi:            # 只用第一张，28 张内容一样
                multi[name] = (d.image, cols, rows, keys)
            continue
        if name not in want:
            continue
        img = d.image
        prev = best.get(name)
        if prev is None or img.width * img.height > prev.width * prev.height:
            best[name] = img
    got = {}
    for name, (img, cols, rows, keys) in multi.items():
        w, h = img.size
        cw, ch = w // cols, h // rows
        for r in range(rows):
            for c in range(cols):
                cell = img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
                key = keys[r * cols + c]
                p = os.path.join(out_dir, key + ".webp")
                cell.save(p, "WEBP", lossless=True, quality=100)
                got[key] = {"src": f"media/img/advfx/{key}.webp",
                            "w": cell.width, "h": cell.height}
                print(f"  图案 {key}: {cell.width}×{cell.height}"
                      f" {os.path.getsize(p) / 1024:.0f} KB")
    for name, img in best.items():
        key, cols, rows = want[name]
        w, h = img.size
        cw, ch = w // cols, h // rows
        cells = []
        for r in range(rows):
            for c in range(cols):
                box = (c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)
                cell = img.crop(box)
                a = cell.convert("RGBA").getchannel("A")
                cells.append((sum(a.getdata()), cell))
        cells.sort(key=lambda t: t[0], reverse=True)
        cell = cells[0][1]
        p = os.path.join(out_dir, key + ".webp")
        cell.save(p, "WEBP", lossless=True, quality=100)
        got[key] = {"src": f"media/img/advfx/{key}.webp",
                    "w": cell.width, "h": cell.height, "cols": cols, "rows": rows}
        print(f"  切片 {key}: {cell.width}×{cell.height}（图集 {cols}×{rows}）"
              f" {os.path.getsize(p) / 1024:.0f} KB")
    return got


def export_screff_tex(env, root):
    """把画面特效用的贴图导成 webp，返回 ``{键: {src,w,h}}``。"""
    out_dir = os.path.join(root, "web", "media", "img", "advfx")
    os.makedirs(out_dir, exist_ok=True)
    want = {v: k for k, v in SCREFF_TEX.items()}
    best = {}
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            d = obj.read()
        except Exception:                                      # noqa: BLE001
            continue
        name = str(d.m_Name)
        key = want.get(name)
        if not key:
            continue
        img = d.image
        # 同名有多张时取**像素最多**的那张：ima_line 就有一个 8×128 的碎片
        # 和一个 1024×1024 的整图，拿错那张导出只有 614 字节，画面上什么都没有。
        prev = best.get(key)
        if prev is None or img.width * img.height > prev[0] * prev[1]:
            best[key] = (img.width, img.height, img)
    got = {}
    for key, (w, h, img) in best.items():
        if key == "lineWide":
            # 集中线的原图是**很淡的半透明线**（79% 全透明，12% 只有 1–64 的浅灰）。
            # 前端把它当 **alpha 遮罩 + 纯白填充** 用，所以这里要的是"线在 / 不在"
            # 二值 alpha —— 遮罩的不透明度就是 alpha，半透明线在明亮背景上照样看不见。
            # 阈值取 24（再低会把压缩噪点也点亮），最后轻微高斯软化边缘。
            rgba = img.convert("RGBA")
            # 亮度补偿：**先按峰值归一化，再做温和 gamma**，不拍魔数。
            #   · 原图 alpha 峰值只有 116（不是 255），主体线是 1–64，直接铺在
            #     明亮画面上只剩最外圈能看见；
            #   · 之前我拿"×2.2"这种自编系数去乘，峰值被压在 60%，所以中间段
            #     几乎看不见（用户 2026-11-09 指出"只有外围能看到"）；
            #   · 归一化保证最亮处一定到 255，gamma 0.7 只负责把暗部抬起来，
            #     曲线**保持单调递增** → "越靠中心越透明"的渐变不会被压平。
            # 实测（原值 → 输出）：3→19  20→74  40→120  116→255
            # **原样导出，不动 alpha**。
            # 之前用 gamma（0.25 / 0.45）提亮，会把空洞边界那一圈的 alpha 从 5 抬到
            # 40 左右 —— 线到中心本来是渐隐到"几乎全透明"的，抬起来之后
            # 末端变成"40 突然断到 0"，视觉上就是被椭圆硬切一刀（用户 2026-11-09 指出）。
            # 亮度问题交给前端"同图叠多层 + screen 合成"解决：
            # 低 alpha 叠 N 层后是 1-(1-a)^N，中间段能提上去，而 a=0 永远是 0，
            # 尖端依然尖。
            img = rgba
        p = os.path.join(out_dir, key + ".webp")
        img.save(p, "WEBP", lossless=True, quality=100)
        got[key] = {"src": f"media/img/advfx/{key}.webp", "w": img.width, "h": img.height}
        print(f"  特效贴图 {key}: {img.width}×{img.height} {os.path.getsize(p) / 1024:.0f} KB")
    return got


# =================================================================== 画面特效
# 每个画面特效在包里是三个结构：
#   da_adv_effect_X   逐帧动画（Position / Scaling / Rotation / RateOpacity / Cell / Priority）
#   dc_adv_effect_X   **这个特效专属**的 TableCellMap（用哪张贴图的哪一格）
#   de_ef_*_P         粒子发射器（暂不导出）
# 之前一直错把 dc_scene_adv 的 4 张 cellmap（表情 mark 用的）套在画面特效上，
# 于是 1024² 的 ima_line 放射线被当成光斑，进而误判"映射不到、只能猜"。
FX_DA = "da_adv_effect_"
FX_DC = "dc_adv_effect_"

# {prefab: (cellmap base key, 变体 tag, 材质 _MainTex 的 Texture2D path_id)}
#
# 同一个 cellmap 名在包里对应**多张同名但内容不同**的贴图，导出脚本原来
# 一律取"名字+尺寸匹配的第一张"。用 audit_atlas.py 逐个比对每个 prefab 的
# TableMaterial._MainTex path_id 后，确认以下 8 个 prefab 拿错了图。
# 这里按 path_id 精确换回它自己的那份；其余 91 个 prefab 的材质贴图与
# "第一张"像素完全相同，保持原样。
FX_TEX_VARIANTS = {
    # adv_effect_cold_ima 256×256 共 5 张
    "image_loop":      ("adv_effect_cold_cell|256X256", "WHITE",  -3039398099843708632),
    "image_bk_loop":   ("adv_effect_cold_cell|256X256", "WHITE",  -3039398099843708632),
    "jaki_loop":       ("adv_effect_cold_cell|256X256", "PURPLE", -6294525184241736296),
    # defo_effect_cell 256×256：回復系变体与水しぶき
    "heal_inori":      ("defo_effect_cell|256X256", "INORI",  -3686832131538954647),
    "heal_inori_all":  ("defo_effect_cell|256X256", "INORI",   7230408365795018398),
    "heal_raika":      ("defo_effect_cell|256X256", "RAIKA",  -2110529091273563547),
    "heal_raika_all":  ("defo_effect_cell|256X256", "RAIKA",  -4393030118571088403),
    "attack_water":    ("defo_effect_cell|256X256", "WATER",   2769080387878643027),
}


def _fx_tex_name(cm_name, size, tex):
    """cellmap 名 → 包里真正的 Texture2D 名。

    有的 cellmap 叫 ``defo_effect_cell`` 而贴图叫 ``defo_effect``；
    有的叫 ``adv_effect_cold_cell`` 而贴图叫 ``adv_effect_cold_ima``。
    因此优先按名字，其次按 SizeOriginal 精确匹配尺寸。
    """
    if cm_name in tex:
        return cm_name
    plain = cm_name.replace("_cell", "")
    for cand in (plain + "_ima", plain, plain.replace("_0", "")):
        if cand in tex:
            return cand
    want = (int(size[0]), int(size[1]))
    same = [n for n, im in tex.items() if im.size == want]
    if not same:
        return None
    stem = plain.split("_")[0]
    for n in same:
        if stem and stem in n:
            return n
    return same[0]


def export_fx_atlas(env, root, extra=None):
    """导出所有画面特效 cellmap 用到的整张贴图，返回 {cellmap名: {src,w,h}}。

    不切格子：前端用 background-position 定位 cell，和表情图集同理。
    ``extra`` 是额外的 dc_ 同构结构（Instance 子 prefab 的 DataCellMap）——
    它们不在 dc_adv_effect_* 命名空间里，但对火柱/爆発/音楽 的贴图同样是必需的。
    """
    out_dir = os.path.join(root, "web", "media", "img", "advfx")
    os.makedirs(out_dir, exist_ok=True)
    tex_all = {}
    tex_pids = {}
    for obj in env.objects:
        if obj.type.name == "Texture2D":
            try:
                d = obj.read()
            except Exception:                                      # noqa: BLE001
                continue
            # 同名贴图在包里有多份、尺寸不同（defo_effect 有 256² 和 512×256）。
            # 以前一律取"像素最多"的那张，但 cell 表的 Rectangle 是按 cellmap
            # 的 SizeOriginal 算的 → 取错尺寸就会把别的格子当成本格，
            # heal / attack 全变成白羽毛和三角形（2026-11-09 用户截图）。
            tex_all.setdefault(str(d.m_Name), []).append(d.image)
            tex_pids.setdefault(str(d.m_Name), []).append((obj.path_id, d.image))
    tex = {k: v[0] for k, v in tex_all.items()}

    got, safe_cache = {}, {}
    dc_trees = []
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            n = str(obj.read().m_Name or "")
            if not n.startswith(FX_DC):
                continue
            dc_trees.append(obj.read_typetree())
        except Exception:                                          # noqa: BLE001
            continue
    dc_trees.extend(extra or [])
    for t in dc_trees:
        for cm in t.get("TableCellMap") or []:
            cmn = str(cm.get("Name") or "")
            sz = cm.get("SizeOriginal") or {}
            want_size = (int(sz.get("x") or 0), int(sz.get("y") or 0))
            # 同一个 cellmap 名在不同 prefab 里是**两种不同尺寸的不同图**：
            #   adv_effect_cold_cell 在 image_loop/cold/image_bk_loop 是 256×256
            #   （8 个小格：kemuri / tama / hinoko / all_ef / kiri / all），
            #   在 smoke_loop 是 512×256（2 个大格：kemuri / kemuri_r）。
            # 以前按 cellmap 名去重（`if cmn in got: continue`），先处理的那种
            # 尺寸赢，另一族就拿着错图切片 —— 雾/球/火花全是从另一张图上抠出来的，
            # 这就是空想/空想黒/冷気/煙「完全不一样」的根因（2026-11-10）。
            # 现在按 (名字, SizeOriginal) 区分，各自导一份图。
            akey = "%s|%dX%d" % (cmn, want_size[0], want_size[1])
            if akey in got:
                continue
            key = _fx_tex_name(cmn, want_size, tex)
            if not key:
                print(f"  [warn] 找不到贴图：{cmn} {want_size}")
                continue
            # 必须挑**与 SizeOriginal 完全一致**的那一份，cell 矩形才对得上
            cands = tex_all.get(key) or []
            chosen = next((im for im in cands if im.size == want_size), None)
            if chosen is None and want_size[0] > 0:
                chosen = next((im for ims in tex_all.values() for im in ims
                               if im.size == want_size), None)
            if chosen is None:
                chosen = max(cands, key=lambda im: im.width * im.height,
                             default=None)
            if chosen is None:
                continue
            if chosen.size != want_size:
                print(f"  [warn] {cmn}: 没有 {want_size} 的同名贴图，"
                      f"用了 {chosen.size}")
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in cmn)
            safe = "%s_%dx%d" % (safe, chosen.width, chosen.height)
            p = os.path.join(out_dir, "fx_" + safe + ".webp")
            # 总是重写：旧版可能存着错误尺寸的同名图，跳过会让修复不生效
            chosen.save(p, "WEBP", lossless=True, quality=100)
            cells = []
            for c in cm.get("TableCell") or []:
                r = c.get("Rectangle") or {}
                pv = c.get("Pivot") or {}
                cells.append([round(float(r.get("x") or 0), 1),
                              round(float(r.get("y") or 0), 1),
                              round(float(r.get("width") or 0), 1),
                              round(float(r.get("height") or 0), 1),
                              round(float(pv.get("x") or 0), 1),
                              round(float(pv.get("y") or 0), 1)])
            got[akey] = {"src": f"media/img/advfx/fx_{safe}.webp",
                         "w": chosen.width, "h": chosen.height,
                         "from": key, "cells": cells}
            # 兜底：按纯名字也能查到（尺寸对不上时才会用到）
            got.setdefault(cmn, got[akey])
            safe_cache[key] = cmn

    # 同名 adv_effect_cold_ima 的 256×256 在包里有 5 张，内容完全不同：
    #   · 2 张 cell4(all_ef) 是 #060F54→#2387D1 的**不透明深蓝渐变**
    #   · 1 张是紫色渐变
    #   · 2 张 cell4 是**纯白、alpha≈89 的半透明雾**，cell2(tama) 也是纯白
    # 材质归属（按 TableMaterial._MainTex 的 path_id 查）：
    #   image_loop      → -3039398099843708632 (WHITE)
    #   image_bk_loop   →  1117286665339571914 (WHITE，与上面像素完全相同)
    #   cold            →  3651059120892410902 (BLUE)
    #   jaki_loop       → -6294525184241736296 (PURPLE)
    # 上面按名字取了第一张(BLUE)，于是空想的 all_ef 变成一整块深蓝
    # 50% 普通混合盖满全屏 → 玩家端整屏压暗约 100 亮度（2026-11-10 用户截图）。
    # 这里额外把白色那份导出来，main() 里只给两个空想 prefab 换上，
    # 冷気/邪気 已定版，保持原来的图不动。
    # 按 FX_TEX_VARIANTS 把"材质实际引用的那份"单独导出，供 main() 按 prefab 换上。
    # 直接从 Texture2D 的 path_id 取对象，不再靠同名猜测。
    tex_by_pid = {o.path_id: o for o in env.objects if o.type.name == "Texture2D"}
    done = set()
    for pf, (base, tag, pid) in FX_TEX_VARIANTS.items():
        vkey = base + "|" + tag
        if vkey in done or base not in got:
            continue
        obj = tex_by_pid.get(pid)
        if obj is None:
            print("  [warn] 变体贴图 path_id 不存在：%s %s" % (pf, pid))
            continue
        try:
            td = obj.read()
            im = td.image
        except Exception:                                          # noqa: BLE001
            continue
        cmn, dim = base.split("|", 1)
        safe = "%s_%s_%s" % (cmn, dim.lower(), tag.lower())
        im.convert("RGBA").save(os.path.join(out_dir, "fx_" + safe + ".webp"),
                                "WEBP", lossless=True, quality=100)
        one = dict(got[base])
        one["src"] = "media/img/advfx/fx_" + safe + ".webp"
        got[vkey] = one
        done.add(vkey)
        print("  [fix] 按材质导变体贴图 %s ← %s (@%s)" % (vkey, pf, pid))
    return got


def make_pc_image(vcs, rate_alpha, base_img, out_dir, key, op=0, bound=2, uniform=False,
                  keep_color=False):
    """把 PartsColor 的 4 个顶点色按**四角双线性插值**烘成一张滤镜图。

    依据 APK 的 IL2CPP metadata（不是猜）：
      · 四角字段名顺序 = colorLU / colorRU / colorRD / colorLD（顺时针），
        所以上排 LU→RU、下排 LD→RD；以前按列优先 LT/LB/RT/RB 是错的。
      · KindBoundBlend   = OVERALL / VERTEX：bound=1 四角同色、bound=2 用四角。
      · KindOperationBlend = MIX / ADD / SUB / MUL / MUL_NA / SCR / EXC / INV，
        Operation 就是它的下标：0=MIX, 1=ADD, 2=SUB, 3=MUL。
      · RateAlpha 是每顶点的 alpha 系数，同样双线性插值。
    """
    from PIL import Image

    w, h = base_img.size
    rgba = base_img.convert("RGBA")
    a = rgba.getchannel("A")
    cols = [[float(v.get(k) or 0) for k in ("r", "g", "b", "a")]
            for v in vcs[:4]]
    while len(cols) < 4:
        cols.append(cols[-1][:])
    if bound == 1:                    # OVERALL：整块一个色
        cols = [cols[0][:]] * 4
    # 顺时针 LU,RU,RD,LD → 便于下面按 上排/下排 插值
    lu, ru, rd, ld = cols[0], cols[1], cols[2], cols[3]
    ra = [float(x) for x in (rate_alpha or [1, 1, 1, 1])[:4]]

    def lerp(p, q, t):
        return [p[i] + (q[i] - p[i]) * t for i in range(4)]

    out = Image.new("RGBA", (w, h))
    px = out.load()
    ap = a.load()
    bp = rgba.load()
    for y in range(h):
        ty = y / max(1, h - 1)
        for x in range(w):
            tx = x / max(1, w - 1)
            # 上排 LU→RU，下排 LD→RD，再按 ty 上下插值
            up = lerp(lu, ru, tx)
            dn = lerp(ld, rd, tx)
            c = lerp(up, dn, ty)
            a_up = ra[0] + (ra[1] - ra[0]) * tx
            a_dn = ra[3] + (ra[2] - ra[3]) * tx
            alpha = a_up + (a_dn - a_up) * ty
            b = bp[x, y]
            br, bg, bb = b[0] / 255, b[1] / 255, b[2] / 255
            if op == 3:                 # 官方 sstypes.h: 3 = sub（base - tint）
                # 依据 SpriteStudio6 SDK 头文件 SsBlendType:
                #   mix=0, mul=1, add=2, sub=3, mulalpha=4, screen=5, ...
                #
                # `all_ef` 走 keep_color 分支：实测 sub 能把绿通道压到游戏值
                # （g 150→99，游戏 95），但同时把 r/b 一起压掉
                # （(102,99,129) vs 游戏 (139,95,229)），而且冷气会从
                # 「(131,153,207) 对游戏 (134,154,207)」变偏。
                # 两个 prefab 参数完全相同，游戏里却一个 g 升、一个 g 降，
                # 说明两张对照帧不是同一条件 —— 在拿到同场景基线前，
                # 先回到「保留图集原色 + 加色」这一版（冷气准、邪气偏蓝）。
                if keep_color:
                    # 三种读法都实测过（四角 48×64 框，冷气目标 (116,138,196)）：
                    #   1) Tex − V          → (34,48,94)   整体压成黑蓝
                    #   2) Tex × (1 − V)    → (34,84,118)  比 1 亮，b 仍不足
                    #   3) 保留贴图原色      → (34,114,192) b 最高，最接近目标
                    # 这里取 3：图集原色本身就是美术画好的紫/蓝渐变，
                    # 顶点色 (0,0.259,0.384) 更像"整体色调标记"而非逐通道运算。
                    rgb = (br, bg, bb)
                else:
                    rgb = (c[0] * br, c[1] * bg, c[2] * bb)
            elif op == 1:               # ADD
                rgb = (c[0] + br, c[1] + bg, c[2] + bb)
            elif op == 2:               # SUB
                rgb = (br - c[0], bg - c[1], bb - c[2])
            else:                       # MIX (0)：按顶点色 alpha 与贴图色混合
                t = max(0.0, min(1.0, c[3]))
                rgb = (br + (c[0] - br) * t,
                       bg + (c[1] - bg) * t,
                       bb + (c[2] - bb) * t)
            rr = max(0.0, min(1.0, rgb[0]))
            rg = max(0.0, min(1.0, rgb[1]))
            rb = max(0.0, min(1.0, rgb[2]))
            av = ap[x, y] * c[3] * alpha
            if op == 3 and not uniform:
                # 乘算后接近黑 = 不发光。这类部件（heal_* 的 obi 全屏光带）
                # 在游戏里按加色叠加，黑色不产生任何光；但 #fxLayer 是独立
                # stacking context，plus-lighter 穿不到舞台背景，黑块会直接
                # 盖住画面 → 整屏压暗发紫（2026-11-09 用户截图）。
                # 把 alpha 乘上亮度，黑区透明，和加色的观感一致。
                # bound=1（整块一个色）例外：它的语义就是「纯色 × 贴图 alpha」，
                # 空的顶点色(0,0,0)是黑色雾（空想／黒），压成 0 就什么都没有了。
                av *= max(rr, rg, rb)
            px[x, y] = (int(rr * 255), int(rg * 255), int(rb * 255), int(av))
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)
    p = os.path.join(out_dir, "fx_pc_" + safe + ".webp")
    out.save(p, "WEBP", lossless=True, quality=100)
    return f"media/img/advfx/fx_pc_{safe}.webp"


def load_pe_payloads(env, by_id=None):
    """`{pe_ GameObject path_id: (de_ CPE tree, dc_ cellmap tree)}`。

    部件（effect_*）的 `PrefabUnderControl` 指向一个 **GameObject**（`pe_ef_xxx_P`），
    这个 GameObject 的 `Script_SpriteStudio6_RootEffect` 组件里写着
    `DataEffect`（具体哪一个 `de_ef_xxx_P`）与 `DataCellMap`（具体哪一个 dc_）。
    同名 `de_` 在包里有多份（煙/tama 各 3 份，参数完全不同），
    只有按 path_id 才能取对。
    """
    by_id = by_id or {o.path_id: o for o in env.objects}
    out = {}
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            tr = obj.read_typetree()
        except Exception:                                          # noqa: BLE001
            continue
        de_pid = (tr.get("DataEffect") or {}).get("m_PathID")
        if not de_pid:
            continue
        go_pid = (tr.get("m_GameObject") or {}).get("m_PathID")
        dc_pid = (tr.get("DataCellMap") or {}).get("m_PathID")
        de = by_id.get(de_pid)
        if not go_pid or not de:
            continue
        try:
            de_tr = de.read_typetree()
        except Exception:                                          # noqa: BLE001
            continue
        dc_tr = None
        dc = by_id.get(dc_pid) if dc_pid else None
        if dc is not None:
            try:
                dc_tr = dc.read_typetree()
            except Exception:                                      # noqa: BLE001
                dc_tr = None
        out[go_pid] = (de_tr, dc_tr)
    return out


def _fix_emit_cells(fx, atlas):
    """粒子 IndexCell 越界时按格数取模。

    煙（smoke_loop）的 dc_ 只声明一张 512×256 / 2 格表（0=kemuri、1=kemuri_r），
    而它的发射器写的是 IndexCell=4 —— 这张表根本没有第 5 格。越界时不处理会让
    前端退回"整张贴图"，煙变成一整块橙方块。
    引擎这边只能是取模（4 % 2 = 0 → kemuri，正好是那团大烟雾）；
    换成 image_loop/cold 那张 8 格表（256×256）反而更错：4 号是 all_ef，
    那是一块**不透明的渐变满屏色块**，当粒子用就是一堆硬边矩形。
    """
    for p in fx.get("particles") or []:
        cells = p.get("cells") or []
        ci = int(p.get("cell") or 0)
        if not cells or ci < len(cells):
            continue
        ci2 = ci % len(cells)
        p["cell"] = ci2
        print(f"  [fix] {p.get('from')} IndexCell={ci} 越界（{len(cells)} 格）"
              f" → 取模 {ci2}")


def _fx_cms(dc, atlas):
    """dc_ 的 TableCellMap → 前端用的 maps（贴图信息 + cell 矩形）。"""
    cms = []
    for cm in (dc.get("TableCellMap") or []):
        name = str(cm.get("Name") or "")
        sz = cm.get("SizeOriginal") or {}
        iw = float(sz.get("x") or 0)
        ih = float(sz.get("y") or 0)
        # atlas 的键带尺寸（同名 cellmap 有 256×256 / 512×256 两份图）
        tex = atlas.get("%s|%dX%d" % (name, int(iw), int(ih))) or atlas.get(name)
        tw = float((tex or {}).get("w") or iw)
        th = float((tex or {}).get("h") or ih)
        cells = []
        for c in cm.get("TableCell") or []:
            r = c.get("Rectangle") or {}
            pv = c.get("Pivot") or {}
            cells.append([round(float(r.get("x") or 0), 1),
                          round(float(r.get("y") or 0), 1),
                          round(float(r.get("width") or 0), 1),
                          round(float(r.get("height") or 0), 1),
                          round(float(pv.get("x") or 0), 1),
                          round(float(pv.get("y") or 0), 1)])
        cms.append({"name": name, "w": round(tw, 1), "h": round(th, 1),
                    "tex": tex, "cells": cells})
    return cms


def _pc_payload(ap, cms, mi, ci, root, key, part_name=""):
    """PartsColor → (顶点色表, 元数据, 烘好的四角渐变图)。

    Bound=2（四角不同色）必须烘图：火焰/音乐光带的贴图本身是**灰白**的
    （实测 cell 可见像素 (176,176,176)），全靠 红→黄 的四角顶点色染出火色；
    不烘就只是一片白灰。Bound=1（四角同色）不烘，前端用「贴图当遮罩 + 纯色」。
    """
    if not ap:
        return [], None, None
    vcs = ap.get("VertexColor") or []
    pcs = [[round(float(v.get("r") or 0), 3), round(float(v.get("g") or 0), 3),
            round(float(v.get("b") or 0), 3), round(float(v.get("a") or 0), 3)]
           for v in vcs]
    pcm = {"bound": int(ap.get("Bound") or 0),
           "op": int(ap.get("Operation") or 0),
           "ra": [float(x) for x in (ap.get("RateAlpha") or [])]}
    src = None
    if (vcs and root and mi < len(cms) and ci < len(cms[mi]["cells"])):
        tinfo = cms[mi].get("tex")
        if tinfo:
            try:
                from PIL import Image
                c = cms[mi]["cells"][ci]
                base = Image.open(os.path.join(
                    root, "web", tinfo["src"].replace("/", os.sep))).convert("RGBA")
                cell_img = base.crop((int(c[0]), int(c[1]),
                                      int(c[0] + c[2]), int(c[1] + c[3])))
                out_dir = os.path.join(root, "web", "media", "img", "advfx")
                os.makedirs(out_dir, exist_ok=True)
                src = make_pc_image(vcs, ap.get("RateAlpha"), cell_img, out_dir,
                                    key, pcm["op"], pcm["bound"],
                                    uniform=(pcm["bound"] == 1),
                                    keep_color=(part_name == "all_ef"))
                # 同时给一份「上/下 端色」，前端加色部件改用 CSS 渐变还原：
                # 实测 Chromium 里 **位图 background-image 不参与 blend**
                # （同一元素换成 background-color 或 linear-gradient 就正常），
                # 而冷気/邪気 的满屏冷色滤镜正是加色位图 —— 以前整块等于没画。
                try:
                    # 取**烘焙后**结果的上下两端色（含顶点色与 Operation），不是贴图原色
                    baked = Image.open(os.path.join(root, "web", src.replace("/", os.sep))).convert("RGBA")
                    bw, bh = baked.size
                    pcm["grad"] = [
                        list(baked.crop((0, 0, bw, 1)).resize((1, 1), Image.BILINEAR).getpixel((0, 0))),
                        list(baked.crop((0, bh - 1, bw, bh)).resize((1, 1), Image.BILINEAR).getpixel((0, 0))),
                    ]
                    # 加色部件的「形状」：alpha 全不透明时（冷気/邪気 的 all_ef），
                    # 空间分布全在 RGB 亮度里 —— 中心近黑≈不加色、下缘亮≈加色。
                    # 只取上下端色做 linear-gradient 会把中心填平 → 全屏罩
                    # （2026-11-10 用户截图：贴图本来就是中空、四周有）。
                    # 这里额外导一张「亮度→alpha」的遮罩，前端渐变 + mask 还原形状。
                    # alpha 本身有形状的（キラキラ２ 的画框）不生成，继续用原 alpha。
                    from PIL import ImageChops
                    if baked.getchannel("A").getextrema()[0] >= 250:
                        r_, g_, b_, _a = baked.split()
                        luma = ImageChops.lighter(ImageChops.lighter(r_, g_), b_)
                        mx = luma.getextrema()[1]
                        if mx > 8:                            # 有亮度变化才值得导
                            # mask 只表示**形状**（中心空 / 四周有），
                            # 上下亮度差交给 linear-gradient，否则会算两遍：
                            #   直接拿亮度当 alpha（0~80）→ 整层被乘掉一次（+14→+1）；
                            #   归一化后四边仍只有 25%~100%，上缘被再削一次。
                            # 这里二值化 + 羽化，得到「中心 0、四周 255」的柔边蒙版。
                            luma = luma.point(lambda v: 255 if v > 10 else 0)
                            try:
                                from PIL import ImageFilter
                                luma = luma.filter(ImageFilter.GaussianBlur(
                                    max(1, baked.size[0] // 64)))
                            except Exception:              # noqa: BLE001
                                pass
                            mask = Image.new("RGBA", baked.size, (255, 255, 255, 0))
                            mask.putalpha(luma)
                            msafe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                                            for ch in key)
                            mname = "fx_pcmask_" + msafe + ".webp"
                            mask.save(os.path.join(out_dir, mname),
                                      "WEBP", lossless=True, quality=100)
                            pcm["mask"] = "media/img/advfx/" + mname
                except Exception:                              # noqa: BLE001
                    pass
            except Exception as e:                             # noqa: BLE001
                print(f"  [warn] PartsColor 图生成失败 {e} / {key}")
    return pcs, pcm, src


def _flatten_seg(struct, seg, cms, root=None, keyprefix=""):
    """把一段 TableAnimation 摊平成逐帧部件（与 build_fx 同一套世界坐标合成）。

    Instance 子 prefab 的部件和 da_ 的部件是**同一套字段**，这里必须一样全：
    以前只带 m/c/k，丢掉了 Feature、混合模式、部件名和 PartsColor ——
    火柱的 16 个火焰部件因此既没有染色（灰白火苗）也没有加色混合。
    """
    parts = struct.get("TableParts") or []
    aps = seg.get("TableParts") or []
    total = int(seg.get("CountFrame") or 0)
    if total <= 0:
        return None
    parent = {}
    for p in parts:
        parent[-1 if p.get("ID") is None else int(p["ID"])] = int(p.get("IDParent") or -1)
    local = {}
    for pi, ap in enumerate(aps):
        bp = parts[pi] if pi < len(parts) else {}
        pid = int(bp.get("ID") or pi)
        cv = ((ap.get("Cell") or {}).get("TableValue") or [])
        cell = None
        if cv and isinstance(cv[0], dict):
            ci, mi = cv[0].get("IndexCell"), cv[0].get("IndexCellMap")
            cell = [-1 if ci is None else int(ci), -1 if mi is None else int(mi)]
        local[pid] = {
            "pos": _track(ap, "Position", total) or [{"x": 0, "y": 0}] * total,
            "scl": _track(ap, "Scaling", total) or [{"x": 1, "y": 1}] * total,
            "rot": _track(ap, "Rotation", total) or [{"x": 0, "y": 0, "z": 0}] * total,
            "opa": _track(ap, "RateOpacity", total) or [1.0] * total,
            "pri": _track(ap, "Priority", total) or [0] * total,
            "cell": cell,
            "pc": (ap.get("PartsColor") or {}).get("TableValue") or [],
            "blend": int(bp.get("OperationBlendTarget") or 0),
            "feature": int(bp.get("Feature") or 0),
            "name": str(bp.get("Name") or ""),
        }

    def world(pid, f, guard=0):
        it = local.get(pid)
        if it is None or guard > 6:
            return 0.0, 0.0, 1.0, 1.0, 0.0, 1.0
        wx, wy, wsx, wsy, wr, wo = world(parent.get(pid, -1), f, guard + 1)
        p, s, r = it["pos"][f], it["scl"][f], it["rot"][f]
        sx, sy = float(s.get("x") or 1), float(s.get("y") or 1)
        return (wx + float(p.get("x") or 0) * wsx, wy + float(p.get("y") or 0) * wsy,
                wsx * sx, wsy * sy, wr + float(r.get("z") or 0),
                wo * float(it["opa"][f]))

    out = []
    for pid in sorted(local, key=lambda x: (local[x]["pri"][0], x)):
        it = local[pid]
        if it["cell"] is None:
            continue
        ci, mi = it["cell"]
        if mi < 0 or mi >= len(cms) or ci < 0 or ci >= len(cms[mi]["cells"]):
            continue
        k = []
        for f in range(total):
            x, y, sx, sy, rot, op = world(pid, f)
            k.append([round(x, 1), round(y, 1), round(sx, 3),
                      round(sy, 3), round(rot, 1), round(op, 3)])
        pcs, pcm, pc_src = _pc_payload(
            (it["pc"][0] if it["pc"] else None), cms, mi, ci, root,
            f"{keyprefix}_{pid}", it["name"])
        out.append({"m": mi, "c": ci, "k": k, "pr": int(it["pri"][0] or 0),
                    "f": it["feature"], "nm": it["name"], "bl": it["blend"],
                    "pc": pcs, "pcm": pcm, "pcSrc": pc_src})
    return {"n": total, "fps": int(seg.get("FramePerSecond") or 30) or 30,
            "parts": out}


def load_instance_prefabs(env):
    """收集所有 **Instance 预制体**（GameObject + 它自己的动画与 cellmap）。

    Feature=3 的部件 ``PrefabUnderControl`` 指向的是 **GameObject**（例如
    ``fire_01_ins`` / ``adv_effect_slash``），不是 ``da_adv_effect_*`` 结构。
    那个 GameObject 上挂的 MonoBehaviour 里：
      · ``DataAnimation`` → 一份 da_ 同构的动画（TableParts / TableAnimation）
      · ``DataCellMap``   → 一份 dc_ 同构的 cell 表（**子 prefab 自己的贴图**）
      · ``NameAnimationUnderControl`` 从它里面挑一段动画名（'ins_fire_01'）
    以前按 ``da_adv_effect_ + 动画名`` 去查，只有名字恰好等于
    'slash'/'blow' 的能查到；火柱/爆発/音楽 的 'ins_fire_01'/'line'
    查不到，16 个火焰部件、5 条音乐光带全部丢掉，画面里只剩粒子。
    返回 {GameObjectPathID: (anim_tree, cellmap_tree|None)}。
    """
    by_id = {o.path_id: o for o in env.objects}
    out = {}
    for o in env.objects:
        if o.type.name != "GameObject":
            continue
        try:
            gd = o.read_typetree()
        except Exception:                                      # noqa: BLE001
            continue
        for c in gd.get("m_Component") or []:
            pid = (c.get("component") or {}).get("m_PathID")
            co = by_id.get(pid)
            if not co or co.type.name != "MonoBehaviour":
                continue
            try:
                t = co.read_typetree()
            except Exception:                                  # noqa: BLE001
                continue
            if "DataAnimation" not in t:
                continue
            da_ref = (t.get("DataAnimation") or {}).get("m_PathID")
            dc_ref = (t.get("DataCellMap") or {}).get("m_PathID")
            da_t = dc_t = None
            if da_ref and da_ref in by_id:
                try:
                    da_t = by_id[da_ref].read_typetree()
                except Exception:                              # noqa: BLE001
                    da_t = None
            if dc_ref and dc_ref in by_id:
                try:
                    dc_t = by_id[dc_ref].read_typetree()
                except Exception:                              # noqa: BLE001
                    dc_t = None
            if da_t:
                out[o.path_id] = (da_t, dc_t)
            break
    return out


def build_instances(da, dc, trees, atlas, root, inst_prefabs=None):
    """Feature=3 的 Instance 部件：实例化另一个 prefab 的一段动画。

    例：火柱(attack_fire_pillar) 的 16 个 ``fire_0X_ins_ins_fire_0Y`` 部件
    各自 ``PrefabUnderControl → fire_00_ins``、``NameAnimationUnderControl
    = 'ins_fire_01'``；``fire_00_ins`` 的 DataAnimation 里正好有这一段 17 帧动画，
    DataCellMap 是**它自己的** ``adv_effect_attack_explosion_all_0``。
    """
    host_cms = _fx_cms(dc, atlas)
    inst_prefabs = inst_prefabs or {}
    a0 = (da.get("TableAnimation") or [{}])[0]
    aps = a0.get("TableParts") or []
    total = int(a0.get("CountFrame") or 0)
    out = []
    for pi, (p, ap) in enumerate(zip(da.get("TableParts") or [], aps)):
        if int(p.get("Feature") or 0) != 3:
            continue
        anim_name = str(p.get("NameAnimationUnderControl") or "")
        if not anim_name:
            continue
        go_id = (p.get("PrefabUnderControl") or {}).get("m_PathID")
        child = cms = None
        if go_id and go_id in inst_prefabs:
            child, dc_sub = inst_prefabs[go_id]
            if dc_sub:
                cms = _fx_cms(dc_sub, atlas)
        if child is None:                    # 退回旧路径：名字直接是 da_ 结构名
            child = trees.get("da_adv_effect_" + anim_name)
        if not child:
            continue
        seg = next((x for x in (child.get("TableAnimation") or [])
                    if str(x.get("Name") or "") == anim_name), None)
        if seg is None and len(child.get("TableAnimation") or []) == 1:
            seg = (child.get("TableAnimation") or [None])[0]
        if seg is None:
            continue
        sub = _flatten_seg(child, seg, cms or host_cms, root, anim_name)
        if not sub or not sub["parts"]:
            continue
        sub["maps"] = cms or host_cms
        # Instance 部件自己的逐帧变换（子动画要挂在它下面）
        pos = _track(ap, "Position", total) or [{"x": 0, "y": 0}] * total
        scl = _track(ap, "Scaling", total) or [{"x": 1, "y": 1}] * total
        rot = _track(ap, "Rotation", total) or [{"x": 0, "y": 0, "z": 0}] * total
        opa = _track(ap, "RateOpacity", total) or [1.0] * total
        # Instance 轨道：PlayCount / RateTime(播放速率) / LabelStart-End(播放区间)
        iplay = ((ap.get("Instance") or {}).get("TableValue") or [])
        iplay = iplay[-1] if iplay else {}
        hk = []
        for f in range(total):
            hk.append([round(float(pos[f].get("x") or 0), 1),
                       round(float(pos[f].get("y") or 0), 1),
                       round(float(scl[f].get("x") or 1), 3),
                       round(float(scl[f].get("y") or 1), 3),
                       round(float(rot[f].get("z") or 0), 1),
                       round(float(opa[f]), 3)])
        out.append({"anim": anim_name, "host": hk, "sub": sub,
                    "rate": float(iplay.get("RateTime") or 1) or 1.0,
                    "play": int(iplay.get("PlayCount") or 0)})
    return out


def build_fx(da, dc, atlas, root, trees=None, inst_prefabs=None):
    """把一个 da_adv_effect_* 摊平成逐帧部件表（坐标已在 Python 里合成世界坐标）。"""
    parts = da.get("TableParts") or []
    parent = {}
    for p in parts:
        pid = p.get("ID")
        par = p.get("IDParent")
        parent[-1 if pid is None else int(pid)] = -1 if par is None else int(par)
    instances = build_instances(da, dc, trees or {}, atlas, root, inst_prefabs)

    # 统一走 _fx_cms：它会把 cell 坐标按"实际贴图尺寸 / SizeOriginal"缩放
    # （adv_effect_cold_cell 这类 2 倍分辨率贴图，坐标不缩放会取到别的区域）
    cms = _fx_cms(dc, atlas)

    anims = []
    for a in da.get("TableAnimation") or []:
        total = int(a.get("CountFrame") or 0)
        if total <= 0:
            continue
        fps = int(a.get("FramePerSecond") or 30) or 30
        aps = a.get("TableParts") or []
        local = {}
        for pi, ap in enumerate(aps):
            bp = parts[pi] if pi < len(parts) else {}
            pid = int(bp.get("ID") or pi)
            ctrack = _cell_track(ap, total)
            cell = ctrack[0] if ctrack else None
            local[pid] = {
                "pos": _track(ap, "Position", total) or [{"x": 0, "y": 0}] * total,
                "scl": _track(ap, "Scaling", total) or [{"x": 1, "y": 1}] * total,
                # ScalingLocal 全库只有 2 处非默认（energy 的 0.9→0.966 逐帧），
                # 以前完全没读；它和 Scaling 是相乘关系。
                "sclL": _track(ap, "ScalingLocal", total) or [{"x": 1, "y": 1}] * total,
                "rot": _track(ap, "Rotation", total) or [{"x": 0, "y": 0, "z": 0}] * total,
                "opa": _track(ap, "RateOpacity", total) or [1.0] * total,
                "pri": _track(ap, "Priority", total) or [0] * total,
                "cell": cell,
                "cellTrack": ctrack,
                "blend": int(bp.get("OperationBlendTarget") or 0),
                "name": str(bp.get("Name") or ""),
                # PartsColor：部件的顶点色。全屏滤镜层（ima_illumination_07/08）
                # 靠它把白色柔光染成暖色/彩虹渐变，以前整个字段没导出 → 滤镜纯白。
                "pc": (ap.get("PartsColor") or {}).get("TableValue") or [],
                # 其余**有值**的轨道也全量带出，不再选择性忽略：
                # ScalingLocal(20 处，全是 1,1) / Instance(69 处，全默认) /
                # UserData(1 处，全默认)。前端记录但不据此改画面。
                "extra": {k: ((ap.get(k) or {}).get("TableValue") or [])
                          for k in ("ScalingLocal", "Instance", "UserData")
                          if ((ap.get(k) or {}).get("TableValue") or [])},
            }

        def world(pid, f, guard=0):
            it = local.get(pid)
            if it is None or guard > 6:
                return 0.0, 0.0, 1.0, 1.0, 0.0, 1.0
            par = parent.get(pid, -1)
            wx, wy, wsx, wsy, wr, wo = world(par, f, guard + 1)
            p, s, r = it["pos"][f], it["scl"][f], it["rot"][f]
            sl = it["sclL"][f]
            sx = float(s.get("x") or 1) * float(sl.get("x") or 1)
            sy = float(s.get("y") or 1) * float(sl.get("y") or 1)
            return (wx + float(p.get("x") or 0) * wsx,
                    wy + float(p.get("y") or 0) * wsy,
                    wsx * sx, wsy * sy,
                    wr + float(r.get("z") or 0),
                    wo * float(it["opa"][f]))

        order = sorted(local, key=lambda pid: (local[pid]["pri"][0], pid))
        out_parts = []
        for pid in order:
            it = local[pid]
            if it["cell"] is None:
                continue                       # root / 纯特效部件
            ci, mi = it["cell"]
            if mi < 0 or mi >= len(cms) or ci < 0 or ci >= len(cms[mi]["cells"]):
                continue
            k = []
            for f in range(total):
                x, y, sx, sy, rot, op = world(pid, f)
                k.append([round(x, 1), round(y, 1), round(sx, 3),
                          round(sy, 3), round(rot, 1), round(op, 3)])
            pcs, pcm, pc_src = _pc_payload(
                (it["pc"][0] if it["pc"] else None), cms, mi, ci, root,
                str(a.get("Name") or "fx") + "_" + str(pid), it["name"])
            out_parts.append({"m": mi, "c": ci, "pr": int(it["pri"][0] or 0),
                              # ck：逐帧切换的 cell（电磁ウェブ的闪电）。
                              # 只有真正会变的部件才写，且要求整段同属一张 cellmap。
                              **({"ck": [v[0] for v in it["cellTrack"]]}
                                 if (it["cellTrack"] and len({v[0] for v in it["cellTrack"]}) > 1
                                     and len({v[1] for v in it["cellTrack"]}) == 1
                                     and next(iter({v[1] for v in it["cellTrack"]})) == mi)
                                 else {}),
                              # f = TableParts.Feature：1 普通图片 / 2 网格 / 4 粒子挂点。
                              # 网格部件（Feature=2）常被当成**全屏雾滤镜**用
                              # （image_loop、cold_loop 的 all_ef 缩放 11×，attack_bless 到 235×），
                              # 前端要按平铺而不是拉伸渲染，所以必须带出去。
                              "f": int(bp.get("Feature") or 0),
                              # nm：部件名。空想/冷气那一族雾滤镜都叫 all_ef，
                              # 前端只按名字认这一种，别把斩击/电光的网格也平铺了。
                              "nm": it["name"],
                              "bl": it["blend"], "k": k, "pc": pcs,
                              "pcm": pcm, "pcSrc": pc_src,
                              "x": it["extra"]})
        # Effect（粒子起始帧 / 播放速率）与 Status（CPE 部件状态位）也全量带出：
        # 前者影响画面，后者是内部标志，前端记录但不据此渲染。
        effects, status = [], []
        for pi2, ap2 in enumerate(aps):
            for ev in ((ap2.get("Effect") or {}).get("TableValue") or []):
                effects.append({"p": pi2,
                                "start": float(ev.get("FrameStart") or 0),
                                "rate": float(ev.get("RateTime") or 1)})
            sv = ((ap2.get("Status") or {}).get("TableValue") or [])
            if sv:
                status.append([pi2, int(sv[0].get("Flags") or 0)])
        anims.append({"fps": fps, "n": total,
                      "cw": round(float(a.get("SizeCanvasX") or 1280)),
                      "ch": round(float(a.get("SizeCanvasY") or 720)),
                      "name": str(a.get("Name") or ""),
                      "valid": [int(a.get("FrameValidStart") or 0),
                                int(a.get("FrameValidEnd") or total - 1)],
                      "maps": cms, "parts": out_parts,
                      "fx": effects, "status": status,
                      "instances": instances})
    return {"anims": anims} if anims else None


# --------------------------------------------------------- 粒子发射器 de_ef_*
# 完整演出 = da_/dc_ 的图片部件 + effect_* 部件挂的粒子 prefab。
# 例：キラキラ(illumination01) 的图片部件只用 ima_illumination_02(彩虹四芒星)
# 和 _03，而**圆形泡泡 ima_illumination_01 只由粒子发射器使用**：
#   da_adv_effect_illumination01.TableParts[1..2].PrefabUnderControl
#     → pe_ef_illumination_002 → de_ef_illumination_002
#   de_ef_illumination_002.TableEmitter[].IndexCellMap = [0,1,0,0,3]
#   TableMaterial 每 4 个一组(同图的 INITIATOR/ADD 两版本)，组 0 = ima_illumination_01
# 所以 5 个 emitter 里有 3 个撒圆形泡泡、1 个撒彩虹星芒、1 个撒细十字星芒。
# 只导出 da_/dc_ 就会丢掉圆泡 —— 这正是「游戏里星芒和圆形泡泡一起演出」的由来。


def _fx_material_tex(env):
    """{Material.path_id: 主贴图名}。"""
    tex = {}
    for o in env.objects:
        if o.type.name == "Texture2D":
            try:
                tex[o.path_id] = str(o.read().m_Name or "")
            except Exception:                                      # noqa: BLE001
                pass
    out = {}
    for o in env.objects:
        if o.type.name != "Material":
            continue
        try:
            t = o.read_typetree()
        except Exception:                                          # noqa: BLE001
            continue
        used = []
        for te in (t.get("m_SavedProperties") or {}).get("m_TexEnvs") or []:
            if isinstance(te, (list, tuple)) and len(te) == 2:
                second = te[1]
            elif isinstance(te, dict):
                second = te.get("second")
            else:
                second = None
            tid = ((second or {}).get("m_Texture") or {}).get("m_PathID")
            if tid in tex:
                used.append(tex[tid])
        out[o.path_id] = used[0] if used else None
    return out


def _fx_emit_pos(pm, ps, sx, sy, ox, oy, rz):
    """把发射器的 Main/Sub（局部坐标）按父部件缩放+旋转，转成画布坐标。

    返回 [x, y, rangeX, rangeY]：粒子放在 (x + rand*rangeX, y + rand*rangeY)。
    旋转 90° 时原本沿 x 展开的"全宽线"会变成沿 y 的竖直线 ——
    旋转必须作用在**整条发射线**上：起点是 R·Main，范围是 R·Sub，
    再取轴对齐包围盒（start = 起点 + min(0, 范围分量)）。
    以前把范围中心当成"旋转后要补的偏移"加到起点上，
    得到 [704, 0, 32, 1280] 这种"从 y=0 往下 1280"的范围 ——
    相当于整条竖线被抬高了半个屏幕（空想／空想／黒 的左右两条雾只有上半屏）。
    """
    mx = float(pm.get("x") or 0) * sx
    my = float(pm.get("y") or 0) * sy
    wx = float(ps.get("x") or 0) * sx
    wy = float(ps.get("y") or 0) * sy
    if rz:
        rad = math.radians(rz)
        c, s = math.cos(rad), math.sin(rad)
        mx, my = mx * c - my * s, mx * s + my * c
        wx, wy = wx * c - wy * s, wx * s + wy * c
    return [mx + min(0.0, wx) + ox, my + min(0.0, wy) + oy, abs(wx), abs(wy)]


def _cells_of(cm, tex):
    """把某个 cellmap 的 TableCell 解析成 [x,y,w,h,px,py]，按实际贴图尺寸缩放。"""
    if not cm:
        return []
    sz = cm.get("SizeOriginal") or {}
    iw = float(sz.get("x") or 0)
    ih = float(sz.get("y") or 0)
    tw = float((tex or {}).get("w") or iw)
    th = float((tex or {}).get("h") or ih)
    rx = (tw / iw) if iw else 1.0
    ry = (th / ih) if ih else 1.0
    out = []
    for c in cm.get("TableCell") or []:
        r = c.get("Rectangle") or {}
        pv = c.get("Pivot") or {}
        out.append([round(float(r.get("x") or 0) * rx, 1),
                    round(float(r.get("y") or 0) * ry, 1),
                    round(float(r.get("width") or 0) * rx, 1),
                    round(float(r.get("height") or 0) * ry, 1),
                    round(float(pv.get("x") or 0) * rx, 1),
                    round(float(pv.get("y") or 0) * ry, 1)])
    return out


def build_particles(env, da, atlas, mat_tex, de_trees, by_id, tex2cm=None, cms=None,
                    pe_payloads=None):
    """把 da_ 挂的粒子 prefab 摊平成发射器参数（全部来自数据，前端只负责撒点）。

    ``atlas`` 的键是 **cellmap 名**（`defo_effect_cell`），而
    ``TableMaterial`` 给的是 **Texture2D 名**（`defo_effect`）。以前直接
    ``atlas.get(tname)`` 查不到 → 整批粒子被跳过：heal_* 全系、attack_slash /
    piercing / water / fire_pillar、electricity、cold、image_loop、jaki_loop、
    music 的发射器全部丢失（2026-11-09 用户截图：「光柱和雾团没有」）。
    ``tex2cm`` 是贴图名 → cellmap 名的反查表。
    """
    tex2cm = tex2cm or {}
    pe_payloads = pe_payloads or {}
    out = []
    a0 = (da.get("TableAnimation") or [{}])[0]
    animparts = (a0.get("TableParts") or [])
    total = int(a0.get("CountFrame") or 0)
    for pi, p in enumerate(da.get("TableParts") or []):
        pid = (p.get("PrefabUnderControl") or {}).get("m_PathID")
        if not pid:
            continue
        # effect_* 部件自己的 Position/Scaling：粒子 prefab 挂在这个部件下。
        # 同一个 prefab 会被**多个** effect_* 部件各挂一次（illumination01 是
        # +500 / -500 左右两组，illumination02 是四个），按 prefab 名去重会
        # 丢掉一半 → 圆泡只出现在一侧（2026-11-09 用户指出）。
        apos, ascl, arot = {}, {}, {}
        if pi < len(animparts):
            vp = ((animparts[pi].get("Position") or {}).get("TableValue") or [])
            vs = ((animparts[pi].get("Scaling") or {}).get("TableValue") or [])
            vr = ((animparts[pi].get("Rotation") or {}).get("TableValue") or [])
            if vp:
                apos = vp[0]
            if vs:
                ascl = vs[0]
            if vr:
                arot = vr[0]
        ox, oy = float(apos.get("x") or 0), float(apos.get("y") or 0)
        sx, sy = float(ascl.get("x") or 1), float(ascl.get("y") or 1)
        # effect_* 部件自己可以旋转：image_loop 的 effect_5/effect_6 是
        # Rotation.z=-90°，把 emitter 的"全宽水平发射线"转成**竖直线**，
        # 于是四条边（上/下/左/右）围成画框。以前只用 Position、忽略旋转，
        # 左右两条变成画面中央的横带，整屏蒙白（2026-11-09 用户对比图）。
        rz = float(arot.get("z") or 0)
        # 这个 effect_* 部件自己的 Effect 轨道：粒子起始帧 / 播放速率，
        # 全库 94 处有值且各不相同，不能只取全局第一条。
        ev = ((animparts[pi].get("Effect") or {}).get("TableValue") or []) \
            if pi < len(animparts) else []
        estart = float(ev[0].get("FrameStart") or 0) if ev else 0.0
        erate = float(ev[0].get("RateTime") or 1) if ev else 1.0
        # 挂点部件自己的 RateOpacity 还要**乘到每颗粒子上**：
        #   空想 effect_3..6 = 常数 0.5（粒子整体半透明）
        #   空想／黒 = 0.2（更淡）、冷気 effect_2 = 0→1→0 的淡入淡出
        # 以前只把 RateOpacity 用在图片部件上，粒子永远满强度 ——
        # 冷気/邪気 的中心雾团一亮到底、空想 的边缘雾比原作浓一倍。
        popa = _track(animparts[pi], "RateOpacity", total) if pi < len(animparts) else None
        if popa:
            if max(popa) - min(popa) < 0.002:
                popa = round(float(popa[0]), 3)          # 常数：存一个数
            else:
                popa = [round(float(v), 3) for v in popa]
        else:
            popa = 1.0
        o = by_id.get(pid)
        if not o:
            continue
        try:
            pen = str(o.read().m_Name or "")
        except Exception:                                          # noqa: BLE001
            continue
        # ⚠ 必须按 **path_id** 取 de_/dc_，不能按名字：
        # 同一个 pe_ GameObject 的 Script_SpriteStudio6_RootEffect 里写着
        #   DataEffect  → 具体那一个 de_ef_xxx_P
        #   DataCellMap → 具体那一个 dc_adv_effect_xxx
        # 而包里 de_ef_smoke_P / de_ef_tama_P 各有 3 份同名不同参数的对象
        # （煙：一份 IndexCell=0/Interval=10/DurationEmitter=120 的连续白烟，
        #  一份 IndexCell=4/Interval=1/Emit=16 的橙色爆发）。
        # 以前用 de_trees 的**名字字典**取，同名后写覆盖前者 → 煙 拿到橙色爆发
        # 那一份，于是"全屏飘白烟"变成了"左下橙色大火圈"（2026-11-10 用户截图）。
        dename = "de_" + pen[3:] if pen.startswith("pe_") else None
        de, dc = pe_payloads.get(pid, (None, None))
        if de is None:
            de = de_trees.get(dename)
        if not de:
            continue

        mats = de.get("TableMaterial") or []
        groups = []
        for gi in range(0, len(mats), 4):
            groups.append(mat_tex.get((mats[gi] or {}).get("m_PathID")))

        # 这个发射器**自己**的 cellmap（pe 的 DataCellMap），格子表以它为准
        cm_list = _fx_cms(dc, atlas) if dc else (cms or [])
        for e in de.get("TableEmitter") or []:
            ci = int(e.get("IndexCellMap") or 0)
            tname = groups[ci] if ci < len(groups) else None
            if not tname:
                continue
            # cell 表要用 **IndexCellMap 指向的 cellmap**，不是材质贴图名：
            # image_loop 的 emitter IndexCellMap=0 → adv_effect_cold_cell（8 格），
            # 而材质贴图叫 adv_effect_cold_ima（只有 2 格）。按贴图名查会拿到
            # 2 格的表，IndexCell=2 直接越界 → cw 退回整张贴图宽 512，
            # 128 个 3000px 的白球把画面糊成纯白（2026-11-09 用户反馈）。
            # IndexCellMap 越界时回退到唯一的 cellmap（image_loop 的 emitter
            # 写的是 2，而 dc_ 只有 1 张表；按 2 取会拿到 None → cells 空）
            cm = cm_list[ci] if ci < len(cm_list) else (cm_list[0] if cm_list else None)
            cells = (cm or {}).get("cells") or []
            a = (cm or {}).get("tex")
            if not a:
                a = atlas.get(tname) or atlas.get(tex2cm.get(tname))
            if not a:
                continue
            pos = e.get("Position") or {}
            pm, ps = pos.get("Main") or {}, pos.get("Sub") or {}
            srs = e.get("ScaleRateStart") or {}
            ss = e.get("ScaleStart") or {}
            ssm = ss.get("Main") or {}
            cv = e.get("ColorVertex") or {}
            cm, cs = cv.get("Main") or {}, cv.get("Sub") or {}
            cvf = e.get("ColorVertexFluctuation") or {}
            cf_, cfs = cvf.get("Main") or {}, cvf.get("Sub") or {}
            dur = e.get("DurationParticle") or {}
            pat = [int(x.get("IndexGenerate") or 0)
                   for x in (e.get("TablePatternEmit") or [])[:140]]
            life = float(dur.get("Main") or 60)
            # ---- 以下字段以前全部被忽略，但全库 198 个 emitter 里大量带值 ----
            ang, spd = e.get("Angle") or {}, e.get("Speed") or {}
            gd, gp = e.get("GravityDirectional") or {}, e.get("GravityPointPosition") or {}
            rot, rf = e.get("Rotation") or {}, e.get("RotationFluctuation") or {}
            se, sre = e.get("ScaleEnd") or {}, e.get("ScaleRateEnd") or {}
            sem, ses = se.get("Main") or {}, se.get("Sub") or {}
            cfl = e.get("ColorVertexFluctuation") or {}
            cfm, cfs = cfl.get("Main") or {}, cfl.get("Sub") or {}
            seeds = [int(x) & 0xFFFFFFFF
                     for x in (e.get("TableSeedParticle") or [])[:140]]
            patoff = [int(x) for x in (e.get("TablePatternOffset") or [])[:140]]
            layout = de.get("ScaleLayout") or {}
            out.append({
                "tex": tname, "src": a["src"], "w": a["w"], "h": a["h"],
                "bl": int(e.get("OperationBlendTarget") or 0),
                # 位置：Main + 0~Sub 的均匀随机（Unity y 向上，前端取负）
                # 部件变换含负缩放（scl=(1,-1) 是上下镜像），Sub 也要跟着取符号；
                # 再把局部偏移按 Rotation.z 旋转到画布坐标
                "pos": _fx_emit_pos(pm, ps, sx, sy, ox, oy, rz),
                "flip": [sx < 0, sy < 0],
                "size": [_n(ssm.get("x"), 1.0), _n(ssm.get("y"), 1.0)],
                "scale": [_n(srs.get("Main"), 1.0), _n(srs.get("Sub"), 0.0)],
                # 顶点色：Main + 0~Sub 随机 → 彩虹圆泡
                # **不能写 `v or 1`**：0 是合法值，会被当成"没填"变成 1。
                # 冷気的冷蓝 (0,0.26,0.38) 曾变成 (1,0.26,0.38)、煙的橙 (1,0.25,0)
                # 变成洋红 (1,0.25,1)、冷気雾的 alpha=0（随机透明度）变成恒 1。
                "col": [_n(cm.get("r"), 1.0), _n(cm.get("g"), 1.0),
                        _n(cm.get("b"), 1.0), _n(cm.get("a"), 1.0),
                        _n(cs.get("r"), 0.0), _n(cs.get("g"), 0.0),
                        _n(cs.get("b"), 0.0), _n(cs.get("a"), 0.0)],
                # ColorVertexFluctuation = 颜色/透明度的**波动幅度**（±Sub），
                # 以前整个没导出。冷気/邪気 的 de_ef_cold_P 是
                #   ColorVertex       (1,1,1, a=0)
                #   ColorVertexFluct  (0,0.251,0.502, a=1)
                # 也就是"白里随机偏蓝 + 每颗随机透明"。只读 ColorVertex
                # 就变成纯白不透明的云 → 正中下方一团白色过曝（用户反复反馈的）。
                "cvf": [_n(cf_.get("r"), 0.0), _n(cf_.get("g"), 0.0),
                        _n(cf_.get("b"), 0.0), _n(cf_.get("a"), 0.0),
                        _n(cfs.get("r"), 0.0), _n(cfs.get("g"), 0.0),
                        _n(cfs.get("b"), 0.0), _n(cfs.get("a"), 0.0)],
                "life": life,
                "interval": float(e.get("Interval") or 15),
                "delay": float(e.get("Delay") or 0),
                "duration": float(e.get("DurationEmitter") or 0),
                "max": int(e.get("CountParticleMax") or 0),
                "seed": int(e.get("SeedRandom") or 0),
                "fade": [float(e.get("AlphaFadeStart") or 0),
                         float(e.get("AlphaFadeEnd") or 0)],
                "cycle": pat[0] if pat else 0,
                "pat": pat,
                "fx": [estart, erate],
                # 挂点部件自己的逐帧 RateOpacity（常数存一个数），粒子 alpha 要乘它
                "pop": popa,
                "from": dename,
                # IndexCell / 该图的 cell 表：粒子可能只用图集里某一格
                "cell": int(e.get("IndexCell") or 0),
                # 用**本 dc 自己的** cellmap 解析（atlas 里同名 cellmap 可能
                # 来自别的 dc_，格子数不同 → IndexCell 越界）
                "cells": cells,
                # 发射方向 / 速度 / 重力（雪、烟、火星靠这些飘动）
                "angle": [float(ang.get("Main") or 0), float(ang.get("Sub") or 0)],
                "speed": [float(spd.get("Main") or 0), float(spd.get("Sub") or 0)],
                "grav": [float(gd.get("x") or 0), float(gd.get("y") or 0),
                         float(gp.get("x") or 0), float(gp.get("y") or 0),
                         float(e.get("GravityPointPower") or 0)],
                # 起始/结束缩放：粒子在寿命内会缩放（不只是固定大小）
                "sEnd": [float(sem.get("x") or 0), float(sem.get("y") or 0),
                         float(ses.get("x") or 0), float(ses.get("y") or 0)],
                "sre": [float(sre.get("Main") or 0), float(sre.get("Sub") or 0)],
                # 旋转 + 波动 + 变化时长
                "rot": [float(rot.get("Main") or 0), float(rot.get("Sub") or 0),
                        float(rf.get("Main") or 0), float(rf.get("Sub") or 0),
                        float(e.get("RotationFluctuationRateTime") or 0)],
                # 颜色波动开关（ColorVertexFluctuation 不是颜色本身）
                "cfluct": [float(cfm.get("r") or 0), float(cfm.get("g") or 0),
                           float(cfm.get("b") or 0), float(cfm.get("a") or 0),
                           float(cfs.get("r") or 0), float(cfs.get("g") or 0),
                           float(cfs.get("b") or 0), float(cfs.get("a") or 0)],
                # 游戏自带的每粒子随机种子 / 发射模式偏移
                "seeds": seeds,
                "patOff": patoff,
                "cntEmit": int(e.get("CountParticleEmit") or 1),
                "fps": int(de.get("CountFramePerSecond") or 30),
                "layout": [float(layout.get("x") or 1), float(layout.get("y") or 1)],
                "order": list(de.get("TableIndexEmitterOrderDraw") or []),
                "flags": int(e.get("FlagData") or 0),
            })
    return out


def collect_fx(env, root):
    """导出全部画面特效（da_ + 同名 dc_）及其贴图。"""
    trees = {}
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            n = str(obj.read().m_Name or "")
            if not n.startswith((FX_DA, FX_DC)):
                continue
            trees[n] = obj.read_typetree()
        except Exception:                                          # noqa: BLE001
            continue
    # Instance 子 prefab（火柱/爆発/音楽 的火焰与光带）自带 cellmap，
    # 贴图必须先一起导出，否则子动画的部件拿不到 atlas。
    inst_prefabs = load_instance_prefabs(env)
    extra_dcs = [t[1] for t in inst_prefabs.values() if t[1]]
    atlas = export_fx_atlas(env, root, extra_dcs)
    print(f"  画面特效贴图 {len(atlas)} 张（含 {len(extra_dcs)} 份 Instance cellmap）")
    # 粒子材质的贴图名 → cellmap 名（atlas 只按 cellmap 名索引）
    tex2cm = {}
    for cm_name, info in atlas.items():
        src = str(info.get("from") or "")
        if src:
            tex2cm.setdefault(src, cm_name)
    mat_tex = _fx_material_tex(env)
    by_id = {o.path_id: o for o in env.objects}
    de_trees = {}
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            nm = str(o.read().m_Name or "")
            if nm.startswith("de_ef_"):
                de_trees[nm] = o.read_typetree()
        except Exception:                                          # noqa: BLE001
            continue

    out = {}
    total_emit = 0
    pe_payloads = load_pe_payloads(env, by_id)
    for n in sorted(trees):
        if not n.startswith(FX_DA):
            continue
        prefab = n[len(FX_DA):]
        dc = trees.get(FX_DC + prefab)
        if not dc:
            print(f"  [skip] {prefab} 没有同名 dc_ 结构")
            continue
        # 按材质 _MainTex 的 path_id 精确换图（见 FX_TEX_VARIANTS）。
        # 只影响该 prefab 自身的 atlas 视图，其它 prefab 仍共用原来的第一张。
        atlas_p = atlas
        v = FX_TEX_VARIANTS.get(prefab)
        if v:
            base, tag, _pid = v
            one = atlas.get(base + "|" + tag)
            if one:
                atlas_p = dict(atlas)
                atlas_p[base] = one
                atlas_p[base.split("|", 1)[0]] = one
        fx = build_fx(trees[n], dc, atlas_p, root, trees, inst_prefabs)
        if fx:
            fx["particles"] = build_particles(env, trees[n], atlas_p,
                                              mat_tex, de_trees, by_id,
                                              tex2cm, fx["anims"][0]["maps"],
                                              pe_payloads)
            _fix_emit_cells(fx, atlas_p)
            total_emit += len(fx["particles"])
            out[prefab] = fx
    print(f"  画面特效 {len(out)} 种逐帧数据 + {total_emit} 个粒子发射器")
    return out


# --------------------------------------------------------- 对话框行内图标
# 剧本台词里的 <sprite=0> 是 Unity/TMP 的**行内图标**（全站 66884 处，index 恒为 0），
# 游戏里显示成一颗粉色小爱心（带白色描边）。它在 bundle_font_66_heart_03 里：
# TMP_SpriteAsset 的 m_SpriteGlyphTable[0].m_GlyphRect 指向 heart_03 贴图的
# (0,0,33,33) 那一格。以前前端把标签整段剥掉，省略号后面的心就没了
# （2026-11-09 用户对比截图指出）。这里按 glyphRect 裁成独立小图给行内图标用。
MSG_SPRITE_BUNDLE = "bundle_font_66_heart_03"


def export_msg_sprite(root, files_dir):
    p = os.path.join(files_dir, MSG_SPRITE_BUNDLE)
    if not os.path.exists(p):
        return None
    env = UnityPy.load(p)
    rect = None
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            t = obj.read_typetree()
        except Exception:                                      # noqa: BLE001
            continue
        glyphs = t.get("m_SpriteGlyphTable") or []
        if not glyphs:
            continue
        r = glyphs[0].get("m_GlyphRect") or {}
        rect = (int(r.get("m_X") or 0), int(r.get("m_Y") or 0),
                int(r.get("m_Width") or 0), int(r.get("m_Height") or 0))
        break
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        img = obj.read().image
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        if rect and rect[2] > 0 and rect[3] > 0:
            img = img.crop((rect[0], rect[1],
                            rect[0] + rect[2], rect[1] + rect[3]))
        out_dir = os.path.join(root, "web", "media", "img", "advfx")
        os.makedirs(out_dir, exist_ok=True)
        img.save(os.path.join(out_dir, "msg_heart.png"))
        return {"src": "media/img/advfx/msg_heart.png",
                "w": img.width, "h": img.height}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--bundle", default="")
    args = ap.parse_args()

    root = os.path.abspath(args.root or (".." if os.path.isdir("char_story") else "."))
    bundle = args.bundle or os.path.join(root, "userdata", "char_story", "files",
                                         "bundle_image_adv")
    if not os.path.exists(bundle):
        print(f"[advfx] 找不到 {bundle}（先下载公共素材里的「剧情演出特效」）")
        return 2

    env = UnityPy.load(bundle)
    cells = read_scene_cells(env)
    mark_cells = (cells.get("ima_mark") or {}).get("cells") or []
    atlas = export_atlas(env, "ima_mark", root, "mark")
    if not atlas:
        print("[advfx] 包里没有 ima_mark 图集")
        return 3
    print(f"  图集 ima_mark {atlas['w']}×{atlas['h']} → {atlas['bytes'] / 1024:.0f} KB")

    trees = collect_marks(env)
    marks = {}
    for name, tree in sorted(trees.items()):
        m = build_mark(tree, mark_cells, print, name)
        if m:
            marks[name] = m
    tex = export_screff_tex(env, root)
    tex.update(export_slices(env, root))
    print(f"  画面特效贴图 {len(tex)} 张（{', '.join(sorted(tex))}）")
    screff = collect_fx(env, root)
    msgspr = export_msg_sprite(root, os.path.dirname(bundle))
    if msgspr:
        print(f"  行内图标 <sprite=0> → {msgspr['src']} "
              f"{msgspr['w']}×{msgspr['h']}")
    else:
        print("  [advfx] 没找到 bundle_font_66_heart_03：<sprite=0> 仍按纯文本处理")
    payload = {"v": 3, "atlas": {"mark": {"src": atlas["src"],
                                          "w": atlas["w"], "h": atlas["h"]}},
               # 台词行内图标（<sprite=0>）
               "msgspr": msgspr,
               "cells": {k: v["cells"] for k, v in cells.items()},
               "tex": tex,
               "marks": marks,
               # 画面特效：da_/dc_ 摊平后的逐帧数据（按 prefab 名索引）
               "screff": screff}
    out_js = os.path.join(root, "web", "data", "advfx.js")
    with open(out_js, "w", encoding="utf-8") as f:
        f.write("window.__ADVFX=")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    print(f"  {len(marks)} 个表情符号 → {out_js}（{os.path.getsize(out_js) / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
