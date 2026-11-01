# -*- coding: utf-8 -*-
"""「其他」（other）剧情的权威分组表 —— 依据用户在游戏里逐屏抄下来的三张表。

背景
----
其它分类脚本（classify_other.py / build_other_episodes.py）以前是**关键词猜**：
抓「グナガン」「殲滅」「周年」这类词再归类，结果把 6 个多段合集（65 段日常短篇）
整包塞进「Hシーン集」，RAID 入口的话名也全是首句截断。本模块改成**逐包点名**的
硬编码映射，不再猜。

游戏里的真实结构（三张表 = 16 个入口）
--------------------------------------
表 A  RAID BOSS BATTLE：100 ゴールデンハニー / 101 グナガン / 102 ウールタール /
      103 堕威大仏 / 104 大往嬢 / 105 狂王 / 107 ヴリトラ
表 B  殲滅戦 -STAMPEDE-（800）＋ 広域戦 戦士/閃忍/神騎/魔女/その他（200/210/220/230/240）
表 C  記念イベント（1000）/ エイプリルフール（7000）/ メインストーリー Hシーン集（9000）

★ 两个关键结构事实（数据验证过，不是猜）
1. **2 段包 = 一次活动的前后篇**：p0 = ボス来襲（登场），p1 = ボス退場（撤退）。
   RAID 40 包 × 2 = 80 段、殲滅戦 4 包 × 2 = 8 段，全部符合。
   各 BOSS 的「その N」按**包号升序**编号：ウールタール 7 包 ↔ 表里正好 その1〜その7；
   グナガン 7、大往嬢 6、堕威大仏 5、狂王 4、ヴリトラ 3 也一一对上。
2. **多段包 = 広域戦 5 个入口的主题短篇集**（不是 H 集！）：
   27 全是閃忍（17 段）→ 210 閃忍；29 全是魔女（14 段）→ 230 魔女；
   28 全是神騎（15 段）→ 220 神騎；26 是戦士/ビート系（13 段）→ 200 戦士；
   31 是職員/非戦闘（6 段）→ 240 その他。
   话名就是「戦士 N」「忍者 N」「神騎 N」「魔女 N」「その他 N」的**连号序列**。
   表里那些「広域戦に挑戦しよう」是**玩家还没解锁的格子**（用户 2026-11-02 澄清），
   不是入口说明文字：每列都按 1..N 连号排下来，锁住的只是看不到名字。
   所以 N 一律取**该包实际段数**，不要照抄表的格数：
   | 列 | 表里格数 | 实际段数 | 包 | 说明 |
   |戦士|12（1,2,4,5,6,7+6 锁）|13|26| 表是约 12〜14 行的可视窗口，被截断 |
   |閃忍|12（1,2,5,6,7+7 锁）  |17|27| 同上 |
   |神騎|13（1,2,5,6,7,8+7 锁）|15|28| 同上 |
   |魔女|13（1..8+5 锁）       |14|29| 同上 |
   |その他|6（1..4+2 锁）      | 6|31| **列到头了，完全吻合** |
   数字跳号（戦士缺3、忍者缺3,4）是 AI 抄表时的误读，只当"存在性"证据。

記念イベント 15 话（2026-11-02 用户逐条截图确认，与数据 15 段一一对应）
------------------------------------------------------------------
游戏列表顺序（侧栏照这个排，见 _rank）：
  2021年 / 2023年 / 2024年 / 2025年 / 2026年ハニーの日   ← 包 14 / 41 / 45 / 58 / 70
  祝！1 / 1.5 / 2 / 2.5 / 3 / 3.5 / 4 / 4.5 / 5 / 5.5周年！！← 包 15/17/24/30/42/44/54/57/66/68
注意 **2022 年没有ハニーの日**（当年 8/2 就是 2 周年包 24 本身），别按"每年一条"补。
第一版表格只抄到 3 条ハニー、且漏了「祝！3.5周年！！」，是 AI 抄表截断，已用真名单改正。
  エイプリルフール：25=2023（唯一单段，表里也只有它是单条）、43=2024（11 段，
    其一明确写「４月１日…エイプリルフール」，对应表里「その2/その4/その11」）、
    55=2025、67=2026（各 2 段 = 本篇 + その後，对应表里「〇〇年＋その後」两条）。
  殲滅戦：四组就是 その1〜その4（用户 2026-11-02 确认），即包号升序 56/63/64/65，
    每包 p0=侵略者来襲 / p1=侵略者退場。

未解 1 项：包 1「人気投票のご案内」（第一回人气投票说明，2 段完全相同）——三张表里
都没有它，暂列独立分组、不挂入口横幅。

新包自动归类（游戏更新后不用再手工维护）
--------------------------------------
上面的 dict 只写"人工确认过"的部分。**表里没有的包/段**由 `classify_pack()` 按规律补：
  ① 2 段包 + 正文出现 BOSS 名（按出现次数取最高，避免别人顺口提到ハニー就串味）
     → RAID 该 BOSS，来襲/退場 そのN（N 接着往后编）
  ② 2 段包 + 「殲滅 / 敵の集団 / 侵略者」→ 殲滅戦
  ③ 正文写「4月1日 / エイプリル」→ エイプリルフール（年取不到就顺延）
  ④ 正文自报「N周年」（含「一周年」汉字）→ 祝！N周年！！
  ⑤ 8月2日 / ハニーの日 → ○○○○年ハニーの日（年份按最近一次 +1，标"中"置信）
  ⑥ ≥6 段且每段超长 → Hシーン集
  ⑦ ≥4 段且立绘职业单色 → 広域戦 对应职种（必须全票一致且≥2票，或≥4票且≥80%）
  ⑧ 老包又加段（広域戦/愚人节/H集 都是这样长的）→ 沿用原入口接着编号
都不匹配 → 入口 **-1「未归类（新剧情）」**，侧栏照样能看到、可播，不会丢也不会塞错。
原则：**宁可归不了，也不能归错**（自测里 7 个判不出的包全部落在未归类，0 个判错）。

用法
----
    python tools/other_taxonomy.py             # 自检：覆盖率 / 包段数 / 重复
    python tools/other_taxonomy.py --selftest   # 把现有 70 包当新包跑自动规则，算命中率
    python tools/build_other_episodes.py        # 重新生成 othereps.js（会自动扫描新包）
"""
from __future__ import annotations

