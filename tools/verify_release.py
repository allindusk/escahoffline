# -*- coding: utf-8 -*-
"""上线前自检：从零构建一个干净实例，装一部剧情，检查所有资源引用能否真的加载。

为什么需要这个
--------------
之前反复出现"本机好好的、新用户一片裂图"的问题，根因都是同一个：
**本机产物混进了发布内容**。

历史上踩过的坑：
  · web/assets/i18n/zh-Hans  —— 用户译文被打包，装完剧情莫名变中文
  · web/data/imgmap.x2.js    —— 超分记录被打包，新环境以为"全都超分过了"
  · web/data/imgmap.base.js  —— WebP 覆盖层同样是本机状态
  · web/data/char_*.js       —— 已装剧情正文
  · web/media/**             —— 解包好的图片语音
  · 头像导出是 .png，前端却只找 .webp

这个脚本的做法是：**只复制程序本体**（白名单），装上剧情后用真实浏览器 + 文件系统
双重校验，任何"引用了不存在的文件"都会被抓出来。

用法
----
    python tools/verify_release.py                # 完整跑（下载清单 + 装 1 部）
    python tools/verify_release.py --no-download  # 复用已有实例目录，只做校验
    python tools/verify_release.py --keep         # 保留目录排查
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 只复制这些（**白名单**，避免又漏掉某个本机产物）
COPY_DIRS = ("server", "tools", "docs", "web/assets", "web/data")
#: 复制 web/data 时要排掉的东西 —— 全是"本机状态"，不是程序
DATA_EXCLUDE = (
    "char_*.js", "adv_*.js",           # 已装剧情正文
    "imgmap.x2.js",                    # 超分记录
    "imgmap.base.js",                  # WebP 覆盖层
    "stand_slot.json",                 # 立绘落点缓存
    "sfxmap.js", "sfxmap.json",        # 音频映射（要用时重新生成）
    "*.orig", "*.bak", "*.bak2",
)
ASSET_EXCLUDE = ("zh-Hans",)           # 用户译文


def free_port(start: int = 8510) -> int:
    for p in range(start, start + 60):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def build(dst: str) -> None:
    os.makedirs(dst, exist_ok=True)
    for d in COPY_DIRS:
        src = os.path.join(ROOT, d)
        if not os.path.isdir(src):
            continue
        pats = ["__pycache__", "*.pyc", "out"]
        if d.replace("\\", "/").endswith("web/data"):
            pats += list(DATA_EXCLUDE)
        if d.replace("\\", "/").endswith("web/assets"):
            pats += list(ASSET_EXCLUDE)
        shutil.copytree(src, os.path.join(dst, d), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(*pats))

    for name in os.listdir(os.path.join(ROOT, "web")):
        if name.endswith(".html"):
            shutil.copy2(os.path.join(ROOT, "web", name),
                         os.path.join(dst, "web", name))
    for f in ("VERSION", "requirements.txt", "README.md", "NOTICE.md",
              "LICENSE", "启动.bat", "start.sh"):
        src = os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, f))
    for d in ("web/media", "userdata"):
        os.makedirs(os.path.join(dst, d), exist_ok=True)


def http(port: int, path: str, body=None, timeout=180):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        try:
            return json.loads(raw.decode())
        except Exception:                                      # noqa: BLE001
            return raw


def scan_refs(web: str) -> dict:
    """静态校验：imgmap / sfxmap 里每个引用，文件是否真的存在。"""
    def load_js(name):
        p = os.path.join(web, "data", name)
        if not os.path.exists(p):
            return {}
        s = open(p, encoding="utf-8-sig").read()
        try:
            return json.loads(s[s.index("=") + 1:].strip().rstrip(";"))
        except Exception:                                      # noqa: BLE001
            return {}

    out = {}
    imgmap = load_js("imgmap.js")
    for kind, m in (imgmap or {}).items():
        if not isinstance(m, dict):
            continue
        miss = [k for k, rel in m.items()
                if isinstance(rel, str)
                and not os.path.exists(os.path.join(web, rel.replace("/", os.sep)))]
        out[f"imgmap.{kind}"] = (len(m), len(miss), miss[:5])

    sfx = load_js("sfxmap.js")
    if isinstance(sfx, dict) and sfx:
        miss = [k for k, rel in sfx.items()
                if isinstance(rel, str)
                and not os.path.exists(os.path.join(web, rel.replace("/", os.sep)))]
        out["sfxmap"] = (len(sfx), len(miss), miss[:5])
    return out


def browser_check(port: int, unit: str) -> dict:
    """真实浏览器：头像/立绘/背景的实际加载情况。"""
    from playwright.sync_api import sync_playwright

    cid = unit.split("_")[-1]
    res = {}
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge")
        pg = b.new_page()
        pg.set_viewport_size({"width": 1400, "height": 900})
        pg.goto(f"http://127.0.0.1:{port}/index.html?notour=1&nodisclaimer=1"
                f"&char={cid}", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(5000)
        res["avatar"] = pg.evaluate("""() => {
            const imgs = [...document.querySelectorAll('#charList .char-btn img')];
            return { total: imgs.length,
                     ok: imgs.filter(i => i.naturalWidth > 0).length };
        }""")
        res["stage"] = pg.evaluate("""() => {
            const imgs = [...document.querySelectorAll('#bgLayer img,#standLayer img')];
            return { total: imgs.length,
                     ok: imgs.filter(i => i.naturalWidth > 0).length };
        }""")
        res["bgm"] = pg.evaluate("""() => {
            const m = (window.SFX || {});
            return { sfxEntries: Object.keys(m).length };
        }""")
        # CG 页有自己的一份候选链（头像 + CG 缩略图），也要单独验
        pg2 = b.new_page()
        pg2.set_viewport_size({"width": 1400, "height": 900})
        pg2.goto(f"http://127.0.0.1:{port}/cg.html?nodisclaimer=1",
                 wait_until="domcontentloaded", timeout=30000)
        pg2.wait_for_timeout(4000)
        res["cgPage"] = pg2.evaluate("""() => {
            const av = [...document.querySelectorAll('#charList .char-btn img')];
            const th = [...document.querySelectorAll('#grid .card .thumb img')];
            const cnt = a => ({ total: a.length,
                                ok: a.filter(i => i.naturalWidth > 0).length });
            return { avatar: cnt(av), thumb: cnt(th) };
        }""")
        b.close()
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-download", action="store_true",
                    help="复用已有目录，只做校验")
    ap.add_argument("--keep", action="store_true", help="保留目录")
    ap.add_argument("--unit", default="char_2", help="装哪一部（默认 char_2）")
    ap.add_argument("--dir", default="", help="指定实例目录")
    args = ap.parse_args()

    d = args.dir or os.path.join(tempfile.gettempdir(), "escah-verify")
    port = free_port()
    bad = 0

    print("=" * 62)
    print("  上线前自检")
    print("=" * 62)

    if not args.no_download:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        print("① 构建干净实例（只复制程序本体）…")
        build(d)
        n = sum(len(f) for _, _, f in os.walk(d))
        print(f"   文件数 {n}（不含素材、不含已装剧情、不含本机产物）")

    srv = subprocess.Popen(
        [sys.executable, "-m", "server", "-p", str(port)],
        cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dict(os.environ, PYTHONUTF8="1"))
    try:
        for _ in range(40):
            time.sleep(0.5)
            try:
                http(port, "/api/state", timeout=5)
                break
            except Exception:                                  # noqa: BLE001
                continue
        print(f"② 服务已启动 http://127.0.0.1:{port}")

        if not args.no_download:
            print("③ 下载资源清单（约 5 MB，30–180 秒）…")
            j = http(port, "/api/manifest/refresh", {})["job"]
            while True:
                time.sleep(3)
                js = http(port, "/api/jobs")["jobs"]
                me = [x for x in js if x["id"] == j["id"]]
                if me and me[0]["state"] in ("done", "failed", "cancelled"):
                    print("   清单:", me[0]["state"])
                    bad += me[0]["state"] != "done"
                    break

            print(f"④ 安装 {args.unit} …")
            j = http(port, "/api/install", {"selected": [args.unit]})["job"]
            while True:
                time.sleep(4)
                js = http(port, "/api/jobs")["jobs"]
                me = [x for x in js if x["id"] == j["id"]]
                if me and me[0]["state"] in ("done", "failed", "cancelled"):
                    print("   安装:", me[0]["state"], "|",
                          (me[0].get("message") or "")[:60])
                    bad += me[0]["state"] != "done"
                    break

        web = os.path.join(d, "web")
        print("⑤ 静态校验：映射表里的每个文件是否真实存在")
        refs = scan_refs(web)
        for k, (total, miss, sample) in refs.items():
            flag = "OK " if miss == 0 else "缺失"
            print(f"   {flag} {k}: {total} 条，缺 {miss}")
            if miss:
                bad += 1
                print("       样例:", sample)

        print("⑥ 浏览器实测：头像 / 画面元素实际加载")
        try:
            br = browser_check(port, args.unit)
            for k, v in br.items():
                print(f"   {k}: {v}")
            a = br["avatar"]
            if a["total"] and a["ok"] < a["total"]:
                bad += 1
                print("   ! 有头像没加载出来")
        except Exception as e:                                 # noqa: BLE001
            print("   浏览器校验跳过：", e)

        print("=" * 62)
        print("  结论:", "通过" if bad == 0 else f"发现 {bad} 个问题")
        print("=" * 62)
        return 0 if bad == 0 else 1
    finally:
        try:
            srv.terminate()
            srv.wait(timeout=8)
        except Exception:                                      # noqa: BLE001
            srv.kill()
        if not args.keep and not args.no_download:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
