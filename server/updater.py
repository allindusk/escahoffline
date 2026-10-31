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

import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile

from . import mirrors, net, paths

#: 允许被更新覆盖的路径前缀（相对项目根）
WHITELIST = (
    "server/", "web/assets/", "tools/", "docs/",
    "web/index.html", "web/cg.html", "web/setup.html", "web/serve.py",
    "VERSION", "README.md", "requirements.txt",
    "start.bat", "start.sh", "停止.bat",
    # 「启动.bat」**可以**放行：仓库里那份优先用 python\python.exe（内置），
    # 找不到才退回系统 Python，便携版与源码版通用。
    # 以前不放行，是因为便携版的 bat 由 CI 另外生成、和仓库这份行为不一致
    # （生成的那份没有自检）；那个生成步骤已经删了，现在只有一份。
    "启动.bat",
)
#: 明确禁止（双保险，即便 zip 里有也不动）
#:
#: ``web/assets/i18n/`` 必须在里面：那里是**用户自己翻译攒下来的译文**，
#: 属于用户数据，不是程序。更新包里若带一份旧译文，会把用户后来的翻译覆盖回去。
BLACKLIST = ("web/media/", "web/data/", "web/assets/i18n/", "userdata/",
             ".git/", ".github/")
#: ``web/data/`` 整体是"装了哪些剧情"的用户侧状态，更新时不能碰；但里面有几个
#: **程序自带的索引**必须能随更新刷新 —— 否则老用户永远停在旧数据（现实例子：
#: charmeta.js 曾经带中文名，改成纯日文后，不刷新索引的老包还会一直显示中文）。
#: 只放行明确是程序产出、且不会被安装流程重写的文件；要放行新的必须在这里加。
WEB_DATA_UPDATABLE = ("web/data/charmeta.js", "web/data/names.js")


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


def check(budget: float = 25.0) -> dict:
    """→ {current, latest, hasUpdate, url, notes, via, error}

    ``budget`` 是**整次检查的总预算（秒）**。为什么要有它：下面按线路顺序逐个试，
    线路多的时候国内实测能串到 2 分钟 —— 用户点「检查更新」看到的就是转圈两分钟，
    还以为是卡死。

    线路规则（2026-11-01 用户定版）：**不测速**，直接按
    ``mirrors.GH_ROUTES`` 的顺序（gh-proxy 主站 → 优选 v4 → 优选 v4/v6 →
    Fastly → 官方直连）逐条试，**有响应就用**。

    两条获得 release 信息的路：

    1. **GitHub API**（信息最全），但 ``api.github.com`` 国内经常连不上
       —— 2026-10-30 实测本机 **DNS 直接解析失败**。所以这里
       **按线路顺序逐个试 API**，而不是只试官方。
    2. **releases.atom**（不需要 API，反代几乎都支持）：API 全挂时用它拿到
       最新 tag 与说明，再按命名规则拼出资产 URL 并 HEAD 验证存在性。

    资产挑选顺序也改了：**先 update 包（0.75 MB）**再 portable 包（114 MB）——
    以前是先挑 portable，自动更新会白下 100 多 MB。
    """
    out = {"current": current_version(), "latest": "", "hasUpdate": False,
           "url": "", "notes": "", "via": "", "staged": "",
           "published": "", "html": "", "error": ""}
    meta = app_meta()
    repo = meta.get("repo") or ""
    if not repo:
        out["error"] = "未配置仓库地址（web/data/app.json 的 repo）"
        return out

    deadline = time.monotonic() + max(6.0, float(budget))
    routes = mirrors.ranked("github")
    rel, via, err = _fetch_latest(repo, routes, deadline)
    if rel is None:
        out["error"] = err or "查询失败：GitHub API 与 releases.atom 都不可达"
        return out

    tag = rel.get("tag_name") or ""
    assets = rel.get("assets") or []
    if not assets and tag:
        # atom 兜底里没有资产列表 → 按命名规则拼，再 HEAD 逐个验证
        assets = _guess_assets(repo, tag, routes,
                               meta.get("name") or "ESCAH-Offline", deadline)
    url, _portable = _pick_assets(assets)
    # 后台已经把新版本下好了：即使远端 tag 与当前相同（比如 Release 刚被重发），
    # 也认为"可更新" —— 否则用户点不动「重启并应用」，暂存区就成了死数据
    staged = pending_version()

    out.update({
        "latest": tag.lstrip("vV"),
        "hasUpdate": _ver_tuple(tag) > _ver_tuple(out["current"]) or bool(staged),
        "url": url,
        "notes": (rel.get("body") or "")[:4000],
        "via": via, "published": rel.get("published_at") or "",
        "html": rel.get("html_url") or "",
        # 前端据此把按钮写成"重启并应用"、并跳过二次下载
        "staged": staged,
    })
    return out


