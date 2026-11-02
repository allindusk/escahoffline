# -*- coding: utf-8 -*-
"""**开发期**工具：查"哪些剧情还没有文字标题"，并按需把封面裁成对照图。

为什么是开发期工具、不是运行时逻辑
--------------------------------
标题在游戏数据里常常只以**封面横幅的美术字**存在（剧本 JSON 没有 title 字段，
``_0`` 段也可能整段没有），没有可读文本。所以"缺标题"这件事只能在发布前由维护者
读图补齐 —— 不能做成运行时去检查：那会让**用户的安装目录里凭空多出待办文件、
日志里刷维护者才看得懂的告警**，对普通玩家毫无意义。

运行时（``server/cdn/stages.py``）只做三件安静的事：剧本 ``_0`` 段自动取 →
``server/cdn/data/story_titles.json`` 里的现成标题 → 都没有就显示「活动剧情 第N话」。

用法：
    python tools/audit_titles.py              # 只出报告
    python tools/audit_titles.py --sheet      # 顺便把缺标题的活动封面拼图到 userdata/title_sheets/
    python tools/audit_titles.py --kind all   # event/other/main/all（默认 event）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
DATA = os.path.join(ROOT, "server", "cdn", "data")
SHEET_DIR = os.path.join(ROOT, "userdata", "title_sheets")

#: 官方一览里**没有独立剧本包**的条目：レイドイベント（在 other 包里）、
#: 広域戦 / 殲滅戦（同上）、○th アニバーサリー（没有剧情）、5.5th 的「特別クエスト」。
NON_ADV_EVENT_RE = re.compile(r"^(レイドイベント|広域戦|殲滅戦)|アニバーサリー|特別クエスト")

PER_SHEET = 3      # 一张图放几条
CROP_L = 0.42      # 裁掉左侧插画，只留 logo 区
OUT_W = 1400


def load_js(path: str):
    s = io.open(path, encoding="utf-8").read()
    return json.loads(s[s.index("=") + 1:].strip().rstrip(";"))


def load_json(name: str) -> dict:
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def official_adv_titles() -> list:
    """官方活动一览 → [(官方序号, 标题)]，只留真有剧本包的。"""
    out = []
    for line in load_json("event_titles.json").get("list") or []:
        m = re.match(r"イベント(\d+)_(.*)$", str(line))
        if not m or NON_ADV_EVENT_RE.search(m.group(2)):
            continue
        out.append((int(m.group(1)), m.group(2)))
    return out


def report(kind: str) -> list:
    """打报告，返回缺标题的条目（给拼图用）。"""
    rows = load_js(os.path.join(WEB, "data", "advindex.js"))
    order = load_json("story_titles.json").get("_event_order") or {}

    mapped = set(order.values())
    gap = [(no, t) for no, t in official_adv_titles() if no not in mapped]
    print("官方活动一览：%d 条有剧本包，已接入标题 %d 条" %
          (len(official_adv_titles()), len(official_adv_titles()) - len(gap)))
    if gap:
        print("  ⚠ 官方一览里有、_event_order 里没有的（新活动？）：")
        for no, t in gap:
            print("     #%d %s" % (no, t))

    picked = [r for r in rows
              if not r.get("title") and (kind == "all" or r["key"].split("_")[1] == kind)]
    print("advindex 共 %d 部；按 kind=%s 统计，缺文字标题 %d 部" % (len(rows), kind, len(picked)))
    for r in picked:
        print("  - %-16s no=%-4s cover=%-32s %s" %
              (r["key"], r.get("no"), r.get("cover") or "-", (r.get("first") or "")[:28]))
    return picked


def sheets(items: list) -> None:
    """把缺标题条目的封面裁成 logo 大图，3 条一张。"""
    from PIL import Image, ImageDraw

    os.makedirs(SHEET_DIR, exist_ok=True)
    for f in os.listdir(SHEET_DIR):
        if f.endswith(".png"):
            os.remove(os.path.join(SHEET_DIR, f))
    made = 0
    for n in range(0, len(items), PER_SHEET):
        tiles = []
        for r in items[n:n + PER_SHEET]:
            rel = r.get("cover") or ""
            path = os.path.join(WEB, rel)
            if not rel or not os.path.exists(path):
                continue
            im = Image.open(path).convert("RGB")
            w, h = im.size
            im = im.crop((int(w * CROP_L), 0, w, h))
            im = im.resize((OUT_W, round(im.height * OUT_W / im.width)), Image.LANCZOS)
            tiles.append((r["key"], im))
        if not tiles:
            continue
        lab, pad = 30, 6
        sheet = Image.new("RGB", (OUT_W + pad * 2,
                                  pad + sum(t[1].height + lab + pad for t in tiles)),
                          (24, 24, 24))
        d = ImageDraw.Draw(sheet)
        y = pad
        for key, im in tiles:
            d.text((pad + 4, y + 6), key, fill=(255, 220, 60))
            y += lab
            sheet.paste(im, (pad, y))
            y += im.height + pad
        out = os.path.join(SHEET_DIR, "sheet_%02d.png" % (n // PER_SHEET + 1))
        sheet.save(out)
        print("  %s  %s" % (out, "、".join(t[0] for t in tiles)))
        made += 1
    if made:
        print("拼图 %d 张，在 userdata/title_sheets/（这些是开发期产物，不进仓库）" % made)


def main() -> int:
    ap = argparse.ArgumentParser(description="查缺标题的剧情，并按需把封面拼成对照图")
    ap.add_argument("--kind", default="event", help="event/other/main/all")
    ap.add_argument("--sheet", action="store_true", help="顺便生成封面拼图")
    args = ap.parse_args()
    items = report(args.kind)
    if args.sheet and items:
        sheets(items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
