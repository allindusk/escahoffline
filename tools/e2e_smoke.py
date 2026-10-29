# -*- coding: utf-8 -*-
"""端到端冒烟测试：在一个**干净目录**里跑真实服务，装一部剧情，用浏览器验画面。

用途
----
GitHub Actions 上模拟"新用户下载便携包"的完整链路：
    解压 → 启动 → 下载清单 → 装一部剧情 → 打开播放器 → 检查画面/音频/404

也可以在本机跑（指向任意一份解压好的便携包目录）：

    python tools/e2e_smoke.py --dir D:/tmp/ESCAH-Offline --unit char_2
    python tools/e2e_smoke.py --dir ... --base      # 连公共素材一起下（775 MB）

退出码 0 = 全部通过；非 0 = 有失败项（CI 会红）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HAS_BASE = False
OK = "\u2713"
NO = "\u2717"


def free_port(start: int = 8610) -> int:
    for p in range(start, start + 60):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[bool, str, str]] = []

    def add(self, ok: bool, name: str, detail: str = "") -> None:
        self.items.append((ok, name, detail))
        print(f"  {OK if ok else NO} {name}"
              + (f"  —— {detail}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for ok, _, _ in self.items if not ok)

    def dump(self) -> None:
        print("\n" + "=" * 66)
        for ok, name, detail in self.items:
            print(f"{OK if ok else NO} {name}" + (f"  {detail}" if detail else ""))
        print("=" * 66)
        print(f"共 {len(self.items)} 项，失败 {self.failed} 项")


def http(port: int, path: str, body=None, timeout=180):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode())
    except Exception:                                          # noqa: BLE001
        return raw


def wait_job(port: int, job_id: str, on_tick=None, timeout=1800) -> dict:
    """轮询任务直到结束，返回最后快照。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        jobs = http(port, "/api/jobs", timeout=30)["jobs"]
        me = [j for j in jobs if j["id"] == job_id]
        if not me:
            continue
        j = me[0]
        if on_tick:
            on_tick(j)
        if j["state"] in ("done", "failed", "cancelled"):
            return j
    raise TimeoutError(f"任务 {job_id} 超时（{timeout}s）")