def _fetch_latest(repo: str, routes: list[dict],
                  deadline: float = 0.0) -> tuple[dict | None, str, str]:
    """→ (release dict, 走通了哪条线路, 错误信息)。两条路都失败才算失败。

    ``deadline`` 是 ``time.monotonic()`` 的绝对时刻，用尽就立即返回，
    不再一条条干等。API 阶段额外**预留 8 秒给 atom 兜底**: 反代对
    ``api.github.com`` 的支持很不一致（不少只代理 github.com），不能把
    预算全烧在 API 上，否则 atom 这条路连试都试不到。
    """
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    err = ""
    tried = ""
    for route in routes:
        left = _left_api(deadline)
        if left <= 1.0:
            err = err or "线路全部超时（总预算用尽）"
            break
        real = mirrors.apply_github(route, api)
        if tried.count(real):                      # 不走重复地址
            continue
        tried += real + "|"
        try:
            d = net.fetch_json(real, timeout=min(6.0, left))
            if isinstance(d, dict) and d.get("tag_name"):
                return d, route.get("name", "?"), ""
        except Exception as e:                                   # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:60]}"
    # API 全线不通 → releases.atom 兜底（反代基本都支持它）
    try:
        d, via = _release_from_atom(repo, routes, deadline)
        return d, via, ""
    except Exception as e:                                       # noqa: BLE001
        err += f"；atom：{type(e).__name__}: {str(e)[:60]}"
    return None, "", err


def _left(deadline: float) -> float:
    """距 deadline 还剩多少秒；``deadline<=0`` 表示不限时。"""
    if not deadline:
        return 3600.0
    return deadline - time.monotonic()


def _left_api(deadline: float) -> float:
    """API 阶段可用时间 = 总预算 - 预留给 atom 的 8 秒。"""
    if not deadline:
        return 3600.0
    return max(0.0, deadline - time.monotonic() - 8.0)


def _release_from_atom(repo: str, routes: list[dict],
                       deadline: float = 0.0) -> tuple[dict, str]:
    """从 ``releases.atom`` 解析最新 release（tag / 说明 / 时间）。"""
    feed = f"https://github.com/{repo}/releases.atom"
    last = ""
    for route in routes:
        left = _left(deadline)
        if left <= 1.0:
            last = last or "releases.atom 线路超时（总预算用尽）"
            break
        try:
            text = net.fetch_text(mirrors.apply_github(route, feed),
                                  timeout=min(8.0, left))
        except Exception as e:                                   # noqa: BLE001
            last = str(e)[:60]
            continue
        m = re.search(r"<entry>(.*?)</entry>", text, re.S)
        if not m:
            last = "atom 里没有 entry"
            continue
        e = m.group(1)
        tag = (re.search(r"<title>([^<]+)</title>", e) or None)
        tag = tag.group(1).strip() if tag else ""
        body = re.search(r"<content[^>]*>(.*?)</content>", e, re.S)
        notes = ""
        if body:
            notes = html.unescape(body.group(1))
            notes = re.sub(r"<[^>]+>", " ", notes)
            notes = re.sub(r"\s{2,}", " ", notes).strip()
        upd = re.search(r"<updated>([^<]+)</updated>", e)
        if tag:
            return {"tag_name": tag, "body": notes, "assets": [],
                    "published_at": upd.group(1) if upd else "",
                    "html_url": f"https://github.com/{repo}/releases/tag/{tag}"}, \
                route.get("name", "?")
    raise RuntimeError(last or "releases.atom 不可达")


