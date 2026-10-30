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
import shutil
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
    """ok=True/False/"skip"。

    三条原则（都是踩过坑才立起来的）：
    * 断言必须能真的失败 —— 不写 ``x or True``、不写 ``isinstance(x, dict)``
      这种"只要返回 JSON 就算过"的假断言；
    * 异常不能当通过 —— 该报失败就报失败，确实测不到的用 ``skip`` 明说；
    * 无论中途发生什么都得 dump 出汇总表，否则 CI 里只剩一句 exit 1。
    """

    def __init__(self) -> None:
        self.items: list[tuple[object, str, str]] = []

    def add(self, ok: bool, name: str, detail: str = "") -> None:
        self.items.append((bool(ok), name, detail))
        print(f"  {OK if ok else NO} {name}"
              + (f"  —— {detail}" if detail else ""))

    def skip(self, name: str, detail: str = "") -> None:
        self.items.append(("skip", name, detail))
        print(f"  － {name}" + (f"  —— {detail}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for ok, _, _ in self.items if ok is False)

    @property
    def skipped(self) -> int:
        return sum(1 for ok, _, _ in self.items if ok == "skip")

    def dump(self) -> None:
        print("\n" + "=" * 66)
        for ok, name, detail in self.items:
            mark = OK if ok is True else ("－" if ok == "skip" else NO)
            print(f"{mark} {name}" + (f"  {detail}" if detail else ""))
        print("=" * 66)
        print(f"共 {len(self.items)} 项，失败 {self.failed} 项，"
              f"跳过 {self.skipped} 项")


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
    ap.add_argument("--full", action="store_true",
                    help="连很慢的也跑：实际超分 + 导出视频（多花十几分钟）")
    args = ap.parse_args()
    HAS_BASE = bool(args.base)

    root = os.path.abspath(args.dir)
    cid = args.unit.split("_")[-1]          # char_2 -> 2（多处要用，别在分支里定义）
    rep = Report()
    print(f"被测目录: {root}")

    # ---------- 0. 目录体检：这必须是"用户解压后"的样子 ----------
    py = os.path.join(root, "python", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
        print(f"  (没找到内置 Python，用当前解释器: {py})")
    rep.add(os.path.exists(os.path.join(root, "server", "api.py")), "程序文件齐全")
    dirty_files = [f for f in ("char_1.js", "char_2.js", "adv_main_1.js")
                   if os.path.exists(os.path.join(root, "web", "data", f))]
    rep.add(not dirty_files, "干净环境（未预装剧情）",
            "，".join(dirty_files) if dirty_files
            else "web/data 里只有程序自带的索引")

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
        cdn_st = st.get("cdn") or {}
        dirty = (cov.get("units") or 0) > 0 or cdn_st.get("manifestReady")
        rep.skip("初始状态（干净 / 已有数据）",
                 ("被测目录已有数据，非纯净环境（CI 里是干净的）"
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
            errs, f404 = [], []
            # 整段包 try：浏览器起不来 / goto 超时也得把汇总表打出来。
            # 否则异常直接冲出 main，CI 上只剩一句 "exit 1"，什么线索都没有。
            r = {"avatars": 0, "avatarTotal": 0, "stand": 0, "standTotal": 0,
                 "bg": 0, "bgTotal": 0, "msg": "", "hasAudio": False}
            browser_ok = False
            try:
                with sync_playwright() as p:
                    b = p.chromium.launch(channel="msedge")
                    pg = b.new_page()
                    pg.set_viewport_size({"width": 1400, "height": 900})
                    pg.on("pageerror", lambda e: errs.append(str(e)[:100]))

                    # imgmap.x2.js / base.js 是可选覆盖层，没有才正常；
                    # 用户译文（i18n/zh-Hans）在干净包里也不存在。这几类不算失败。
                    def on_resp(resp):
                        if resp.status != 404:
                            return
                        u = resp.url
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
                    # 光带 ?char= 只是侧栏预选，还得点一下才会真正加载剧情。
                    # **按 data-id 找，不能按下标猜**：侧栏顺序是排序后的，
                    # 跟角色 id 完全不同，按下标点会点到别的角色还"测通过"。
                    clicked = pg.evaluate(
                        """(cid) => {
                            const btns = [...document.querySelectorAll(
                                '#charList .char-btn')];
                            const hit = btns.find(x => x.dataset.id === String(cid));
                            if (!hit) return false;
                            hit.scrollIntoView({block: 'center'});
                            hit.click();
                            return true;
                        }""", cid)
                    rep.add(clicked, "侧栏里找到该角色", f"data-id={cid}")
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
                    # 播放控制：就在这个已加载的页面上测，不重新 goto
                    # （实测二次导航 60s 都会超时）
                    try:
                        txt0 = pg.evaluate(
                            "() => (document.getElementById('msgText')||{}).textContent || ''")
                        pg.keyboard.press("Enter")
                        pg.wait_for_timeout(1200)
                        txt1 = pg.evaluate(
                            "() => (document.getElementById('msgText')||{}).textContent || ''")
                        hasSet = pg.evaluate(
                            "() => !!document.getElementById('settings')")
                        rep.add(hasSet, "设置面板存在")
                        # 文字变了 = 确实切到了下一句
                        rep.add(bool(txt1) and txt1 != txt0, "回车切下一句",
                                f"{(txt0 or '(空)')[:12]} -> {(txt1 or '(空)')[:12]}")
                    except Exception as ex:                   # noqa: BLE001
                        rep.add(False, "播放控制", str(ex)[:70])
                    b.close()
                    browser_ok = True
            except Exception as ex:                           # noqa: BLE001
                rep.add(False, "浏览器实测", f"{type(ex).__name__}: {str(ex)[:80]}")

            if browser_ok:
                # 头像**属于公共素材**：只有下了公共素材才该有，不装就不该要求。
                if HAS_BASE:
                    rep.add(r["avatarTotal"] > 0
                            and r["avatars"] == r["avatarTotal"],
                            "侧栏头像加载", f"{r['avatars']}/{r['avatarTotal']}")
                else:
                    rep.skip("侧栏头像加载", "未装公共素材，头像本就该缺")
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

        # ---------- 6.5 扩展功能（这些以前完全没测） ----------
        print("\n扩展功能检查…")

        # (a) CG 页：它有自己的一套候选链（曾出过只认 .webp 的 bug）
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            rep.skip("CG 页", "playwright 未装")
        else:
            errs2 = []
            try:
                with sync_playwright() as pw:
                    bb = pw.chromium.launch(channel="msedge")
                    pgx = bb.new_page()
                    pgx.set_viewport_size({"width": 1400, "height": 900})
                    pgx.on("pageerror", lambda e: errs2.append(str(e)[:80]))
                    pgx.goto(f"http://127.0.0.1:{port}/cg.html?nodisclaimer=1",
                             wait_until="domcontentloaded", timeout=30000)
                    pgx.wait_for_timeout(3500)
                    r2 = pgx.evaluate("""() => {
                        const q = sel => [...document.querySelectorAll(sel)];
                        const ok = a => a.filter(i => i.naturalWidth > 0).length;
                        const av = q('#charList .char-btn img');
                        const th = q('#grid .card .thumb img');
                        return { avTotal: av.length, avOk: ok(av),
                                 thTotal: th.length, thOk: ok(th) };
                    }""")
                    bb.close()
            except Exception as e:                            # noqa: BLE001
                rep.add(False, "CG 页打开", str(e)[:70])
                r2 = None
            if r2 is not None:
                rep.add(r2["avTotal"] > 0, "CG 页角色列表",
                        f"{r2['avOk']}/{r2['avTotal']} 头像")
                rep.add(not errs2, "CG 页无 JS 报错", "; ".join(errs2[:1]))
                if r2["thTotal"]:
                    rep.add(r2["thOk"] > 0, "CG 缩略图加载",
                            f"{r2['thOk']}/{r2['thTotal']}")
                else:
                    rep.skip("CG 缩略图加载", "该角色没有可显示的 CG")

        # (c) 超分：默认只测「能否算出计划」，--full 才真的跑
        try:
            plan = http(port, "/api/upscale/plan?kinds=stand,back,cg&scale=2",
                        timeout=120)
            rep.add((plan.get("total") or 0) > 0, "超分预估有结果",
                    f"{plan.get('total')} 张 / {plan.get('etaText')}")
            # --full 才真的跑（33 张约 1 分钟，4K 屏/弱 CPU 可能更久）
            if args.full:
                ju = http(port, "/api/upscale",
                          {"kinds": ["stand", "back", "cg"], "scale": 2},
                          timeout=120)["job"]
                jru = wait_job(port, ju["id"], timeout=3600)
                rep.add(jru["state"] == "done", "实际超分",
                        jru.get("message") or str(jru)[:50])
                x2 = os.path.join(web, "data", "imgmap.x2.js")
                rep.add(os.path.exists(x2), "超分产出记录 imgmap.x2.js")
            else:
                rep.skip("实际超分", "加 --full 才跑")
        except Exception as e:                                # noqa: BLE001
            rep.add(False, "超分预估", str(e)[:70])

        # (d) 译文导出 → 导入 → 再导出（块式格式往返）
        #     导入会往真实译文库里写测试串，所以整段用备份/还原包住——
        #     以前就是没还原，把 "E2E测试译文" 这种垃圾留在了用户译文里。
        tfile = os.path.join(web, "assets", "i18n", "zh-Hans", f"{args.unit}.json")
        tbak = tfile + ".e2ebak"
        had_t = os.path.exists(tfile)
        # index.json 也要备份：导入会调 _update_trans_index() 改里面的字数统计，
        # 只还原 zh-Hans/<unit>.json 的话，本机跑一次 e2e 计数就被测试串带偏了。
        ifile = os.path.join(web, "assets", "i18n", "index.json")
        ibak = ifile + ".e2ebak"
        had_i = os.path.exists(ifile)
        try:
            if had_t:
                shutil.copy2(tfile, tbak)
            if had_i:
                shutil.copy2(ifile, ibak)
            # 注意：这个路由在 **GET 分支**，参数走 query string，
            # 返回纯文本（不是 json），所以按 bytes 解；导出带 UTF-8 BOM
            exp0 = http(port, f"/api/export/trans?unit={args.unit}", timeout=300)
            raw = exp0 if isinstance(exp0, (bytes, bytearray)) else b""
            t0 = raw.decode("utf-8-sig", "replace") if raw else (exp0.get("text") or "")
            lines0 = t0.splitlines()
            heads = [l for l in lines0 if l.startswith("### ")]
            rep.add(bool(heads) and "|||" not in t0 and raw[:3] == b"\xef\xbb\xbf",
                    "译文导出为块式（带 BOM）", f"{len(heads)} 块")
            # 取第一个真实块，模拟"AI 翻好了导回来"：块头 + 原有 ja 行 + 补一行 zh
            i = next((k for k, l in enumerate(lines0) if l.startswith("### ")), -1)
            pick = [lines0[i]] if i >= 0 else []
            j = i + 1
            while i >= 0 and j < len(lines0) and lines0[j].startswith("ja:"):
                pick.append(lines0[j])
                j += 1
            imp = http(port, "/api/import/trans",
                       {"unit": args.unit,
                        "text": "\n".join(pick + ["zh: 端到端测试译文"]) + "\n"},
                       timeout=120)
            ok_imp = (imp.get("added") or 0) + (imp.get("updated") or 0) > 0
            rep.add(ok_imp, "译文导入（块式）",
                    f"新增{imp.get('added')} 坏{imp.get('bad')} "
                    f"失配{len(imp.get('mismatch') or [])}")
            exp = http(port, f"/api/export/trans?unit={args.unit}", timeout=300)
            txt = exp.decode("utf-8-sig", "replace") \
                if isinstance(exp, (bytes, bytearray)) else (exp.get("text") or "")
            rep.add("端到端测试译文" in txt,
                    "译文导出含刚导入的内容", f"{len(txt)} 字符")
        except Exception as e:                                # noqa: BLE001
            rep.add(False, "译文导入导出", str(e)[:70])
        finally:
            # 还原译文库：测试数据一条都不许留下（正文与字数统计都要还原）
            if had_t and os.path.exists(tbak):
                shutil.move(tbak, tfile)
            elif not had_t and os.path.exists(tfile):
                os.remove(tfile)
            if had_i and os.path.exists(ibak):
                shutil.move(ibak, ifile)
            elif not had_i and os.path.exists(ifile):
                os.remove(ifile)

        # (e) 导出 CG
        try:
            jc = http(port, "/api/export/cg", {"chars": [int(cid)]},
                      timeout=120)["job"]
            jr2 = wait_job(port, jc["id"], timeout=900)
            rep.add(jr2["state"] == "done", "导出 CG",
                    jr2.get("message") or str(jr2)[:50])
        except Exception as e:                                # noqa: BLE001
            rep.add(False, "导出 CG", str(e)[:70])

        # (f) 检查程序更新（需要访问 GitHub，CI 里可以）
        try:
            # 注意是 **GET**（这条路由挂在 _api_get 里）。以前传了空 dict →
            # 变成 POST → 404 → 异常被 except 当"跳过=通过"，等于一直没测。
            up = http(port, "/api/update/check", timeout=180)
            # 真断言：拿到最新版本号才算通过。以前只判 isinstance(dict) ——
            # 返回 {"error": "…"} 也照样"通过"，等于没测。
            rep.add(bool(up.get("latest")), "检查程序更新",
                    f"当前 {up.get('current')} / 最新 {up.get('latest')}"
                    f" / 线路 {up.get('via') or '-'}"
                    + (f" / 错误 {up['error'][:60]}" if up.get("error") else ""))
        except Exception as e:                                # noqa: BLE001
            rep.add(False, "检查程序更新", str(e)[:70])

        # (g) 打开目录按钮
        try:
            # 参数名是 which（不是 kind）：{"kind":"data"} 会被当成默认的 export，
            # 而且 "data" 本身不是合法值 —— 以前这么写只能是"测了个寂寞"。
            op = http(port, "/api/open", {"which": "export"}, timeout=60)
            rep.add(op.get("ok") is True, "打开导出目录接口", str(op)[:60])
        except Exception as e:                                # noqa: BLE001
            rep.add(False, "打开导出目录", str(e)[:70])

        # (h) 暂停 / 取消任务：用一个清单独刷任务验证
        try:
            j2 = http(port, "/api/manifest/refresh", {})["job"]
            # 暂停只对 running 生效，任务可能还没起来/已经跑完，先等它进入 running
            t0 = time.time()
            state = ""
            while time.time() - t0 < 15:
                js = http(port, "/api/jobs", timeout=30)["jobs"]
                me = [j for j in js if j["id"] == j2["id"]]
                state = me[0]["state"] if me else "gone"
                if state != "queued":
                    break
                time.sleep(0.3)
            if state == "running":
                pz = http(port, f"/api/jobs/{j2['id']}/pause", {}, timeout=60)
                rep.add(pz.get("ok") is True, "暂停任务", str(pz)[:50])
            else:
                rep.skip("暂停任务", f"任务已 {state}，来不及暂停（清单刷得太快）")
            cc = http(port, f"/api/jobs/{j2['id']}/cancel", {}, timeout=60)
            rep.add(cc.get("ok") is True, "取消任务", str(cc)[:50])
            time.sleep(1)
            js = http(port, "/api/jobs", timeout=30)["jobs"]
            me = [j for j in js if j["id"] == j2["id"]]
            rep.add(bool(me) and me[0]["state"] == "cancelled",
                    "取消后状态为 cancelled",
                    me[0]["state"] if me else "任务已消失")
            rep.add(bool(me) and me[0].get("ended"),
                    "取消后记录了结束时间（否则底部条不消失）")
        except Exception as e:                                # noqa: BLE001
            rep.add(False, "暂停/取消任务", str(e)[:70])

        # (i) 导出视频（很慢，--full 才跑）
        if args.full:
            try:
                # /api/export/video 返回的是 {"job": …}（后台任务），**必须等**：
                # 以前这里直接判 vd["ok"] → 永远 False（必红），而且没等任务就去
                # 卸载，把正在截帧的剧本文档删了，视频 0 帧失败还被标成 done。
                n0 = len((http(port, "/api/exports", timeout=30) or {})
                         .get("items") or [])
                vd = http(port, "/api/export/video",
                          {"unit": args.unit, "limit": 24}, timeout=300)
                if "error" in vd:
                    rep.add(False, "导出视频", str(vd["error"])[:80])
                else:
                    jv = vd["job"]
                    jrv = wait_job(port, jv["id"], timeout=3600)
                    rep.add(jrv["state"] == "done", "导出视频",
                            jrv.get("error") or jrv.get("message")
                            or str(jrv)[:60])
                    items = (http(port, "/api/exports", timeout=30) or {}) \
                        .get("items") or []
                    rep.add(len(items) > n0, "导出目录里多出文件",
                            f"{n0} -> {len(items)}")
            except Exception as e:                            # noqa: BLE001
                rep.add(False, "导出视频", str(e)[:70])
        else:
            rep.skip("导出视频", "加 --full 才跑（很慢）")

        # (j) 卸载：放最后（会把刚装的剧情删掉）
        try:
            rj = http(port, "/api/uninstall", {"units": [args.unit]}, timeout=120)
            if "error" in rj:
                # 有任务占着这部剧时接口会拒绝（这是对的，不是缺陷）
                rep.add(False, "卸载剧情", str(rj["error"])[:80])
            else:
                wait_job(port, rj["job"]["id"], timeout=900)
                time.sleep(2)
                st2 = http(port, "/api/state", timeout=30)
                left = (st2.get("coverage") or {}).get("units") or 0
                script = os.path.join(web, "data", f"{args.unit}.js")
                rep.add(left == 0 and not os.path.exists(script),
                        "卸载剧情", f"剩余 {left} 部，剧本文件已删="
                        f"{not os.path.exists(script)}")
        except Exception as e:                                # noqa: BLE001
            rep.add(False, "卸载剧情", str(e)[:70])

        # ---------- 7. 日志里不该有异常 ----------
        log = os.path.join(root, "userdata", "logs", "app.log")
        if os.path.exists(log):
            txt = open(log, encoding="utf-8", errors="replace").read()
            bad_words = [w for w in ("Traceback", "NameError", "TypeError")
                         if w in txt]
            rep.add(not bad_words, "日志无异常", ", ".join(bad_words))
        else:
            rep.skip("日志无异常", f"没有 {log}")

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
