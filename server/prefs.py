"""界面偏好的本地副本：``userdata/prefs.json``（播放器设置 / 列表筛选 / 语言）。

为什么设置也要落盘
------------------
``localStorage`` 是浏览器自己的存储：换浏览器、换电脑、清缓存就全没了。收藏和
播放进度已经有一份 ``marks.json``（见 :mod:`server.marks`），设置同理 ——
用户 2026-11-03 的要求是「多设备下体验一致」：在手机上把 BGM 音量调小，回到
电脑上还是那个音量；在电脑上切成日文，手机打开也是日文。

数据格式
--------
::

    {
      "schema": 1,
      "set": {                      # 播放器「设置」面板的各项
        "bgmVol": {"v": 0.5, "ts": 1793700000000},
        "msgFont": {"v": "serif", "ts": 1793700000001}
      },
      "ui": {                       # 其余零散偏好
        "lang": {"v": "ja", "ts": ...},
        "cgSort": {"v": "date-desc", "ts": ...}
      }
    }

每个键单独带时间戳，合并规则是**逐键 LWW**（谁后改谁赢）。

为什么不像收藏那样需要墓碑：设置永远有值 —— 用户按「恢复默认」写进去的就是
默认值（并拿到新的时间戳），这本身就能传播出去；没有"这个键不存在"这种状态。

哪些不同步（有意为之，见 web/assets/js/userdata.js 的注释）：
``escah_sidebar`` / ``escah_hist``（手机上侧栏是抽屉，开合状态是设备特性）、
``escah_disclaimer_v1`` / ``escah_tour_*``（免责声明与新手引导是每台设备各自确认
一次的动作，不该被别的设备代劳）。
"""

from __future__ import annotations

import threading

from . import paths

SCHEMA = 1
NAMESPACES = ("set", "ui")
#: 单个命名空间最多记这么多键（正常只有几十个，纯粹防坏数据撑爆文件）
MAX_KEYS = 200

_LOCK = threading.Lock()


def _empty() -> dict:
    return {"schema": SCHEMA, "set": {}, "ui": {}}


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _clean_value(v):
    """只留能 JSON 往返的标量；对象/数组一律丢弃（设置项都是标量）。"""
    if isinstance(v, bool) or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    return None


def normalize(d) -> dict:
    out = _empty()
    if not isinstance(d, dict):
        return out
    for ns in NAMESPACES:
        src = d.get(ns)
        if not isinstance(src, dict):
            continue
        for key, ent in src.items():
            if not isinstance(key, str) or not key or len(key) > 60:
                continue
            if not isinstance(ent, dict):
                continue
            ts = _int(ent.get("ts"))
            val = _clean_value(ent.get("v"))
            if not ts or val is None:
                continue
            out[ns][key] = {"v": val, "ts": ts}
        if len(out[ns]) > MAX_KEYS:
            # 时间戳最新的那些留下
            keep = sorted(out[ns].items(), key=lambda kv: kv[1]["ts"], reverse=True)[:MAX_KEYS]
            out[ns] = dict(keep)
    return out


def merge(base: dict, extra) -> tuple[dict, bool]:
    """逐键合并（ts 大者赢），返回 (结果, 是否有变化)。"""
    out = normalize(base)
    add = normalize(extra)
    changed = False
    for ns in NAMESPACES:
        dst = out[ns]
        for key, ent in add[ns].items():
            cur = dst.get(key)
            if cur is None or int(ent["ts"]) > int(cur["ts"]):
                if cur != ent:
                    dst[key] = ent
                    changed = True
    return out, changed


def load() -> dict:
    return normalize(paths.load_json(paths.PREFS_FILE, None))


def merge_into(incoming) -> dict:
    """前端推上来的偏好 → 与本机文件逐键合并 → 写回 → 返回合并结果。

    没有变化就**不写文件**：每次打开页面 hydrate 都会推一次整份，全都一样时
    还去写盘等于每次刷新都碰一次磁盘（也没必要惊动备份/杀软）。
    """
    with _LOCK:
        merged, changed = merge(load(), incoming)
        if changed:
            paths.save_json(paths.PREFS_FILE, merged)
    return merged