import collections
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "web", "data")

#: 入口号 → 游戏里显示的名字（横幅上写的）
ENTRIES = {
    100: "RAID BOSS ゴールデンハニー",
    101: "RAID BOSS グナガン",
    102: "RAID BOSS ウールタール",
    103: "RAID BOSS 堕威大仏",
    104: "RAID BOSS 大往嬢",
    105: "RAID BOSS 狂王",
    107: "RAID BOSS ヴリトラ",
    200: "広域戦 戦士",
    210: "広域戦 閃忍",
    220: "広域戦 神騎",
    230: "広域戦 魔女",
    240: "広域戦 その他",
    800: "殲滅戦 -STAMPEDE-",
    1000: "記念イベント",
    7000: "エイプリルフール",
    9000: "メインストーリー Hシーン集",
    0: "投票のご案内",          # 三张表里没有，单独放最后
    -1: "未归类（新剧情）",     # 游戏更新后新包自动归类失败时的兜底，不挂入口横幅
}

#: 侧栏分组显示顺序（表 A → 表 B → 表 C → 未解）
ORDER = [100, 101, 102, 103, 104, 105, 107, 200, 210, 220, 230, 240,
         800, 1000, 7000, 9000, 0, -1]

#: RAID BOSS：入口 → 包号列表（升序即「その N」的顺序）
RAID_PACKS = {
    100: [3, 6, 10, 18, 32, 37, 50, 72],
    101: [2, 5, 9, 23, 36, 48, 61],
    102: [4, 7, 11, 19, 38, 49, 62],
    103: [8, 12, 21, 39, 51],
    104: [13, 20, 33, 40, 52, 71],
    105: [22, 34, 46, 60],
    107: [35, 47, 59],
}

#: 话名里的 BOSS 简称（表 A 的写法）
RAID_LABEL = {
    100: "Gハニー",
    101: "グナガン",
    102: "ウールタール",
    103: "堕威大仏",
    104: "大往嬢",
    105: "狂王",
    107: "ヴリトラ",
}

