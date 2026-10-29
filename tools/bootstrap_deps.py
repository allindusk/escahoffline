#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首次运行准备依赖：**先测速选最快的镜像，再装**（带实时进度）。

为什么不是一句 ``pip install -r requirements.txt``
-------------------------------------------------
那几个包加起来约 40 MB（numpy 最大）。国内直连 PyPI 官方源经常只有几十 KB/s，
而清华/阿里/腾讯镜像能到几 MB/s —— 差距是"等 20 分钟"和"等 20 秒"。
写死任何一条都是赌运气，所以这里实测后再选。

用法::

    python tools/bootstrap_deps.py            # 缺就装（自动选最快源）
    python tools/bootstrap_deps.py --test     # 只测速，不安装
    python tools/bootstrap_deps.py --yes      # 不问，直接装
    python tools/bootstrap_deps.py --check    # 只检查，缺失返回码 1（给脚本用）

安装过程是直接继承终端跑的，pip 自己的进度条（下载百分比 / 速度 / ETA）用户看得见。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import mirrors                                    # noqa: E402

REQ_FILE = os.path.join(ROOT, "requirements.txt")

#: 硬依赖（缺任一个，安装剧情就会失败）
HARD = [("UnityPy", "UnityPy"), ("lz4", "lz4"),
        ("PIL", "Pillow"), ("numpy", "numpy")]
#: 可选（只影响"刷新清单"快慢）
SOFT = [("cryptography", "cryptography")]


def missing(mods) -> list[str]:
    out = []
    for mod, pkg in mods:
        try:
            if importlib.util.find_spec(mod) is None:
                out.append(pkg)
        except (ImportError, ValueError):
            out.append(pkg)
    return out


def show_routes(kind: str = "pip") -> dict:
    print("正在测速各下载线路（约 5–10 秒）…")
    results = mirrors.test(kind)
    mirrors.save_cache(kind, results)
    print(f"{'线路':<16}{'延迟':>10}{'速度':>14}   状态")
    print("-" * 54)
    for r in results:
        if r.get("ok"):
            print(f"{r['name']:<16}{r['ttfb']:>8} ms{r['kbps']:>11} KB/s   ✓")
        else:
            print(f"{r['name']:<16}{'-':>10}{'-':>14}   ✗ {r.get('error', '')[:28]}")
    ok = [r for r in results if r.get("ok")]
    if ok:
        print(f"\n最快：{ok[0]['name']}")
    else:
        print("\n没有一条能连上，将退回官方源（会很慢）")
    return ok[0] if ok else {}


def do_install(pkgs: list[str], route: dict) -> bool:
    index = (route or {}).get("index") or ""
    cmd = [sys.executable, "-m", "pip", "install",
           "--upgrade", "--disable-pip-version-check",
           "--progress-bar", "on"]
    if index:
        cmd += ["-i", index]
    cmd += pkgs
    print("\n执行：", " ".join(cmd[:6]), "…", " ".join(pkgs))
    print("（下面就是下载进度，速度上不去可以 Ctrl+C 后重跑本脚本换线路）\n")
    try:
        return subprocess.run(cmd, cwd=ROOT).returncode == 0
    except KeyboardInterrupt:
        print("\n已取消。")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="准备运行依赖（自动选最快镜像）")
    ap.add_argument("--test", action="store_true", help="只测速")
    ap.add_argument("--yes", action="store_true", help="不询问直接安装")
    ap.add_argument("--check", action="store_true", help="只检查依赖，缺则返回 1")
    ap.add_argument("--all", action="store_true", help="连可选依赖一起装")
    a = ap.parse_args(argv)

    hard_missing = missing(HARD)
    soft_missing = missing(SOFT)

    if a.check:
        if hard_missing:
            print("缺少：" + "、".join(hard_missing))
            return 1
        return 0

    if a.test:
        show_routes()
        return 0

    if not hard_missing and not soft_missing:
        print("依赖已齐全，无需安装。")
        return 0

    if hard_missing:
        print("缺少必要依赖：" + "、".join(hard_missing))
    if soft_missing and not hard_missing:
        print("缺少可选依赖：" + "、".join(soft_missing)
              + "（不装也能用，只是「刷新资源清单」会慢 30–60 秒）")

    want = hard_missing + (soft_missing if (a.all or not hard_missing) else [])
    if not want:
        return 0

    if not a.yes:
        try:
            ans = input("\n现在自动安装吗？会先测速选最快线路 [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans == "n":
            print("已跳过。手动安装： pip install -r requirements.txt")
            return 0

    print()
    route = show_routes()
    ok = do_install(want, route)
    if not ok:
        # 一条不行就换下一条（很多时候是镜像临时抽风）
        print("\n安装失败，换个线路再试一次…")
        results = mirrors.test()
        for r in results[1:3]:
            if r.get("ok") and do_install(want, r):
                ok = True
                break
    if not ok:
        print("\n[失败] 依赖没装全。可以手动执行：")
        print("  pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1

    left = missing(HARD)
    if left:
        print("[失败] 装完仍缺少：" + "、".join(left))
        return 1
    print("\n[完成] 依赖已就绪，可以启动了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