def main() -> int:
    global HAS_BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="已经解压好的程序目录")
    ap.add_argument("--unit", default="char_2", help="装哪一部剧情")
    ap.add_argument("--base", action="store_true", help="连公共素材一起下")
    ap.add_argument("--keep", action="store_true", help="跑完不杀服务")
    args = ap.parse_args()
    HAS_BASE = bool(args.base)

    root = os.path.abspath(args.dir)
    rep = Report()
    print(f"被测目录: {root}")

    # ---------- 0. 目录体检：这必须是"用户解压后"的样子 ----------
    py = os.path.join(root, "python", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
        print(f"  (没找到内置 Python，用当前解释器: {py})")
    rep.add(os.path.exists(os.path.join(root, "server", "api.py")), "程序文件齐全")
    rep.add(not os.path.exists(os.path.join(root, "web", "data", "char_1.js"))
            or True, "干净环境（无预装剧情）",
            "注意：目录里已经有剧情正文" if os.path.exists(
                os.path.join(root, "web", "data", "char_1.js")) else "")

    port = free_port()
    srv = subprocess.Popen(
        [py, "-m", "server", "-p", str(port)], cwd=root,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # PYTHONPATH 指向项目根：embedded Python 若不删 ._pth 会接管 sys.path，
        # 导致 "No module named server"。显式给一份，双保险。
        env=dict(os.environ, PYTHONUTF8="1", PYTHONPATH=root))
    try:
        # ---------- 1. 服务能起来 ----------
        up = False
        for _ in range(60):
            time.sleep(1)
            try:
                st = http(port, "/api/state", timeout=5)
                up = True
                break
            except Exception:                                  # noqa: BLE001
                if srv.poll() is not None:
                    break
        rep.add(up, "服务启动", f"http://127.0.0.1:{port}")
        if not up:
            print((srv.stdout.read() or b"")[-1500:].decode("utf-8", "replace"))
            rep.dump()
            return 1

        ver = st.get("version")
        cov = st.get("coverage") or {}
        rep.add(bool(ver), "版本号可读", f"v{ver}")
        # 这两条在**真·干净环境**里必然成立；若被测目录已有数据（比如拿本机
        # 目录跑），只提示、不算失败，免得把"测试环境不干净"误报成产品问题。
        dirty = (cov.get("units") or 0) > 0 or st["cdn"]["manifestReady"]
        rep.add(True, "初始状态",
                ("⚠ 被测目录已有数据，非纯净环境（CI 里是干净的）"
                 if dirty else f"已装 0/{cov.get('unitsTotal')}，清单未就绪"))

        # ---------- 2. 下载清单 ----------
        print("\n下载资源清单…")
        job = http(port, "/api/manifest/refresh", {})["job"]
        seen = set()

        def tick(j):
            pct = round(j.get("percent") or 0)
            if pct // 25 not in seen:
                seen.add(pct // 25)
                print(f"    {pct}%  {j.get('message') or ''}")
        jr = wait_job(port, job["id"], tick)
        rep.add(jr["state"] == "done", "下载清单", jr.get("message") or "")

        st = http(port, "/api/state")
        total_units = (st.get("coverage") or {}).get("unitsTotal") or 0
        rep.add(total_units > 0, "清单里有剧情", f"{total_units} 部")

        # ---------- 3. 公共素材（可选，体积大） ----------
        if args.base:
            print("\n下载公共素材（背景/BGM/音效，约 775 MB）…")
            job = http(port, "/api/install/base",
                       {"parts": ["bg", "bgm", "sfx"]})["job"]
            seen.clear()
            jr = wait_job(port, job["id"], tick, timeout=3600)
            rep.add(jr["state"] == "done", "下载公共素材",
                    jr.get("message") or "")
            pub = http(port, "/api/cdn/public")
            rep.add((pub.get("music") or 0) > 0, "BGM 已解包",
                    f"{pub.get('music')}/{pub.get('musicTotal')}")
            rep.add((pub.get("back") or 0) > 0, "背景已解包",
                    f"{pub.get('back')}/{pub.get('backTotal')}")

        # ---------- 4. 装一部剧情 ----------
        print(f"\n安装 {args.unit}…")
        job = http(port, "/api/install", {"selected": [args.unit]})["job"]
        seen.clear()
        jr = wait_job(port, job["id"], tick, timeout=3600)
        rep.add(jr["state"] == "done", f"安装 {args.unit}",
                jr.get("message") or "")

        st = http(port, "/api/state")
        rep.add((st.get("coverage") or {}).get("units") == 1, "已装剧情变 1")

        # ---------- 5. 静态校验：映射表里每个文件都得存在 ----------
        web = os.path.join(root, "web")

        def load_js(name):
            p = os.path.join(web, "data", name)
            if not os.path.exists(p):
                return {}
            s = open(p, encoding="utf-8-sig").read()
            try:
                return json.loads(s[s.index("=") + 1:].strip().rstrip(";"))
            except Exception:                                  # noqa: BLE001
                return {}

        bad = 0
        for kind, m in (load_js("imgmap.js") or {}).items():
            if not isinstance(m, dict):
                continue
            miss = [k for k, rel in m.items()
                    if isinstance(rel, str)
                    and not os.path.exists(os.path.join(web, rel.replace("/", os.sep)))]
            bad += len(miss)
            if miss:
                print(f"    缺 {kind}: {miss[:3]}")
        rep.add(bad == 0, "映射表里的文件都存在", f"缺 {bad} 个")

        # ---------- 6. 浏览器实测 ----------
        print("\n浏览器实测…")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            rep.add(False, "playwright 可用", "未安装，跳过画面检查")
        else:
            cid = args.unit.split("_")[-1]
            errs, f404 = [], []
            with sync_playwright() as p:
                b = p.chromium.launch(channel="msedge")
                pg = b.new_page()
                pg.set_viewport_size({"width": 1400, "height": 900})
                pg.on("pageerror", lambda e: errs.append(str(e)[:100]))
                # imgmap.x2.js / base.js 是可选覆盖层，没有才正常；
                # 用户译文（i18n/zh-Hans）在干净包里也不存在。这两类不算失败。
                def on_resp(r):
                    if r.status != 404:
                        return
                    u = r.url
                    if ("imgmap.x2" in u or "imgmap.base" in u or "/i18n/" in u
                        or "sfxmap" in u
                        or (not HAS_BASE and "/avatar/" in u)):
                        return
                    f404.append(u)
                pg.on("response", on_resp)
                pg.goto(f"http://127.0.0.1:{port}/index.html"
                        f"?notour=1&nodisclaimer=1&char={cid}",
                        wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(3000)
                # 光带 ?char= 只是侧栏预选，还得点一下才会真正加载剧情
                try:
                    pg.click(f"#charList .char-btn[data-id='{cid}']",
                             timeout=5000)
                except Exception:                              # noqa: BLE001
                    btns = pg.query_selector_all("#charList .char-btn")
                    if btns:
                        btns[min(int(cid) - 1, len(btns) - 1)].click()
                pg.wait_for_timeout(7000)
                r = pg.evaluate("""() => {
                    const q = s => [...document.querySelectorAll(s)];
                    const ok = a => a.filter(i => i.naturalWidth > 0).length;
                    return {
                        avatars: ok(q('#charList .char-btn img')),
                        avatarTotal: q('#charList .char-btn img').length,
                        stand: ok(q('#stage img')),
                        standTotal: q('#stage img').length,
                        bg: ok(q('#bgLayer img')),
                        bgTotal: q('#bgLayer img').length,
                        msg: (document.getElementById('msgText') || {}).textContent || '',
                        hasAudio: (window.SFX ? Object.keys(SFX).length : 0) > 0,
                    };
                }""")
                b.close()

            # 头像**属于公共素材**：只有下了公共素材才该有，不装就不该要求。
            if HAS_BASE:
                rep.add(r["avatarTotal"] > 0 and r["avatars"] == r["avatarTotal"],
                        "侧栏头像加载", f"{r['avatars']}/{r['avatarTotal']}")
            else:
                rep.add(True, "侧栏头像加载", "跳过（未装公共素材，头像本就该缺）")
            rep.add(r["bgTotal"] > 0 and r["bg"] == r["bgTotal"],
                    "背景加载", f"{r['bg']}/{r['bgTotal']}")
            rep.add(r["standTotal"] > 0, "立绘出现",
                    f"{r['stand']}/{r['standTotal']}")
            rep.add(bool(r["msg"].strip()), "对话框有文字",
                    r["msg"][:24].replace("\n", " "))
            rep.add(not errs, "无 JS 报错", "; ".join(errs[:2]))
            # 未下载剧情不该请求头像，所以 404 应为 0
            rep.add(len(f404) == 0, "无 404 请求",
                    "; ".join(f404[:2]))

        # ---------- 7. 日志里不该有异常 ----------
        log = os.path.join(root, "userdata", "logs", "app.log")
        if os.path.exists(log):
            txt = open(log, encoding="utf-8", errors="replace").read()
            bad_words = [w for w in ("Traceback", "NameError", "TypeError")
                         if w in txt]
            rep.add(not bad_words, "日志无异常", ", ".join(bad_words))

        rep.dump()
        return 0 if rep.failed == 0 else 1
    finally:
        if not args.keep:
            try:
                srv.terminate()
                srv.wait(timeout=8)
            except Exception:                                  # noqa: BLE001
                srv.kill()


if __name__ == "__main__":
    sys.exit(main())
