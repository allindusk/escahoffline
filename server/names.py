"""角色显示名：导出文件命名统一走这里。

为什么单独抽出来
----------------
导出的东西最终躺在 ``userdata/export/`` 里，用户翻文件夹时看到
``char_1_p2-4.mp4`` 根本不知道是谁的。所以**文件名一律用角色名**，
并且中文名优先（``charmeta.js`` 的 ``zh`` 字段）、日文名兜底（``names.js``）、
再兜底 ``角色<id>``。

注意 ``names.js`` 里的日文名可能是 ``A/B`` 这种带斜杠的组合名，
直接当文件名会变成子目录，所以必须过 :func:`safe`。
"""

from __future__ import annotations

import json
import os
import re

from . import paths

#: Windows 非法字符 + 控制字符。全角符号、假名、汉字都保留（合法且可读）
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

_CACHE: dict = {}


def _load_js(name: str) -> dict:
    p = os.path.join(paths.DATA, name)
    try:
        with open(p, encoding="utf-8-sig") as f:
            s = f.read()
        return json.loads(s[s.find("=") + 1:].strip().rstrip(";"))
    except Exception:                                          # noqa: BLE001
        return {}


def _int(k):
    try:
        return int(k)
    except (TypeError, ValueError):
        return None


def all_names() -> dict[int, str]:
    """``{cid: 名字}``。带缓存 —— 导出多角色时会被反复调用。"""
    if _CACHE.get("all"):
        return _CACHE["all"]
    jp = _load_js("names.js")
    meta = _load_js("charmeta.js")
    out: dict[int, str] = {}
    for k, v in (meta or {}).items():                          # 先放中文名
        cid = _int(k)
        zh = ((v or {}).get("zh") or "").strip()
        if cid is not None and zh:
            out[cid] = zh
    for k, v in (jp or {}).items():                            # 日文名补缺
        cid = _int(k)
        if cid is None or cid in out:
            continue
        nm = str(v or "").strip()
        if nm:
            out[cid] = nm
    _CACHE["all"] = out
    return out


def of(cid) -> str:
    """角色名（未收录时用 ``角色<id>``，绝不返回空串）。"""
    cid = _int(cid)
    if cid is None:
        return "未命名"
    return all_names().get(cid) or f"角色{cid}"


def safe(s: str, limit: int = 60) -> str:
    """把任意名字压成合法文件名。"""
    s = _BAD.sub("_", str(s or "")).strip().strip(".")
    return (s or "未命名")[:limit]


def file_stem(cid, suffix: str = "") -> str:
    """→ ``神騎プラチネル_p2-4`` 这种可直接做文件名的串（无扩展名）。"""
    return safe(of(cid) + (suffix or ""))


def invalidate() -> None:
    """``names.js`` 变了（装新角色后）就清缓存。"""
    _CACHE.pop("all", None)