#: 広域戦：入口 → 多段包号
WIDE_PACKS = {200: 26, 210: 27, 220: 28, 230: 29, 240: 31}

#: 広域戦各入口的话名前缀（表 B 的写法：戦士/忍者/神騎/魔女/その他）
WIDE_LABEL = {200: "戦士", 210: "忍者", 220: "神騎", 230: "魔女", 240: "その他"}

#: 殲滅戦：包号 → 「その N」（用户 2026-11-02 确认：就是 その1〜その4，即包号升序）
STAMPEDE_PACKS = {56: 1, 63: 2, 64: 3, 65: 4}

#: 記念イベント：周年包 → 周年数（台词自报）
ANNIV_PACKS = {
    15: "1", 17: "1.5", 24: "2", 30: "2.5", 42: "3",
    44: "3.5", 54: "4", 57: "4.5", 66: "5", 68: "5.5",
}

#: 記念イベント：ハニーの日（8/2）包 → 年份（用户 2026-11-02 按游戏列表确认：无 2022 年）
HONEYDAY_PACKS = {14: "2021", 41: "2023", 45: "2024", 58: "2025", 70: "2026"}

#: エイプリルフール：包号 → 逐段话名
APRIL_PACKS = {
    25: ["2023年エイプリルフール"],
    43: [f"2024年エイプリルフール その{i + 1}" for i in range(11)],
    55: ["2025年エイプリルフール", "2025年エイプリルフール その後"],
    67: ["2026年エイプリルフール", "2026年エイプリルフール その後"],
}

#: メインストーリー Hシーン集：包 69 的 11 段（顺序与游戏列表一致）
HSCENE_PACK = 69
HSCENE_TITLES = [
    "ユーノ（1部プロローグ）",
    "エスカ・ルビー（1部1章2話）",
    "エスカ&ハルカリバース（1部12章8話）",
    "オルバ（1部17章8話）",
    "一の神鍵アズエル（1部神騎外伝7話）",
    "閃忍イノリ（2部プロローグ）",
    "閃忍ライカ（2部2章1話）",
    "閃忍ライカ（2部2章8話）",
    "閃忍ムツカ（2部3章8話）",
    "黒い少女（2部16章5話）",
    "レガリアの神騎ユリエル（3部2章8話）",
]

#: 三张表里都没有的包：(包号, 入口号, [逐段话名])
EXTRA_PACKS = [(1, 0, ["人気投票のご案内"])]

#: 已知的重复段（同一包内多段台词完全一样，只保留第 1 段，避免侧栏出现两条一样的话）
DUP_PARTS = [(44, 1), (1, 1)]  # 3.5 周年包 / 投票说明的 p1 与 p0 台词逐句相同


def read_js(path):
    raw = open(path, encoding="utf-8-sig").read()
    i = raw.find("{")
    return json.loads(raw[i:raw.rindex("}") + 1])


def messages(part):
    return [str(m.get("Message") or "") for m in part["methods"]
            if m.get("Method") == "ShowMessage"]


def pack_parts():
    """{包号: 段数}"""
    out = {}
    for fn in glob.glob(os.path.join(DATA, "adv_other_*.js")):
        no = int(fn[:-3].rsplit("_", 1)[1])
        out[no] = len(read_js(fn)["parts"])
    return out


