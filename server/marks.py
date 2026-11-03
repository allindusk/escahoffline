"""收藏夹 / 播放进度的本地存档：``userdata/marks.json``。

为什么服务端也存一份
--------------------
浏览器 ``localStorage`` 才是**主副本**（播放器页与 CG 页共用同一份），但它有三个
很现实的坑，用户 2026-11-03 明确问到「有没有必要存一份」：

* 清理浏览器数据 / 换浏览器 / 换电脑 → 收藏和进度全丢，且无从找回；
* 手机扫码（局域网）进来看到的是**另一份** localStorage，收藏/进度不通用；
* 无痕窗口关掉即没。

所以这里落一份 JSON 到 ``userdata/``：更新时不会被覆盖（``updater.WHITELIST``
只放行程序文件，``userdata/`` 在黑名单里），也不联网、不上传，纯粹是本机文件。

数据格式
--------
::

    {
      "schema": 1,
      "fav":  {"story": {"char_1": 1793700000000}, "cg": {"7|名場面": 1793...}},
      "prog": {"char_1": {"p": 0, "mi": 120, "l": 87, "n": 402, "ts": ..., "t": "…"}}
    }

收藏值是**毫秒时间戳**；**负值表示"在这个时刻取消了收藏"**（墓碑）。
为什么需要墓碑：同步是全量推送，如果取消收藏只是"删掉这一条"，另一台设备下次
把它的旧正值推上来，刚删掉的就会复活。有了墓碑，比对规则统一成"绝对值大的
（时间更晚的）赢"，删除也能传播出去。

``prog`` 里的 ``{"del": 1, "ts": …}`` 同理，表示这条进度被删过（单条删除 / 清空）。

合并是幂等的：同一份数据推两次结果一样；多端各自推整份也能收敛
（时间戳完全相同时保持本机已有的那条）。
"""

from __future__ import annotations

import threading
import time

from . import paths

#: 「读文件 → 合并 → 写回」必须串行。
#: 服务端是多线程的：收藏时前端会连发两个 POST（连点两颗星），两个线程各自读到
#: 旧文件、各自写回，后写的会把先写的**整条收藏吞掉**（实测丢过一条 CG 收藏）。
_LOCK = threading.Lock()

#: 结构版本。将来改格式时用它做迁移判断（老文件只认已知字段，不会炸）
SCHEMA = 1
#: 最多留这么多条播放进度。前端上限 400，这里放宽一点，避免同步时互删
MAX_PROG = 600
#: 墓碑（负收藏 / 删除标记）保留天数：超过就清掉，别让文件无限长
#: 180 天足够覆盖"换设备/重装/很久没开"的场景，再久的旧记录复活也无所谓了
TOMBSTONE_DAYS = 180
KINDS = ("story", "cg")


def _empty() -> dict:
    return {"schema": SCHEMA,
            "fav": {k: {} for k in KINDS},
            "prog": {}}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _int(v, lo: int = 0) -> int:
    """宽松取整：手改过的文件里可能是 "12" / 12.7 / null。"""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return lo
    return n if n > lo else lo


def normalize(d) -> dict:
    """把读到的任意 JSON 收成合法结构（手改、老版本、半截文件都认）。"""
    out = _empty()
    if not isinstance(d, dict):
        return out
    fav = d.get("fav")
    if isinstance(fav, dict):
        for kind in KINDS:
            src = fav.get(kind)
            if not isinstance(src, dict):
                continue
            for fid, v in src.items():
                if not isinstance(fid, str) or not fid:
                    continue
                ts = _int(abs(v) if isinstance(v, (int, float)) else 0)
                if not ts:                       # 0 = 既没收藏也没取消，丢弃
                    continue
                out["fav"][kind][fid] = ts if v > 0 else -ts
    prog = d.get("prog")
    if isinstance(prog, dict):
        for key, rec in prog.items():
            if not isinstance(key, str) or not key or not isinstance(rec, dict):
                continue
            ts = _int(rec.get("ts"))
            if not ts:
                continue
            if rec.get("del"):
                out["prog"][key] = {"del": 1, "ts": ts}
            else:
                out["prog"][key] = {
                    "p": _int(rec.get("p")),
                    "mi": _int(rec.get("mi")),
                    "l": _int(rec.get("l")),
                    "n": _int(rec.get("n")),
                    "ts": ts,
                    "t": str(rec.get("t") or "")[:120],
                }
    return out


def prune(m: dict) -> int:
    """就地瘦身：老墓碑清掉、进度只留最近的 MAX_PROG 条。返回删掉的条数。

    注意只删**墓碑**：正记录（真收藏 / 真进度）一条都不动，删了就是丢用户数据。
    """
    cut = _now_ms() - TOMBSTONE_DAYS * 86400 * 1000
    gone = 0
    for kind in KINDS:
        d = m["fav"][kind]
        for fid in [f for f, v in d.items() if v < 0 and -v < cut]:
            del d[fid]
            gone += 1
    for key in [k for k, r in m["prog"].items() if r.get("del") and r["ts"] < cut]:
        del m["prog"][key]
        gone += 1
    live = sorted(((k, r) for k, r in m["prog"].items() if not r.get("del")),
                  key=lambda kv: kv[1]["ts"])
    if len(live) > MAX_PROG:
        for key, _rec in live[:len(live) - MAX_PROG]:
            del m["prog"][key]
            gone += 1
    return gone


def merge(base: dict, extra) -> tuple[dict, bool]:
    """把 ``extra`` 并进 ``base``（就地），返回 (结果, 是否有变化)。

    * 收藏：``abs(时间戳)`` 大的赢；负值代表"已取消"，所以删除也能传播。
    * 进度：``ts`` 大的赢；``{"del":1}`` 代表"已删除"。
    """
    out = normalize(base)
    add = normalize(extra)
    changed = False
    for kind in KINDS:
        dst = out["fav"][kind]
        for fid, v in add["fav"][kind].items():
            cur = dst.get(fid)
            if cur is None or abs(v) > abs(int(cur)):
                if cur != v:
                    dst[fid] = v
                    changed = True
    for key, rec in add["prog"].items():
        cur = out["prog"].get(key)
        if cur is None or int(rec["ts"]) > int(cur["ts"]):
            if cur != rec:
                out["prog"][key] = rec
                changed = True
    if prune(out):
        changed = True          # 瘦身也算改动，否则文件永远不会被真正重写
    return out, changed


def load() -> dict:
    """读本机存档（没有/坏了都返回空结构，不抛异常）。"""
    return normalize(paths.load_json(paths.MARKS_FILE, None))


def merge_into(incoming) -> dict:
    """前端推上来的整份数据 → 与本机文件合并 → 写回 → 返回合并结果。

    合并而不是覆盖，是因为可能有多个端（桌面 + 手机局域网 + 两个标签页）同时写。
    前端拿到返回值后还会再本地合并一次，最终三边一致。
    整个过程加锁：并发的两次推送不能各自读到旧文件再各自覆盖。
    没有变化就不写盘（每次打开页面都会推一次整份）。
    """
    with _LOCK:
        merged, changed = merge(load(), incoming)
        if changed:
            paths.save_json(paths.MARKS_FILE, merged)
    return merged
