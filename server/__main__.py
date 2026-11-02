"""入口：``python -m server [-p 8000] [--host 127.0.0.1] [--open]``

也兼容旧的 ``python web/serve.py``（它只是转调到这里）。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
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

    # 同理：新包 bat 里这行已被删掉，旧包 bat 也从来没有过，
    # 统一由 Python 打印，两种启动方式输出一致。
    _say("using")
    print("  " + sys.executable)
    print()

    # 子进程统一管理：不弹黑窗；Ctrl+C / 关窗口时杀掉所有子进程，避免残留
    from . import log as applog, proc
    proc.install_signal_handlers()
    applog.log(f"boot v{paths.app_version()} python={ver}", "boot")
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
    _warn = paths.path_warning()
    if _warn:
        print("[!] " + _warn.replace("\n", "\n    "))
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
    print(f"打开：{url}")
    if host == '0.0.0.0':
        lan = _lan_ip()
        if lan:
            print(f"手机/同网段设备访问：http://{lan}:{port}/{a.page}")
    return api.serve(host, port)


if __name__ == "__main__":
    sys.exit(main())
