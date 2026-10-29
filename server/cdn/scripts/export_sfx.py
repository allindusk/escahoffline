"""下载并导出 BGM / 音效：每个包一个 AudioClip，resource 内为 MP4。

用法：python export_sfx.py music|sound [--limit N] [--offset N]
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import struct
import time
import urllib.parse
import urllib.request

import UnityPy

from unityfs_probe import parse_unityfs

BASE = os.environ.get("ESC_CDN_BASE", "").rstrip("/") + "/" if os.environ.get("ESC_CDN_BASE") else ""
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SRC = "files_audio"
OUT = "../web/media/audio/sfx"
os.makedirs(SRC, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

def _manifest(name):
    """找清单文件。清单现在存在 ``userdata/cdn/``（不再随仓库分发），
    但旧工作区可能放在 cwd 或上一层，三种位置都认。"""
    for p in (os.path.join("cdn", name), name,
              os.path.join("..", name),
              os.path.join("..", "server", "cdn", "data", name)):
        if os.path.exists(p):
            return p
    raise SystemExit(
        f"找不到 {name}：请先在控制台「基础数据 → 下载资源清单」下载一次")


M = (json.load(open(_manifest("Manifest1Ex.json"), encoding="utf-8"))["data"] +
     json.load(open(_manifest("Manifest2Ex.json"), encoding="utf-8"))["data"])
IDX = {x["bundle_name"]: x["size"] for x in M}


def safe(name):
    s = re.sub(r"[^0-9A-Za-z_\-]", "_", name)
    return s or "unnamed"


def split_mp4(res):
    """切出 resource 里的 MP4（通常只有一个，稳妥起见仍按 box 解析）。"""
    out = []
    pos = 0
    n = len(res)
    while pos + 8 <= n:
        size = struct.unpack(">I", res[pos:pos + 4])[0]
        typ = res[pos + 4:pos + 8]
        if size == 1 and pos + 16 <= n:
            size = struct.unpack(">Q", res[pos + 8:pos + 16])[0]
        if size < 8 or pos + size > n:
            break
        if typ == b"ftyp" or not out:
            out.append([pos, pos + size])
        else:
            out[-1][1] = pos + size
        pos += size
    if not out:
        return [res]
    return [res[a:b] for a, b in out]


def dl(x):
    p = os.path.join(SRC, x["bundle_name"])
    if os.path.exists(p) and os.path.getsize(p) == IDX[x["bundle_name"]]:
        return "skip"
    try:
        req = urllib.request.Request(
            BASE + urllib.parse.quote(x["bundle_name"], safe=""),
            headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=180) as r:
            b = r.read()
        open(p, "wb").write(b)
        return "ok"
    except Exception as e:
        return f"fail:{e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["music", "sound"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    args = ap.parse_args()

    pref = f"bundle_{args.kind}_"
    items = [x for x in M if x["bundle_name"].startswith(pref)]
    total = sum(x["size"] for x in items)
    print(f"[{args.kind}] {len(items)} 个包，{total/1048576:.1f} MB", flush=True)

    # 1) 下载
    t0 = time.time()
    st = {"ok": 0, "skip": 0, "fail": 0}
    with cf.ThreadPoolExecutor(max_workers=48) as ex:
        for r in ex.map(dl, items):
            if r == "ok":
                st["ok"] += 1
            elif r == "skip":
                st["skip"] += 1
            else:
                st["fail"] += 1
    print(f"  下载 {st}  {time.time()-t0:.0f}s", flush=True)

    # 2) 导出
    tasks = items[args.offset:]
    if args.limit:
        tasks = tasks[:args.limit]
    t0 = time.time()
    n_ok = n_skip = n_fail = 0
    mapping = {}
    for i, x in enumerate(tasks, 1):
        name = x["bundle_name"]
        path = os.path.join(SRC, name)
        if not os.path.exists(path):
            n_fail += 1
            continue
        try:
            env = UnityPy.load(path)
            clip = None
            for obj in env.objects:
                if obj.type.name == "AudioClip":
                    clip = obj.read()
                    break
            if clip is None:
                n_fail += 1
                continue
            cid = clip.m_Name
            fn = safe(cid) + ".m4a"
            out = os.path.join(OUT, fn)
            mapping[cid] = "audio/sfx/" + fn
            if os.path.exists(out):
                n_skip += 1
                continue
            payload, nodes, *_ = parse_unityfs(open(path, "rb").read())
            res = None
            for off, sz, nf, nm in nodes:
                if nm.endswith(".resource"):
                    res = payload[off:off + sz]
                    break
            if res is None:
                n_fail += 1
                continue
            clips = split_mp4(res)
            open(out, "wb").write(clips[0])
            n_ok += 1
            n_skip += len(clips) - 1
        except Exception:
            n_fail += 1
        if i % 100 == 0:
            print(f"  [{i}/{len(tasks)}] ok={n_ok} skip={n_skip} fail={n_fail} "
                  f"{time.time()-t0:.0f}s", flush=True)

    print(f"  导出 ok={n_ok} skip={n_skip} fail={n_fail}  {time.time()-t0:.0f}s")

    # 3) 合并映射表
    mp = os.path.join("../web/data", "sfxmap.json")
    old = {}
    if os.path.exists(mp):
        old = json.load(open(mp, encoding="utf-8"))
    old.update(mapping)
    json.dump(old, open(mp, "w", encoding="utf-8"), ensure_ascii=False,
              separators=(",", ":"))
    print(f"  映射表累计 {len(old)} 条 -> web/data/sfxmap.json")


if __name__ == "__main__":
    main()
