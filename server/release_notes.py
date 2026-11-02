# -*- coding: utf-8 -*-
"""把 Release 正文裁成「这一版改了什么」。

为什么单独一个模块
------------------
同一段逻辑要给两边用：
  * 运行时：「检查更新」把 ``notes`` 塞进控制台的小框（``server/updater.py``）；
  * 构建时：``tools/release_notes_body.py`` 生成 Release 正文（``release.yml``）。
放在这里就不必在 YAML / pwsh 里再写一遍正则（写两遍必然走味）。
本模块**只用标准库**，CI 上用系统 python 直接跑，不拉任何依赖。
"""
from __future__ import annotations

import re

_HEAD_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_VER_RE = re.compile(r"v?\d+\.\d+")

DEFAULT_LIMIT = 1800


def notes_brief(body: str, version: str = "", limit: int = DEFAULT_LIMIT) -> str:
    """只留 ``version`` 那一版的「新增 / 修复」小节。

    Release 正文若用整份 ``docs/release-notes.md`` 模板，会带上标题、引子、
    所有历史版本、下载说明、第一次用、注意事项 —— 几千字。控制台的
    「检查更新」小框塞进去就是一堵墙（2026-11-03 用户反馈：只留更新版本的
    更新和修复内容）。

    取法：找到标题里带 ``version`` 的 ``### vX.Y.Z``（找不到就退到第一个带版本号的
    三级标题），一路取到**同级或更高级的下一个标题**为止；顺带丢掉 ``>`` 引用行
    （那是写给我们自己看的模板说明）。长度再兜一层，免得将来某版写太长又把框撑爆。
    """
    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    heads = []
    for i, ln in enumerate(lines):
        m = _HEAD_RE.match(ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    if not heads:
        return (body or "").strip()[:limit]

    target = None
    if version:
        v = str(version).lstrip("vV")
        # 不能取"第一个包含版本号的标题"：正文第一行是 `# … v1.0.7`（一级标题），
        # 从它开始取就等于还是整篇。优先级：标题以 `v1.0.7` 开头（= 就是这一版的小节）
        # > 只是包含；同级里取更深的（### 优先于 #）。
        cands = []
        for i, lv, title in heads:
            tl = title.strip()
            if v and v in tl:
                head_on = tl.lower().startswith("v" + v) or tl.startswith(v)
                cands.append((0 if head_on else 1, -lv, i, lv))
        if cands:
            cands.sort()
            target = (cands[0][2], cands[0][3])
    if target is None:
        # 兜底：第一个「带版本号的三级/四级标题」= 最新那一版
        for i, lv, title in heads:
            if lv >= 3 and _VER_RE.search(title):
                target = (i, lv)
                break
    if target is None:
        return (body or "").strip()[:limit]

    start, level = target
    end = len(lines)
    for i, lv, _title in heads:
        if i > start and lv <= level:
            end = i
            break
    chunk = []
    for ln in lines[start + 1:end]:
        s = ln.rstrip()
        if s.lstrip().startswith(">"):
            continue                       # 模板说明的引用块，不给用户看
        chunk.append(s)
    text = "\n".join(chunk).strip()
    # 行内多余缩进去掉（渲染器只认行首缩进做续行）
    text = "\n".join(ln if ln[:1].strip() else ln.strip() for ln in text.split("\n"))
    if len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        text = (text[:cut] if cut > 200 else text[:limit]).rstrip() \
            + "\n\n…（更多见项目主页的 Release 说明）"
    return text