def plan():
    """返回 [(包号, 段号, 入口号, 话名)]，顺序即侧栏顺序。

    硬编码表（上面那些 dict）打底；**表里没有的包**（游戏更新后新出的 other_NN）
    交给 `scan()` 按规律自动归类，归不进去的进「未归类」入口 -1，绝不丢、也不瞎塞。
    """
    rows = []

    for entry, packs in RAID_PACKS.items():
        for n, no in enumerate(packs, 1):
            label = RAID_LABEL[entry]
            rows.append((no, 0, entry, f"{label}来襲 その{n}"))
            rows.append((no, 1, entry, f"{label}退場 その{n}"))

    for entry, no in WIDE_PACKS.items():
        label = WIDE_LABEL[entry]
        # 段数由数据决定（13/17/15/14/6），话名就是「戦士 1」「忍者 1」…
        n = pack_parts()[no]
        for i in range(n):
            rows.append((no, i, entry, f"{label} {i + 1}"))

    for no, n in STAMPEDE_PACKS.items():
        rows.append((no, 0, 800, f"侵略者来襲 その{n}"))
        rows.append((no, 1, 800, f"侵略者退場 その{n}"))

    for no, ver in ANNIV_PACKS.items():
        rows.append((no, 0, 1000, f"祝！{ver}周年！！"))

    for no, year in HONEYDAY_PACKS.items():
        rows.append((no, 0, 1000, f"{year}年ハニーの日"))

    for no, titles in APRIL_PACKS.items():
        for i, t in enumerate(titles):
            rows.append((no, i, 7000, t))

    for i, t in enumerate(HSCENE_TITLES):
        rows.append((HSCENE_PACK, i, 9000, t))

    for no, entry, titles in EXTRA_PACKS:
        for i, t in enumerate(titles):
            rows.append((no, i, entry, t))

    dup = {(p, i) for p, i in DUP_PARTS}
    known = {(r[0], r[1]) for r in rows} | dup
    rows += _auto_rows(rows, known)
    rows = [r for r in rows if (r[0], r[1]) not in dup]
    rows.sort(key=lambda r: (ORDER.index(r[2]), _rank(*r)))
    return rows


# --------------------------------------------------------------------------
# 新包自动归类（游戏更新后 other_NN 变多，不用再手工维护）
# --------------------------------------------------------------------------

#: BOSS 关键词 → 入口。注意 100 不能只用「ハニー」（日常剧情里到处都是），
#: 必须带 G/ゴールデン；包3 那条只说「ハニー」的早期话会识别不出 → 进未归类。
BOSS_KW = {
    100: ["ゴールデンハニー", "ゴールデン・ハニー", "Ｇハニー", "Gハニー"],
    101: ["グナガン"],
    102: ["ウールタール"],
    103: ["堕威大仏"],
    104: ["大往嬢"],
    105: ["狂王"],
    107: ["ヴリトラ"],
}

#: 広域戦 5 个入口的职种关键词（用来判定一个多段包属于哪个入口）
WIDE_KW = {
    200: ["ビート", "トレーニング", "アイドル", "戦士", "スピナー", "ブレイカー",
          "ハンター", "シューター", "ボンデージ", "デュアル", "ネイチャー", "スナイパー"],
    210: ["閃忍", "忍者", "殲忍", "滅忍", "鬼神谷", "猫屋敷", "忍"],
    220: ["神騎", "天使", "レガリア", "皇帝", "審判", "鍵", "聖"],
    230: ["魔女", "魔術", "電脳"],
}

#: 全角数字 → 半角（剧本里「３．５周年」「2.5周年」两种写法都有）
_FW = str.maketrans("０１２３４５６７８９．", "0123456789.")

#: 汉数字 → 阿拉伯（「一周年」「リリース一周年」）
_KANJI = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
          "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}


def _num(t):
    return t.translate(_FW)


def _part_text(parts):
    return [_num("".join(messages(p))) for p in parts]


def _wide_votes(parts):
    """多段包 → {入口: 命中次数}，按每个立绘组的职种关键词计票。"""
    votes = collections.Counter()
    for p in parts:
        gids = [str(m["GroupId"]) for m in p["methods"]
                if m.get("GroupId") and m.get("Method") == "ShowStand"
                and str(m["GroupId"]) != "タイトルロゴ"]
        best = None
        for entry, kws in WIDE_KW.items():
            n = sum(1 for g in gids if any(k in g for k in kws))
            if n and (best is None or n > best[1]):
                best = (entry, n)
        if best:
            votes[best[0]] += 1
    return votes


