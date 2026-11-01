"""导出：角色 CG 打包、剧情视频落盘、译文导入 / 导出。

为什么 CG 用后端打包
--------------------
浏览器里没有 zip 能力（不想为了打包再引第三方库），而"选 10 个角色、几百张
CG"通过 HTTP 逐张下载再存盘对用户是灾难。后端直接读本地文件打包最省事。

视频为什么不在这里渲染
----------------------
``web/assets/js/recorder.js`` 用 canvas 逐帧重绘 + MediaRecorder 录制（浏览器
本身就有编解码器），录完把 blob POST 上来，这里只负责落盘和可选的 ffmpeg 转码。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile

from . import media, paths

TRANS_DIR = os.path.join(paths.WEB, "assets", "i18n", "zh-Hans")

#: 全局发言人译名表。以 _ 开头，所以译文统计不会把它当成"一部剧情"。
#: 为什么不塞进每部剧情的译文文件：ShowMessageWindow.Name 是全站共用的口语
#: 称呼（同一个角色在几百部剧情里都叫同一个名字），按剧情存等于把同一件事
#: 翻几百遍，而且各部之间译名还会打架。
SPK_FILE = "_speakers.json"

TRANS_HEADER = """# ===== ESCAH 离线阅读器 · 剧情译文文件 =====
# 剧情标识 : {unit}
# 剧情名称 : {title}
# 段落/句数: {parts} 段 / {lines} 句
# 导出时间 : {at}
#
# ===== 翻译要求（给 AI，请逐条照做）=====
#
# 1. **只输出简体中文译文。**
#    · 不要复制日文原文；不要输出「日文 / 中文」对照；不要复述我给的内容。
#    · 不要写解释、前言、总结、术语说明。
#    · 不要用 markdown 代码块（```），直接给纯文本。
# 2. 保留每个块的第一行「### <编号>」，编号**一个字都不要改**；
#    不要合并、拆分、新增或删除块。
# 3. 每句只写 zh: 行，**不要写 ja: 行** —— 原文我这边已经有了，
#    重复输出只会浪费 token：
#
#      ### 001-p01-m003 【トキサダ】 (2行)
#      zh: 「所以说……你们是在活动警备时
#      zh: 遇到幻魔的吧」
#
# 4. 块头里的 (N行) 是原文行数：译文最多写 N 行 zh:，少了没关系
#    （多了会撑破游戏文本框）。
# 5. 【】里是说话人，没有就是旁白。按角色性格决定语气、自称与口癖。
# 6. 拟声词、语气词、感叹词也要译成中文，**不要留日文假名**；
#    没有对应说法就用最接近的中文拟声词。
# 7. 术语与人名：**先读词汇表目录**
#      {glossary}
#    （只读，不要修改里面的文件。按下面的两档规则用，别一律硬套）
#
#    · **硬性 —— 必须照用**：角色名 / NPC 名 / 组织势力 / 地名 /
#      技能·装备·道具名 / 专有称号。这些是设定名词，换了就不是同一个人或东西。
#      对应文件：characters.yaml（含 characters_generated.yaml）、names.yaml。
#    · **软性 —— 优先参考，可依上下文调整**：普通词汇、惯用语、四字熟语。
#      词汇表给的是"最常用译法"；如果这里它是比喻、双关、固定搭配，
#      或者硬套会和前后语气冲突，就按上下文意译，**不必硬套**。
#      对应文件：terms.yaml / phrases.yaml / phrases_manual.yaml / high_freq.yaml。
#
#    两种都要守住一条：**同一个词在同一语境下全篇只用一个译法**，
#    不要这里「魔女」那里「女巫」。词汇表里没有的专有名词，音译或意译都行，
#    但一旦定了就全文统一。
# 8. 不需要翻译的块直接跳过（不写 zh: 行），导入时会自动忽略。
# 9. 输出 UTF-8，一次给完整文件内容，不要用「……（以下省略）」跳过中间块。
#
# ===== 文件正文（从下一行开始）=====
#
"""


# ---------------------------------------------------------------- 基础
def _load_js(p: str):
    with open(p, encoding="utf-8-sig") as f:
        s = f.read()
    return json.loads(s[s.find("=") + 1:].strip().rstrip(";"))


def unit_data(unit: str) -> dict | None:
    p = os.path.join(paths.DATA, f"{unit}.js")
    if not os.path.exists(p):
        return None
    try:
        return _load_js(p)
    except Exception:                                            # noqa: BLE001
        return None


#: 非角色剧情的分类中文名（与 web/assets/js/common.js 的 ADV_LABEL 一致）。
#: 以前这里直接吐内部 kind（"main 第 1 话"），译稿开头的「剧情名称」读起来像乱码。
ADV_LABEL = {"main": "主线", "event": "活动", "other": "其他",
             "tutorial": "新手教程", "op": "开场动画"}


def unit_title(unit: str) -> str:
    """译文文件头里那句「剧情名称」。

    **不再拼「角色 415」**：角色一共 384 个、ID 却到 415，序号化写法会让
    打开译稿的人以为"角色缺了 31 个"；名字取不到才退回「ID 415」。
    """
    try:
        if unit.startswith("char_"):
            cid = unit[5:]
            names = _load_js(os.path.join(paths.DATA, "names.js"))
            return names.get(cid) or f"ID {cid}"
        advs = _load_js(os.path.join(paths.DATA, "advindex.js"))
        for a in advs:
            if a.get("key") == unit and not isinstance(a.get("part"), int):
                label = ADV_LABEL.get(a.get("kind"), "剧情")
                no = a.get("no")
                return f"{label} 第 {no} 话" if no else label
    except Exception:                                            # noqa: BLE001
        pass
    return unit


def _unique_path(base: str, ext: str = ".zip") -> str:
    os.makedirs(paths.EXPORT, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    p = os.path.join(paths.EXPORT, f"{base}_{stamp}{ext}")
    i = 2
    while os.path.exists(p):
        p = os.path.join(paths.EXPORT, f"{base}_{stamp}_{i}{ext}")
        i += 1
    return p


# ---------------------------------------------------------------- CG 导出
#: cgmap 里的负数 id 是**剧情 CG 的分类桶**（不是角色）。导出时要给它们人类可读的名字，
#: 否则 zip 里会出现「角色-1」这种文件夹。
CG_CAT = {-1: "主线剧情CG", -2: "活动剧情CG", -3: "其他剧情CG"}


def export_cg(job, chars: list[int] | None = None,
              groups: list[list[int]] | None = None,
              whole: bool = False) -> dict:
    """导出 CG 为 zip（角色 CG 和剧情 CG 都支持）。

    ``chars``  角色 id 列表（None = 全部有 CG 的角色）
    ``groups`` 精确到事件组：[charId, groupIndex] 的列表（剧情 CG 走这条，charId 是 -1/-2/-3）
    ``whole``  True 表示忽略筛选导出全部
    """
    cgmap = _load_js(os.path.join(paths.DATA, "cgmap.js"))
    imgmap = media.load_imgmap()
    base = media.load_overlay("base")
    x2 = media.load_overlay("x2")

    picked: list[tuple[int, str, list[str], str]] = []
    for gi, g in enumerate(cgmap):
        cid = g.get("c")
        if groups and not whole:
            if [cid, gi] not in groups:
                continue
        elif chars and not whole and cid not in chars:
            continue
        picked.append((cid, g.get("k") or f"group{gi}", g.get("f") or [],
                       g.get("u") or ""))

    total = sum(len(f) for _, _, f, _u in picked)
    if job is not None:
        job.set_total(total)
        job.set_items(total)
    if not total:
        return {"path": "", "files": 0, "note": "没有匹配到 CG"}

    # zip 名字用角色名，别再用 cg_20261029_xxx 这种看不出是谁的
    from . import names as nameutil

    def owner(cid):
        """负数 id → 「活动剧情CG」；正数 → 角色名。"""
        return CG_CAT.get(cid) or nameutil.of(cid)

    ids = sorted({cid for cid, _k, _f, _u in picked})
    if len(ids) == 1:
        # 剧情桶本身就叫「活动剧情CG」，别再拼 _CG
        tag = owner(ids[0]) + ("" if ids[0] < 0 else "_CG")
    elif len(ids) <= 3:
        tag = "、".join(owner(c) for c in ids) + "_CG"
    else:
        tag = f"{owner(ids[0])}等{len(ids)}个_CG"
    out = _unique_path(nameutil.safe(tag, 70))

    written = missing = 0
    names = _safe_names(cgmap)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as z:
        for gi, (cid, gname, files, unit) in enumerate(picked):
            if job is not None and job.cancelled:
                break
            if cid in CG_CAT:
                # 剧情 CG：按「活动剧情CG/活动 第16话/组名」分层，别出现「角色-2」
                folder = f"{CG_CAT[cid]}/{_san(unit_title(unit) if unit else gname)}" \
                         f"/{_san(gname)}"
            else:
                base_nm = nameutil.safe(names.get(cid) or nameutil.of(cid))
                # 只有真的两个角色同名时才退回 id 区分（正常情况直接用名字）
                if len(ids) > 1 and \
                        sum(1 for c in ids if nameutil.of(c) == nameutil.of(cid)) > 1:
                    base_nm += f"_{cid}"
                folder = f"{base_nm}/{_san(gname)}"
            for f in files:
                if job is not None and job.cancelled:
                    break
                key = os.path.splitext(f)[0]
                rel = ((x2.get("cg") or {}).get(key)
                       or (base.get("cg") or {}).get(key)
                       or (imgmap.get("cg") or {}).get(key))
                src = paths.web_path(rel) if rel else None
                if not src or not os.path.exists(src):
                    missing += 1
                    if job is not None:
                        job.progress(add=1)
                    continue
                z.write(src, f"{folder}/{os.path.basename(src)}")
                written += 1
                if job is not None:
                    job.progress(add=1, message=f"打包 {written}/{total} 张")
    if job is not None:
        job.finish(f"已导出 {written} 张 CG")
    return {"path": paths.rel(out) if out.startswith(paths.WEB) else out,
            "abs": out, "files": written, "missing": missing}


def cg_list() -> dict:
    """CG 清单（给导出页做选择）：{chars:[{id,name,groups:[{i,k,n}],total}], total}"""
    try:
        cgmap = _load_js(os.path.join(paths.DATA, "cgmap.js"))
    except Exception:                                            # noqa: BLE001
        return {"chars": [], "total": 0}
    names = _safe_names(cgmap)
    by: dict[int, dict] = {}
    for gi, g in enumerate(cgmap):
        cid = int(g.get("c") or 0)
        item = by.setdefault(cid, {"id": cid, "name": names.get(cid, str(cid)),
                                   "groups": [], "total": 0})
        n = len(g.get("f") or [])
        item["groups"].append({"i": gi, "k": g.get("k") or "", "n": n})
        item["total"] += n
    chars = sorted(by.values(), key=lambda x: -x["total"])
    return {"chars": chars, "total": sum(c["total"] for c in chars)}


def _safe_names(cgmap) -> dict:
    """``{cid: 角色名}``。命名规则统一由 :mod:`server.names` 决定（中文优先）。"""
    from . import names as nameutil
    out = {}
    for g in cgmap:
        cid = g.get("c")
        if cid is None:
            continue
        out[cid] = nameutil.of(cid)
    return out


def _san(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s)).strip()
    return (s or "x")[:60]


# ---------------------------------------------------------------- 视频落盘
def save_video(blob_path: str, name: str = "", convert_mp4: bool = False) -> dict:
    """把浏览器上传的录制文件搬到 export 目录，可选 ffmpeg 转 mp4。"""
    os.makedirs(paths.EXPORT, exist_ok=True)
    ext = os.path.splitext(blob_path)[1] or ".webm"
    dst = _unique_path(_san(name or "story_video"), ext)
    shutil.move(blob_path, dst)
    info = {"path": dst, "name": os.path.basename(dst),
            "bytes": os.path.getsize(dst), "mp4": ""}
    if convert_mp4:
        ff = shutil.which("ffmpeg")
        if ff:
            mp4 = os.path.splitext(dst)[0] + ".mp4"
            import subprocess
            try:
                subprocess.run([ff, "-y", "-i", dst, "-c:v", "libx264",
                                "-preset", "veryfast", "-crf", "20",
                                "-c:a", "aac", "-b:a", "160k", mp4],
                               capture_output=True, timeout=3600, check=False)
                if os.path.exists(mp4):
                    info["mp4"] = mp4
            except Exception:                                # noqa: BLE001
                pass
    return info


def list_exports() -> list[dict]:
    out = []
    if not os.path.isdir(paths.EXPORT):
        return out
    for name in sorted(os.listdir(paths.EXPORT), reverse=True)[:100]:
        p = os.path.join(paths.EXPORT, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": name, "bytes": st.st_size,
                    "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


# ---------------------------------------------------------------- 译文导入导出
# 块式双语对照：一个 ### 块是一句台词
#
#   ### 001-p01-m003 【トキサダ】 (3行)
#   ja: 原文第一行
#   ja: 原文第二行
#   zh: 译文第一行
#
# 为什么不再用 "原文 ||| 译文"
#   1. 多行台词只能把换行转义成 \n，而 AI 常常把它还原成真换行 → 一句变两句
#   2. 拿原文全文当主键，AI 改一个标点就永久失配，而且是**静默**失配
#   3. 没有说话人、没有位置：AI 不知道谁在说，出错了也没法定位到某一句
# 块头编号 = 剧情号-part 序号-句序号，是稳定的锚点；万一编号对不上（资源包
# 重导导致索引漂移），还能用 ja: 行兜底匹配。两条路互为备份，不至于全丢。
_BLOCK = re.compile(r"^#{2,4}\s+(.*?)\s*$")
# 发言人译名块的块头：### spk:トキサダ / ### speaker トキサダ / ### 发言人：トキサダ
_SPK_HEAD = re.compile(r"^(?:spk|speaker|发言人|角色名)\s*[:：]?\s*(.+)$", re.I)
_PFX_JA = re.compile(r"^(?:ja|jp|jpn|原文|日文)\s*[:：]\s?")
_PFX_ZH = re.compile(r"^(?:zh|zho|chs|cn|译文|中文|汉语)\s*[:：]\s?")
_MD = re.compile(r"^[>\-*•][ \t]+")                     # AI 爱加的列表/引用符号
_ORD = re.compile(r"^[ \t]*(?:\d{1,3}|[①-⑳])[ \t]*[.、)）:：][ \t]+")   # AI 爱加的序号
_BOLD = re.compile(r"^\*\*(.+?)\*\*$")
# 下面两处刻意只写 [ \t] 而不是 \s：\s 在 unicode 下会吃掉 \u3000，
# 而全角空格是日文原文的真实缩进（换行后顶格的「　」），把它剥了等于改了原文。
_WS = " \t\r\n"



def _tidy(s: str) -> str:
    s = _ORD.sub("", s)
    m = _BOLD.match(s)
    if m:
        s = m.group(1)
    return s.strip(_WS)


def _split_line(raw: str) -> tuple[str, str]:
    """→ (kind, body)。kind 取 'ja' / 'zh' / ''，'' 表示没有前缀的裸行。"""
    s = _MD.sub("", raw.rstrip("\r"))
    m = _PFX_JA.match(s)
    if m:
        return "ja", _tidy(s[m.end():])
    m = _PFX_ZH.match(s)
    if m:
        return "zh", _tidy(s[m.end():])
    return "", _tidy(s)


def _parse_head(s: str) -> dict:
    """`001-p01-m003 【トキサダ】 (3行)` → {id, name, n}"""
    s = s.replace("**", "").replace("`", "")
    d = {"id": "", "name": "", "n": 0}
    m = re.search(r"【([^】]*)】", s)
    if m:
        d["name"] = m.group(1).strip()
    m = re.search(r"[（(](\d+)\s*行[)）]?", s)
    if m:
        try:
            d["n"] = int(m.group(1))
        except ValueError:
            pass
    for tok in s.split():
        if re.fullmatch(r"[\w.\-]*\d+-p\d+-m\d+", tok):
            d["id"] = tok
            break
    if not d["id"]:
        for tok in s.split():
            if re.fullmatch(r"[\w.\-]{3,}", tok):
                d["id"] = tok
                break
    return d


def _parse_blocks(text: str) -> list:
    """→ blocks（按 ### 切好的块列表）。块头之前的游离行一律丢弃。

    整段 Optimization：这里只做「切块 + 打标签」，真正的 ja/zh 分流要等拿到
    原文行数之后才能定，所以先攒在 raw 里。
    """
    blocks = []
    cur = None
    for raw in text.replace("\ufeff", "").splitlines():
        line = raw.rstrip("\r")
        m = _BLOCK.match(line)
        if m:
            head = m.group(1)
            sm = _SPK_HEAD.match(head)
            if sm:                              # ### spk:トキサダ → 发言人译名块
                cur = {"kind": "spk", "spk": sm.group(1).strip(),
                       "id": "", "name": "", "n": 0,
                       "ja": [], "zh": [], "raw": []}
            else:
                cur = _parse_head(head)
                cur["kind"] = "msg"
                cur["ja"], cur["zh"], cur["raw"] = [], [], []
            blocks.append(cur)
            continue
        if line.lstrip().startswith("#"):
            # 块之间的注释（比如文件末尾「发言人译名」那节的分隔说明）
            # 必须直接丢掉。否则它会被当成上一个块的裸行，灌进那句的译文里。
            continue
        if not line.strip(_WS):
            continue
        if cur is None:
            continue                        # 第一个 ### 之前的游离行，丢弃
        kind, body = _split_line(line)
        if kind or body:
            cur["raw"].append((kind, body))
    return blocks


def _classify(b: dict, n_hint: int, has_ja: bool) -> None:
    """把裸行分到 ja / zh。

    AI 三种常见写法都要接住：
      · 只给译文（全裸行）        → 全部当译文
      · 原文译文都带前缀          → 按前缀走
      · 原文多行但续行漏了 ja: 前缀 → 靠「ja 还没收满原文行数」补进去
    所以裸行怎么判，取决于 ja 是否已经收够原文声明的行数。
    """
    for kind, body in b["raw"]:
        if kind == "ja":
            b["ja"].append(body)
        elif kind == "zh":
            b["zh"].append(body)
        elif has_ja and n_hint and len(b["ja"]) < n_hint:
            b["ja"].append(body)
        else:
            b["zh"].append(body)


def _unit_no(unit: str, d: dict) -> str:
    n = d.get("no")
    if n is None:                                       # char_* 没有 no，取名字里的数字
        m = re.search(r"(\d+)", unit)
        n = m.group(1) if m else 0
    try:
        return f"{int(n):03d}"
    except (TypeError, ValueError):
        return "000"


def unit_lines(unit: str) -> list[dict]:
    """剧情里所有不重复的 ShowMessage → [{id, name, ja, n}]

    剧本缺失时返回 []——资源包没下载也不该挡住导入。
    编号只依赖遍历顺序（part 序号 + 该 part 内第几句），重复导出结果一致。
    说话人取它前面最近那个 ShowMessageWindow 的 Name，旁白时为空。
    """
    d = unit_data(unit)
    if not d:
        return []
    no = _unit_no(unit, d)
    rows, seen = [], set()
    for pi, p in enumerate(d.get("parts") or []):
        name, n = "", 0
        for m in p.get("methods") or []:
            mt = m.get("Method")
            if mt == "ShowMessageWindow":
                name = (m.get("Name") or "").strip()
                continue
            if mt != "ShowMessage":
                continue
            ja = (m.get("Message") or "").replace("\r", "")
            if not ja.strip() or ja in seen:
                continue
            seen.add(ja)
            n += 1
            rows.append({"id": f"{no}-p{pi + 1:02d}-m{n:03d}", "name": name,
                         "ja": ja, "n": ja.count("\n") + 1})
    return rows


def load_speakers() -> dict:
    """全局发言人译名 ``{日文名: 中文}``。"""
    d = paths.load_json(os.path.join(TRANS_DIR, SPK_FILE), {}) or {}
    return {k: v for k, v in (d.get("messages") or {}).items() if k and v}


def save_speakers(msgs: dict) -> None:
    os.makedirs(TRANS_DIR, exist_ok=True)
    paths.save_json(os.path.join(TRANS_DIR, SPK_FILE),
                    {"lang": "zh-Hans",
                     "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "messages": msgs})


def unit_speakers(unit: str) -> list[str]:
    """这部剧情里出现过的说话人，按首次出现顺序去重。

    取值来自 ShowMessageWindow.Name —— 口语称呼（トキサダ / ハルカ），跟
    names.js 里的全长名（神騎プラチネル）对不上，自动映射只有 2.3% 命中，
    所以只能靠人/AI 翻，这里负责把"要翻哪些名字"列全。
    """
    d = unit_data(unit)
    if not d:
        return []
    out, seen = [], set()
    for p in d.get("parts") or []:
        for m in p.get("methods") or []:
            if m.get("Method") != "ShowMessageWindow":
                continue
            nm = (m.get("Name") or "").strip()
            if nm and nm not in seen:
                seen.add(nm)
                out.append(nm)
    return out


def _glossary_note() -> str:
    """词汇表目录的**绝对路径**（写进提示词，让 AI 自己去读）。

    为什么给绝对路径而不是"glossary 目录"：AI 在用户自己的电脑上没有工作目录
    概念，只有完整路径它才能明确去读哪。
    """
    p = os.path.abspath(paths.GLOSSARY)
    try:
        names = sorted(n for n in os.listdir(p) if not n.startswith("."))
    except OSError:
        return f"{p}（本机暂未找到该目录，请保持全篇译名一致）"
    if not names:
        return p
    head = "、".join(names[:10])
    more = f" 等 {len(names)} 个文件" if len(names) > 10 else ""
    return f"{p}（含：{head}{more}）"


def export_trans(unit: str) -> str:
    """生成某部剧情的块式双语对照 txt（可交给 AI 翻译后再导回）。"""
    d = unit_data(unit)
    if not d:
        raise FileNotFoundError(f"没有剧情数据：data/{unit}.js（该剧情的资源包还没下载）")
    rows = unit_lines(unit)
    cur = {}
    fp = os.path.join(TRANS_DIR, f"{unit}.json")
    if os.path.exists(fp):
        cur = (paths.load_json(fp, {}) or {}).get("messages", {}) or {}
    buf = [TRANS_HEADER.format(unit=unit, title=unit_title(unit),
                               parts=len(d.get("parts") or []),
                               lines=len(rows),
                               glossary=_glossary_note(),
                               at=time.strftime("%Y-%m-%d %H:%M:%S"))]
    for r in rows:
        who = f" 【{r['name']}】" if r["name"] else ""
        buf.append(f"### {r['id']}{who} ({r['n']}行)")
        for ln in r["ja"].split("\n"):
            buf.append(("ja: " + ln).rstrip(_WS))
        zh = (cur.get(r["ja"]) or "").strip()
        for ln in zh.split("\n"):
            buf.append(("zh: " + ln).rstrip(_WS))
        buf.append("")
    spk = unit_speakers(unit)
    if spk:
        have = load_speakers()
        buf += ["# ===== 发言人译名（对话框上方那个名字）=====",
                "# 只给日文名补 zh: 就行；留空也完全可以，那就原样显示日文。",
                "# 这张表全站共用：翻过一次，所有剧情里的同一个名字都跟着变。"]
        for nm in spk:
            buf += ["", f"### spk:{nm}",
                    ("zh: " + have[nm]).rstrip(_WS) if have.get(nm) else "zh:"]
    while buf and not buf[-1]:
        buf.pop()
    return "\n".join(buf) + "\n"


def import_trans(unit: str, text: str, mode: str = "merge") -> dict:
    """解析并合并译文。

    只吃 ### 块式（ja:/zh: 成对）。
    早期的「原文 ||| 译文」单行式已移除 —— 那种写法把原文全文当主键，
    AI 改一个标点就静默失配，且多行台词要靠转义换行，实际用起来问题更多。
    → {added, updated, skipped, bad, lineOver, mismatch, total, path}
      lineOver  译文行数多过原文的块编号（会撑破游戏文本框，建议改）
      mismatch  原文对不上的块编号（可能是串行了，已按编号落位）
    """
    rows = unit_lines(unit)
    by_id = {r["id"]: r for r in rows}
    by_ja = {r["ja"]: r for r in rows}
    # 归一化索引：容忍 AI 把它揉成一坨的空白差异，避免逐句再算一遍
    by_ja_ns = {re.sub(r"\s+", "", k): v for k, v in by_ja.items()}

    fp = os.path.join(TRANS_DIR, f"{unit}.json")
    cur = {}
    if os.path.exists(fp) and mode != "replace":
        cur = dict((paths.load_json(fp, {}) or {}).get("messages", {}) or {})

    blocks = _parse_blocks(text or "")
    has_ja = any(k == "ja" for b in blocks if b.get("kind") == "msg" for k, _ in b["raw"])
    # 发言人表是全站共用的，所以永远 merge —— 「覆盖整部」也不许清它，
    # 否则导入 A 剧情就能把 B 剧情的角色名全抹掉。
    spk_cur = load_speakers()
    spk_add = spk_upd = 0
    added = updated = skipped = bad = 0
    over: list[str] = []
    mism: list[str] = []

    def put(row, zh: str, ja_txt: str = "", tag: str = "") -> None:
        """落一句。row 是剧本里的句子（带 id / 行数），找不到就退回用原文文本当 key。"""
        nonlocal added, updated, skipped
        ja = row["ja"] if row is not None else ja_txt
        if not ja or not zh.strip():
            skipped += 1
            return
        n = row["n"] if row is not None else 0
        if n and len(zh.split("\n")) > n:
            over.append((row or {}).get("id") or tag or "(无编号)")
        if cur.get(ja) == zh:
            skipped += 1
            return
        if ja in cur:
            updated += 1
        else:
            added += 1
        cur[ja] = zh

    for b in blocks:
        if b.get("kind") == "spk":
            # 发言人块：裸行和 zh: 行都算译名（AI 写不写前缀都认）
            nm = b["spk"]
            zh_spk = "\n".join(t for k, t in b["raw"] if k in ("", "zh")).strip()
            if nm and zh_spk and spk_cur.get(nm) != zh_spk:
                if nm in spk_cur:
                    spk_upd += 1
                else:
                    spk_add += 1
                spk_cur[nm] = zh_spk
            continue
        tag = b["id"] or "(无编号)"
        hit_id = by_id.get(b["id"]) if b["id"] else None
        _classify(b, (hit_id or {}).get("n") or b.get("n") or 0, has_ja)
        ja_txt = "\n".join(b["ja"]) if b["ja"] else ""
        zh_txt = "\n".join(b["zh"])
        hit_ja = (by_ja.get(ja_txt) or by_ja_ns.get(re.sub(r"\s+", "", ja_txt))) \
            if ja_txt else None
        if hit_id is not None:
            # 编号命中就按编号落位。ja: 行却对不上 → 这块可能被人改过，
            # 报告给用户，但不丢：总比整句静默消失强
            if ja_txt and hit_ja is None:
                mism.append(tag)
            put(hit_id, zh_txt, ja_txt, tag)
        elif hit_ja is not None:
            put(hit_ja, zh_txt, ja_txt, tag)
        elif ja_txt:
            # 编号和原文都对不上：要么是资源包没下/重导过，要么导入错了剧情。
            # 仍然按原文文本存下来（key 就是原文，游戏侧照样能命中），但要报
            put(None, zh_txt, ja_txt, tag)
            mism.append(tag)
        else:
            bad += 1

    os.makedirs(TRANS_DIR, exist_ok=True)
    paths.save_json(fp, {"unit": unit, "lang": "zh-Hans",
                         "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "messages": cur})
    if spk_add or spk_upd:
        save_speakers(spk_cur)
    _update_trans_index(unit, len(cur))
    return {"added": added, "updated": updated, "skipped": skipped,
            "bad": bad, "lineOver": sorted(set(over))[:20],
            "mismatch": sorted(set(mism))[:20],
            "total": len(cur), "path": paths.rel(fp),
            "speakers": {"added": spk_add, "updated": spk_upd,
                         "total": len(spk_cur)}}


def import_trans_multi(items: list[dict], mode: str = "merge") -> dict:
    """批量导入多部剧情的译文（与 export_trans_batch 对称，同步版）。

    ``items`` 是 [{unit, text}]。每部独立调用 import_trans，
    单部出错只记进 errors，不拖垮整批。
    """
    return _run_import(items, mode, None)


def import_trans_zip(data: bytes, mode: str = "merge") -> dict:
    """从 zip 压缩包里挑出所有 .txt，逐部导入（同步版）。

    剧情标识优先取文件头「# 剧情标识 : xxx」，取不到再用文件名（char_415.txt）。
    遍历是递归的（子目录里的 txt 也算）。与批量导出产物（译文.zip）直接对上。
    """
    return _run_import(_extract_zip_items(data), mode, None)


def import_trans_multi_job(j, items: list[dict], mode: str = "merge") -> dict:
    """批量导入（后台任务版）：每导入一部就往进度条推一下。"""
    return _run_import(items, mode, j)


def import_trans_zip_job(j, data: bytes, mode: str = "merge") -> dict:
    """zip 导入（后台任务版）。先解包再逐部推进度。"""
    return _run_import(_extract_zip_items(data), mode, j)


def _extract_zip_items(data: bytes) -> list[dict]:
    """从 zip 字节里抽出所有 .txt（去 BOM、自动认剧情标识、去重）。"""
    import io
    if not data or data[:2] != b"PK":
        raise ValueError("不是合法的 zip 文件（应以 PK 开头）")
    items: list[dict] = []
    seen: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            if not name.lower().endswith(".txt"):
                continue
            try:
                raw = z.read(name)
            except Exception:                                    # noqa: BLE001
                continue
            if raw[:3] == b"\xef\xbb\xbf":                       # 去 BOM
                raw = raw[3:]
            text = raw.decode("utf-8", "replace")
            unit = _unit_of_text(text) or os.path.basename(name)[:-4]
            if not unit or unit in seen:
                continue
            seen.add(unit)
            items.append({"unit": unit, "text": text})
    if not items:
        raise FileNotFoundError("压缩包里没有找到任何 .txt 译文文件")
    return items


def _run_import(items: list[dict], mode: str, j=None) -> dict:
    """逐部导入的核心循环。``j`` 非空时（后台任务）实时推进度。"""
    out = {"units": [], "added": 0, "updated": 0, "skipped": 0, "bad": 0,
           "ok": 0, "fail": 0}
    todo = []
    for it in items or []:
        unit = (it.get("unit") or "").strip()
        text = it.get("text") or ""
        if not unit or not text:
            out["units"].append({"unit": unit or "(空)", "ok": False,
                                 "error": "缺少 unit 或译文内容"})
            out["fail"] += 1
        else:
            todo.append((unit, text))
    n = len(todo)
    if j:
        j.set_total(n or 1); j.set_items(n)        # 进度条按部数走
        if n == 0:
            j.progress(done=1, message="没有可导入的内容")
    for i, (unit, text) in enumerate(todo, 1):
        try:
            r = import_trans(unit, text, mode)
            out["added"] += r.get("added", 0)
            out["updated"] += r.get("updated", 0)
            out["skipped"] += r.get("skipped", 0)
            out["bad"] += r.get("bad", 0)
            out["units"].append({"unit": unit, "ok": True,
                                 "added": r.get("added", 0),
                                 "updated": r.get("updated", 0),
                                 "skipped": r.get("skipped", 0),
                                 "bad": r.get("bad", 0),
                                 "total": r.get("total", 0)})
            out["ok"] += 1
            if j:
                j.progress(add=1, message="已导入 %d/%d：%s" % (i, n, unit))
        except Exception as e:                                  # noqa: BLE001
            out["units"].append({"unit": unit, "ok": False,
                                 "error": str(e)[:200]})
            out["fail"] += 1
            if j:
                j.progress(add=1, message="导入失败 %d/%d：%s" % (i, n, unit))
    if j:
        j.detail["result"] = out
    return out


def _unit_of_text(text: str) -> str:
    """从译文 txt 头里取剧情标识；取不到返回空串。"""
    m = re.search(r"#\s*剧情标识\s*:\s*(\S+)", text)
    return m.group(1).strip() if m else ""


def _update_trans_index(unit: str, n: int) -> None:
    idx_path = os.path.join(paths.WEB, "assets", "i18n", "index.json")
    idx = paths.load_json(idx_path, {}) or {}
    idx.setdefault("chars", {})
    cid = unit[5:] if unit.startswith("char_") else unit
    idx["chars"].setdefault(cid, {})
    idx["chars"][cid]["zh-Hans"] = n
    paths.save_json(idx_path, idx)


# ---------------------------------------------------------------- 译文批量导出
def _trans_units(scope: str = "translated") -> list[str]:
    """批量导出要包含哪些 unit。

    ``translated``：只列**已有译文文件**的（备份 / 合并用）
    ``installed`` ：所有已安装剧情（把原文一次性交给 AI 从头翻）
    """
    out: list[str] = []
    if scope == "installed":
        if os.path.isdir(paths.DATA):
            for name in os.listdir(paths.DATA):
                if name.endswith(".js"):
                    u = name[:-3]
                    if u.startswith(("char_", "adv_")):
                        out.append(u)
    elif os.path.isdir(TRANS_DIR):
        for name in os.listdir(TRANS_DIR):
            if not name.endswith(".json") or name.startswith("_"):
                continue
            u = name[:-5]
            if u.startswith(("char_", "adv_")):
                out.append(u)
    return sorted(out)


def export_trans_batch(job=None, scope: str = "translated",
                       units: list[str] | None = None) -> dict:
    """把多部剧情的译文 txt 打成一个 zip（与「批量导入」对称）。

    单部导出在播放器控制条上（``export_trans``）；这里补的是批量：
    备份，或一次性交给 AI 批量翻译。

    ``units`` 给了就**只导这些剧情**（前端用与视频/CG 导出同一套选择组件），
    没给才按 ``scope`` 推（``translated`` 已有译文 / ``installed`` 全部已安装）。
    """
    if units:
        want = [u for u in dict.fromkeys(units) if u]
        units = [u for u in want
                 if os.path.exists(os.path.join(paths.DATA, f"{u}.js"))]
        if not units:
            raise FileNotFoundError("所选剧情都没有剧本数据（可能还没安装）")
    else:
        units = _trans_units(scope)
        if not units:
            raise FileNotFoundError(
                "还没有任何译文（先在播放器里逐部翻译，或用「批量导入」）"
                if scope != "installed" else "还没有安装任何剧情")
    # 注意 _unique_path 自己会拼时间戳，这里只给个名字前缀，
    # 否则会变成「译文_20261031_0046_20261031_004651.zip」这种双时间戳。
    dst = _unique_path("译文")
    if job is not None:
        job.set_total(len(units))
    n = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for i, u in enumerate(units, 1):
            if job is not None and job.cancelled:
                break
            try:
                txt = export_trans(u)
            except Exception:                                  # noqa: BLE001
                txt = ""
            if txt:
                z.writestr(f"{u}_{_san(unit_title(u)) or u}.txt", txt)
                n += 1
            if job is not None:
                job.progress(add=1, message=f"打包 {i}/{len(units)}")
    if not n:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise FileNotFoundError("这些剧情都没有可导出的台词")
    size = os.path.getsize(dst)
    if job is not None:
        job.log(f"已导出 {n} 部剧情的译文 → {paths.rel(dst)}"
                f"（{size / 1048576:.1f} MB）")
    return {"path": dst, "name": os.path.basename(dst), "files": n,
            "bytes": size, "scope": scope}


def trans_stats() -> dict:
    # ``list`` 给译文页的「全选已翻译」用（前端要用它和标准剧情选择组件联动）
    out = {"units": 0, "messages": 0, "list": [],
           "speakers": len(load_speakers())}
    if not os.path.isdir(TRANS_DIR):
        return out
    for name in os.listdir(TRANS_DIR):
        # _ 开头是发言人表这类全局文件，不是"一部剧情"
        if not name.endswith(".json") or name.startswith("_"):
            continue
        d = paths.load_json(os.path.join(TRANS_DIR, name), {}) or {}
        n = len(d.get("messages") or {})
        if n:
            out["units"] += 1
            out["messages"] += n
            out["list"].append(name[:-5])
    out["list"].sort()
    return out
