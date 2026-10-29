# -*- coding: utf-8 -*-
"""发布前隔离验收：把项目复制到临时目录 + 独立依赖环境，跑完整链路。

为什么要"隔离"
--------------
只换一个文件夹**不够**：那样 Python 依赖还是本机全局装的，测不出
"用户机器上没装 UnityPy"这类真实问题；`userdata/` 里也还留着你自己的
清单和素材，测不出"首次使用"的引导。

所以这里做三层隔离：
  1. 代码复制到临时目录（不碰现有工作区）
  2. 依赖装进独立 venv（或 embedded Python，用 --portable）
  3. 数据目录从零开始（新的 userdata/cdn、web/data 只有索引）

测不了的只有：系统浏览器（视频导出用 Edge/Chrome）、网络、操作系统。
这三样都不属于"我们可能写错"的范围。

用法
----
    python tools/acceptance.py --quick      # 不装依赖、不下载素材，验降级与接口
    python tools/acceptance.py              # 装依赖 + 真实下载清单与一部剧情
    python tools/acceptance.py --portable   # 用 embedded Python 3.11（等价 CI 产物）
    python tools/acceptance.py --keep       # 保留临时目录，便于手动排查

退出码：0 全通过 / 1 有失败项 / 2 环境准备失败
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
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "Mozilla/5.0 acceptance-check"

#: 复制到隔离目录的东西（**不含** web/media 与 userdata：那是素材和用户数据）
#: 注意 web/data 只搬索引（见 prepare 里的 ignore_patterns），
#: 否则会把本机已装的 char_*.js 一起带过去，测不出"首次使用"。
COPY_DIRS = ("server", "tools", "docs", "web/assets", "web/data")
COPY_FILES = ("VERSION", "requirements.txt", "README.md", "NOTICE.md", "LICENSE",
              "启动.bat", "start.sh")
WEB_FILES = ("index.html", "cg.html", "setup.html", "serve.py")

EMBED_URL = ("https://www.python.org/ftp/python/3.11.9/"
             "python-3.11.9-embed-amd64.zip")


class Ctx:
    def __init__(self, args):
        self.args = args
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = os.path.join(args.work, "escah-accept-" + self.stamp)
        self.app = os.path.join(self.dir, "app")
        self.venv = os.path.join(self.dir, "venv")
        self.port = args.port
        self.py = ""                 # 隔离环境里的 python
        self.srv = None
        self.results = []

    # ---------------------------------------------------------------- 记录
    def ok(self, name, detail="", cost=0):
        self.results.append((True, name, detail, cost))
        print("  [OK]   {:<34} {}{}".format(name, detail, self._t(cost)))

    def bad(self, name, detail="", cost=0):
        self.results.append((False, name, detail, cost))
        print("  [FAIL] {:<34} {}{}".format(name, detail, self._t(cost)))

    def skip(self, name, why):
        self.results.append((None, name, why, 0))
        print("  [SKIP] {:<34} {}".format(name, why))

    @staticmethod
    def _t(cost):
        return "  ({:.1f}s)".format(cost) if cost else ""

    # ---------------------------------------------------------------- 环境
    def prepare(self) -> bool:
        print("\n=== 1/5 准备隔离目录 ===")
        print("  目标:", self.dir)
        os.makedirs(self.app, exist_ok=True)
        for d in COPY_DIRS:
            src = os.path.join(ROOT, d)
            if not os.path.isdir(src):
                continue
            dst = os.path.join(self.app, d)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            # web/data 里既有索引也有"已安装的剧情正文"，只搬索引才能模拟首次使用
            pats = ["__pycache__", "*.pyc", "out"]
            if d.replace("\\", "/").endswith("web/data"):
                pats += ["char_*.js", "adv_*.js", "*.bak", "*.prev.json"]
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*pats))
        for f in COPY_FILES:
            src = os.path.join(ROOT, f)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(self.app, f))
        for f in WEB_FILES:
            src = os.path.join(ROOT, "web", f)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(self.app, "web", f))

        # 隔离环境里必须没有素材与用户数据（否则测不出"首次使用"）
        for gone in ("web/media", "userdata"):
            p = os.path.join(self.app, gone)
            if os.path.exists(p):
                shutil.rmtree(p, ignore_errors=True)
        os.makedirs(os.path.join(self.app, "web", "media"), exist_ok=True)
        os.makedirs(os.path.join(self.app, "userdata"), exist_ok=True)
        print("  代码与索引已复制（不含素材/用户数据）")

        # --reuse：用当前 Python 环境（不建 venv），只隔离代码与数据目录。
        # 目的是"几十秒内起一个能点的实例"给人工查看；依赖、路径隔离不严，
        # 但数据是全新的，能看首次使用体验。
        if self.args.reuse:
            self.py = sys.executable
            self.ok("复用当前 Python", "只隔离代码与数据目录（最快）")
            return True
        if self.args.portable:
            return self._prepare_embedded()
        return self._prepare_venv()

    def _prepare_venv(self) -> bool:
        # quick 也建 venv，但**不装依赖** —— 正好用来验证"用户没装依赖"时的
        # 引导是否正确（这是最容易做砸、又最难被自己发现的一条路径）。
        print("  创建 venv …")
        t = time.time()
        r = subprocess.run([sys.executable, "-m", "venv", self.venv],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("  venv 创建失败:", (r.stderr or "")[-400:])
            return False
        self.py = os.path.join(self.venv, "Scripts", "python.exe")
        if not os.path.exists(self.py):
            self.py = os.path.join(self.venv, "bin", "python")
        if self.args.quick:
            self.ok("创建空 venv（不装依赖）", "专测缺依赖引导", time.time() - t)
            return True
        self.ok("创建独立 venv", "", time.time() - t)

        # 装依赖也走"先测速选最快镜像"，否则国内直连 PyPI 可能只有几十 KB/s
        index = ""
        try:
            sys.path.insert(0, ROOT)
            from server import mirrors
            t = time.time()
            route = mirrors.best("pip", refresh=True)
            index = route.get("index") or ""
            self.ok("pip 镜像测速", "{}（{} ms）".format(
                route.get("name", "?"), route.get("ttfb", 0)), time.time() - t)
        except Exception as e:                                 # noqa: BLE001
            self.skip("pip 镜像测速", str(e)[:50])

        print("  安装依赖（约 90 MB，可能要几分钟）…")
        t = time.time()
        cmd = [self.py, "-m", "pip", "install", "-q",
               "--disable-pip-version-check"]
        if index:
            cmd += ["-i", index]
        cmd += ["-r", os.path.join(self.app, "requirements.txt")]
        r = subprocess.run(
            cmd, capture_output=True, text=True, cwd=self.app,
            env=dict(os.environ, PYTHONUTF8="1"))
        if r.returncode != 0:
            print("  依赖安装失败:", (r.stderr or "")[-600:])
            self.bad("安装依赖", "见上方错误")
            return False
        r2 = subprocess.run(
            [self.py, "-c", "import UnityPy,lz4,PIL,numpy;print('ok')"],
            capture_output=True, text=True)
        self.ok("安装依赖", r2.stdout.strip()[:20], time.time() - t)
        return True

    def _prepare_embedded(self) -> bool:
        """等价 CI：下载 embedded Python 3.11 + 把依赖装进它的 site-packages。"""
        import zipfile
        py_dir = os.path.join(self.dir, "python")
        os.makedirs(py_dir, exist_ok=True)
        z = os.path.join(self.dir, "py.zip")
        print("  下载 embedded Python 3.11 …")
        t = time.time()
        try:
            urllib.request.urlretrieve(EMBED_URL, z)
            with zipfile.ZipFile(z) as zf:
                zf.extractall(py_dir)
        except Exception as e:                                 # noqa: BLE001
            self.bad("下载 embedded Python", str(e)[:80])
            return False
        self.ok("下载 embedded Python", "", time.time() - t)

        # embedded 版默认不 import site，需要改 ._pth
        for name in os.listdir(py_dir):
            if name.endswith("._pth"):
                p = os.path.join(py_dir, name)
                with open(p, "a", encoding="utf-8") as f:
                    f.write("\nimport site\nLib\\site-packages\n")
                break
        self.py = os.path.join(py_dir, "python.exe")

        print("  往 embedded 里装依赖 …")
        t = time.time()
        with open(os.path.join(self.app, "requirements.txt"), encoding="utf-8") as f:
            reqs = [ln.strip() for ln in f
                    if ln.strip() and not ln.strip().startswith("#")]
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--target",
             os.path.join(py_dir, "Lib", "site-packages"),
             "--only-binary=:all:", *reqs],
            capture_output=True, text=True)
        if r.returncode != 0:
            self.bad("安装依赖(embedded)", (r.stderr or "")[-300:])
            return False
        r2 = subprocess.run([self.py, "-c", "import UnityPy,lz4,PIL,numpy;print('ok')"],
                            capture_output=True, text=True)
        if "ok" not in (r2.stdout or ""):
            self.bad("embedded 依赖可用", (r2.stderr or "")[-200:])
            return False
        self.ok("安装依赖(embedded)", "", time.time() - t)
        return True

    # ---------------------------------------------------------------- 服务
    def start(self) -> bool:
        print("\n=== 2/5 启动隔离服务 ===")
        if not self._free(self.port):
            print("  端口 {} 被占用，换一个".format(self.port))
            self.port = self._find_port()
        env = dict(os.environ, PYTHONUTF8="1")
        self.srv = subprocess.Popen(
            [self.py, "-m", "server", "-p", str(self.port)],
            cwd=self.app, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        for _ in range(40):
            time.sleep(0.4)
            if self._get("/api/state", timeout=3) is not None:
                self.ok("服务启动", "port={}".format(self.port))
                return True
            if self.srv.poll() is not None:
                out = (self.srv.stdout.read() if self.srv.stdout else "")[-800:]
                self.bad("服务启动", out.replace("\n", " ")[:200])
                return False
        self.bad("服务启动", "20 秒内没有响应")
        return False

    @staticmethod
    def _free(port) -> bool:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def _find_port(self) -> int:
        for p in range(self.port + 1, self.port + 30):
            if self._free(p):
                return p
        return self.port

    # ---------------------------------------------------------------- HTTP
    def _url(self, path):
        return "http://127.0.0.1:{}{}".format(self.port, path)

    def _get(self, path, timeout=20):
        try:
            req = urllib.request.Request(self._url(path), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:                                      # noqa: BLE001
            return None

    def _post(self, path, body=None, timeout=30):
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            self._url(path), data=data,
            headers={"Content-Type": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def wait_job(self, job, timeout=600):
        """轮询任务直到结束。返回 (state, message, error, detail)。"""
        jid = (job or {}).get("job", {}).get("id") or (job or {}).get("id")
        t0 = time.time()
        last = ""
        while time.time() - t0 < timeout:
            time.sleep(1.2)
            d = self._get("/api/jobs", timeout=15) or {}
            me = [j for j in (d.get("jobs") or []) if j.get("id") == jid]
            if not me:
                continue
            j = me[0]
            if j.get("message") and j["message"] != last:
                last = j["message"]
            if j.get("state") in ("done", "failed", "cancelled"):
                return j.get("state"), j.get("message") or "", j.get("error") or "", \
                    j.get("detail") or {}
        return "timeout", "等待超时", "", {}

    # ---------------------------------------------------------------- 检查
    def checks(self):
        print("\n=== 3/5 接口与降级 ===")
        t = time.time()
        st = self._get("/api/state")
        if st and st.get("version"):
            self.ok("GET /api/state", "v{} units {}/{}".format(
                st["version"],
                (st.get("coverage") or {}).get("units", 0),
                (st.get("coverage") or {}).get("unitsTotal", 0)), time.time() - t)
        else:
            self.bad("GET /api/state", "无响应")

        t = time.time()
        dep = self._get("/api/deps/status")
        if dep is not None:
            if self.args.quick:
                # quick 模式故意不装依赖 → 应该报缺
                self.ok("依赖状态(未装)", "缺 {}".format(
                    "、".join(dep.get("missing") or []) or "无"), time.time() - t)
            else:
                if dep.get("ready"):
                    self.ok("依赖状态(已装)", "全部就绪", time.time() - t)
                else:
                    self.bad("依赖状态(已装)", "仍缺 {}".format(
                        "、".join(dep.get("missing") or [])))
        else:
            self.bad("GET /api/deps/status")

        t = time.time()
        info = self._get("/api/cdn/info")
        if info is not None:
            self.ok("GET /api/cdn/info",
                    "manifestReady={} cdn={}".format(
                        info.get("manifestReady"), (info.get("cdn") or "")[:32]),
                    time.time() - t)
            if not info.get("manifestReady"):
                self.ok("首次使用：缺清单能被识别", "→ 应引导用户下载清单")
        else:
            self.bad("GET /api/cdn/info")

        t = time.time()
        u = self._get("/api/cdn/units")
        if u is not None:
            if u.get("needManifest"):
                self.ok("GET /api/cdn/units(无清单)", "needManifest=true", time.time() - t)
            else:
                self.ok("GET /api/cdn/units", "char={} adv={}".format(
                    len((u.get("units") or {}).get("char") or []),
                    len((u.get("units") or {}).get("adv") or [])), time.time() - t)
        else:
            self.bad("GET /api/cdn/units")

        t = time.time()
        cg = self._get("/api/cglist")
        if cg is not None:
            self.ok("GET /api/cglist", "{} 张 CG".format(cg.get("total", 0)),
                    time.time() - t)
        else:
            self.bad("GET /api/cglist")

    def manifest(self):
        print("\n=== 4/5 下载资源清单（真实网络，约 5 MB）===")
        if self.args.quick:
            self.skip("POST /api/manifest/refresh", "--quick 不联网")
            return
        t = time.time()
        try:
            job = self._post("/api/manifest/refresh")
        except Exception as e:                                 # noqa: BLE001
            self.bad("POST /api/manifest/refresh", str(e)[:100])
            return
        state, msg, err, _ = self.wait_job(job, timeout=180)
        cost = time.time() - t
        if state == "done":
            self.ok("下载并解密清单", msg or "完成", cost)
            u = self._get("/api/cdn/units") or {}
            chars = len((u.get("units") or {}).get("char") or [])
            if chars > 300:
                self.ok("清单解析出单元", "{} 角色".format(chars))
            else:
                self.bad("清单解析出单元", "只看到 {} 个角色".format(chars))
        else:
            self.bad("下载并解密清单", (err or msg)[:120], cost)

    def install(self):
        print("\n=== 5/5 安装一部剧情（真实下载 + 解包）===")
        if self.args.quick:
            self.skip("安装剧情", "--quick 不下载素材")
            return
        u = self._get("/api/cdn/units") or {}
        chars = (u.get("units") or {}).get("char") or []
        if not chars:
            self.skip("安装剧情", "清单里没有角色")
            return
        unit = chars[0]
        t = time.time()
        try:
            job = self._post("/api/download", {"selected": [unit], "threads": 32})
        except Exception as e:                                 # noqa: BLE001
            self.bad("POST /api/download", str(e)[:100])
            return
        state, msg, err, _ = self.wait_job(job, timeout=600)
        cost = time.time() - t
        if state == "done":
            self.ok("安装 {}".format(unit), msg or "完成", cost)
            st = self._get("/api/state") or {}
            v = (st.get("status") or {}).get(unit)
            if v in ("ready", "soft-missing"):
                self.ok("安装后状态", v)
            else:
                self.bad("安装后状态", str(v))
            # 剧本文件必须落地
            p = os.path.join(self.app, "web", "data", unit + ".js")
            if os.path.exists(p) and os.path.getsize(p) > 5000:
                self.ok("剧本文件落盘",
                        "{:.0f} KB".format(os.path.getsize(p) / 1024))
            else:
                self.bad("剧本文件落盘", "缺失或过小")
        else:
            self.bad("安装 {}".format(unit), (err or msg)[:140], cost)

    def ui(self):
        print("\n=== 附加：页面与导出 ===")
        for path, name in (("/index.html", "播放器页"), ("/cg.html", "CG 页"),
                           ("/setup.html", "控制台页")):
            t = time.time()
            try:
                req = urllib.request.Request(self._url(path),
                                             headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = r.read()
                self.ok("GET " + name, "{} KB".format(len(body) // 1024),
                        time.time() - t)
            except Exception as e:                             # noqa: BLE001
                self.bad("GET " + name, str(e)[:60])

        # 译文导出（不需要素材也能测：接口会返回空/或已有剧情）
        t = time.time()
        try:
            d = self._get("/api/trans/export?unit=char_1", timeout=20)
            if d is None:
                self.skip("导出译文", "没有 char_1（正常，隔离环境刚装第一个角色）")
            else:
                self.ok("导出译文", "{} 行".format(len((d.get("text") or "").splitlines())),
                        time.time() - t)
        except Exception as e:                                 # noqa: BLE001
            self.skip("导出译文", str(e)[:60])

    # ---------------------------------------------------------------- 收尾
    def finish(self):
        keep_alive = bool(self.args.serve)
        if self.srv and self.srv.poll() is None and not keep_alive:
            try:
                self.srv.terminate()
                self.srv.wait(timeout=8)
            except Exception:                                  # noqa: BLE001
                self.srv.kill()

        ok = sum(1 for r in self.results if r[0] is True)
        bad = sum(1 for r in self.results if r[0] is False)
        skip = sum(1 for r in self.results if r[0] is None)
        print("\n" + "=" * 62)
        print("  通过 {}  失败 {}  跳过 {}".format(ok, bad, skip))
        if bad:
            print("  失败项：")
            for r in self.results:
                if r[0] is False:
                    print("   · {}  {}".format(r[1], r[2]))
        print("  隔离目录:", self.dir)
        print("=" * 62)

        if keep_alive:
            print()
            print("  ★ 隔离实例还在跑，可以直接打开看：")
            print("      http://127.0.0.1:{}".format(self.port))
            print("      （它的 userdata/ 与 web/media 是全新的，"
                  "相当于「第一次用这个程序」）")
            print("    看完想关掉：")
            print("      Get-CimInstance Win32_Process -Filter \"name='python.exe'\" |")
            print("        Where-Object { $_.CommandLine -like '*escah-accept*' } |")
            print("        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
            print("    目录留在:", self.dir, "（不要了直接删）")
            return 0 if bad == 0 else 1

        if not self.args.keep:
            shutil.rmtree(self.dir, ignore_errors=True)
            print("  已清理临时目录（--keep 可保留）")
        return 0 if bad == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="发布前隔离验收")
    ap.add_argument("--quick", action="store_true",
                    help="不装依赖、不联网（专测降级引导与接口）")
    ap.add_argument("--portable", action="store_true",
                    help="用 embedded Python 3.11（等价 CI 产物，较慢）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("--serve", action="store_true",
                    help="跑完不关服务、不删目录，方便手动点开看（配合 --reuse 最快）")
    ap.add_argument("--reuse", action="store_true",
                    help="复用当前 Python（不建 venv），只隔离代码与数据 —— 30 秒起一个可看的实例")
    ap.add_argument("--work", default=os.environ.get("TEMP") or "D:/tmp",
                    help="临时目录的父目录")
    ap.add_argument("--port", type=int, default=8399)
    a = ap.parse_args()

    ctx = Ctx(a)
    try:
        if not ctx.prepare():
            return ctx.finish() or 2
        if not ctx.start():
            return ctx.finish() or 2
        ctx.checks()
        ctx.manifest()
        ctx.install()
        ctx.ui()
    finally:
        pass
    return ctx.finish()


if __name__ == "__main__":
    sys.exit(main())