def classify_pack(no, parts, st):
    """按规律给一个**表里没有的**包猜归属。

    返回 (rows, confidence, reason)；rows = [(包号, 段号, 入口号, 话名)]。
    confidence: 高 = 台词/结构直接决定；中 = 立绘职业或年份是推的；低 = 猜不出。
    `st` 是跨包计数器（见 `_state`），保证多个新包不会都叫「その1」。
    """
    texts = _part_text(parts)
    joined = "\n".join(texts)
    n = len(parts)

    # ① RAID BOSS：2 段包 + 正文出现 BOSS 名（p0=来襲 / p1=退場）
    #    按**出现次数**取最高的 BOSS，不能按 dict 顺序取第一个：
    #    包48 是グナガン的话，但台词里旁人提到ハニー，先命中 100 就错了。
    if n == 2:
        score = {}
        for entry, kws in BOSS_KW.items():
            c = sum(joined.count(k) for k in kws)
            if c:
                score[entry] = c
        if score:
            entry = max(score, key=lambda e: (score[e], -list(BOSS_KW).index(e)))
            st["raid"][entry] = st["raid"].get(entry, len(RAID_PACKS[entry])) + 1
            i, label = st["raid"][entry], RAID_LABEL[entry]
            return ([(no, 0, entry, f"{label}来襲 その{i}"),
                     (no, 1, entry, f"{label}退場 その{i}")], "高",
                    f"2段包 + 正文出现 {label}（{score[entry]} 次）")

    # ② 殲滅戦：2 段包 + 成建制的敌群
    if n == 2 and ("殲滅" in joined or "敵の集団" in joined or "侵略者" in joined):
        st["stampede"] += 1
        i = st["stampede"]
        return ([(no, 0, 800, f"侵略者来襲 その{i}"),
                 (no, 1, 800, f"侵略者退場 その{i}")], "高", "2段包 + 殲滅/敵の集団")

    # ③ エイプリルフール：正文写 4月1日 / エイプリル
    if "4月1日" in joined or "エイプリル" in joined:
        m = re.search(r"(20\d\d)年", joined)
        if m:
            y = int(m.group(1))
        else:
            st["april_year"] += 1
            y = st["april_year"]
        titles = [f"{y}年エイプリルフール"] + \
                 ([f"{y}年エイプリルフール その後"] if n == 2 else []) + \
                 [f"{y}年エイプリルフール その{k}" for k in range(3, n + 1)]
        return ([(no, k, 7000, t) for k, t in enumerate(titles)], "高",
                "正文写 4月1日/エイプリル")

    # ④ 記念イベント・周年：正文自报 N 周年（3.5 周年那种 2 段包也算，取第 1 段）
    #    数字有「一周年」「リリース一周年」这种汉字写法，也要认
    m = re.search(r"(\d+(?:\.\d+)?|[一二三四五六七八九十])周年", joined)
    if m and n <= 2:
        v = _KANJI.get(m.group(1), m.group(1))
        return [(no, 0, 1000, f"祝！{v}周年！！")], "高", "正文自报周年数"

    # ⑤ 記念イベント・ハニーの日：8月2日 / ハニーの日（年份推 = 最近一次 + 1）
    if n == 1 and ("8月2日" in joined or "八月二日" in joined or "ハニーの日" in joined):
        st["honey_year"] += 1
        y = st["honey_year"]
        return [(no, 0, 1000, f"{y}年ハニーの日")], "中", f"8月2日剧情（年份按最近一次+1 推为 {y}）"

    # ⑥ メインストーリー Hシーン集：多段 + 每段句数异常大（H 场景长篇）
    if n >= 6:
        avg = sum(len(t) for t in texts) / n
        if avg >= 3000:      # 现有 H 集平均 ~6400 字/段，其他合集都在 1500 以下
            return ([(no, i, 9000, f"新規 Hシーン（追加 {len(HSCENE_TITLES) + i + 1}）")
                     for i in range(n)], "中", f"{n}段平均 {avg:.0f} 字/段，符合 H 集长篇特征")

    # ⑦ 広域戦：多段包 + 立绘职业单色。
    #    不能按"全体段数"算比例——很多立绘组名字里没有职种词，会导致包26/28/29 全判不出；
    #    改成只看**投得出票的段落**，且要求「全票同一职种 且 至少 2 票」，
    #    或「≥4 票且 ≥80% 一致」。包31（その他，靠 1 票会误判成神騎）就是靠这条门槛
    #    落到未归类的——**宁可归不了，也不能归错**。
    if n >= 4:
        v = _wide_votes(parts)
        if v:
            entry, cnt = v.most_common(1)[0]
            voted = sum(v.values())
            if (len(v) == 1 and voted >= 2) or (voted >= 4 and cnt >= voted * 0.8):
                titles = []
                for _k in range(n):
                    st["wide"][entry] += 1
                    titles.append(f"{WIDE_LABEL[entry]} {st['wide'][entry]}")
                return ([(no, k, entry, t) for k, t in enumerate(titles)], "中",
                        f"投得出职种的 {voted} 段里有 {cnt} 段是 {WIDE_LABEL[entry]} 系立绘")

    return None, "低", "规则都不匹配（2段无BOSS名 / 单段无周年无8月2日 / 多段职种不明）"


