"""应用自更新：检查 GitHub Release → 暂存 → 退出前替换 → 重启。

为什么不做"热更新"
------------------
这是便携应用：``server/*.py`` 正被 python 解释执行、``web/*.js`` 正被浏览器
持有。在进程内改自己必然踩文件锁。成熟做法（qBittorrent、PortableGit、各种
Electron 更新器）都是同一套：

1. 下载新版本到**临时目录**
2. 拉起一个独立的"更新器"进程（脱离本进程）
3. 本进程退出，腾出文件锁
4. 更新器按**白名单**把新文件覆盖到程序目录
5. 更新器拉起新版本，然后自己消失

白名单是关键 —— 只覆盖程序文件：

    server/**  web/*.html  web/assets/**  tools/*.py  docs/**  VERSION
    README.md  requirements.txt  start.bat / start.sh

``web/media/**``、``web/data/**``、``userdata/**`` 永远不会被碰，
所以更新不会丢资源、不会丢译文、不会丢配置。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

from . import net, paths

#: 允许被更新覆盖的路径前缀（相对项目根）
WHITELIST = (
    "server/", "web/assets/", "tools/", "docs/",
    "web/index.html", "web/cg.html", "web/setup.html", "web/serve.py",
    "VERSION", "README.md", "requirements.txt",
    "start.bat", "start.sh", "停止.bat",
    # 注意：**不要**把「启动.bat」加进来。
    # 便携版的启动脚本是打包时专门生成的（直接调 python\python.exe），
    # 而仓库里是"找系统 Python"的源码版；一旦被更新覆盖，便携版下次启动就崩。
)
#: 明确禁止（双保险，即便 zip 里有也不动）
#:
#: ``web/assets/i18n/`` 必须在里面：那里是**用户自己翻译攒下来的译文**，
#: 属于用户数据，不是程序。更新包里若带一份旧译文，会把用户后来的翻译覆盖回去。
BLACKLIST = ("web/media/", "web/data/", "web/assets/i18n/", "userdata/",
             ".git/", ".github/")


def app_meta() -> dict:
    p = os.path.join(paths.DATA, "app.json")
    meta = paths.load_json(p, {}) or {}
    meta.setdefault("repo", "")
    meta.setdefault("name", "ESCAH-Offline")
    meta.setdefault("channel", "stable")
    return meta


def current_version() -> str:
    return paths.app_version()


def _ver_tuple(v: str) -> tuple:
    out = []
    for part in str(v or "0").lstrip("vV").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:4])


def check() -> dict:
    """→ {current, latest, hasUpdate, url, notes, error}"""
    meta = app_meta()
    cur = current_version()
    repo = meta.get("repo") or ""
    if not repo:
        return {"current": cur, "latest": "", "hasUpdate": False, "url": "",
                "notes": "", "error": "未配置仓库地址（web/data/app.json 的 repo）"}
    try:
        rel = net.fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
    except Exception as e:                                       # noqa: BLE001
        return {"current": cur, "latest": "", "hasUpdate": False, "url": "",
                "notes": "", "error": f"查询失败：{e}"}
    tag = rel.get("tag_name") or ""
    # 优先挑 windows 便携包，其次任意 zip
    url = ""
    for a in rel.get("assets") or []:
        n = (a.get("name") or "").lower()
        if n.endswith(".zip") and ("win" in n or "portable" in n):
            url = a.get("browser_download_url") or ""
            break
    if not url:
        for a in rel.get("assets") or []:
            if (a.get("name") or "").lower().endswith(".zip"):
                url = a.get("browser_download_url") or ""
                break
    return {
        "current": cur, "latest": tag.lstrip("vV"),
        "hasUpdate": _ver_tuple(tag) > _ver_tuple(cur),
        "url": url, "notes": (rel.get("body") or "")[:4000],
        "published": rel.get("published_at") or "",
        "html": rel.get("html_url") or "", "error": "",
    }


class ManualDownload(RuntimeError):
    """所有内置线路都不通 —— 别耗时间了，引导用户去 GitHub 手动下载。

    为什么单独做成异常：这个失败**不是错误，是预期内的兜底**。
    调用方需要拿到 ``releases`` 页面地址和文件名，渲染成可点击的卡片，
    而不是丢一句"下载失败"让用户猜。
    """

    def __init__(self, url: str, reason: str = ""):
        self.file_name = (url.rstrip("/").split("/")[-1] or "更新包")
        self.page = _release_page(url)
        self.reason = reason
        self.url = url
        repo = ""
        try:
            from . import updater as _self          # 自身模块，取 app_meta
            repo = _self.app_meta().get("repo") or ""
        except Exception:                          # noqa: BLE001
            pass
        if not self.page and repo:
            self.page = f"https://github.com/{repo}/releases"
        super().__init__(
            "自动下载失败：" + (reason or "所有内置线路都不通")
            + "\n\n请手动下载：\n"
            f"  1. 打开 {self.page or 'https://github.com/<你的仓库>/releases'}\n"
            f"  2. 下载 {self.file_name}（更新包约 3–5 MB；也可以用全量包）\n"
            "  3. 解压后覆盖到程序目录（userdata/ 与素材不会被覆盖），"
            "再双击 启动.bat")

    def payload(self) -> dict:
        return {"page": self.page, "file": self.file_name,
                "url": self.url, "reason": self.reason}


def _release_page(url: str) -> str:
    """``.../releases/download/v1.0.0/a.zip`` → ``.../releases/tag/v1.0.0``。"""
    p = url.split("/releases/download/")
    if len(p) == 2 and p[1]:
        tag = p[1].split("/")[0]
        return p[0] + "/releases/tag/" + tag
    p2 = url.split("/releases/")
    if len(p2) == 2:
        return p2[0] + "/releases"
    return ""


def stage(url: str, job) -> str:
    """下载并解压到 userdata/update/staging。返回 staging 目录。

    GitHub 的下载域名在国内常常不可达，所以这里先**测速选一条能用的线路**
    （直连 / ghfast / ghproxy / gh-proxy），再套模板下载。
    """
    if not url:
        raise RuntimeError("没有可用的下载地址")
    staging = os.path.join(paths.USERDATA, "update", "staging")
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    zp = os.path.join(paths.USERDATA, "update", "update.zip")

    from . import mirrors
    # 只有 github.com 的地址才需要反代。自建镜像 / 局域网地址直接下 ——
    # 否则会把 http://192.168.1.5/upd.zip 套成
    # https://edgeone.gh-proxy.org/http://192.168.1.5/upd.zip，必然失败。
    if "github.com" in url or "githubusercontent.com" in url:
        routes = mirrors.ranked("github", job=job)
        if not routes:
            raise ManualDownload(url, "所有内置线路测速都不可用")
    else:
        job.log("下载地址不是 GitHub，直接下载（不套反代）")
        routes = [{"id": "direct", "name": "直连", "tpl": "{url}"}]

    job.set_total(1)

    # ① 探活：每条最多 6 秒。不通就立刻跳过，**绝不在这里等下载超时**
    #    （一个 70 MB 的包，线路不通会卡好几分钟，用户只会觉得"死了"）
    picked = None
    for route in routes[:2]:               # 只探前两条，别让用户干等
        if job.cancelled:
            raise RuntimeError("已取消")
        job.log(f"探测 {route.get('name', '?')}（最多 6 秒）…")
        if mirrors.quick_check(route, url, timeout=6):
            picked = route
            break
        job.log("  6 秒内没响应，跳过")
    if picked is None:
        raise ManualDownload(url, "所有内置线路在 6 秒内都没有响应")

    if picked.get("slow"):
        # 能通但慢（<1 MB/s）：更新包 3–5 MB 还能忍，全量包会很煎熬。
        # 把链接摆在眼前，让用户自己决定要不要改成手动。
        job.log("⚠ 当前可用线路较慢（{} MB/s）。更新包 3–5 MB 还能接受；"
                "如果下的是全量包可能会很久，随时可以取消并去 "
                "{} 手动下载。".format(picked.get("mbps") or "?", _release_page(url)))

    # ② 下载：先用选中的，失败再换其余（每条仍受 net 自己的超时保护）
    order = [picked] + [r for r in routes[:3]
                        if r.get("id") != picked.get("id")][:2]
    last = None
    tried = 0
    for route in order:
        tried += 1
        real = mirrors.apply_github(route, url)
        job.log("下载线路 {}/{}：{}{}".format(
            tried, len(order), route.get("name", "?"),
            "（较慢，兜底）" if route.get("slow") else ""))
        if real != url:
            job.log(f"  经反代：{real[:110]}")
        try:
            net.download(real, zp, threads=4, job=job)
            last = None
            break
        except Exception as e:                                 # noqa: BLE001
            last = e
            job.log(f"  这条线路失败（{type(e).__name__}: {str(e)[:80]}），换下一条…")
    if last is not None:
        raise ManualDownload(url, f"已尝试 {tried} 条线路，最后一条报错：{last}")
    job.log("正在解压…")
    with zipfile.ZipFile(zp) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            # 有些 release 包外面套了一层同名目录，剥掉
            if "/" in name and not _is_program(name):
                continue
            dst = os.path.join(staging, name)
            if not os.path.abspath(dst).startswith(os.path.abspath(staging)):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with z.open(info) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
    return staging


def _is_program(name: str) -> bool:
    low = name.lower()
    if low.startswith(("__macosx", ".")):
        return False
    for b in BLACKLIST:
        if low.startswith(b) or f"/{b}" in low:
            return False
    return any(low.startswith(w.lower()) or f"/{w.lower()}" in low
               for w in WHITELIST)


def allowed(relpath: str) -> bool:
    low = relpath.replace("\\", "/").lower()
    if any(low.startswith(b) for b in BLACKLIST):
        return False
    return any(low.startswith(w.lower()) for w in WHITELIST)


UPDATER_PY = r'''# -*- coding: utf-8 -*-
"""独立更新器：等主程序退出 → 覆盖白名单文件 → 重启。"""
import os, shutil, subprocess, sys, time

ROOT, STAGING, PID = sys.argv[1], sys.argv[2], sys.argv[3]
WHITELIST = sys.argv[4].split("|")
LOG = os.path.join(ROOT, "userdata", "logs", "update.log")

def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%F %T')}] {msg}\n")

def alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True, timeout=10).stdout
        return str(pid) in out
    except Exception:
        return False

log(f"更新器启动 pid={PID}")
for _ in range(120):
    if not alive(PID):
        break
    time.sleep(0.5)
time.sleep(0.8)

n = 0
for dire, _dirs, files in os.walk(STAGING):
    for fn in files:
        src = os.path.join(dire, fn)
        rel = os.path.relpath(src, STAGING).replace("\\", "/")
        if not any(rel.lower().startswith(w.lower()) for w in WHITELIST):
            continue
        dst = os.path.join(ROOT, rel.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        except Exception as e:
            log(f"跳过 {rel}: {e}")
log(f"覆盖完成 {n} 个文件")

# 记录版本
try:
    vp = os.path.join(STAGING, "VERSION")
    if os.path.exists(vp):
        shutil.copy2(vp, os.path.join(ROOT, "VERSION"))
except Exception:
    pass

shutil.rmtree(os.path.join(ROOT, "userdata", "update"), ignore_errors=True)

# 重启（Windows 用 .bat 里那条命令同款）
try:
    if os.name == "nt":
        subprocess.Popen([sys.executable, "-m", "server"],
                         cwd=ROOT, creationflags=0x00000008)
    else:
        subprocess.Popen([sys.executable, "-m", "server"], cwd=ROOT,
                         start_new_session=True)
    log("已拉起新版本")
except Exception as e:
    log(f"重启失败: {e}")
'''


def apply_update(staging: str, job=None) -> dict:
    """拉起独立更新器，并告诉调用方"可以退出主进程了"。"""
    d = os.path.join(paths.USERDATA, "update")
    os.makedirs(d, exist_ok=True)
    script = os.path.join(d, "apply_update.py")
    with open(script, "w", encoding="utf-8", newline="\n") as f:
        f.write(UPDATER_PY)
    args = [sys.executable, script, paths.ROOT, staging,
            str(os.getpid()), "|".join(WHITELIST)]
    flags = 0x00000008 if os.name == "nt" else 0
    subprocess.Popen(args, cwd=paths.ROOT,
                     creationflags=flags, start_new_session=(os.name != "nt"),
                     close_fds=True)
    if job is not None:
        job.log("更新器已启动，主程序即将退出以释放文件锁")
    return {"staged": staging, "version": _staged_version(staging)}


def _staged_version(staging: str) -> str:
    p = os.path.join(staging, "VERSION")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""
