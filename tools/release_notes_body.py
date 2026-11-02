# -*- coding: utf-8 -*-
"""生成 Release 正文：**只放这一版的「新增 / 修复」**。

    python tools/release_notes_body.py dist/notes.md [版本号] [构建提交]

为什么不是直接把 ``docs/release-notes.md`` 整份贴上去
--------------------------------------------------
那份文件是模板：标题、引子、所有历史版本、「下载哪个」「第一次用」「注意」，
四千多字。控制台「检查更新」会把 Release 正文原样显示给用户 —— 一整篇糊在
小框里，看不到重点（2026-11-03 用户反馈："只留更新版本的更新和修复内容"）。
用户手上的旧版本还没有裁剪逻辑，所以**正文本身**就得是短的，否则老客户端照样是一堵墙。

裁剪规则与运行时同源（``server/release_notes.py``），不在这里另写一份正则。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.release_notes import notes_brief  # noqa: E402


def main(argv: list[str]) -> int:
    out = argv[1] if len(argv) > 1 else os.path.join("dist", "notes.md")
    ver = argv[2] if len(argv) > 2 else ""
    sha = argv[3] if len(argv) > 3 else ""
    if not ver:
        try:
            with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
                ver = f.read().strip()
        except OSError:
            ver = ""
    with open(os.path.join(ROOT, "docs", "release-notes.md"), encoding="utf-8") as f:
        body = f.read().replace("{VERSION}", ver)
    brief = notes_brief(body, f"v{ver}")
    if not brief:
        brief = f"v{ver}"
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))
    text = brief.rstrip() + "\n"
    if sha:
        # 不用 `>` 引用：老客户端的说明框不认引用块，会原样显示那个 ">" 符号
        text += f"\n本包构建自 main@{sha}（北京时间 {stamp}）\n"
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"已生成 {out}（v{ver}，{len(text)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
