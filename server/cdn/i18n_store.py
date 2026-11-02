"""中文等译文仓库（i18n store）——把译文从剧情数据里彻底搬出来。

为什么不在 ``char_<id>.js`` 里塞 ``Message_zh``
----------------------------------------------
``char_*.js`` 是**由清单资源再生的产物**：只要重跑一次剧本提取（比如某角色
内容更新了），文件就会被整体重写，手改过的 ``Message_zh`` 全丢。译文和剧情数据
必须分离，才能各自独立演进。

存储格式（``web/i18n/<lang>/<cid>.json``）
-----------------------------------------
.. code-block:: json

   {
     "v": 1,
     "char": 69,
     "lang": "zh-Hans",
     "updated": "2026-10-26T12:00:00",
     "source_jp_count": 809,
     "messages": { "「あのぉ……長官さん……」": "「那个……长官……」" }
   }

**用日文原文做 key**（而不是"第几段第几句"的下标）是这里的核心决定：

- 下标会因为一句台词的新增/删减整体错位，key 浮动导致译文错配，且看不出来；
- 原文做 key 时，一旦上游文本变动，旧译文自然失配、干净地落到「回退显示日文」，
  绝不会把 A 句的译文贴到 B 句上；
- 同一句台词在不同段落重复出现时天然共享译文，不用重复维护；
- diff / merge / 人工校对都直观（一眼能对照原文改）。

界面文字放在 ``web/i18n/ui.json``，按逻辑 key 索引。
"""

from __future__ import annotations

import json
import os
import sys
import time

from .config import DATA, I18N, LANGUAGES, LANG_NATIVE
from .select import load_char

STORE_VERSION = 1

# ---------------------------------------------------------------- 路径


def lang_dir(lang: str) -> str:
    return os.path.join(I18N, lang)


def unit_file(unit) -> str:
    """单元 → 译文文件名：``415`` / ``char_415`` → ``char_415.json``；
    ``adv_main_1`` / ``adv_op`` 原样 → ``adv_main_1.json``、``adv_op.json``。

    三方口径必须一致，否则 adv 剧情翻了也白翻：
      * 前端请求 ``assets/i18n/<lang>/<storyKey>.json``（i18n.js``loadStory``）
      * 控制台导入写 ``TRANS_DIR/<unit>.json``（exports.import_trans）
      * 本模块以前硬编码 ``char_{cid}.json``，adv 单元会被写成
        ``char_adv_main_1.json`` —— 前端永远请求不到。
    """
    u = str(unit or "")
    if u.startswith("char_"):
        u = u[5:]
    return f"char_{u}.json" if u.isdigit() else f"{u}.json"


def char_message_path(lang: str, cid) -> str:
    if lang == LANG_NATIVE:
        raise ValueError("源语言不需要译文文件（原文就在 char_*.js 里）")
    return os.path.join(lang_dir(lang), unit_file(cid))


def ui_path() -> str:
    return os.path.join(I18N, "ui.json")


def index_path() -> str:
    return os.path.join(I18N, "index.json")


# ---------------------------------------------------------------- 读


def load_messages(lang: str, cid) -> dict:
    """返回 ``{"messages": {...}, "meta": {...}}``；没有译文时 messages 为空 dict。"""
    empty = {"messages": {}, "meta": None}
    if lang == LANG_NATIVE:
        return empty
    p = char_message_path(lang, cid)
    if not os.path.exists(p):
        return empty
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    return {"messages": d.get("messages") or {}, "meta": d}


def load_ui(lang: str) -> dict:
    if lang == LANG_NATIVE:
        return {}
    p = ui_path()
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    return (d.get("messages") or {}).get(lang) or {}


# ---------------------------------------------------------------- 写


def _unit_key(cid):
    """``"415"`` → ``415``；adv 单元保留字符串（文件里的 ``char`` 字段同口径）。"""
    s = str(cid or "")
    return int(s) if s.isdigit() else s


def _read_store(lang: str, cid) -> dict:
    p = char_message_path(lang, cid)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"v": STORE_VERSION, "char": _unit_key(cid), "lang": lang, "updated": "",
            "source_jp_count": 0, "messages": {}}


def save_messages(lang: str, cid, messages: dict, source_jp_count: int = 0) -> str:
    """写回单个剧情单元的译文文件（与已有译文合并，重复的以本次为准）。"""
    os.makedirs(lang_dir(lang), exist_ok=True)
    store = _read_store(lang, cid)
    merged = dict(store.get("messages") or {})
    merged.update(messages)
    store.update({
        "v": STORE_VERSION,
        "char": _unit_key(cid),
        "lang": lang,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_jp_count": source_jp_count or (store.get("source_jp_count") or 0),
        "messages": merged,
    })
    p = char_message_path(lang, cid)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    return p


