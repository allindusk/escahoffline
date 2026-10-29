"""资源包清单、依赖求解、下载链接（加密）与已装状态。

依赖模型
--------
每个单元（一部剧情）声明两类依赖：

* **硬依赖** ``depots``  —— core + 自己的专属包。缺任一 → 无法播放
* **软依赖** ``soft``    —— 公共背景 / 公共立绘 / 音乐音效。
  缺了仍然能播，只是对应元素降级（无背景 / 无立绘 / 静音）

前端"智能安装"就是：勾一部剧情 → 自动勾上它的硬依赖，软依赖默认也勾上但
**允许取消**。这样既不会出现"点开黑屏"，也不会强迫用户为了一个角色下 1 GB。

下载链接
--------
``web/data/packs.links.js`` 里是 **RC4+base64 混淆**过的 JSON，
解出来形如::

    {"base": "https://你的存储/packs/", "pattern": "{id}.zip",
     "overrides": {"char_1": "https://..."}, "note": "..."}

这样做不是为了真正的安全（客户端必然能解），而是为了：

1. 仓库里不出现明文直链，避免被搜索引擎/爬虫索引
2. 链接更换时只需替换这一个字符串，不用改代码
3. 用户可以完全绕过它：``userdata/links.json`` 写自己的明文配置即可

明文优先级：userdata/links.json > config.mirror > 内置混淆串
"""

from __future__ import annotations

import base64
import json
import os
import re

from . import paths

#: 与 web/assets/js/links.js 里的 KEY 必须完全一致（改了就两边一起改）
RC4_KEY = "escah-offline-v1"

_MANIFEST_CACHE: dict = {"at": 0.0, "data": None}


