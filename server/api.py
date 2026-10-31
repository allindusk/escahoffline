"""HTTP 服务：静态站点 + JSON API + SSE 进度流。

设计原则
--------
* 页面与 API 同源（不需要 CORS），默认只监听 127.0.0.1
* 所有响应禁缓存：数据和覆盖层文件经常被改写（F5 就该看到最新）
* 耗时操作一律返回 ``{"job": {...}}``，前端订阅 ``/api/jobs/stream`` 看进度
* 出错统一 ``{"error": "人话错误信息"}`` + HTTP 状态码
* 所有写操作都有"没有就报错并说清楚缺什么"，不静默失败
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse

from . import (exports, jobs, log as applog, media, mirrors, packs, paths, proc,
               updater, videxport)
from .cdn import install as cdn_install
from .cdn import manifest as cdn_manifest
from .cdn import stages as cdn_stages
from .cdn import wiki as cdn_wiki

STOP = threading.Event()

#: 本次进程的唯一标识。前端记住第一次拿到的值，一旦变了就说明**服务重启过**
#: （程序更新：主进程退出 → 更新器覆盖文件 → 拉起新进程）。
#: 没有它的话，更新完旧页面会一直显示旧 DOM，用户以为"没更新成功"。
BOOT_ID = "%x%x" % (int(time.time() * 1000), os.getpid())


class SubJob:
    """把子任务的进度"平移"到父任务上，且不改父任务的 total。"""

    def __init__(self, parent, base: float, weight: float = 1.0):
        self.p = parent
        self.base = base
        self.weight = weight

    @property
    def cancelled(self) -> bool:
        return self.p.cancelled

    def set_total(self, n, unit: str = "") -> None:      # 忽略：父级已统筹
        pass

    def set_items(self, n) -> None:                       # noqa: D401
        pass

    def progress(self, done=None, add=0.0, message=None, **detail) -> None:
        if done is not None:
            self.p.progress(self.base + float(done) * self.weight,
                            message=message, **detail)
        elif add:
            self.p.progress(add=add * self.weight, message=message, **detail)
        elif message:
            self.p.log(message)

    def log(self, msg: str) -> None:
        self.p.log(msg)


# ---------------------------------------------------------------- 业务动作
# 注：这里原本有 _download_one / net_download / run_pipeline ——
# 那是「从自建地址下载 zip、本地解包」的旧流程，早已被官方 CDN 直连取代
# （见下方 run_install）。留着它只会让人以为还支持 zip 安装。


# ---------------------------------------------------------------- 安装（官方 CDN 直连）
def run_install(job, units: list[str], workers: int = 32,
                voice: bool = True, image: bool = True,
                upscale: bool = False, scale: int = 2,
                engine: str = "cugan") -> dict:
    """串行安装多个剧情单元（每个单元内部并发下载 bundle）。

    进度条语义：``total/done`` 交给下载层（用户最关心"下了多少包"），
    单元计数走 ``items_total/items_done`` + ``message``，两者不打架。
    """
    job.set_items(len(units))
    job.log(f"共 {len(units)} 部剧情，下载并发 {workers}")
    ok, failed = [], []
    for i, u in enumerate(units, 1):
        if job.cancelled:
            job.state = "cancelled"
            break
        job.log(f"[{i}/{len(units)}] 安装 {u}")
        try:
            r = cdn_install.install_unit(
                u, job, workers=workers, voice=voice, image=image,
                upscale=upscale, upscale_scale=scale, upscale_engine=engine)
            ok.append(r)
            job.items_done = i
            job.log(f"[{u}] 完成（{r.get('seconds', 0)} 秒）")
        except Exception as e:                                 # noqa: BLE001
            failed.append({"unit": u, "error": str(e)[:300]})
            job.items_done = i
            job.log(f"[{u}] 失败：{e}")
    cdn_install.invalidate_units()
    if not failed:
        job.finish(f"完成：{len(ok)} 部剧情")
    elif ok:
        job.finish(f"部分完成：{len(ok)} 成功 / {len(failed)} 失败")
    else:
        # 一部都没装上：必须标失败。以前这里什么都不做，runner 兜底调 finish()，
        # 于是"全部失败"在界面上显示为"完成"。
        job.fail("全部失败：" + (failed[0]["error"] if failed else ""))
    job.detail["failed"] = failed
    return {"installed": ok, "failed": failed}


# ---------------------------------------------------------------- 依赖安装
#: 解包要用到的包：(import 名, pip 名)
DEPS = [("UnityPy", "UnityPy"), ("lz4", "lz4"), ("PIL", "Pillow"),
        ("numpy", "numpy"), ("cryptography", "cryptography")]


def _foreground_folder() -> None:
    """把资源管理器窗口提到前台（Windows 专用，失败无所谓）。

    为什么需要：``os.startfile`` 打开文件夹后，Windows 不一定给前台焦点，
    用户会以为"没打开"。置前台只是补救；真正的保证是前端把路径显示出来。
    """
    try:
        import ctypes
        u = ctypes.windll.user32                        # type: ignore[attr-defined]
        hwnd = u.FindWindowW("CabinetWClass", None)
        if not hwnd:
            return
        u.ShowWindow(hwnd, 9)                           # SW_RESTORE（最小化的先还原）
        # Windows 只允许"当前前台窗口"或"刚按过 Alt"的进程抢前台，
        # 单纯 SetForegroundWindow 会被系统忽略 —— 窗口仍留在任务栏里，
        # 小白用户就会以为"没给我打开"。这里补一次 Alt 按键再置前。
        u.keybd_event(0x12, 0, 0, 0)                    # Alt down
        u.SetForegroundWindow(hwnd)
        u.BringWindowToTop(hwnd)
        u.keybd_event(0x12, 0, 2, 0)                    # Alt up
    except Exception:                                      # noqa: BLE001
        pass


def cdn_manifest_cdn() -> str:
    """当前生效的资源源地址（解密后的真实值）。只给本机设置页核对用。"""
    try:
        from .cdn import config as cdn_config
        return cdn_config.cdn_base()
    except Exception:                                          # noqa: BLE001
        return ""


def _missing_pkgs() -> list[str]:
    out = []
    for mod, pkg in DEPS:
        try:
            __import__(mod)
        except Exception:                                      # noqa: BLE001
            out.append(pkg)
    return out


def _test_mirrors(job, kind: str = "pip") -> dict:
    job.log(f"正在测速 {kind} 线路…")
    res = mirrors.test(kind)
    mirrors.save_cache(kind, res)
    for r in res:
        if r.get("ok"):
            job.log(f"  {r['name']}：{r['ttfb']} ms / {r['kbps']} KB/s"
                    f"（评分 {r['score']}）")
        else:
            job.log(f"  {r['name']}：不可用（{r.get('error', '')[:40]}）")
    ok = [r for r in res if r.get("ok")]
    job.log(f"最快：{ok[0]['name']}" if ok else "全部不可用，将退回官方源")
    return {"results": res}


# 注：这里原本有 run_deps_install（在服务进程里 pip 装依赖）。**已移除**，原因：
#   1. 装完必须重启服务才生效，用户点完看到"完成"却发现功能还是不能用，以为没修好；
#   2. 如果服务是用系统 Python 启动的，pip 会装进全局环境，污染用户机器；
#   3. 正确的入口是 启动.bat —— 它调 tools/bootstrap_deps.py，会先测速选最快镜像，
#      而且不需要"重启才生效"这种隐藏条件。
# 控制台只保留"检测缺什么 + 告诉用户怎么办"（/api/deps/status）。


def _run_uninstall(job, units: list[str], drop_cache: bool = False) -> dict:
    """卸载已解包资源。默认保留 bundle 缓存（重装时不用重下）。"""
    job.set_items(len(units))
    ok, failed = [], []
    for i, u in enumerate(units, 1):
        if job.cancelled:
            job.state = "cancelled"
            break
        try:
            r = cdn_install.uninstall(u, job=job)
            ok.append(r)
        except Exception as e:                                 # noqa: BLE001
            failed.append({"unit": u, "error": str(e)[:200]})
            job.log(f"[{u}] 卸载失败：{e}")
        job.items_done = i
    if drop_cache and ok:
        job.log("顺带清理不再被引用的资源包缓存…")
        try:
            cdn_install.prune_cache(job, dry=False)
        except Exception as e:                                 # noqa: BLE001
            job.log(f"  缓存清理失败（不影响卸载）：{e}")
    freed = sum(x.get("bytes") or 0 for x in ok) / 1048576
    if failed and not ok and not job.cancelled:
        job.fail("全部卸载失败：" + failed[0]["error"])
    else:
        job.finish(f"已卸载 {len(ok)} 部，释放 {freed:.1f} MB"
                   + ("（缓存已清理）" if drop_cache else "（bundle 缓存保留，重装免下载）"))
    job.detail["failed"] = failed
    return {"removed": ok, "failed": failed}


def _installed_char_units() -> list[str]:
    """当前装着的角色单元（给"全站级"任务登记占用用）。"""
    try:
        return [k for k in (cdn_install.local_units() or {}) if k.startswith("char_")]
    except Exception:                                          # noqa: BLE001
        return []


def run_video_export(job, units: list[str], parts: list[int],
                     limit: int = 0, port: int = 8000) -> dict:
    """逐个角色导出一条 mp4（多段合成一条）。"""
    job.set_items(len(units))
    job.log(f"共 {len(units)} 条视频，每段按所选 {len(parts)} 段合成一条")
    ok, failed = [], []
    for i, u in enumerate(units, 1):
        if job.cancelled:
            job.state = "cancelled"
            break
        job.log(f"[{i}/{len(units)}] {u}")
        try:
            r = videxport.export(job, u, parts, limit=limit, port=port)
            ok.append(r)
            job.log(f"[{u}] 完成（{r.get('seconds', 0)} 秒）")
        except Exception as e:                                 # noqa: BLE001
            failed.append({"unit": u, "error": str(e)[:300]})
            job.log(f"[{u}] 失败：{e}")
        job.items_done = i
    job.detail["failed"] = failed
    if not failed:
        job.finish(f"完成：{len(ok)} 条视频")
    elif ok:
        job.finish(f"部分完成：{len(ok)} 成功 / {len(failed)} 失败")
    else:
        # 一条都没导出来：必须标 failed。以前这里不进任何分支，runner 兜底
        # finish() → 界面上是"完成"，用户找一个不存在的 mp4（e2e 就是这么漏掉的）。
        job.fail("全部失败：" + (failed[0]["error"] if failed else ""))
    return {"done": ok, "failed": failed}


def _sync_wiki(job, proxy: str | None) -> dict:
    """从公开 Wiki 拉角色信息。连不上（多半没开代理）时把 proxy_needed 标进
    job.detail，前端据此提示用户开启代理后补下载。"""
    try:
        return cdn_stages.sync_wiki(job=job, proxy=proxy)
    except cdn_wiki.WikiUnreachable as e:
        job.detail["proxy_needed"] = bool(e.proxy_hint)
        job.detail["wiki_error"] = str(e)[:300]
        raise


def _refresh_manifest_and_wiki(job) -> dict:
    """一次做两件事：刷新官方清单 → 更新角色信息（稀有度 / 类型 / 实装日）。

    为什么合并成一个动作：用户点「更新」是想"把该更新的都更新了"，拆两个按钮
    只会让人漏点其中一个（而且第二步本来就依赖第一步刷出来的新剧情）。
    第 ② 步失败**不能拖垮第 ① 步** —— 清单已经刷新成功，用户至少要能看到
    新剧情列表。
    """
    res = _refresh_manifest(job)
    if job.cancelled:
        return res
    job.log("接着更新角色信息（类型 / 稀有度 / 实装日）…")
    try:
        meta = _sync_wiki(job, None)
        for k in ("merged", "scraped", "with_rarity", "source"):
            if k in meta:
                res[k] = meta[k]
    except Exception as e:                                     # noqa: BLE001
        res["metaError"] = str(e)[:300]
        job.log(f"角色信息没更新成功：{e}（不影响清单与剧情安装）")
    return res


def _refresh_manifest(job) -> dict:
    """重新拉官方清单（AES 解密），并算出"本地还没装的新剧情"。

    进度按"两份清单"计数（Manifest1Ex / Manifest2Ex）：单份下载 3.5 MB + 解密
    是一次不可再分的操作，所以这里给的是有意义的阶段进度，而不是假动画。
    """
    try:
        before = set()
        u = cdn_install.list_units()
        before = set(u["char"]) | set(u["adv"])
    except Exception:                                          # noqa: BLE001
        before = set()

    names = list(cdn_manifest.MANIFEST_NAMES)
    job.set_total(len(names), "个")      # 两份清单，别按字节显示成 "1 B/2 B"
    job.set_items(len(names))
    for i, name in enumerate(names, 1):
        if job.cancelled:
            break
        job.log(f"下载并解密 {name}（{i}/{len(names)}，约 "
                f"{'3.5' if name.endswith('1Ex') else '1.5'} MB）…")
        cdn_manifest.refresh(names=[name])
        job.progress(done=i, message=f"{name} 完成")

    cdn_install.invalidate_units()
    after = cdn_install.list_units(job)
    alln = set(after["char"]) | set(after["adv"])
    new = sorted(alln - before)
    job.detail["newUnits"] = new[:300]
    job.detail["totalUnits"] = len(alln)
    job.log(f"清单已更新：云端共 {len(alln)} 部剧情"
            + (f"，新增 {len(new)} 部" if new else "，没有新增"))

    # 公共素材是必选包：清单变了就顺手补齐，用户不用再跑一趟
    try:
        add = cdn_install.new_public()
    except Exception as e:                                     # noqa: BLE001
        job.log(f"（公共素材差异检查跳过：{e}）")
        add = {}
    if add and not job.cancelled:
        desc = "、".join(f"{k} +{v} 个" for k, v in add.items())
        job.log(f"公共素材有新增：{desc}，开始自动补下（已有的会跳过）…")
        try:
            cdn_install.install_base(job, parts=tuple(add))
            job.log("公共素材已补齐")
        except Exception as e:                                 # noqa: BLE001
            job.log(f"公共素材补下失败（不影响剧情列表）：{e}")
    elif add:
        job.log("公共素材有新增，但任务已取消，未自动下载")

    return {"newUnits": new, "total": len(alln), "publicAdded": add}


class _LegacyNote:
    """占位：旧的 zip 资源包流程已由官方 CDN 直连取代。"""


# ---------------------------------------------------------------- handler
class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "ESCAH/" + paths.app_version()
    protocol_version = "HTTP/1.1"

    #: Windows 上 ``mimetypes`` 常常不认 webp/m4a，会退化成
    #: ``application/octet-stream``。浏览器没 nosniff 时靠嗅探还能显示，但很脆；
    #: 这里显式补上（超分图、语音、视频导出都用到）。
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".webp": "image/webp",
        ".avif": "image/avif",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".woff2": "font/woff2",
        ".wasm": "application/wasm",
    }

    # ---------------- 基础设施
    def end_headers(self):
        self.send_header("Cache-Control",
                         "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        # API 走 _log_req（带耗时/状态码/请求体摘要），这里只管静态资源，
        # 否则每个 /api 请求会被记两遍。
        if urllib.parse.urlparse(self.path).path.startswith("/api/"):
            return
        try:
            code = int(str(args[1])) if len(args) > 1 else 0
        except (ValueError, IndexError):
            code = 0
        if code >= 400:
            try:
                applog.warn("static " + (fmt % args), tag="http")
            except Exception:                                    # noqa: BLE001
                pass
            # stdlib 的 log_message 会写 sys.stderr，而**无控制台启动**
            # （pythonw / CREATE_NO_WINDOW）时 sys.stderr 是 None →
            # AttributeError 一路冒到 send_error 里，把本该 404 的静态资源
            # 变成 500。日志已经落到 app.log，这里直接跳过，别再往下走。
            if sys.stderr is not None:
                try:
                    super().log_message(fmt, *args)
                except Exception:                                # noqa: BLE001
                    pass

    def translate_path(self, path):
        # 静态文件根固定为 web/
        p = path.split("?", 1)[0].split("#", 1)[0]
        p = urllib.parse.unquote(p)
        parts = [x for x in p.split("/") if x not in ("", ".", "..")]
        return os.path.join(paths.WEB, *parts)

    # ---------------- 响应工具
    def _json(self, obj, code: int = 200):
        self._status = int(code)
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg: str, code: int = 400):
        self._status = int(code)
        self._err_msg = str(msg)[:600]
        self._json({"error": str(msg)[:600]}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        # 只留个摘要：别把整段译文/大 JSON 塞进日志
        self._body_note = "%dB %s" % (
            n, raw[:160].decode("utf-8", "replace").replace("\n", " "))
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                return {}
        return {"_raw": raw}

    def _q(self) -> dict:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    def _log_req(self, method: str, t0: float) -> None:
        """每条 API 请求都记一条：方法 路径 状态码 耗时 (+POST 体摘要/错误)。

        当天的日志是"全量"的 —— 出问题时能按时间线还原用户点了什么、
        哪个请求返回了什么。1 天后只保留 ERROR（见 log.prune）。
        """
        ms = (time.time() - t0) * 1000
        code = getattr(self, "_status", 200)
        msg = "%s %s %d %.0fms" % (method, self.path, code, ms)
        note = getattr(self, "_body_note", "")
        if note:
            msg += " body=" + note
        err = getattr(self, "_err_msg", "")
        if err:
            msg += " err=" + err
        if code >= 500:
            applog.error(msg, tag="http")
        elif code >= 400:
            applog.warn(msg, tag="http")
        else:
            applog.debug(msg, tag="http")
        # handler 实例会被 keep-alive 复用，必须清干净
        self._status = 200
        self._err_msg = ""
        self._body_note = ""

    # ---------------- 路由
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        t0 = time.time()
        try:
            if path.startswith("/api/"):
                r = self._api_get(path)
                self._log_req("GET", t0)
                return r
            return super().do_GET()
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError):
            # 客户端提前断开（刷新/关标签/切页面）。这不是服务端错误，
            # 记成 ERROR 只会淹没真正的问题（实测一天刷了 40 条）。
            applog.debug("GET %s 客户端断开" % self.path, tag="http")
            return None
        except Exception as e:                                   # noqa: BLE001
            self._status = 500
            applog.error("GET %s 未捕获异常\n%s"
                         % (self.path, traceback.format_exc()), tag="http")
            try:
                self._err(e, 500)
            except Exception:                                    # noqa: BLE001
                pass
            self._log_req("GET", t0)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        t0 = time.time()
        try:
            if not path.startswith("/api/"):
                self._err("not found", 404)
                self._log_req("POST", t0)
                return None
            r = self._api_post(path)
            self._log_req("POST", t0)
            return r
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError):
            applog.debug("POST %s 客户端断开" % self.path, tag="http")
            return None
        except Exception as e:                                   # noqa: BLE001
            self._status = 500
            applog.error("POST %s 未捕获异常\n%s"
                         % (self.path, traceback.format_exc()), tag="http")
            try:
                self._err(e, 500)
            except Exception:                                    # noqa: BLE001
                pass
            self._log_req("POST", t0)

    # ---------------- GET
    def _api_get(self, path: str):
        q = self._q()
        if path == "/api/state":
            return self._json(self.state_payload())
        if path == "/api/manifest":
            return self._json(packs.manifest())
        if path == "/api/links":
            cfg = packs.links()
            return self._json({"cdn": cfg.get("cdn", ""),
                               "base": cfg.get("base", ""),
                               "pattern": cfg.get("pattern", "{id}.zip"),
                               "overrides": cfg.get("overrides", {}),
                               "note": cfg.get("note", ""),
                               "effective": cdn_manifest_cdn(),
                               "custom": os.path.exists(paths.LINKS_FILE)})
        if path == "/api/jobs":
            # boot/version 让前端能发现"服务重启过"：程序更新会先退出主进程、
            # 由更新器覆盖文件后拉起新进程，旧页面完全感知不到（DOM 还是旧的），
            # 必须靠这个标识触发自动刷新。
            return self._json({"jobs": jobs.all_jobs(),
                               "boot": BOOT_ID,
                               "version": paths.app_version()})
        if path == "/api/jobs/stream":
            return self._sse()
        if path == "/api/stats":
            return self._json({"img": media.img_stats(force=q.get("force") == "1"),
                               "export": exports.list_exports()[:20],
                               "trans": exports.trans_stats(),
                               "coverage": packs.coverage()})
        if path == "/api/export/trans":
            unit = q.get("unit") or ""
            text = exports.export_trans(unit)
            # 带 BOM：不加的话 Windows 记事本另存为时容易落成 GBK，导回来全乱码
            body = text.encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{unit}.txt"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        if path == "/api/update/check":
            return self._json(updater.check())
        if path == "/api/exports":
            return self._json({"items": exports.list_exports()})
        if path == "/api/engine":
            return self._json({"pillow": media.have_pillow(),
                               "engine": media.find_engine()})
        if path == "/api/mirrors":
            return self._json(mirrors.summary(q.get("kind") or "pip"))
        if path == "/api/deps/status":
            miss = _missing_pkgs()
            return self._json({"missing": miss, "ready": not miss})
        if path == "/api/cglist":
            return self._json(exports.cg_list())
        if path == "/api/upscale/plan":
            kinds = (q.get("kinds") or "stand,back,cg").split(",")
            kinds = tuple(k for k in (x.strip() for x in kinds) if k)
            try:
                scale = int(q.get("scale") or 2)
            except ValueError:
                scale = 2
            try:
                return self._json(media.upscale_plan(kinds, scale))
            except Exception as e:                             # noqa: BLE001
                return self._err(e)
        if path == "/api/export/video/info":
            miss = videxport.missing_deps()
            return self._json({"missing": miss, "ready": not miss})
        if path == "/api/export/video/probe":
            unit = q.get("unit") or ""
            try:
                parts = [int(x) for x in (q.get("parts") or "1").split(",")
                         if x.strip()]
            except ValueError:
                return self._err("段落参数不对")
            try:
                return self._json(videxport.probe(unit, parts))
            except Exception as e:                             # noqa: BLE001
                return self._err(e)
        if path == "/api/cdn/info":
            return self._json(cdn_install.status())
        if path == "/api/cdn/public":
            return self._json(cdn_install.public_status())
        if path == "/api/cdn/units":
            # 清单不随仓库分发，所以首次使用必须先拉一次（约 5 MB）。
            # 这里**不阻塞**去下载，直接告诉前端"还没清单"，让用户点按钮走带进度的
            # 后台任务；否则这个请求会卡十几秒，看起来像页面死了。
            if not cdn_manifest.has_manifest():
                return self._json({
                    "needManifest": True,
                    "units": {"char": [], "adv": []},
                    "state": cdn_install.local_units(),
                })
            try:
                u = cdn_install.list_units()
            except Exception as e:                             # noqa: BLE001
                return self._err(e)
            return self._json({"units": u,
                               "state": cdn_install.unit_status_map()})
        return self._err("unknown api", 404)

    # ---------------- POST
    def _api_post(self, path: str):
        if path == "/api/config":
            b = self._body()
            cfg = paths.load_config()
            cfg.update({k: v for k, v in b.items() if k in paths.DEFAULTS})
            return self._json({"config": paths.save_config(cfg)})
        if path == "/api/jobs/clear":
            return self._json({"cleared": jobs.clear_finished()})
        if path.startswith("/api/jobs/") and path.endswith("/pause"):
            jid = path.split("/")[3]
            return self._json({"ok": jobs.pause(jid)})
        if path.startswith("/api/jobs/") and path.endswith("/resume"):
            jid = path.split("/")[3]
            return self._json({"ok": jobs.resume(jid)})
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            jid = path.split("/")[3]
            return self._json({"ok": jobs.cancel(jid)})
        if path in ("/api/download", "/api/install"):
            b = self._body()
            sel = b.get("selected") or b.get("units") or []
            if not sel:
                return self._err("没有选择要安装的剧情")
            miss = cdn_install.missing_deps()
            if miss:
                return self._err("缺少依赖：" + "、".join(miss)
                                 + "。请先运行 pip install -r requirements.txt")
            workers = int(b.get("threads") or 32)
            workers = max(1, min(64, workers))
            voice = bool(b.get("voice", True))
            image = bool(b.get("image", True))
            up = bool(b.get("upscale", False))
            scale = int(b.get("scale") or 2)
            engine = b.get("engine") or "cugan"
            job = jobs.submit(
                "install", f"安装 {len(sel)} 部剧情",
                lambda j: run_install(j, sel, workers, voice, image,
                                      up, scale, engine),
                holds=sel)
            return self._json({"job": job.snapshot()})
        if path == "/api/upscale":
            b = self._body()
            kinds = tuple(b.get("kinds") or ("stand", "back", "cg", "standfull"))
            scale = int(b.get("scale") or 2)
            engine = b.get("engine") or "cugan"
            limit = int(b.get("limit") or 0)

            def fn(job):
                media.upscale(job, kinds=kinds, scale=scale, engine=engine,
                              limit=limit)
            job = jobs.submit("upscale", "图片超分", fn,
                              holds=_installed_char_units())
            return self._json({"job": job.snapshot()})
        if path == "/api/convert":
            b = self._body()
            q = int(b.get("quality") or paths.load_config()["webpQuality"])
            kinds = tuple(b.get("kinds") or ("stand", "back", "cg", "standfull"))
            job = jobs.submit("convert", "批量转 WebP",
                              lambda j: media.convert_existing(j, q, kinds),
                              holds=_installed_char_units())
            return self._json({"job": job.snapshot()})
        if path == "/api/delete-originals":
            job = jobs.submit("cleanup", "删除已超分的原图",
                              lambda j: media.delete_originals(j),
                              holds=_installed_char_units())
            return self._json({"job": job.snapshot()})
        if path == "/api/install/base":
            b = self._body()
            raw = b.get("parts") or ["bg", "bgm", "sfx"]
            parts = [p for p in raw if p in cdn_install.PUBLIC_PARTS]
            if not parts:
                return self._err("没有选择要下载的公共素材")
            workers = max(1, min(64, int(b.get("threads") or 32)))
            job = jobs.submit(
                "base", "下载公共素材（背景 / BGM / 音效）",
                lambda j: cdn_install.install_base(j, workers, tuple(parts)))
            return self._json({"job": job.snapshot()})
        if path == "/api/uninstall":
            b = self._body()
            units = b.get("units") or ([b["unit"]] if b.get("unit") else [])
            units = [u for u in units if u]
            if not units:
                return self._err("没有选择要卸载的剧情")
            # 卸载会删剧本与素材。若有任务正在读它们（导出视频/安装/导出 CG），
            # 删下去就是"文件凭空消失"式的崩溃 —— 直接拦住，并说清是谁在占。
            busy = jobs.holders(units)
            if busy:
                who = "、".join(f"{x['unit']}（{x['title']}）" for x in busy[:3])
                return self._err(f"这些剧情正被任务占用：{who}。"
                                 "等它结束后再卸载，或先点「取消」停掉它")
            drop = bool(b.get("dropCache", False))
            job = jobs.submit("uninstall", f"卸载 {len(units)} 部剧情",
                              lambda j: _run_uninstall(j, units, drop),
                              holds=units)
            return self._json({"job": job.snapshot()})
        if path == "/api/cdn/prune":
            b = self._body()
            dry = b.get("dry", True) is not False
            job = jobs.submit(
                "prune", "清理未引用的资源包" + ("（预演，不会删）" if dry else "（真删）"),
                lambda j: cdn_install.prune_cache(j, dry=dry))
            return self._json({"job": job.snapshot()})
        if path == "/api/export/video":
            b = self._body()
            units = b.get("units") or ([b["unit"]] if b.get("unit") else [])
            units = [u for u in units if u]
            if not units:
                return self._err("没有选择角色")
            try:
                # 段落号 **1 基**（与 story.Timeline 的 `pi - 1`、导出名 p<N> 一致）
                parts = sorted({int(x) for x in (b.get("parts") or [1])})
            except (TypeError, ValueError):
                return self._err("段落参数不对")
            miss = videxport.missing_deps()
            if miss:
                return self._err("缺少依赖：" + "、".join(miss)
                                 + "。到「剧情安装」页点「自动安装依赖」")
            limit = int(b.get("limit") or b.get("maxSeconds") or 0)
            port = self.server.server_address[1]
            job = jobs.submit(
                "video", f"导出 {len(units)} 条视频",
                lambda j: run_video_export(j, units, parts, limit, port),
                holds=units)
            return self._json({"job": job.snapshot()})
        if path == "/api/export/cg":
            b = self._body()
            chars = [int(x) for x in (b.get("chars") or [])]
            groups = [[int(x[0]), int(x[1])] for x in (b.get("groups") or [])]
            whole = bool(b.get("whole", False))
            # 导出 CG 要读当前装着的图；卸载同时把图删了就只能导出半套，所以也占住单元
            hold = ([f"char_{c}" for c in chars] if chars
                    else _installed_char_units())
            job = jobs.submit(
                "export", "导出角色 CG", lambda j: exports.export_cg(
                    j, chars=chars or None, groups=groups or None, whole=whole),
                holds=hold)
            return self._json({"job": job.snapshot()})
        if path == "/api/export/video":
            q = self._q()
            name = urllib.parse.unquote(q.get("name") or "story")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self._err("没有收到视频数据")
            os.makedirs(paths.DOWNLOADS, exist_ok=True)
            tmp = os.path.join(paths.DOWNLOADS, f"upload_{int(time.time())}.webm")
            left = n
            with open(tmp, "wb") as f:
                while left > 0:
                    buf = self.rfile.read(min(1024 * 1024, left))
                    if not buf:
                        break
                    f.write(buf)
                    left -= len(buf)
            info = exports.save_video(tmp, name)
            return self._json({"ok": True, "file": os.path.basename(info["path"]),
                               "bytes": info["bytes"]})
        if path == "/api/import/trans":
            b = self._body()
            unit = (b.get("unit") or "").strip()
            text = b.get("text") or ""
            mode = b.get("mode") or "merge"
            if not unit or not text:
                return self._err("缺少 unit 或译文内容")
            return self._json(exports.import_trans(unit, text, mode))
        if path == "/api/export/trans/batch":
            # 与「批量导入」对称：把多部剧情的 txt 打成一个 zip
            # scope=translated（默认，已有译文）| installed（全部已装剧情原文）
            b = self._body()
            scope = (b.get("scope") or "translated").strip()
            if scope not in ("translated", "installed"):
                return self._err("scope 只能是 translated / installed")
            units = b.get("units")
            if units is not None:
                if not isinstance(units, list):
                    return self._err("units 必须是数组")
                units = [str(u) for u in units if u]
                if not units:
                    return self._err("没有选择剧情")
            job = jobs.submit(
                "transzip", "批量导出译文",
                lambda j: exports.export_trans_batch(j, scope, units))
            return self._json({"job": job.snapshot()})
        if path == "/api/update/apply":
            b = self._body()
            url = b.get("url") or ""
            job = jobs.submit("update", "应用更新", lambda j: _do_update(j, url))
            return self._json({"job": job.snapshot()})
        if path == "/api/shutdown":
            STOP.set()
            self._json({"ok": True, "msg": "服务即将退出"})
            threading.Thread(target=self._delayed_exit, daemon=True).start()
            return None
        if path == "/api/reset-overlay":
            b = self._body()
            kind = b.get("kind") or "base"
            return self._json({"ok": media.reset_overlay(kind)})
        if path == "/api/manifest/refresh":
            # 一个入口做两步：清单 → 角色信息（前端只有一个「更新」按钮）
            job = jobs.submit("manifest", "刷新清单与角色信息",
                              lambda j: _refresh_manifest_and_wiki(j))
            return self._json({"job": job.snapshot()})
        if path == "/api/wiki/sync":
            b = self._body()
            proxy = (b.get("proxy") or "") or None
            job = jobs.submit("wiki", "从 Wiki 更新角色信息",
                              lambda j: _sync_wiki(j, proxy))
            return self._json({"job": job.snapshot()})
        if path == "/api/mirrors/test":
            b = self._body()
            kind = b.get("kind") or "pip"
            job = jobs.submit("mirrors", f"测速 {kind} 线路",
                              lambda j: _test_mirrors(j, kind))
            return self._json({"job": job.snapshot()})
        if path == "/api/links":
            b = self._body()
            # cdn = 资源源根（现在的唯一用途）；base 保留兼容旧调用
            cdn = (b.get("cdn") or b.get("base") or "").strip()
            overrides = b.get("overrides") or {}
            pattern = (b.get("pattern") or "{id}.zip").strip() or "{id}.zip"
            if cdn:
                paths.save_json(paths.LINKS_FILE,
                                {"cdn": cdn.rstrip("/"), "pattern": pattern,
                                 "overrides": overrides,
                                 "note": b.get("note") or ""})
            else:
                try:
                    os.remove(paths.LINKS_FILE)
                except OSError:
                    pass
            return self._json({"ok": True, "links": packs.links(),
                               "effective": cdn_manifest_cdn()})
        if path == "/api/open":
            b = self._body()
            which = b.get("which") or "export"
            target = {"export": paths.EXPORT, "media": paths.MEDIA,
                      "userdata": paths.USERDATA, "root": paths.ROOT,
                      "logs": os.path.join(paths.USERDATA, "logs")}.get(which)
            if not target:
                return self._err("未知目录")
            os.makedirs(target, exist_ok=True)
            try:
                if os.name == "nt":
                    os.startfile(target)                          # noqa: S606
                    # Windows 经常把新开的资源管理器窗口留在后台，用户以为没打开。
                    # 延时一点点再置前台（窗口还没创建时置前台无效）。
                    threading.Timer(0.45, _foreground_folder).start()
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", target])
            except Exception as e:                               # noqa: BLE001
                return self._err(f"打不开目录：{e}")
            # 把路径回给前端 → 页面直接显示"已打开 XXX"，用户不用猜
            return self._json({"ok": True, "path": target})
        if path == "/api/rebuild-x2":
            job = jobs.submit("rebuild", "重建超分覆盖层",
                              lambda j: media.rebuild_x2_overlay(j))
            return self._json({"job": job.snapshot()})
        if path == "/api/clientlog":
            # 前端未捕获异常 / Promise 拒绝落盘。浏览器控制台用户看不到，
            # 反馈"点了没反应 / 白屏"时这是我们唯一的线索。
            b = self._body()
            items = b.get("items")
            if items is None:
                items = [b]
            n = 0
            for it in (items or [])[:30]:
                if not isinstance(it, dict):
                    continue
                lv = str(it.get("level") or "error").lower()
                page = str(it.get("page") or "")[:120]
                msg = str(it.get("msg") or "")[:500]
                src = str(it.get("src") or "")[:200]
                stack = str(it.get("stack") or "")[:1200]
                line = f"{page} {msg}" if page else msg
                if src:
                    line += f" @ {src}"
                if stack:
                    # 堆栈首行通常就是 "Error: <msg>"，和上面重复，去掉
                    parts = stack.splitlines()
                    if parts and parts[0].strip() == msg.strip():
                        parts = parts[1:]
                    if parts:
                        line += "\n" + "\n".join(parts)
                applog.log(line, tag="web",
                           level="ERROR" if lv == "error" else "WARN")
                n += 1
            return self._json({"ok": True, "logged": n})
        return self._err("unknown api", 404)

    def _delayed_exit(self):
        time.sleep(0.6)
        os._exit(0)

    # ---------------- SSE
    # 注意：前端现在用 /api/jobs 轮询，不再用 SSE —— 长连接会让浏览器与
    # 无头截图工具永远等不到"加载完成"。这里保留接口（给脚本/第三方用），
    # 但加了 30 分钟硬上限，避免客户端异常断开后线程被永久占用。
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last = ""
        deadline = time.time() + 30 * 60
        try:
            while not STOP.is_set() and time.time() < deadline:
                snap = {"jobs": jobs.all_jobs()[:12],
                        "state": self.state_payload(full=False)}
                body = json.dumps(snap, ensure_ascii=False)
                if body != last:
                    self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last = body
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True

    # ---------------- 状态聚合
    def state_payload(self, full: bool = True) -> dict:
        """full=False 只给 SSE 用的极小快照（每 0.5s 推一次，别塞大对象）。"""
        if not full:
            return {"coverage": cdn_install.coverage()}
        cfg = paths.load_config()
        return {
            "version": paths.app_version(),
            "python": paths.python_ok()[1],
            "pillow": media.have_pillow(),
            "coverage": cdn_install.coverage(),
            "config": cfg,
            "free": paths.disk_free(),
            "status": cdn_install.unit_status_map(),
            "cdn": cdn_install.status(),
            "engine": media.find_engine(),
            "meta": updater.app_meta(),
            "trans": exports.trans_stats(),
            # 已下载解压、等你点重启的新版本号（空串 = 没有待装更新）
            # health.js 每 60s 读一次，有值就顶部挂条
            "updateReady": updater.pending_version(),
            # 路径含中文时的提示（前端顶部条显示），以及日志位置
            "warn": paths.path_warning(),
            "logPath": applog.log_path(),
        }


def serve(host: str = "127.0.0.1", port: int = 8000) -> int:
    paths.ensure_dirs()
    # data/imgmap.x2.js 被三个页面无条件加载，缺它每次开页面都 404（刷日志 +
    # 控制台噪声）。空覆盖层等价于"没有任何超分图"，这里补一个。
    try:
        if not os.path.exists(os.path.join(paths.DATA, "imgmap.x2.js")):
            media.rebuild_x2_overlay()
    except Exception:                                          # noqa: BLE001
        pass
    # 同理补空表：这几个索引被页面**无条件加载**，但只有用户做过对应操作才会生成
    #   sfxmap.js        装过公共素材的「音效」才有（视频导出还直接读它）
    #   imgmap.base.js   ｜做过「转 webp」才有
    # 缺文件就是每次开页面 404（刷日志），无控制台运行时还会因为 stderr=None
    # 把 404 变成 500。空表 = "没有任何覆盖层"，语义正确。
    for _name, _body in (("sfxmap.js", "window.__SFX={};"),
                         ("imgmap.base.js", "window.__IMGBASE={};")):
        try:
            _p = os.path.join(paths.DATA, _name)
            if not os.path.exists(_p):
                with open(_p, "w", encoding="utf-8") as _f:
                    _f.write(_body)
        except Exception:                                      # noqa: BLE001
            pass
    handler = Handler
    try:
        httpd = http.server.ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        print(f"端口 {port} 不可用（{e}），换一个：python -m server -p 8012")
        return 1
    httpd.daemon_threads = True
    print("=" * 56)
    print(f"  ESCAH 离线阅读器  v{paths.app_version()}")
    print("=" * 56)
    print(f"  播放器   http://{host}:{port}/index.html")
    print(f"  控制台   http://{host}:{port}/setup.html")
    print(f"  资源根   {paths.MEDIA}")
    print("  关闭此窗口即停止服务（Ctrl+C）")
    print("=" * 56, flush=True)
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


#: **不设任何定时检查**：只有用户点了「检查程序更新」才发一次外网请求。
#:
#: 为什么要这么较真：这是个本地离线用工具，偷偷每隔几小时去 GitHub 发请求，
#: 用户（还有他的网络环境）完全不知情 —— 也不该由我们来决定什么时候联网。
#: 想查就在控制台点一下，查完想装再点一下，全程用户自己掌握。


def _do_update(job, url: str) -> dict:
    job.log("检查更新…")
    # 已经后台下载好的直接用：省掉二次下载，也让"点一下就重启"真的只要一秒
    ready = updater.pending_version()
    staging = updater.staging_dir()
    if ready and os.path.isdir(staging):
        job.log(f"使用已下载的 v{ready}，跳过下载")
    else:
        info = updater.check()
        if not url:
            url = info.get("url") or ""
        if not url:
            raise RuntimeError(info.get("error") or "没有可下载的更新包")
        try:
            staging = updater.stage(url, job)
        except updater.ManualDownload as e:
            # 不是错误，是兜底：把"去 GitHub 手动下载"的结构化信息交给前端渲染
            job.detail["manual"] = e.payload()
            job.log("自动下载失败，已切换到手动下载引导")
            raise
    job.log(f"已解压到暂存区：{staging}")
    res = updater.apply_update(staging, job)
    job.log("即将退出，更新器会自动覆盖文件并重启")
    threading.Timer(1.5, lambda: os._exit(0)).start()
    return res
