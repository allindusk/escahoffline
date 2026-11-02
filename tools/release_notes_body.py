# -*- coding: utf-8 -*-
"""生成 Release 正文：**只放这一版的「新增 / 修复」**。

    python tools/release_notes_body.py dist/notes.md [版本号]

为什么不是直接把 ``docs/release-notes.md`` 整份贴上去
--------------------------------------------------
那份文件是模板：标题、引子、所有历史版本、「下载哪个」「第一次用」「注意」，
四千多字。控制台「检查更新」会把 Release 正文原样显示给用户 —— 一整篇糊在
小框里，看不到重点（2026-11-03 用户反馈："只留更新版本的更新和修复内容"）。
用户手上的旧版本还没有裁剪逻辑，所以**正文本身**就得是短的，否则老客户端照样是一堵墙。

裁剪规则与运行时同源（``server/release_notes.py``），不在这里另写一份正则。

注意 Windows 上的编码：GH Actions 的 windows runner 里 ``python`` 的 stdout 是
**cp1252**，直接 ``print("已生成…")`` 会抛 ``UnicodeEncodeError`` 把构建搞挂
（2026-11-03 手动跑 CI 撞到）。所以开头统一把 stdout 重设成 UTF-8，
并且任何打印都不允许影响退出码。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.release_notes import notes_brief  # noqa: E402


def _safe_stdout() -> None:
    """把 stdout/stderr 切成 UTF-8，切不动就退化成"打不出来也不报错"。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            pass


def _say(msg: str) -> None:
    try:
        print(msg)
    except Exception:                                          # noqa: BLE001
        try:
            print(msg.encode("ascii", "backslashreplace").decode("ascii"))
        except Exception:                                      # noqa: BLE001
            pass


def main(argv: list[str]) -> int:
    _safe_stdout()
    out = argv[1] if len(argv) > 1 else os.path.join("dist", "notes.md")
    ver = argv[2] if len(argv) > 2 else ""
    if not ver:
        try:
            with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
                ver = f.read().strip()
        except OSError:
            ver = ""
    with open(os.path.join(ROOT, "docs", "release-notes.md"), encoding="utf-8") as f:
        body = f.read().replace("{VERSION}", ver)
    brief = notes_brief(body, f"v{ver}") or f"v{ver}"
    text = brief.rstrip() + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    _say(f"已生成 {out}（v{ver}，{len(text)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
