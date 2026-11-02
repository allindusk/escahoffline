"""全项目"会变的数字"的唯一来源（容量 / 耗时）。

为什么单独一个模块
------------------
这些数字以前散落在 HTML 文案、JS 拼串、确认弹窗和服务端提示里，结果同一个
"公共素材"在概览页写 775 MB、在基础数据行写 835 MB —— 两处对不上（真实发生过）。
所以这里只写一次：

* 体积一律存 **字节**，单位换算与显示格式只在本文件里做（``size_text``）；
* 耗时一律存 **秒**（区间存元组），"30–90 秒""10–20 分钟"由 ``duration_text`` 生成；
* 前端通过 ``GET /api/facts`` 取显示文本（见 ``web/assets/js/facts.js``），
  Python 侧直接 ``from . import facts`` 引用。

改数字只改这里：页面占位、确认弹窗、服务端提示会一起变。

数据来源
--------
体积是 2026-10-29 对官方清单实测的估值（``PUBLIC_PARTS`` / ``ALL_ASSETS_BYTES``），
不是精确值；估值的用途是"让用户在动手前知道大概要花多少流量和时间"。
"""

from __future__ import annotations

KB = 1024
MB = 1024 * KB
GB = 1024 * MB

# ---------------------------------------------------------------- 容量（字节）
#: 官方资源清单（两份加密清单合计）。下载一次即可，之后只在"更新目录"时校验。
CATALOG_BYTES = 5 * MB

#: 一部角色剧情的体积（语音 + 立绘 + CG + 剧本）。``UNIT_BYTES`` 是中位估值，
#: 只用于"勾了 N 部大概要下多少"，界面上一律显示区间，别让人以为是精确值。
UNIT_BYTES = 45 * MB
UNIT_MIN_BYTES = 30 * MB
UNIT_MAX_BYTES = 60 * MB

#: 全量素材（所有剧情 + 公共素材，官方 CDN 上全部资源包之和）。
#: 实测 2026-10-29：25204 个包的 size 字段求和 = 26,502,962,265 字节（24.7 GB）。
#: 界面上"全部装上""全部素材约多少"都用它，估出来的部分不会大于它。
ALL_ASSETS_BYTES = 26502962265

#: 程序更新包（增量）与全量便携包。
UPDATE_PKG_BYTES = (3 * MB, 5 * MB)
UPDATE_FULL_BYTES = 114 * MB

#: 公共素材的类别、包名前缀与体积（MB）。
#:
#: **avatar / banner 必须在这里登记**：`/api/install/base` 会用本字典做白名单
#: 过滤（`parts = [p for p in raw if p in PUBLIC_PARTS]`），以前漏了 avatar，
#: 前端又没显式传 parts → 头像分支永远不执行 → 用户"下完公共素材还是没头像"
#: （侧栏与 CG 页都靠 `avatarsReady()`，于是整片首字色块）。
PUBLIC_PARTS = {
    "bg": ("背景", "bundle_bgimage_", 205),
    "bgm": ("BGM 音乐", "bundle_music_", 539),
    "sfx": ("音效", "bundle_sound_", 31),
    "avatar": ("角色头像", "bundle_chara_c-panel_", 20),
    # 剧情横幅：角色剧情入口图 + 主线/活动/其他的章节横幅（播放器当标题用）
    "banner": ("剧情横幅", "bundle_episodeicons_episode_character_|bundle_image_story_", 40),
}

# ---------------------------------------------------------------- 耗时（秒）
SEC_CATALOG = (10, 20)             # 更新剧情目录
SEC_UNIT_INSTALL = (30, 90)        # 装一部剧情（下载 + 解包）
SEC_PUBLIC_INSTALL = (600, 1200)   # 公共素材（下载 + 解包）
SEC_SPACE_SCAN = (1, 3)            # 容量统计
SEC_REBUILD_X2 = (1, 2)            # 重新登记高清图
SEC_EXPORT_LINE = (2, 2)           # 导出视频：每句
SEC_EXPORT_PART = (180, 300)       # 导出视频：单段
SEC_EXPORT_ALL = (900, 1500)       # 导出视频：6 段全导


# ---------------------------------------------------------------- 格式化
def size_text(n: float) -> str:
    """字节 → "5 MB" / "835 MB" / "20.8 GB"（整数就不带小数点）。"""
    n = float(n or 0)
    for unit, step in (("GB", GB), ("MB", MB), ("KB", KB)):
        if n >= step:
            v = n / step
            txt = f"{v:.1f}"
            if txt.endswith(".0"):
                txt = txt[:-2]
            return f"{txt} {unit}"
    return f"{int(n)} B"