# ---------------------------------------------------------------- RC4 混淆
def rc4(data: bytes, key: str) -> bytes:
    k = key.encode("utf-8")
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + k[i % len(k)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for n, ch in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = ch ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def obfuscate(obj, key: str = RC4_KEY) -> str:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(rc4(raw, key)).decode("ascii")


def deobfuscate(text: str, key: str = RC4_KEY):
    text = "".join(text.split())
    raw = rc4(base64.b64decode(text), key)
    return json.loads(raw.decode("utf-8"))


def read_builtin_links() -> dict:
    """从 web/data/packs.links.js 解出内置链接配置（解不出返回空配置）。"""
    p = os.path.join(paths.DATA, "packs.links.js")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8-sig") as f:
            s = f.read()
        m = re.search(r"window\.__PACK_LINKS\s*=\s*[\"']([A-Za-z0-9+/=\s]+)[\"']", s)
        if not m:
            return {}
        return deobfuscate(m.group(1)) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def links() -> dict:
    """生效的下载源配置（明文）。"""
    cfg = paths.load_config()
    base = {"base": "", "pattern": "{id}.zip", "overrides": {},
            "note": ""}
    base.update(read_builtin_links())
    user = paths.load_json(paths.LINKS_FILE, {}) or {}
    if isinstance(user.get("base"), str) and user["base"].strip():
        base.update({k: v for k, v in user.items() if v is not None})
    if cfg.get("mirror"):
        base["base"] = cfg["mirror"]
        base["overrides"] = {}
    return base


def url_for(depot_id: str, links_cfg: dict | None = None) -> str:
    cfg = links_cfg or links()
    ov = cfg.get("overrides") or {}
    if depot_id in ov:
        return ov[depot_id]
    base = (cfg.get("base") or "").rstrip("/")
    if not base:
        return ""
    pat = cfg.get("pattern") or "{id}.zip"
    return base + "/" + pat.format(id=depot_id)


# ---------------------------------------------------------------- 清单
def manifest(force: bool = False) -> dict:
    import time
    p = os.path.join(paths.DATA, "packs.json")
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return {"schema": 0, "depots": [], "units": {}, "totals": {}}
    if not force and _MANIFEST_CACHE["data"] is not None \
            and _MANIFEST_CACHE["at"] == mtime:
        return _MANIFEST_CACHE["data"]
    data = paths.load_json(p, {}) or {}
    _MANIFEST_CACHE.update(at=mtime, data=data)
    return data


def depots() -> dict[str, dict]:
    return {d["id"]: d for d in manifest().get("depots", [])}


def units() -> dict[str, dict]:
    return manifest().get("units", {}) or {}


# ---------------------------------------------------------------- 依赖求解
def resolve(selected: list[str], include_soft: bool = True) -> dict:
    """把用户选择（单元 id 或 depot id）展开成要下载的 depot 列表。

    → {"hard": [...], "soft": [...], "unknown": [...], "bytes": n, "bytesSoft": n}
    """
    M, U = depots(), units()
    hard, soft, unknown = [], [], []
    for item in selected:
        if item in U:
            hard += [d for d in U[item]["depots"] if d not in hard]
            if include_soft:
                soft += [d for d in U[item].get("soft", [])
                         if d not in soft and d not in hard]
        elif item in M:
            hard.append(item)
            for d in M[item].get("requires", []):
                if d not in hard:
                    hard.append(d)
            if include_soft:
                for d in M[item].get("soft", []):
                    if d not in soft and d not in hard:
                        soft.append(d)
        else:
            unknown.append(item)
    # 硬依赖的 requires 也要闭包
    i = 0
    while i < len(hard):
        for d in M.get(hard[i], {}).get("requires", []):
            if d not in hard:
                hard.append(d)
        i += 1
    soft = [d for d in soft if d not in hard]
    return {
        "hard": hard, "soft": soft, "unknown": unknown,
        "bytes": sum(M.get(d, {}).get("bytes", 0) for d in hard),
        "bytesSoft": sum(M.get(d, {}).get("bytes", 0) for d in soft),
    }


# ---------------------------------------------------------------- 已装状态
def installed() -> dict[str, dict]:
    return paths.load_state().get("depots", {}) or {}


def mark_installed(depot_id: str, files: int = 0, byt: int = 0) -> dict:
    import time
    st = paths.load_state()
    st["depots"][depot_id] = {
        "files": files, "bytes": byt,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": manifest().get("generated", ""),
    }
    st["files"] = sum(v.get("files", 0) for v in st["depots"].values())
    st["bytes"] = sum(v.get("bytes", 0) for v in st["depots"].values())
    paths.save_state(st)
    return st["depots"][depot_id]


def unmark_installed(depot_id: str) -> bool:
    st = paths.load_state()
    if depot_id not in st["depots"]:
        return False
    st["depots"].pop(depot_id, None)
    st["files"] = sum(v.get("files", 0) for v in st["depots"].values())
    st["bytes"] = sum(v.get("bytes", 0) for v in st["depots"].values())
    paths.save_state(st)
    return True


def unit_status() -> dict[str, str]:
    """单元 → 'ready' | 'soft-missing' | 'missing'

    这是"哪些剧情还没下载资源"的唯一判定来源（前端侧栏标记用）。
    """
    M, U, ins = depots(), units(), installed()
    out = {}
    for uid, u in U.items():
        hard_ok = all(d in ins for d in u["depots"])
        if not hard_ok:
            out[uid] = "missing"
        else:
            soft_ok = all(d in ins for d in u.get("soft", []))
            out[uid] = "ready" if soft_ok else "soft-missing"
    return out


def unit_ready(uid: str) -> bool:
    return unit_status().get(uid) == "ready"


def refresh_manifest() -> dict:
    """从下载源拉取最新 packs.json，覆盖本地清单（旧的备份成 packs.prev.json）。

    这是"更新资源列表"的实现：上游（你自己）发布新剧情后，把新的 packs.json 也
    传到资源源根目录，用户点一下就能看到新剧情条目；它们是 missing 状态，
    点开会被引导去下载对应的包。
    """
    import time
    from . import net
    cfg = links()
    base = (cfg.get("base") or "").rstrip("/")
    if not base:
        raise RuntimeError("还没有配置下载源：到「设置 → 下载源」填上你的存储地址")
    url = base + "/packs.json"
    try:
        txt = net.fetch_text(url)
        remote = json.loads(txt)
    except Exception as e:                                       # noqa: BLE001
        raise RuntimeError(f"拉取 {url} 失败：{e}") from e
    if not isinstance(remote.get("depots"), list):
        raise RuntimeError("远端 packs.json 格式不对（缺 depots）")

    old = manifest(force=True)
    old_ids = {d["id"] for d in old.get("depots", [])}
    new_ids = {d["id"] for d in remote.get("depots", [])}
    new_units = [u for u in (remote.get("units") or {}) if u not in (old.get("units") or {})]

    p = os.path.join(paths.DATA, "packs.json")
    if os.path.exists(p):
        try:
            os.replace(p, os.path.join(paths.DATA, "packs.prev.json"))
        except OSError:
            pass
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(remote, f, ensure_ascii=False, separators=(",", ":"))
    manifest(force=True)
    return {
        "ok": True,
        "generated": remote.get("generated", ""),
        "newDepots": sorted(new_ids - old_ids),
        "goneDepots": sorted(old_ids - new_ids),
        "newUnits": new_units,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": url,
    }


def coverage() -> dict:
    """整体进度：已装文件数 / 总文件数。"""
    M = manifest()
    tot = M.get("totals", {})
    ins = installed()
    files = sum(v.get("files", 0) for v in ins.values())
    byt = sum(v.get("bytes", 0) for v in ins.values())
    return {
        "files": files, "filesTotal": tot.get("files", 0),
        "bytes": byt, "bytesTotal": tot.get("bytes", 0),
        "depots": len(ins), "depotsTotal": tot.get("depots", 0),
        "percent": round(100.0 * files / tot["files"], 1) if tot.get("files") else 0.0,
    }
