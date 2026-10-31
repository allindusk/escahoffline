# -*- coding: utf-8 -*-
"""索引类数据（cgmap / index / advindex）的**合并式**更新。

为什么不能整份覆盖
------------------
这三个索引在本机也会被增量合并过：

* ``cgmap.js``：装一部剧情后 :func:`server.cdn.stages.sync_cgmap` 会把剧本引用、
  而表里还没有的 CG 组补进去；
* ``index.js``：:func:`sync_index` 按 id 合并本地 ``char_*.js``；
* ``advindex.js``：:func:`sync_advindex` 汇总本地 ``adv_*.js``。

于是每次发版，这三个文件里同时存在两种东西：**程序逻辑的修正**（例如
2026-11-01 那次 CG 分组判定重写：384 个角色恒 2 组）和**本机独有的条目**
（用户装过的、出厂包里还没有的角色/章节）。

* 直接覆盖 → 丢本机条目（新角色的 CG/章节整块消失）；
* 不覆盖 → 程序逻辑的修正永远到不了老用户手里。v1.0.2→v1.0.3 就是这么翻车的：
  更新包里的 cgmap.js 是修好的，但 ``updater.WEB_DATA_UPDATABLE`` 只放行了
  names/charmeta，cgmap 被 ``web/data/`` 黑名单挡掉 —— 老用户升到 1.0.3 后
  CG 页还是「3 组 / 13 张」，新装用户却是对的，同一版本两种表现。

规则（本项目定，别随便改）
--------------------------
* 出厂版里有的键 → **用出厂版**（程序逻辑已修）；
* 出厂版里没有的键 → **保留本机版**（那是装过的东西）；
* 键相同但内容不同 → 出厂版赢（这正是要送达的修正）；
* 解析失败（文件坏了）→ 用出厂版整份替换，至少别把坏文件留着。
"""

from __future__ import annotations

import json
import os

#: relpath → (JS 前缀, 取键函数)。键相同即视为同一个条目，出厂版赢。
SPECS = {
    "web/data/cgmap.js": (
        "window.__CGMAP=",
        # CG 组按角色整体替换：一次修正往往重排整个角色的分组，
        # 逐组比对会留下「旧的第5段 + 新的 ラ・ハウゼル」这种混合体。
        lambda e: ("c", e.get("c")),
    ),
    "web/data/index.js": (
        "window.__INDEX=",
        lambda e: ("id", e.get("id")),
    ),
    "web/data/advindex.js": (
        "window.__ADV=",
        lambda e: ("k", e.get("key"), e.get("part", 0)),
    ),
}


def read_js(path: str):
    """``window.__X=[...];`` → (前缀, 数据)。读不了就抛异常，由调用方兜底。

    前缀**带等号**（``window.__X=``）：:func:`write_js` 直接把它拼回去，
    少一个 ``=`` 就会写出 ``window.__CGMAP[...]`` 这种整份报废的文件。
    """
    raw = open(path, encoding="utf-8-sig").read()
    i = raw.index("=")
    return raw[:i + 1], json.loads(raw[i + 1:].strip().rstrip(";"))


def write_js(path: str, prefix: str, data) -> None:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(prefix + body + ";")
    os.replace(tmp, path)


def merge_list(key_of, old: list, new: list) -> list:
    """出厂版 new 为主，补上本机独有（new 里没有那个键）的 old 条目。"""
    have = {key_of(e) for e in new}
    extra = [e for e in old if key_of(e) not in have]
    return list(new) + extra


def merge(rel: str, old: list, new: list) -> list:
    """按 :data:`SPECS` 的规则合并两个索引（old = 本机现状，new = 出厂包）。"""
    _prefix, key_of = SPECS[rel]
    return merge_list(key_of, old or [], new or [])


def merge_into(staged: str, live: str | None = None, rel: str = "") -> dict:
    """把 ``staged``（出厂包里的那份）与 ``live``（本机现状）合并，**写回 staged**。

    只改暂存区，不动本机文件 —— 真正落盘由独立更新器按白名单覆盖，
    这样"只下载不应用"也不会把本机数据改脏。
    """
    _prefix, key_of = SPECS[rel]
    out = {"rel": rel, "kept": 0, "total": 0, "used": "factory"}
    if not os.path.exists(live or ""):
        return out
    try:
        _p, old = read_js(live)
    except Exception:                                          # noqa: BLE001
        return out                       # 本机文件坏了/读不了 → 直接用出厂版
    try:
        prefix, new = read_js(staged)
    except Exception:                                          # noqa: BLE001
        return out                       # 出厂文件坏了 → 保持原样（让更新器报错）
    if not isinstance(new, list) or not isinstance(old, list):
        return out
    merged = merge(rel, old, new)
    write_js(staged, prefix, merged)
    out.update(kept=len(merged) - len(new), total=len(merged), used="merged")
    return out


def merge_staged(staging: str, root: str, log=None) -> list[dict]:
    """更新包解压后调用：把暂存区里这几个索引与本机现状合并。"""
    done = []
    for rel in SPECS:
        staged = os.path.join(staging, rel.replace("/", os.sep))
        if not os.path.exists(staged):
            continue
        live = os.path.join(root, rel.replace("/", os.sep))
        try:
            r = merge_into(staged, live, rel)
        except Exception as e:                                 # noqa: BLE001
            r = {"rel": rel, "error": f"{type(e).__name__}: {e}"}
        done.append(r)
        if log:
            if r.get("error"):
                log(f"索引合并失败（{rel}）：{r['error']}，将整份用出厂版")
            elif r["used"] == "merged":
                log(f"索引合并：{rel} 出厂 {r['total'] - r['kept']} 条 + "
                    f"本机独有 {r['kept']} 条")
    return done