def _state(rows):
    """跨包计数器：从硬表现状算出各入口"下一个序号"。"""
    return {
        "raid": {e: len(p) for e, p in RAID_PACKS.items()},
        "stampede": max(STAMPEDE_PACKS.values(), default=0),
        "wide": {e: sum(1 for r in rows if r[2] == e) for e in WIDE_LABEL},
        "honey_year": max(int(x) for x in HONEYDAY_PACKS.values()),
        "april_year": max(int(t[:4]) for ts in APRIL_PACKS.values()
                          for t in ts if t[:4].isdigit()),
    }


def _append_parts(no, missing, known_rows, st):
    """老包又加了新段（広域戦/愚人节/H集 都是这样长的）：入口沿用，话名接着编号。"""
    entry = known_rows[0][2]
    out = []
    if entry in WIDE_LABEL:
        for i in missing:
            st["wide"][entry] += 1
            out.append((no, i, entry, f"{WIDE_LABEL[entry]} {st['wide'][entry]}"))
    elif entry == 7000:
        titles = [r[3] for r in known_rows]
        year = next((t[:4] for t in titles if t[:4].isdigit()), str(st["april_year"]))
        k = sum(1 for t in titles if "その" in t)
        for i in missing:
            k += 1
            out.append((no, i, entry, f"{year}年エイプリルフール その{k}"))
    elif entry == 9000:
        k = len(known_rows)
        for i in missing:
            k += 1
            out.append((no, i, entry, f"新規 Hシーン（追加 {k}）"))
    else:
        for i in missing:
            out.append((no, i, entry, f"追加 第{i + 1}段"))
    return out


def _auto_rows(rows, known):
    """把表里没有的**包**或**段**按规律补进来；猜不出的挂到「未归类」入口 -1。

    两种更新都要管：
    * 新包（`other_73` 这种）→ `classify_pack` 按规律归类；
    * 老包加段（広域戦 26/27/28/29/31、愚人节 43、H集 69 都是"往里加段"）→ 沿用原入口接着编号。
    """
    parts_count = pack_parts()
    st = _state(rows)
    by_pack = collections.defaultdict(list)
    for r in rows:
        by_pack[r[0]].append(r)
    out = []
    for no in sorted(parts_count):
        missing = [i for i in range(parts_count[no]) if (no, i) not in known]
        if not missing:
            continue
        if no in by_pack:
            extra = _append_parts(no, missing, by_pack[no], st)
            out += extra
            print(f"  [自动续号] other_{no} 多出 {len(missing)} 段 → "
                  f"{ENTRIES.get(extra[0][2], '?')}：{extra[0][3]} …")
            continue
        parts = read_js(os.path.join(DATA, f"adv_other_{no}.js"))["parts"]
        got, conf, why = classify_pack(no, parts, st)
        if got:
            out += got
            print(f"  [自动归类·{conf}] other_{no}（{len(parts)}段）→ "
                  f"{ENTRIES.get(got[0][2], '?')}：{got[0][3]}  （{why}）")
        else:
            out += [(no, i, -1, f"新剧情 包{no} 第{i + 1}段") for i in missing]
            print(f"  [未归类] other_{no}（{len(parts)}段）：{why}")
    return out


