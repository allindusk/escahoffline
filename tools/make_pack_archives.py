#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地素材打成「资源包」zip（维护者用，不在 CI 里跑）

为什么不在 CI 里打
------------------
素材不在仓库里（20 GB + 版权），GitHub 的 Actions 拿不到，也不合适让它代管
分发。资源包应该由**你自己**在本地打好，上传到你自己的对象存储 / 网盘，
再把地址填进 ``userdata/links.json`` 或 ``web/data/packs.links.js``。

包的内容和划分完全由 ``tools/build_packs.py`` 决定（同一份引用分析），
本脚本只是把它的结论落成 zip，所以不会出现"清单里有、包里没有"的错位。

用法::

    python tools/make_pack_archives.py                    # 打全部包（20 GB，很久）
    python tools/make_pack_archives.py --only core,shared_back,char_1
    python tools/make_pack_archives.py --list             # 只看要怎么分
    python tools/make_pack_archives.py --workers 4        # 并行打包
    python tools/make_pack_archives.py --update-only      # 只打上次之后变过的（按时间戳）

产物::

    dist/packs/<包名>.zip          每个包一个 zip（内部是站点相对路径）
    dist/packs/packs.json          清单副本，用户「检查资源列表更新」会拉它
    dist/packs/_index.json         每个包的体积 / 文件数 / sha256（发布时对账用）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
OUT = os.path.join(ROOT, "dist", "packs")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_packs                                        # noqa: E402


def sha256_of(path: str, buf: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def make_one(depot_id: str, files: list[str], compress: int = 3) -> dict:
    dst = os.path.join(OUT, f"{depot_id}.zip")
    total = 0
    written = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=compress) as z:
        for rel in sorted(files):
            src = os.path.join(WEB, rel.replace("/", os.sep))
            if not os.path.exists(src):
                continue
            z.write(src, rel)
            total += os.path.getsize(src)
            written += 1
    return {"id": depot_id, "files": written, "srcBytes": total,
            "zipBytes": os.path.getsize(dst), "sha256": sha256_of(dst)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成资源包 zip")
    ap.add_argument("--only", default="", help="只打这些包（逗号分隔）")
    ap.add_argument("--list", action="store_true", help="只打印分配结果")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--compress", type=int, default=3, help="zip 压缩级别 1-9")
    ap.add_argument("--update-only", action="store_true",
                    help="只打比已有 zip 更新过的包")
    a = ap.parse_args(argv)

    manifest, depot_files, _units = build_packs.build(build_packs.SHARE_MIN)
    ids = sorted(depot_files)
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        ids = [i for i in ids if i in want]
        missing = want - set(ids)
        if missing:
            print(f"  ! 不存在的包：{', '.join(sorted(missing))}")

    if a.list:
        for i in ids:
            n = len(depot_files[i])
            b = sum(os.path.getsize(os.path.join(WEB, f))
                    for f in depot_files[i] if os.path.exists(os.path.join(WEB, f)))
            print(f"  {i:<18}{b / 1024 ** 2:9.1f} MB {n:>7,} 文件")
        print(f"\n共 {len(ids)} 个包")
        return 0

    os.makedirs(OUT, exist_ok=True)
    print(f"输出目录：{OUT}")

    todo = []
    for i in ids:
        dst = os.path.join(OUT, f"{i}.zip")
        if a.update_only and os.path.exists(dst):
            newest = 0.0
            for f in depot_files[i]:
                p = os.path.join(WEB, f.replace("/", os.sep))
                try:
                    newest = max(newest, os.path.getmtime(p))
                except OSError:
                    pass
            if newest <= os.path.getmtime(dst):
                continue
        todo.append(i)

    if not todo:
        print("没有需要更新的包")
        _write_meta(manifest, ids)
        return 0

    print(f"将打包 {len(todo)} 个包（并行 {a.workers}）…")
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(make_one, i, list(depot_files[i]), a.compress): i for i in todo}
        for n, fut in enumerate(futs, 1):
            i = futs[fut]
            try:
                r = fut.result()
                results.append(r)
                print(f"  [{n}/{len(todo)}] {i:<18} "
                      f"{r['zipBytes'] / 1024 ** 2:8.1f} MB  {r['files']:,} 文件")
            except Exception as e:                              # noqa: BLE001
                print(f"  [X] {i}: {e}")

    _write_meta(manifest, ids)
    total = sum(r["zipBytes"] for r in results)
    print(f"\n完成 {len(results)} 个包，共 {total / 1024 ** 3:.2f} GB，"
          f"用时 {time.time() - t0:.0f}s")
    print(f"把 {OUT} 整个目录上传到你的存储，"
          f"然后在「设置 → 下载源」填基础地址即可。")
    return 0


def _write_meta(manifest: dict, ids: list[str]) -> None:
    # 清单副本：用户的「检查资源列表更新」就是拉这个文件
    with open(os.path.join(OUT, "packs.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, separators=(",", ":"))
    idx = {}
    for i in ids:
        p = os.path.join(OUT, f"{i}.zip")
        if os.path.exists(p):
            idx[i] = {"bytes": os.path.getsize(p), "sha256": sha256_of(p)}
    with open(os.path.join(OUT, "_index.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump({"generated": manifest.get("generated"),
                   "packs": idx}, f, ensure_ascii=False, indent=1)
    print(f"  写入 packs.json 与 _index.json")


if __name__ == "__main__":
    sys.exit(main())
