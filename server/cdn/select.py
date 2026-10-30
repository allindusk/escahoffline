"""资源包选择规则：``角色ID / 资源ID -> bundle_name``。

把所有命名约定集中在这里。以前这些正则散落在 ``char_story_export.py``、
``export_images.py``、``export_standfull.py``、``extract_scripts.py`` 里，
同一个约定改一处要改四个地方；现在链路问题先看这个文件。

命名速查（Shift-JIS 十六进制段是开发者日文目录名）
------------------------------------------------
``bundle_scenarioscript_char_<cid>``                     角色剧本（内含 6 段剧情）
``bundle_scenarioscript_main_<n>``                       主线剧情（第 n 章，含 0–8 段）
``bundle_scenarioscript_event_<n>``                      活动剧情（第 n 期）
``bundle_scenarioscript_other_<n>``                      其他剧情（特别篇等）
``bundle_voice_adv_char_<cid>_<seg>``                    语音（seg 0–5，仅角色剧情有）
``bundle_chara_c-panel_<cid>``                           半身像（头像来源）
``bundle_statusbase_status_character_<cid>``             状态图示
``bundle_episodeicons_episode_character_<cid>``          章节图标
``bundle_standimage_stand_<num>_<cloth>``    (单数)      分层立绘部件（脸是透明洞）
``bundle_standimages_stand_<num>``           (复数)      完整立绘（可直接用）
``bundle_bgimage_<hex>``                                 背景
``bundle_eventimage_<id>_<num>``                         CG（可能带 16 位随机 hash 前缀）
``bundle_music_<id小写>`` / ``bundle_sound_<id小写>``      BGM / 音效
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict

from .config import DATA

# ---------------------------------------------------------------- 角色 ID

PAT_SCENARIO = re.compile(r"bundle_scenarioscript_char_(\d+)")

# ---------------------------------------------------------------- 非角色剧情
# 剧本包在 CDN 上一共只有这 **6 种前缀**（清单全量核对过，没有 collab/harem 等
# 第 7 类）：char(383) / main(61) / event(94) / other(70) / tutorial(1) / op(1)。
# 它们的**内部结构与角色剧本完全相同**（同一套 adv JSON schema，都带
# FileName/StandImages/BackImages/Voices/Methods），差别只在两处：
#   1) 包名后缀不是角色 ID，而是 <kind>_<no>（tutorial / op 是单包、无编号）；
#   2) FileName 是 ``adv_<kind>_<seg>`` / ``adv_<kind>_<no>_<seg>``。
# 所以解包/播放/译文链路可以整体复用，只在「key 命名」上分叉。
ADV_KINDS = ("main", "event", "other", "tutorial", "op")
ADV_LABEL = {"main": "主线剧情", "event": "活动剧情", "other": "其他剧情",
             "tutorial": "新手教程", "op": "开场动画"}
# 游戏 UI 的菜单归类：tutorial / op 不单独占菜单，归在「其他」里
ADV_MENU = {"other": ("other", "tutorial", "op")}
PAT_ADV = re.compile(r"bundle_scenarioscript_(main|event|other|tutorial|op)(?:_(\d+))?$")
PAT_ADV_FILE = re.compile(r"adv_(main|event|other|tutorial|op)(?:_(\d+))?\.js")
PAT_VOICE = re.compile(r"bundle_voice_adv_char_(\d+)_(\d+)")
PAT_CPANEL = re.compile(r"bundle_chara_c-panel_(\d+)")
PAT_STATUS = re.compile(r"bundle_statusbase_status_character_(\d+)")
PAT_EPISODE = re.compile(r"bundle_episodeicons_episode_character_(\d+)")

# ---------------------------------------------------------------- 图片 ID
PAT_STAND_KEY = re.compile(r"stand_(\d+)_([0-9A-Fa-f]+)_")
PAT_STAND_PARTIAL = re.compile(r"bundle_standimage_stand_(\d+)_([0-9a-z]+)")
PAT_STAND_FULL = re.compile(r"bundle_standimages_stand_(\d+)")
BG_PREFIX = "bundle_bgimage_"
CG_PREFIX = "bundle_eventimage_"


def char_ids(index: dict) -> list[int]:
    """清单里有哪些角色有剧本。"""
    ids = set()
    for name in index:
        m = PAT_SCENARIO.fullmatch(name)
        if m:
            ids.add(int(m.group(1)))
    return sorted(ids)


def adv_ids(index: dict) -> list:
    """清单里有哪些非角色剧本，返回 ``[(kind, no|None), ...]``。

    ``no`` 为 ``None`` 表示该类型是**单个整包**（tutorial / op 各只有 1 个包，
    包名不带编号，包内才是 28 / 5 段）。
    """
    out = []
    for name in index:
        m = PAT_ADV.fullmatch(name)
        if m:
            out.append((m.group(1), int(m.group(2)) if m.group(2) else None))
    return sorted(out, key=lambda t: (ADV_KINDS.index(t[0]), t[1] or 0))


def story_key(kind: str, no) -> str:
    """非角色剧情的网页数据 key（= 文件名主干，也是译文目录名）。"""
    return f"adv_{kind}" if no is None else f"adv_{kind}_{no}"


def parse_story_key(key: str):
    """``adv_main_3`` -> ``("main", 3)``；``adv_op`` -> ``("op", None)``。"""
    m = PAT_ADV_FILE.fullmatch(key + ".js")
    return (m.group(1), int(m.group(2)) if m.group(2) else None) if m else None


def parse_adv_spec(spec: str):
    """命令行写法 ``main_1`` / ``adv_main_1`` / ``tutorial`` / ``op`` 都能解析。"""
    m = re.fullmatch(r"(?:adv_)?(main|event|other|tutorial|op)(?:_(\d+))?",
                     str(spec).strip())
    return (m.group(1), int(m.group(2)) if m.group(2) else None) if m else None


def adv_voice_bundles(index: dict, kind: str, no) -> list:
    """非角色剧情的语音包名列表（按段号升序）。

    ``bundle_voice_adv_`` 下有 5 个子类，**不是只有角色语音**：
    ``char_<cid>_<seg>``(2297) / ``event_<n>_<seg>``(775) / ``main_<n>_<seg>``(527) /
    ``other_<n>_<seg>``(13) / ``op_<seg>``(4)，合计 17.9 GB。
    主线/活动/其他的语音按「剧情 key + 段号」分包，段号与剧本 parts 顺序一致。
    """
    if kind in ("main", "event", "other"):
        pre = f"bundle_voice_adv_{kind}_{no}_"
    elif kind in ("op", "tutorial"):
        pre = f"bundle_voice_adv_{kind}_"
    else:
        return []
    out = [n for n in index
           if n.startswith(pre) and n[len(pre):].isdigit()]
    return sorted(out, key=lambda n: int(n[len(pre):]))


def strip_prefix(seg: str) -> str:
    """剥离 CG 资源名首段的随机 hash 前缀（按 8 字符对齐切）。"""
    while len(seg) > 8 and len(seg) % 8 == 0:
        seg = seg[-8:]
    return seg


def norm(s: str) -> str:
    """归一化资源名：去数字前导零、hex 统一大写，弥合剧本与包内命名差异。"""
    if not s:
        return s
    out = []
    for p in s.split("_"):
        if p.isdigit():
            out.append(str(int(p)))
        elif re.fullmatch(r"[0-9A-Fa-f]{4,}", p):
            out.append(p.upper())
        else:
            out.append(p)
    return "_".join(out)


class Manifest:
    """清单索引的只读视图，带各种取名所需的反向索引（构造一次，到处复用）。"""

    def __init__(self, index: dict):
        self.index = index
        # 注意：按**原始编号字符串**建索引，不能只按 int 归一化 ——
        # CDN 上同时存在 standimage_stand_36_8aee967b（鷹守ハルカ 便服）
        # 和 standimage_stand_036_8aee967b（閃忍ハルカ 忍者装），是两套不同素材。
        self.stand_parts: dict[str, list] = defaultdict(list)   # 原样编号 -> [(cloth, bundle)]
        self.stand_parts_by_int: dict[int, list] = defaultdict(list)
        self.stand_full: dict[int, str] = {}                    # num -> bundle
        self.bg: dict[str, str] = {}
        self.cg: dict[str, str] = {}
        self.music: dict[str, str] = {}
        self.sound: dict[str, str] = {}
        for name in index:
            if m := PAT_STAND_PARTIAL.fullmatch(name):
                self.stand_parts[m.group(1)].append((m.group(2), name))
                self.stand_parts_by_int[int(m.group(1))].append((m.group(2), name))
            elif m := PAT_STAND_FULL.fullmatch(name):
                self.stand_full[int(m.group(1))] = name
            elif name.startswith(BG_PREFIX):
                self.bg[name[len(BG_PREFIX):].lower()] = name
            elif name.startswith(CG_PREFIX):
                suf = name[len(CG_PREFIX):]
                self.cg[suf] = name
                parts = suf.split("_")
                if len(parts) >= 2:
                    key = f"{strip_prefix(parts[0])}_{parts[1]}".lower()
                    self.cg.setdefault(key, name)
            elif name.startswith("bundle_music_"):
                self.music[name[len("bundle_music_"):].upper()] = name
            elif name.startswith("bundle_sound_"):
                self.sound[name[len("bundle_sound_"):].upper()] = name

    # ------------------------------------------------------------ 单角色所需包
    def scenario(self, cid: int):
        return self.index.get(f"bundle_scenarioscript_char_{cid}")

    # ------------------------------------------------------------ 非角色剧情
    def adv_scenario(self, kind: str, no):
        """tutorial / op 是单包（no=None，包名不带编号）。"""
        name = (f"bundle_scenarioscript_{kind}" if no is None
                else f"bundle_scenarioscript_{kind}_{no}")
        return self.index.get(name)

    def voices(self, cid: int) -> list:
        out = []
        for seg in range(12):                    # 目前一期最多 6 段，多留一倍余量
            x = self.index.get(f"bundle_voice_adv_char_{cid}_{seg}")
            if x:
                out.append(x)
        return out

    def meta_images(self, cid: int) -> list:
        """头像/状态图/章节图标（按 cid 直接命名，不依赖剧本内容）。"""
        return [x for x in (
            self.index.get(f"bundle_chara_c-panel_{cid}"),
            self.index.get(f"bundle_statusbase_status_character_{cid}"),
            self.index.get(f"bundle_episodeicons_episode_character_{cid}"),
        ) if x]

    def stand_bundle(self, key: str):
        """``stand_<num>_<cloth>_<expr>`` -> 分层部件包名。

        编号的**补零宽度是有意义的**：``stand_36_...``（鷹守ハルカ 便服）与
        ``stand_036_...``（閃忍ハルカ 忍者装）是两套不同素材。以前这里用
        ``int(num)`` 归一化，两者被并成一组、取到哪个全看清单顺序 —— 结果剧情
        里的 ``stand_36_...`` 被解析成 036 的包并下载，立绘整套变成忍者装。
        所以先按**原始字符串**精确匹配；只有精确形态不存在时才按数值退回
        （历史上剧本 key 写 ``stand_2``、包名写 ``stand_02``，需要这种容错）。
        """
        m = PAT_STAND_KEY.match(key)
        if not m:
            return None
        num_s, cloth = m.group(1), m.group(2).lower()
        exact = self.stand_parts.get(num_s, [])
        loose = self.stand_parts_by_int.get(int(num_s), [])
        for pool in (exact, loose):
            hit = next((n for h, n in pool if h == cloth), None)
            if hit:
                return hit
        # 同编号下没找到这套衣服：退回该编号的第一套（比返回 None 好）
        for pool in (exact, loose):
            if pool:
                return pool[0][1]
        return None

    def back_bundle(self, key: str):
        return self.bg.get(key.lower())

    def cg_bundle(self, key: str):
        return self.cg.get("_".join(key.split("_")[:2]).lower())


# ---------------------------------------------------------------- 剧本资产


def load_char(cid) -> dict:
    """读 ``web/data/char_<id>.js``（剥掉 ``window.__CHAR=`` 外壳）。"""
    path = os.path.join(DATA, f"char_{cid}.js")
    return load_char_file(path)


def load_char_file(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])


def dump_char(cid, data: dict, compact: bool = True) -> str:
    """写回 ``web/data/char_<id>.js``，返回文件路径。"""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, f"char_{cid}.js")
    body = json.dumps(data, ensure_ascii=False,
                      separators=(",", ":") if compact else (", ", ": "))
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"window.__CHAR={body};")
    return path


def load_story(key: str) -> dict:
    """读任意剧情数据 ``web/data/<key>.js``（key = ``char_69`` / ``adv_main_1``）。"""
    return load_char_file(os.path.join(DATA, key + ".js"))


def dump_story(key: str, data: dict, compact: bool = True) -> str:
    """写 ``web/data/<key>.js``。

    角色剧本用 ``window.__CHAR``（老约定，播放器/下游脚本都认它），
    非角色剧情用 ``window.__STORY``——两者分开变量名，播放器按需读取，
    这样加载 adv 时不会覆盖已缓存的角色数据。
    """
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, key + ".js")
    body = json.dumps(data, ensure_ascii=False,
                      separators=(",", ":") if compact else (", ", ": "))
    var = "__CHAR" if key.startswith("char_") else "__STORY"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"window.{var}={body};")
    return path


def collect_assets(data: dict) -> dict:
    """从已解析的角色剧本里收集它引用的全部图片资源 ID。"""
    out = {"stand": set(), "back": set(), "cg": set()}
    for p in data["parts"]:
        out["stand"].update(p.get("stand") or [])
        out["back"].update(p.get("back") or [])
        out["cg"].update(p.get("cg") or [])
        # 演出指令里的立绘也要收齐：有些 key 没列在 parts.stand 里
        for m in p.get("methods") or []:
            if m.get("StandImage"):
                out["stand"].add(m["StandImage"])
            if m.get("BackImage"):
                out["back"].add(m["BackImage"])
    return out


def messages_of(data: dict) -> list:
    """按出现顺序返回全部 ``ShowMessage`` 指令字典（i18n 与统计共用）。"""
    return [(pi, mi, m) for pi, p in enumerate(data["parts"])
            for mi, m in enumerate(p.get("methods") or [])
            if m.get("Method") == "ShowMessage"]