def prune_missing(lang: str, cid) -> int:
    """清掉本地已不在原文里的译文（上游文本变动后的收敛动作）。"""
    p = char_message_path(lang, cid)
    if not os.path.exists(p):
        return 0
    if not str(cid).isdigit():
        return 0                      # adv 单元的原文不归 load_char 管，不动它
    try:
        d = load_char(cid)
    except Exception:
        return 0
    ja = set()
    for pi, part in enumerate(d["parts"]):
        for m in part.get("methods") or []:
            if m.get("Method") == "ShowMessage":
                ja.add(m.get("Message") or "")
    store = json.load(open(p, encoding="utf-8"))
    old = store.get("messages") or {}
    keep = {k: v for k, v in old.items() if k in ja}
    removed = len(old) - len(keep)
    store["messages"] = keep
    store["source_jp_count"] = len(ja)
    store["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    return removed


# ---------------------------------------------------------------- 统计


def coverage(cid) -> dict:
    """各语言在某个角色上的译文覆盖率。"""
    try:
        d = load_char(cid)
    except Exception:
        return {"char": cid, "error": "没有该角色的剧情数据"}
    ja = set()
    for part in d["parts"]:
        for m in part.get("methods") or []:
            if m.get("Method") == "ShowMessage":
                ja.add(m.get("Message") or "")
    out = {"char": int(cid), "total": len(ja), "langs": {}}
    for lang in LANGUAGES:
        if lang == LANG_NATIVE:
            out["langs"][lang] = {"translated": len(ja), "pct": 100.0}
            continue
        msgs = load_messages(lang, cid)["messages"]
        hit = len([k for k in ja if k in msgs])
        out["langs"][lang] = {
            "translated": hit,
            "pct": round(100.0 * hit / len(ja), 1) if ja else 0.0,
            "stale": len([k for k in msgs if k not in ja]),
        }
    return out


def build_index(langs=LANGUAGES) -> dict:
    """生成 ``web/i18n/index.json``：前端据此决定是否请求各译文文件。

    除 chars 覆盖情况外，还记录 speakers（全站共用发言人译名表）的条数——
    前端只在 speakers[lang] > 0 时才请求 ``<lang>/_speakers.json``，
    没译过名字时该文件不存在，请求必 404。
    """
    out = {"v": STORE_VERSION, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "langs": list(langs), "chars": {}, "speakers": {}}
    for lang in langs:
        if lang == LANG_NATIVE:
            continue
        spk_path = os.path.join(lang_dir(lang), "_speakers.json")
        if os.path.exists(spk_path):
            try:
                spk = json.load(open(spk_path, encoding="utf-8")).get("messages") or {}
                out["speakers"][lang] = len(spk)
            except Exception:
                pass
    for fn in sorted(os.listdir(DATA)):
        if not fn.endswith(".js"):
            continue
        # 名单必须和前端「哪部剧情可能有译文」对齐：
        #   char_<id>.js → key "<id>"；adv_<kind>_<no>.js → key "adv_<kind>_<no>"
        # （前端 i18n.js ``unitTranslated`` 就是 char_ 去前缀、其余原样）。
        # 以前只扫 char_*.js，于是 adv 剧情即使翻了也不会出现在清单里，
        # 前端永远请求不到 —— 和控制台导入写出的 chars["adv_main_1"] 口径不一致。
        unit = fn[:-3]
        if unit.startswith("char_"):
            cid = unit[5:]
            if not cid.isdigit():
                continue
            ikey = int(cid)
        elif unit.startswith("adv_"):
            cid = unit
            ikey = unit
        else:
            continue
        entry = {}
        for lang in langs:
            if lang == LANG_NATIVE:
                continue
            msgs = load_messages(lang, cid)["messages"]
            if msgs:
                entry[lang] = len(msgs)
        if entry:
            out["chars"][ikey] = entry
    os.makedirs(I18N, exist_ok=True)
    with open(index_path(), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


# ---------------------------------------------------------------- 迁移


def migrate_from_message_zh(dry: bool = False) -> int:
    """把历史遗留的 ``Message_zh``（写在 char_*.js 里）搬进 i18n 仓库。

    只读体内的译文、生成对应语言的 json 文件；``dry=True`` 时什么都不写。
    **不会**删除源文件里的 ``Message_zh``——那是人工操作，留给核对后再做。
    """
    total = 0
    for fn in sorted(os.listdir(DATA)):
        if not fn.startswith("char_") or not fn.endswith(".js"):
            continue
        cid = int(fn[5:-3])
        try:
            d = load_char(cid)
        except Exception:
            continue
        pairs = {}
        ja = set()
        for part in d["parts"]:
            for m in part.get("methods") or []:
                if m.get("Method") == "ShowMessage":
                    src = m.get("Message") or ""
                    ja.add(src)
                    zh = m.get("Message_zh")
                    if zh:
                        pairs[src] = zh
        if not pairs:
            continue
        total += len(pairs)
        print(f"  角色 {cid}：{len(pairs)}/{len(ja)} 条遗留译文")
        if not dry:
            save_messages("zh-Hans", cid, pairs, source_jp_count=len(ja))
    print(f"合计迁移 {total} 条" + ("（dry-run，未写入）" if dry else ""))
    return total


def import_backup(path: str, cid, lang: str = "zh-Hans") -> int:
    """导入 ``inject_char_story.py --dump`` 产出的备份 json（jp/zh 对位锚点）。

    这类文件里每条都带 ``jp`` 原文，正好是新的 i18n key。
    """
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    pairs = {}
    for e in d.get("entries") or []:
        jp, zh = e.get("jp"), e.get("zh")
        if jp and zh:
            pairs[jp] = zh
    if not pairs:
        print("  备份里没有可用条目")
        return 0
    try:
        c = load_char(cid)
        total = sum(1 for p in c["parts"] for m in p.get("methods") or []
                    if m.get("Method") == "ShowMessage")
    except Exception:
        total = 0
    save_messages(lang, cid, pairs, source_jp_count=total)
    print(f"  导入 {len(pairs)} 条 -> {char_message_path(lang, cid)}")
    return len(pairs)
