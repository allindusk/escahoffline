"""角色显示名：导出文件命名统一走这里。

为什么单独抽出来
----------------
导出的东西最终躺在 ``userdata/export/`` 里，用户翻文件夹时看到
``char_1_p2-4.mp4`` 根本不知道是谁的。所以**文件名一律用角色名**，
并且中文名优先、日文名兜底（``names.js``）、再兜底 ``角色<id>``。

中文名从哪来（2026-10-30 改动）
------------------------------
以前中文名跟着出厂索引 ``web/data/charmeta.js`` 一起下发，结果**全新安装的
包里也带着中文名**，CG 页角色列表直接显示「节拍魔法·玛吉珂 · 2 组 / 12 张」——
用户要的是"全量包纯日文，中文只来自我自己的译文"。所以现在分两层：

* **包内没有任何中文名**：``web/data/charmeta.js`` 只留 r/d，
  ``server/cdn/data/char_meta.json`` 也只留公开的稀有度/实装日期；
* **想用中文名自己维护** ``userdata/char_meta.json``（首启会种一份不含 zh 的
  模板过去，加一列 ``zh`` 即可生效）。它在用户目录里，永远不会进包。

导出文件名仍然"中文名优先 > 日文名 > ``角色<id>``"：有中文名就用，没有就用日文名。

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


def _zh_map() -> dict[int, str]:
    """``{cid: 中文名}``，只从 ``char_meta.json`` 的 ``zh`` 列取（键是日文名）。

    为什么不再读前端索引：出厂索引一旦带 ``zh``，全量包就不"纯净"了 ——
    全新安装的 CG 页/侧栏会直接出现中文名；包内那份 ``char_meta.json`` 也
    已经把 ``zh``/``icon`` 去掉了，所以**干净的包查不到中文名，文件名走日文名**。
    想在导出文件名里用中文，就在 ``userdata/char_meta.json`` 里加 ``zh`` 列 ——
    用户目录那份优先，且不会被打包。
    """
    if "zh" in _CACHE:
        return _CACHE["zh"]
    table: dict = {}
    for p in (os.path.join(paths.USERDATA, "char_meta.json"),
              os.path.join(paths.ROOT, "server", "cdn", "data", "char_meta.json")):
        try:
            with open(p, encoding="utf-8") as f:
                table = json.load(f) or {}
            break
        except Exception:                                      # noqa: BLE001
            continue
    out: dict[int, str] = {}
    if table:
        for k, nm in (_load_js("names.js") or {}).items():
            cid = _int(k)
            if cid is None:
                continue
            # 表里的键是不带 "(13)" 这类重名后缀的日文名
            base = re.sub(r"\s*\(\d+\)$", "", str(nm or "").strip())
            zh = ((table.get(base) or {}).get("zh") or "").strip()
            if zh:
                out[cid] = zh
    _CACHE["zh"] = out
    return out


def all_names() -> dict[int, str]:
    """``{cid: 名字}``。带缓存 —— 导出多角色时会被反复调用。"""
    if _CACHE.get("all"):
        return _CACHE["all"]
    jp = _load_js("names.js")
    meta = _load_js("charmeta.js")
    out: dict[int, str] = {}
    # 老包（改动前生成的）charmeta.js 里可能还有 zh，读一下做兼容
    for k, v in (meta or {}).items():
        cid = _int(k)
        zh = ((v or {}).get("zh") or "").strip()
        if cid is not None and zh:
            out[cid] = zh
    for cid, zh in _zh_map().items():                          # 服务端表里的中文名
        out.setdefault(cid, zh)
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
    _CACHE.pop("zh", None)
