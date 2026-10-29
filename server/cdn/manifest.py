"""资源清单（Manifest）：下载、解密、加载、对比。

清单是整个增量更新的入口。三条必须记住的事实（踩过坑）：

1. **必须带浏览器 UA**，否则 CDN 返回 404。
2. ``size`` 是唯一可靠的完整性校验字段；``hash`` 是 Unity 打包期的 Hash128
   （不是文件 md5），**不能**用来校验内容，但它是很好的"版本指纹"——
   hash 变了就说明这个包的内容更新了。
3. ``Manifest1Ex`` 是新资产、``Manifest2Ex`` 是旧资产（含分层立绘），两者
   都还在 CDN 上，缺任何一个都会漏资源。
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import time
import zlib

from . import aes
from .config import AES_IV, AES_KEY, MANIFEST_DIR, MANIFEST_NAMES, ROOT
from .net import head, http_get

# 旧脚本读取的清单位置（保持兼容，refresh 后会同步更新这里）
LEGACY_NAME = "%s.json"
PREV_NAME = "%s.prev.json"


def _raw_path(name: str) -> str:
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    return os.path.join(MANIFEST_DIR, name + ".raw")


def _json_path(name: str) -> str:
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    return os.path.join(MANIFEST_DIR, name + ".json")


def download_raw(name: str) -> tuple[str, bytes]:
    """下载清单密文（Base64 文本）。返回 ``(HTTP Last-Modified, 原始字节)``。"""
    from . import config
    base = config.cdn_base()
    if not base:
        raise RuntimeError("还没有配置资源源地址（见「设置 → 下载源」）")
    url = base + name
    info = head(url)
    blob = http_get(url, timeout=120)
    with open(_raw_path(name), "wb") as f:
        f.write(blob)
    return info.get("Last-Modified", ""), blob


def decode(raw: bytes) -> dict:
    """Base64 -> AES-256-CBC ->（必要时解压）-> JSON 字典。"""
    s = b"".join(raw.split())                # 剥掉换行/空白
    data = base64.b64decode(s + b"=" * (-len(s) % 4))
    plain = aes.cbc_decrypt(data, AES_KEY, AES_IV)

    payload = plain
    if plain[:1] not in (b"{", b"["):
        for fn in (gzip.decompress, zlib.decompress,
                   lambda x: zlib.decompress(x, -15)):
            try:
                payload = fn(plain)
                break
            except Exception:
                continue
    obj = json.loads(payload.decode("utf-8-sig"))
    if isinstance(obj, dict) and "data" not in obj:
        raise ValueError("清单格式异常：顶层没有 'data' 字段")
    return obj


def load_index(files: dict | None = None) -> dict:
    """加载全部清单，返回 ``{bundle_name: entry}``。

    ``entry`` 额外带一个 ``_src`` 字段标注入清单来源，便于排错。
    """
    index: dict = {}
    for name in MANIFEST_NAMES:
        path = files.get(name) if files else None
        if not path:
            cand = _json_path(name)
            path = cand if os.path.exists(cand) else os.path.join(ROOT, LEGACY_NAME % name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)["data"]
        for x in data:
            x = dict(x)
            x["_src"] = name
            index.setdefault(x["bundle_name"], x)
    return index


def has_manifest() -> bool:
    """清单是否就绪。

    两处都认：``userdata/cdn/<name>.json``（本包写的位置）与 ``<项目根>/<name>.json``
    （旧工作区遗留）。**只查项目根会漏掉随仓库分发的种子清单**，导致首次启动
    白白重新下载 + 解密 3.5 MB（纯 Python AES 要几十秒）。
    """
    for n in MANIFEST_NAMES:
        if os.path.exists(_json_path(n)) or \
                os.path.exists(os.path.join(ROOT, LEGACY_NAME % n)):
            return True
    return False


def refresh(names=MANIFEST_NAMES, install: bool = True) -> dict:
    """重新拉取清单、解密、落盘（旧文件备份成 ``.prev.json`` 供 diff）。

    ``install`` 参数保留只为兼容旧调用；现在一律写到 ``MANIFEST_DIR``，
    不再往项目根写文件（那里是要提交进仓库的干净目录）。

    注意耗时：清单约 5 MB，AES-256-CBC 用纯标准库实现要 **30–60 秒**（CPU 单核）。
    这是"检查资源列表更新"要等一会儿的原因，不是卡死。
    """
    out = {}
    for name in names:
        lm, raw = download_raw(name)
        try:
            obj = decode(raw)
        finally:
            # 原始密文用完就删：长期要留的是解密后的 .json。
            # 以前不管成功、失败还是用户中途取消，都会在 userdata/cdn/ 里
            # 留一份 3–4 MB 的 .raw（取消时还是半截文件），纯占地方。
            try:
                os.remove(_raw_path(name))
            except OSError:
                pass
        cur = _json_path(name)
        prev = os.path.join(MANIFEST_DIR, PREV_NAME % name)
        if os.path.exists(cur):
            try:
                os.replace(cur, prev)        # 旧版本留作 diff 基线
            except OSError:
                pass
        with open(cur, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        out[name] = {"bundles": len(obj.get("data", [])), "last_modified": lm}
        print(f"  {name}: {len(obj.get('data', []))} 个包  Last-Modified={lm or '-'}")
    return out


def diff_snapshots(names=MANIFEST_NAMES) -> dict:
    """对比 ``<name>.prev.json``（上次刷新）与当前 json，找出增删改。

    返回 ``{"added": {name:[...]}, "removed": {...}, "changed": {...}}``，
    其中 ``changed`` 用 ``(bundle_name, old_hash, new_hash, size)`` 三元组描述。
    """
    res: dict = {"added": {}, "removed": {}, "changed": {}}
    for name in names:
        prev_p = os.path.join(ROOT, PREV_NAME % name)
        cur_p = os.path.join(ROOT, LEGACY_NAME % name)
        if not (os.path.exists(prev_p) and os.path.exists(cur_p)):
            continue
        prev = {x["bundle_name"]: x for x in json.load(open(prev_p, encoding="utf-8"))["data"]}
        cur = {x["bundle_name"]: x for x in json.load(open(cur_p, encoding="utf-8"))["data"]}
        added = [b for b in cur if b not in prev]
        removed = [b for b in prev if b not in cur]
        changed = [(b, prev[b]["hash"], cur[b]["hash"], cur[b]["size"])
                   for b in cur.keys() & prev.keys() if prev[b]["hash"] != cur[b]["hash"]]
        res["added"][name] = added
        res["removed"][name] = removed
        res["changed"][name] = changed
    return res


def summary() -> str:
    """一行摘要，给 ``doctor`` / ``info`` 用。"""
    parts = []
    for name in MANIFEST_NAMES:
        p = os.path.join(ROOT, LEGACY_NAME % name)
        if os.path.exists(p):
            st = os.stat(p)
            n = len(json.load(open(p, encoding="utf-8"))["data"])
            parts.append(f"{name}={n}包 ({time.strftime('%Y-%m-%d', time.localtime(st.st_mtime))})")
        else:
            parts.append(f"{name}=缺失")
    return "  ".join(parts)
