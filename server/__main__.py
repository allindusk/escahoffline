"""入口：``python -m server [-p 8000] [--host 127.0.0.1] [--open]``

也兼容旧的 ``python web/serve.py``（它只是转调到这里）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser

from . import api, paths


def _port_free(port: int, host: str) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


# 局域网 IP 的判定统一用 api 里那一份：它会排除 Clash 等代理的 TUN 网段
# （198.18.x.x），优先 192.168 / 10 / 172.16-31。
# 以前这里另写了一份"只借默认路由"的实现，结果启动横幅打印的是 198.18.0.1，
# 而页面二维码显示 192.168.120.18 —— 两个地方说法不一致（用户 2026-11-02 反馈）。
_lan_ip = api._lan_ip


def _portable() -> bool:
    """是不是便携版（目录里带 python\\python.exe）。"""
    return os.path.exists(os.path.join(paths.ROOT, "python", "python.exe"))


#: 种子里的这几个文件要**跟着版本覆盖**（其余只在缺失时补，别动用户文件）。
#: 它们的共同点：是程序自带的文案 / 表，key 或条目随版本增删，不是用户攒的数据。
_SEED_OVERWRITE = ("web/assets/i18n/ui.json", "web/data/stand_box.js")


def _seed_overwrites(rel: str) -> bool:
    """这个种子文件要不要「内容不同就覆盖」。

    为什么页面也要覆盖（2026-11-17 实测）：更新是由**升级前那版**的更新器执行的，
    1.1.6 的白名单只认 index/cg/setup/notice —— 于是 1.1.6→1.1.7 时 ``music.html``
    留在旧版，而 ``music.js`` 已经换成新版：新版里多出来的 ``#plClose`` / ``#plMask``
    在旧页面上不存在，``$('#plClose').onclick`` 直接抛错，原声集页的初始化中断。
    只「补缺失文件」救不了这种「文件在、但内容是旧的」，所以页面按内容覆盖。
    页面 / 索引 / 界面文案都是程序文件，用户不会去改；真正属于用户的数据
    （``web/data/char_*.js``、``web/assets/i18n/<lang>/*.json``、``userdata/``）
    根本不会进种子。
    """
    return rel in _SEED_OVERWRITE or (rel.startswith("web/") and rel.endswith(".html"))

#: 种子路径 → 真实落点。**为什么不能直接按相同相对路径放**：种子里若出现
#: ``web/assets/i18n/ui.json``，老版本更新器的黑名单（``web/assets/i18n/``）会在
#: 解压阶段就把它滤掉，等于没送到。改个不带 i18n 段的名字放进种子，老版本照常
#: 拷贝，新版本启动时再搬到这里。
_SEED_MAP = {"extras/i18n-ui.json": "web/assets/i18n/ui.json"}


def _same_file(a: str, b: str) -> bool:
    """两个文件内容是否逐字节相同（种子自愈时用来省掉无意义的复制）。"""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return True          # 读不了就当"一样"，宁可不写也别把文件写坏


def _heal_seed_files() -> int:
    """把 ``web/assets/seed/`` 里的种子按**相同相对路径**补到本机缺失的位置。

    为什么需要这一层（2026-11-16 实测事故）：更新包里的文件是新的，但**执行覆盖的
    是升级前那个版本生成的更新器脚本**，它只认当时那份白名单 —— v1.1.6 新增的
    ``shell.html`` / ``stand.html`` / ``music.html``、v1.1.3 才加的
    ``data/stand_box.js`` 全被挡在门外，于是升级完自动打开的
    ``http://127.0.0.1:8000/shell.html`` 直接 404，用户在页面里点「检查更新」也补不
    回来（同一个旧清单）。

    种子随更新包一起下发（CI 把它放在 ``web/assets/`` 下，这个前缀任何版本都放行），
    新版本启动时按同名相对路径补缺；页面 / 程序索引 / 界面文案这三类**内容不同就覆盖**
    （见 :func:`_seed_overwrites`），其余只在目标不存在时写。
    白名单已经改成 ``web/*.html``，以后新页面不会再漏，这层只管老用户跨版本救场。
    """
    seed = os.path.join(paths.WEB, "assets", "seed")
    if not os.path.isdir(seed):
        return 0
    n = 0
    for dirpath, _dirs, files in os.walk(seed):
        for fn in files:
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, seed).replace("\\", "/")
            rel = _SEED_MAP.get(rel, rel)
            dst = os.path.join(paths.ROOT, rel)
            # 例外：页面 / 程序索引 / 界面文案是**程序文件**，必须跟着版本走 ——
            # 老更新器的白名单 / 黑名单把它们挡掉了（它只认升级前那份清单），
            # 不在这里覆盖的话老用户会停在旧页面 / 缺新 key。只在内容不同时写。
            if _seed_overwrites(rel):
                try:
                    if os.path.exists(dst) and _same_file(src, dst):
                        continue
                except OSError:
                    pass
            elif os.path.exists(dst):
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                n += 1
            except Exception:                                  # noqa: BLE001
                pass
    if n:
        try:
            from . import log as _log
            _log.log(f"启动自愈：按种子同步 {n} 个文件（页面 / 程序索引 / 界面文案）",
                     "boot")
        except Exception:                                      # noqa: BLE001
            pass
    return n


class _SafeStream:
    """兜底流：编码不出来的字符换成 "?"，绝不抛 UnicodeEncodeError。"""

    def __init__(self, inner):
        self._inner = inner

    def write(self, text):
        try:
            return self._inner.write(text)
        except UnicodeEncodeError:
            enc = (getattr(self._inner, "encoding", "") or "utf-8")
            return self._inner.write(text.encode(enc, "replace").decode(enc, "replace"))

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _harden_stdio() -> None:
    """让 print 永不因编码失败（进程启动第一件事就做）。

    为什么必须做：Windows 下 stdout 一旦被**重定向**（管道 / IDE 终端 / 第三方
    启动器 / 服务被别的进程拉起），Python 就不再走控制台的 UTF-8 通道，而是用
    系统 ANSI 代码页（中文 Windows = GBK）。此时 ``print("✓")``、打印带「・」
    的日文标题会抛 ``UnicodeEncodeError``。

    要命的不是这行字没打出来，而是**异常会顺着调用栈冒上去**：这些 print 混在
    装剧情 / 超分的后台任务里，装一步炸一次，整个单元就被判成失败。
    2026-11-02 19:13「安装 74 部剧情」74 部全灭，报的就是
    ``'gbk' codec can't encode character '\\u2713' in position 9``
    （来自 ``stages.compose_stands()`` 末尾那句「立绘尺寸一致 ✓」）。

    编码跟着**控制台的活动代码页**走：控制台是 65001（启动脚本都 ``chcp 65001``）
    就改回 UTF-8，中文/日文/符号全都正常；是 GBK 就保持 GBK，只把编码不出来的
    ✓ / ⚠ / ・ 降级成 "?"。两种情况下都不再抛异常，也不影响任何业务结果。
    """
    cp = 0
    try:
        import ctypes
        cp = int(ctypes.windll.kernel32.GetConsoleOutputCP() or 0)   # type: ignore[attr-defined]
    except Exception:                                    # noqa: BLE001
        cp = 0
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:                                    # pythonw：没有控制台
            continue
        try:
            if cp == 65001:                              # 控制台要 UTF-8，别再按 GBK 编
                s.reconfigure(encoding="utf-8", errors="replace")
            else:
                s.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            try:
                setattr(sys, name, _SafeStream(s))
            except Exception:                            # noqa: BLE001
                pass


def _say(name: str) -> None:
    """打印 tools/msg/<name>.txt 里的提示文案（缺文件就静默跳过）。"""
    p = os.path.join(paths.ROOT, "tools", "msg", name + ".txt")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            print(f.read().rstrip())
    except OSError:
        pass


def main(argv=None) -> int:
    _harden_stdio()          # 必须在任何 print 之前：见 _harden_stdio 的注释
    ap = argparse.ArgumentParser(prog="python -m server",
                                 description="ESCAH 离线阅读器本地服务")
    ap.add_argument("-p", "--port", type=int, default=0,
                    help="端口（默认 8000，占用时自动往后找）")
    ap.add_argument("--host", default=None,
                    help="监听地址（便携版默认 0.0.0.0 允许局域网；"
                         "源码版默认仅本机 127.0.0.1）")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--page", default="index.html")
    a = ap.parse_args(argv)

    # 横幅由 Python 打印，而不是启动脚本：老用户更新上来的 bat 可能还是
    # 1.0.2 那一份（更新器白名单曾排除 启动.bat），不会 call :say banner。
    _say("banner")
    print()

    # 默认监听地址由 Python 自己决定，不再依赖启动脚本传参：
    # 1.0.2 时代更新器的白名单**故意排除**了「启动.bat」，老用户通过页面
    # 更新上来时那份 bat 永远停在旧版本（没有 --host 0.0.0.0），于是
    # 局域网功能实际是失效的 —— 而全量包却是好的，两边体验不一致。
    # 现在只要目录里有 python\python.exe 就默认 0.0.0.0，源码版保持仅本机。
    host = a.host or ("0.0.0.0" if _portable() else "127.0.0.1")

    ok, ver = paths.python_ok()
    if not ok:
        print(f"需要 Python 3.8+，当前 {ver}")
        return 2

    # 「根目录 / 正在使用的 Python」由 api.serve() 的启动横幅打印（2026-11-18 用户要求：
    # 报错时得知道装在哪、跑的是哪个解释器）。这里那行 boot 日志仍然记一份，便于事后查。

    # 子进程统一管理：不弹黑窗；Ctrl+C / 关窗口时杀掉所有子进程，避免残留
    from . import log as applog, proc
    proc.install_signal_handlers()
    applog.log(f"boot v{paths.app_version()} python={ver} exe={sys.executable}", "boot")

    # 这次是**从旧版本升级上来**吗？是、并且本机立绘确实是旧合成逻辑做的，就留一个
    # 标记，资源管理页打开时弹一次「旧版立绘可能有问题，要不要现在修」——立绘是本机
    # 合成的产物，更新器不碰它，用户不点这个修复就永远不会好。新装用户（没有旧版
    # 记录）不打扰；已经修过的也不再问（旧写法只看「版本号变了」就弹，于是
    # 1.1.6 -> 1.1.7 这种合成逻辑没动过的升级也会白问一次，2026-11-18 反馈）。
    try:
        _up = paths.note_run_version()
        if _up:
            if paths.stand_repair_needed():
                paths.save_json(paths.STAND_PROMPT_FILE,
                                {"from": _up["from"], "to": _up["to"],
                                 "at": time.strftime("%Y-%m-%d %H:%M:%S")})
                applog.log(f"版本升级 {_up['from']} -> {_up['to']}："
                           "立绘是旧合成逻辑做的，已登记修复提示", "boot")
            else:
                # 版本变了但这个不用修：顺手清掉上次没点掉的旧标记（否则会接着弹）
                try:
                    os.remove(paths.STAND_PROMPT_FILE)
                except OSError:
                    pass
                applog.log(f"版本升级 {_up['from']} -> {_up['to']}："
                           "立绘已是当前合成逻辑，不提示", "boot")
    except Exception:                                          # noqa: BLE001
        pass

    # 更新包的兜底种子：老用户升级时"新版文件到了、却被旧白名单挡掉"的救场，
    # 说明见 _heal_seed_files()
    try:
        _heal_seed_files()
    except Exception:                                          # noqa: BLE001
        pass

    # 本机生成的数据表（standmap/gallery/voicemap）不随程序分发，覆盖升级后必然
    # 丢失；剧情/素材还在，但没人重新触发重建 → 立绘页、原声集页列表全空。
    # 启动时后台补建一次（有产物、缺索引才做），不阻塞开页面。用户看到的是
    # "几秒后刷新即可"，而不是一个永远空白的页面（2026-11-16 覆盖升级实测）。
    def _heal_local_indexes():
        try:
            from .cdn import config as _cfg, stages as _st
            data, img = _cfg.DATA, _cfg.IMG
            # 立绘：有合成产物却缺分组表 → 重建
            has_stand = os.path.isdir(os.path.join(img, "stand"))
            if has_stand and not os.path.exists(os.path.join(data, "standmap.js")):
                applog.log("启动自愈：立绘产物在、standmap.js 缺失，后台重建索引", "boot")
                _st.regen_standmap()
        except Exception as e:                                 # noqa: BLE001
            try:
                applog.log("启动自愈本地索引失败：" + str(e), "boot")
            except Exception:
                pass
    threading.Thread(target=_heal_local_indexes, name="heal-indexes",
                     daemon=True).start()

    # 按保留策略收敛日志：当天全量、>1 天只留 ERROR、>7 天删除
    try:
        _pr = applog.prune()
        applog.log("日志保留：当天全量 / >%d 天仅 ERROR / >%d 天删除；"
                   "本次压缩 %d 份、删除 %d 份；当前文件 %s"
                   % (applog.COMPACT_DAYS, applog.KEEP_DAYS,
                      _pr.get("compacted", 0), _pr.get("dropped", 0),
                      applog.log_path()), "boot")
    except Exception:                                          # noqa: BLE001
        pass
    # 路径含中文这类**警告**不进启动窗口（2026-11-18 用户：窗口里出现警告会让用户
    # 以为程序坏了）：只落 app.log，页面上由 /api/state 的 warn 字段挂提示条。
    _warn = paths.path_warning()
    if _warn:
        applog.log(_warn.replace("\n", " "), "boot")

    port = a.port or int(paths.load_config().get("port") or 8000)
    if not _port_free(port, host):
        for p in range(port + 1, port + 21):
            if _port_free(p, host):
                print(f"端口 {port} 被占用，改用 {p}")
                port = p
                break
        else:
            print(f"{port}–{port + 20} 都被占用，请手动指定：-p 8123")
            return 1

    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/{a.page}"
    if a.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # 访问地址由 api.serve() 的横幅统一打印（含手机/同网段那条），这里只记日志：
    # 以前这里也 print 一遍，同一个地址在窗口里出现两次，用户以为起两个服务。
    applog.log("将打开 %s" % url, "boot")

    # 立绘体检**不在启动时跑**：全库比对要读上千个资源包（老机器一两分钟），
    # 开机自启时风扇转起来用户会以为程序出问题。改由用户在
    # 「资源管理 → 设置 / 清理空间 → 维护」里点按钮触发（/api/stand/repair）。
    return api.serve(host, port)


if __name__ == "__main__":
    sys.exit(main())