def _rank(no, part, entry, _title):
    """同一入口内的话序。默认按包号/段号（= 游戏里的发布顺序）。

    例外是「記念イベント」：游戏列表是**先 5 条ハニーの日（2021/2023/2024/2025/2026），
    再 10 条祝！N周年！！（1→5.5）**（用户 2026-11-02 逐条截图确认），
    跟包号顺序（两者按日期交错）不同，所以这里按游戏的实际排法重排。
    """
    if entry == 1000:
        if no in HONEYDAY_PACKS:
            return (0, int(HONEYDAY_PACKS[no]))
        return (1, float(ANNIV_PACKS[no]))
    return (0, no, part)


def verify():
    """自检：包/段是否全覆盖、有无重复、重复段是否真的一样。"""
    parts = pack_parts()
    rows = plan()
    got = {(r[0], r[1]) for r in rows}
    skip = {(p, i) for p, i in DUP_PARTS}
    bad = []

    for no, n in sorted(parts.items()):
        for i in range(n):
            if (no, i) not in got and (no, i) not in skip:
                bad.append(f"漏了 包{no} 段{i}")
    for no, i in got:
        if no not in parts:
            bad.append(f"包{no} 不存在")
        elif i >= parts[no]:
            bad.append(f"包{no} 只有 {parts[no]} 段，却引用了段{i}")
    if len(got) != len(rows):
        bad.append(f"有重复条目：{len(rows)} 条 / {len(got)} 个唯一 (包,段)")

    # 跳过的重复段必须真的和 p0 台词逐句相同
    for no, i in DUP_PARTS:
        p = read_js(os.path.join(DATA, f"adv_other_{no}.js"))["parts"]
        if messages(p[0]) != messages(p[i]):
            bad.append(f"包{no} 段{i} 被当成重复段跳过，但台词并不相同！")

    for e in {r[2] for r in rows}:
        if e not in ENTRIES:
            bad.append(f"入口 {e} 没有名字")
    return rows, parts, bad


def selftest():
    """拿现有的 70 个包**假装成新包**跑一遍自动规则，跟人工表比命中率。

    这是"以后能不能自动归类"的唯一硬指标：命中率高，才敢让新包自动进侧栏。
    """
    rows, parts, _ = verify()
    truth = {}
    for no, i, entry, _t in rows:
        truth[(no, i)] = entry
    st = _state(rows)
    ok = same = 0
    miss = []
    for no in sorted(parts):
        n = parts[no]
        p = read_js(os.path.join(DATA, f"adv_other_{no}.js"))["parts"]
        got, conf, why = classify_pack(no, p, st)
        want = {truth[(no, i)] for i in range(n) if (no, i) in truth}
        if got is None:
            got = [(no, i, -1, "") for i in range(n)]
            conf = "低"
        have = {r[2] for r in got}
        if have == want:
            ok += 1
            if len(want) == 1:
                same += 1
        else:
            miss.append((no, n, conf, sorted(want), sorted(have), why))
    total = len(parts)
    print(f"自动规则自测：{ok}/{total} 包判对（{ok / total * 100:.0f}%）")
    if miss:
        print("判错/判不出的包：")
        for no, n, conf, want, have, why in miss:
            w = "/".join(ENTRIES.get(e, str(e)) for e in want)
            h = "/".join(ENTRIES.get(e, str(e)) for e in have)
            print(f"  other_{no:<3} {n}段 应为[{w}] → 判成[{h}]（{conf}：{why}）")
    print("\n注：判不出的包会进「未归类（新剧情）」组，不会丢、也不会被塞进错的入口。")
    return 0 if ok == total else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="「其他」剧情分组权威表 / 新包自动归类")
    ap.add_argument("--selftest", action="store_true", help="用现有 70 包验证自动规则的命中率")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    rows, parts, bad = verify()
    print(f"其他：{len(parts)} 包 / {sum(parts.values())} 段 → {len(rows)} 话")
    for e in ORDER:
        rs = [r for r in rows if r[2] == e]
        if not rs:
            continue
        print(f"  etc_{e:<4} {ENTRIES[e]:<24} {len(rs):>3} 话")
    print()
    print("自检：", "全部覆盖，无重复 ✓" if not bad else "有问题 ✗")
    for b in bad:
        print("  -", b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