def _guess_assets(repo: str, tag: str, routes: list[dict],
                  app_name: str, deadline: float = 0.0) -> list[dict]:
    """按命名规则拼候选 URL，HEAD 验真后返回。

    只在 GitHub API 完全不可用时走这条路 —— 用户的包名是固定的
    ``ESCAH-Offline-<版本>-<类型>.zip``，而 tag 可能带或不带 ``v``，
    所以两组 tag × 三种后缀都试一遍。

    线路同样**按顺序**试：某条线路对 6 个 HEAD 全都没有响应（连 404 都没
    返回）说明它连不上，换下一条；只要有响应就认定"包名对不上"，不再浪费
    时间问其它线路（同一个 release 的资产列表是全局一致的）。
    """
    tags = [tag]
    if tag[:1] in ("v", "V"):
        tags.append(tag[1:])
    else:
        tags.append("v" + tag)
    kinds = ("update.zip", "win64-portable.zip", "portable.zip")
    base = f"https://github.com/{repo}/releases/download/{tag}/"
    for route in routes or [{"tpl": "{url}"}]:
        responded = False
        for t in tags:
            for k in kinds:
                if _left(deadline) <= 1.0:      # 预算用尽：别把 6 个 HEAD 也拖到底
                    return []
                name = f"{app_name}-{t}-{k}"
                cand = base + name
                try:
                    size, _ = net.probe(mirrors.apply_github(route, cand))
                    responded = True
                except Exception:                                # noqa: BLE001
                    continue
                if size > 0:
                    # 只返回"确实存在"的那些，UI/下载逻辑照 API 格式消费
                    return [{"name": name, "browser_download_url": cand}]
        if responded:
            return []          # 线路通了但包名不存在 → 问别的线路也是同样答案
    return []


def _pick_assets(assets: list[dict]) -> tuple[str, str]:
    """→ (优先用的更新包, 便携包)。有 update 包就绝不选 114 MB 的全量包。"""
    update, portable, anyzip = "", "", ""
    for a in assets or []:
        n = (a.get("name") or "").lower()
        u = a.get("browser_download_url") or ""
        if not n.endswith(".zip") or not u:
            continue
        if not anyzip:
            anyzip = u
        if "update" in n:
            update = update or u
        elif "portable" in n or "win" in n:
            portable = portable or u
    return (update or portable or anyzip), (portable or update or anyzip)


# ---------------------------------------------------------------- 启动检查
#: 启动自查的结果（只读给前端挂提示条；**不含任何下载动作**）
_STARTUP: dict = {"enabled": False, "done": False, "at": 0.0, "current": "",
                  "latest": "", "hasUpdate": False, "staged": "", "via": "",
                  "error": ""}
_STARTUP_LOCK = threading.Lock()


def startup_state() -> dict:
    """启动检查的当前状态（``/api/state`` 读它，页面据此挂提示条）。"""
    with _STARTUP_LOCK:
        return dict(_STARTUP)


def auto_check(delay: float = 3.0) -> bool:
    """按设置决定是否**在启动后查一次更新**，返回是否真起了线程。

    设置项：``userdata/config.json`` 的 ``autoCheckUpdate``（**默认开**，
    控制台「更新」页有勾选框；用户取消勾选后启动时零网络请求）。

    这条自动检查的边界（用户 2026-11-01 明确要求加的，别扩大范围）：
      * 只查一次 —— 不轮询、不"每 N 小时再来一次"；
      * 只读 release 信息，**绝不下载、绝不重启**；
      * 结果只用来在页面上挂一条"发现新版本"的提示，装不装由用户点。
    也就是说：开关打开时唯一被"自动发起"的请求就是这一次查询，
    其它任何对外请求仍然只发生在用户点按钮之后。
    """
    cfg = paths.load_config()
    if not cfg.get("autoCheckUpdate", True):
        return False
    with _STARTUP_LOCK:
        _STARTUP.update({"enabled": True, "done": False, "error": ""})
    threading.Thread(target=_auto_check_worker, args=(max(0.0, float(delay)),),
                     name="auto-check-update", daemon=True).start()
    return True


def _auto_check_worker(delay: float) -> None:
    time.sleep(delay)
    try:
        info = check(budget=20.0)
    except Exception as e:                                       # noqa: BLE001
        info = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    with _STARTUP_LOCK:
        _STARTUP.update({
            "done": True, "at": time.time(),
            "current": info.get("current") or current_version(),
            "latest": info.get("latest") or "",
            "hasUpdate": bool(info.get("hasUpdate")),
            "staged": info.get("staged") or "",
            "via": info.get("via") or "",
            "error": info.get("error") or "",
        })