def span_text(lo: float, hi: float, unit: str = "") -> str:
    """区间文字：``(30, 60, "MB")`` → "30–60 MB"；上下限相同时只给一个数。"""
    lo, hi = float(lo), float(hi)
    body = f"{lo:g}" if lo == hi else f"{lo:g}–{hi:g}"
    return f"{body} {unit}".strip()


def duration_text(lo: float, hi: float | None = None) -> str:
    """秒 → "30–90 秒" / "10–20 分钟"（超过 2 分钟就换成分钟）。"""
    hi = lo if hi is None else hi
    if max(lo, hi) >= 120:
        return span_text(lo / 60.0, hi / 60.0, "分钟")
    return span_text(lo, hi, "秒")


def size_span(lo: float, hi: float) -> str:
    """字节区间 → "30–60 MB"（上下限相同就退化成单值 "45 MB"）。"""
    lo, hi = float(lo), float(hi)
    if lo == hi:
        return size_text(lo)
    for unit, step in (("GB", GB), ("MB", MB), ("KB", KB)):
        if lo >= step:
            return span_text(lo / step, hi / step, unit)
    return span_text(lo, hi, "B")


def public_bytes() -> int:
    """公共素材总体积（= 各类别之和，界面上的"约 835 MB"就是它）。"""
    return sum(int(mb) * MB for _label, _prefix, mb in PUBLIC_PARTS.values())


def public_breakdown() -> str:
    """"背景 205 + BGM 音乐 539 + …"：数字跟着 ``PUBLIC_PARTS`` 走，不再手抄。"""
    return " + ".join(f"{label} {mb:g}" for label, _p, mb in PUBLIC_PARTS.values())


def units_size_text(count: float) -> str:
    """N 部剧情约多少字节（"6.6 GB"），用于"全部装上"这类估算。"""
    return size_text(float(count or 0) * UNIT_BYTES)


# ---------------------------------------------------------------- 对外载荷
def text_map() -> dict:
    """前端直接显示的成品文案（单位、约等于、区间都已经是人话）。"""
    catalog = size_text(CATALOG_BYTES)
    public = size_text(public_bytes())
    return {
        "catalog": catalog,
        "public": public,
        "publicBreakdown": public_breakdown(),
        "unit": size_span(UNIT_MIN_BYTES, UNIT_MAX_BYTES) + " / 部",
        "unitEach": size_span(UNIT_MIN_BYTES, UNIT_MAX_BYTES),
        "unitEst": size_text(UNIT_BYTES),
        "allAssets": size_text(ALL_ASSETS_BYTES),
        "update": size_span(*UPDATE_PKG_BYTES),
        "updateFull": size_text(UPDATE_FULL_BYTES),
        "secCatalog": duration_text(*SEC_CATALOG),
        "secUnit": duration_text(*SEC_UNIT_INSTALL),
        "secPublic": duration_text(*SEC_PUBLIC_INSTALL),
        "secScan": duration_text(*SEC_SPACE_SCAN),
        "secRebuild": duration_text(*SEC_REBUILD_X2),
        "secExportLine": duration_text(*SEC_EXPORT_LINE),
        "secExportPart": duration_text(*SEC_EXPORT_PART),
        "secExportAll": duration_text(*SEC_EXPORT_ALL),
    }


def payload() -> dict:
    """``GET /api/facts`` 的响应：原始数值给 JS 算，成品文案给页面直接贴。"""
    return {
        "bytes": {
            "catalog": CATALOG_BYTES,
            "unit": UNIT_BYTES,
            "unitMin": UNIT_MIN_BYTES,
            "unitMax": UNIT_MAX_BYTES,
            "allAssets": ALL_ASSETS_BYTES,
            "public": public_bytes(),
        },
        "secs": {
            "catalog": SEC_CATALOG,
            "unit": SEC_UNIT_INSTALL,
            "public": SEC_PUBLIC_INSTALL,
            "scan": SEC_SPACE_SCAN,
            "rebuild": SEC_REBUILD_X2,
            "exportLine": SEC_EXPORT_LINE,
            "exportPart": SEC_EXPORT_PART,
            "exportAll": SEC_EXPORT_ALL,
        },
        "parts": [
            {"key": k, "label": label, "bytes": int(mb) * MB,
             "text": size_text(int(mb) * MB)}
            for k, (label, _prefix, mb) in PUBLIC_PARTS.items()
        ],
        "text": text_map(),
    }
