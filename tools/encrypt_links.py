#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把下载源地址加密进 web/data/packs.links.js

为什么加密
----------
不是为了"安全"（客户端必然能解），而是：

1. 仓库与网页源码里不出现明文直链 —— 避免被搜索引擎、爬虫、镜像站抓走
2. 链接要换的时候只改这一个文件，不用翻代码
3. 想完全绕过也简单：``userdata/links.json`` 里写明文，优先级最高

用法::

    python tools/encrypt_links.py --base https://cdn.example.com/escah/packs
    python tools/encrypt_links.py --base https://cdn.example.com/p --pattern "{id}.zip"
    python tools/encrypt_links.py --base https://x/y --override char_1=https://other/1.zip
    python tools/encrypt_links.py --show        # 打印当前解密出来的内容（自检）
    python tools/encrypt_links.py --key mykey   # 改密钥（web/assets/js/links.js 要同步改）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.packs import RC4_KEY, deobfuscate, obfuscate      # noqa: E402

OUT = os.path.join(ROOT, "web", "data", "packs.links.js")
JS = os.path.join(ROOT, "web", "assets", "js", "links.js")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成加密的下载源配置")
    ap.add_argument("--base", default="", help="基础地址，如 https://cdn.x.com/packs")
    ap.add_argument("--host", default="",
                    help="配合 --path 使用的主机名，如 cdn.example.com")
    ap.add_argument("--path", default="",
                    help="配合 --host 的路径，如 choukou/offline/packs")
    ap.add_argument("--pattern", default="{id}.zip",
                    help="包名拼接规则，默认 {id}.zip")
    ap.add_argument("--override", action="append", default=[],
                    help="个别包单独指定地址：--override char_1=https://...")
    ap.add_argument("--note", default="", help="备注（也会被加密）")
    ap.add_argument("--key", default=RC4_KEY, help="RC4 密钥（默认与 server 一致）")
    ap.add_argument("--show", action="store_true", help="只看当前配置")
    a = ap.parse_args(argv)

    if a.show:
        if not os.path.exists(OUT):
            print("还没有生成 packs.links.js")
            return 0
        with open(OUT, encoding="utf-8") as f:
            s = f.read()
        m = re.search(r"[\"']([A-Za-z0-9+/=]+)[\"']", s)
        if not m:
            print("文件格式不是预期的 window.__PACK_LINKS=\"...\"")
            return 1
        print(json.dumps(deobfuscate(m.group(1), a.key),
                         ensure_ascii=False, indent=2))
        return 0

    # --path 是 --base 的快捷写法：拼上默认 CDN 主机
    if not a.base and a.path:
        a.base = f"https://{a.host}/{a.path.strip('/')}"
    if not a.base:
        print("需要 --base 或 --path（--show 可查看当前值）")
        return 1

    overrides = {}
    for kv in a.override:
        if "=" not in kv:
            print(f"  ! 忽略格式不对的 --override：{kv}")
            continue
        k, v = kv.split("=", 1)
        overrides[k.strip()] = v.strip()

    # 现在只有一种用途：资源源（AssetBundle 根）。字段名用 cdn，
    # 旧的 zip 分发模式已废弃，base 留空。
    cfg = {"cdn": a.base.rstrip("/"), "base": "", "pattern": a.pattern,
           "overrides": overrides, "note": a.note}
    enc = obfuscate(cfg, a.key)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("/* 下载源配置（RC4+base64 混淆，勿手改；用 tools/encrypt_links.py 生成）\n"
                " * 明文优先级：userdata/links.json > 设置页里填的地址 > 本文件 */\n")
        f.write(f'window.__PACK_LINKS="{enc}";\n')
    print(f"已写入 {os.path.relpath(OUT, ROOT)}（{len(enc)} 字符）")
    print(f"  基础地址：{cfg['base']}")
    if overrides:
        print(f"  单独指定：{len(overrides)} 个")
    if not os.path.exists(JS):
        print("  ! 缺少 web/assets/js/links.js（前端解密用），请确认仓库完整")
    return 0


if __name__ == "__main__":
    sys.exit(main())