# ---------------------------------------------------------------- 暂存区
# 注意：**没有定时/轮询任务**。启动时那一次检查由 ``auto_check()`` 按设置
# （默认开，可关）决定，且只查不装；其余对外请求仍只发生在用户点过按钮之后：
# 点「检查程序更新」→ 一次 release 查询；点「下载并应用」→ 下载。
def staging_dir() -> str:
    return os.path.join(paths.USERDATA, "update", "staging")


def pending_version() -> str:
    """暂存区里**准备好且比当前新**的版本号；没有就返回空串。

    前端靠它决定要不要挂"新版本已就绪，重启即生效"的提示条。
    """
    v = _staged_version(staging_dir())
    if v and _ver_tuple(v) > _ver_tuple(current_version()):
        return v
    return ""


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

    线路规则（2026-11-01 用户定版）：**不测速**，按 ``mirrors.GH_ROUTES`` 的
    顺序逐条试 —— gh-proxy 主站 → 优选 v4 → 优选 v4/v6 → Fastly → 官方直连。
    每条先花最多 6 秒探活，**有响应就下**（不管快慢）；下载失败再换下一条。
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
    # https://gh-proxy.org/http://192.168.1.5/upd.zip，必然失败。
    if "github.com" in url or "githubusercontent.com" in url:
        routes = mirrors.ranked("github", job=job)
        if not routes:
            raise ManualDownload(url, "没有可用的下载线路")
    else:
        job.log("下载地址不是 GitHub，直接下载（不套反代）")
        routes = [{"id": "direct", "name": "直连", "tpl": "{url}"}]

    job.set_total(1)

    last = None
    for i, route in enumerate(routes, 1):
        if job.cancelled:
            raise RuntimeError("已取消")
        name = route.get("name", "?")
        real = mirrors.apply_github(route, url)
        # 先探活（最多 6 秒）：线路不通时 net.download 会一直等到超时，
        # 一个 70 MB 的包能卡好几分钟，用户只会觉得"死了"。
        job.log(f"线路 {i}/{len(routes)}：{name}（探活最多 6 秒）…")
        if not mirrors.quick_check(route, url, timeout=6):
            job.log("  没有正常响应，换下一条")
            last = last or RuntimeError(f"{name} 无响应")
            continue
        if real != url:
            job.log(f"  经反代：{real[:110]}")
        try:
            net.download(real, zp, threads=4, job=job)
            last = None
            break
        except Exception as e:                                 # noqa: BLE001
            last = e
            job.log(f"  这条线路下载失败（{type(e).__name__}: {str(e)[:80]}），换下一条…")
    if last is not None:
        raise ManualDownload(url, f"已尝试 {len(routes)} 条线路，最后一条报错：{last}")
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
    if any(low.endswith(p) for p in WEB_DATA_UPDATABLE):
        return True                       # 例外：这几个索引要能随更新刷新
    for b in BLACKLIST:
        if low.startswith(b) or f"/{b}" in low:
            return False
    return any(low.startswith(w.lower()) or f"/{w.lower()}" in low
               for w in WHITELIST)


def allowed(relpath: str) -> bool:
    low = relpath.replace("\\", "/").lower()
    if low in WEB_DATA_UPDATABLE:
        return True                       # 同上：索引例外
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

# 重启：**必须给用户一个看得见的窗口**。
# 以前用 CREATE_NO_WINDOW(0x8)：新进程在后台悄悄跑，任务栏里什么都没有 ——
# 用户既不知道程序还活着，也没法"关窗口"停掉它。
# 现在新开一个控制台(0x10)，并带 --open 打开一个**新标签页**：
# 更新前那个标签页停在旧 DOM（实测角色列表会一直转圈），新标签才是干净的。
try:
    if os.name == "nt":
        subprocess.Popen([sys.executable, "-m", "server", "--open",
                          "--page", "setup.html"],
                         cwd=ROOT, creationflags=0x00000010)
    else:
        subprocess.Popen([sys.executable, "-m", "server", "--open",
                          "--page", "setup.html"],
                         cwd=ROOT, start_new_session=True)
    log("已拉起新版本（新控制台窗口 + 自动打开页面）")
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
    # 独立更新器只认这份清单，所以把索引例外也并进去（否则它会跳过 charmeta.js）
    allow = tuple(WHITELIST) + tuple(WEB_DATA_UPDATABLE)
    args = [sys.executable, script, paths.ROOT, staging,
            str(os.getpid()), "|".join(allow)]
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
