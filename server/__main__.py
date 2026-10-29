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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server",
                                 description="ESCAH 离线阅读器本地服务")
    ap.add_argument("-p", "--port", type=int, default=0,
                    help="端口（默认 8000，占用时自动往后找）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认仅本机；0.0.0.0 允许局域网访问）")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--page", default="index.html")
    a = ap.parse_args(argv)

    ok, ver = paths.python_ok()
    if not ok:
        print(f"需要 Python 3.8+，当前 {ver}")
        return 2

    # 子进程统一管理：不弹黑窗；Ctrl+C / 关窗口时杀掉所有子进程，避免残留
    from . import log as applog, proc
    proc.install_signal_handlers()
    applog.log(f"boot v{paths.app_version()} python={ver}", "boot")
    _warn = paths.path_warning()
    if _warn:
        print("[!] " + _warn.replace("\n", "\n    "))
        applog.log(_warn.replace("\n", " "), "boot")

    port = a.port or int(paths.load_config().get("port") or 8000)
    if not _port_free(port, a.host):
        for p in range(port + 1, port + 21):
            if _port_free(p, a.host):
                print(f"端口 {port} 被占用，改用 {p}")
                port = p
                break
        else:
            print(f"{port}–{port + 20} 都被占用，请手动指定：-p 8123")
            return 1

    url = f"http://{'127.0.0.1' if a.host == '0.0.0.0' else a.host}:{port}/{a.page}"
    if a.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"打开：{url}")
    return api.serve(a.host, port)


if __name__ == "__main__":
    sys.exit(main())
