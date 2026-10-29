"""导出/回灌角色第5、6段（成人内容，parts[4]/parts[5]）的 v3 送翻稿。

送翻稿格式（与人工翻译模板一致：### 块头 + JA: 原文行；译稿只回 ### 块头 + 中文）：

    #========================================================================
    # 《Escalation Heroines》 — 日译中（成人向 / R18）
    # 角色：char_126  バレンタインハルカ（情人节遥）
    # 范围：第 5 段 217 句 ＋ 第 6 段 180 句 ＝ 397 句
    # 最后一个块的 ID：126-s6-179
    #========================================================================
    # 【任务】【输出格式】【示例】【用词/专名对照表】 ...
    #
    ########## ★★★ 进入第 5 段   共 217 句  ★★★
    ### 126-s5-000 【トキサダ】(1行)
    JA: 「ハルカさん、無事か？」

用法：
    python tools/export_r18_56.py export <cid> [-o FILE]
    python tools/export_r18_56.py import <cid> <译稿FILE> [--lang zh-Hans] [--dry-run]
"""
from __future__ import annotations

import os
import re
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
from cdn.select import load_char
from cdn import i18n_store


SEG = (4, 5)  # parts 索引：4=第5段(s5)，5=第6段(s6)
LABEL = {4: "s5", 5: "s6"}

# 专名/术语对照表（共享的遥系 + 游戏通用词；情人节向，去掉了圣诞专属项）。
# 右侧是定型译法，译稿须照抄；表里没有的按语境直译。
GLOSSARY = [
    ("ダイビート基地", "DIE-BEAT基地"),
    ("ダイビート", "DIE-BEAT"),
    ("超昂戦士", "超昂战士"),
    ("Ｄチャージ", "D充能"),
    ("ＤＤＤ", "DDD"),
    ("エスカレイヤー", "艾斯卡蕾雅"),
    ("高円寺沙由香", "高圆寺沙由香"),
    ("閃忍ハルカ", "闪忍遥"),
    ("ハルカ", "遥"),
    ("エスカ・ルビー", "艾斯卡·露比"),
    ("エスカ・サファイア", "艾斯卡·蓝宝石"),
    ("エスカ・トパーズ", "艾斯卡·托帕兹"),
    ("エスカ・アメイズ", "艾斯卡·阿梅兹"),
    ("フーマン", "修曼"),
    ("上弦衆", "上弦众"),
    ("閂市", "闩市"),
    ("長官", "长官"),
    ("トキサダ", "时贞"),
    ("バレンタインハルカ", "情人节遥"),
    ("バレンタイン", "情人节"),
    ("チョコレート", "巧克力"),
    ("チョコ", "巧克力"),
    ("ハート", "心形"),
    ("シチュエーション", "场景"),
    ("あっという間に", "转瞬之间"),
    ("あられもない", "不堪入目的"),
    ("気持ち良くて", "太舒服了"),
    ("どろどろに", "泥泞般/浓稠地"),
    ("気持ちいい", "舒服"),
    ("気持ち良い", "舒服"),
    ("ありません", "没有"),
    ("テクニック", "技巧"),
    ("パーティー", "队伍"),
    ("恥ずかしい", "害羞"),
    ("ぬるぬる", "湿哒哒/滑溜溜"),
    ("遠慮なく", "毫不客气地"),
    ("お姉さん", "姐姐"),
    ("それでも", "即便如此"),
    ("ではなく", "而不是"),
    ("どうして", "为什么"),
    ("ましょう", "吧"),
    ("もちろん", "当然"),
    ("やっぱり", "果然"),
    ("ゆっくり", "缓缓"),
    ("セックス", "性行为"),
    ("ドキドキ", "心跳不已"),
    ("パイズリ", "乳交"),
    ("ピストン", "活塞运动"),
    ("ポイント", "点数"),
    ("滅茶苦茶", "乱七八糟"),
    ("無意識に", "下意识地"),
    ("赤ちゃん", "婴儿"),
    ("んあっ", "嗯啊…… (喘息)"),
    ("感じる", "感觉"),
    ("結合部", "结合处"),
    ("豊満な", "丰满的"),
    ("あなた", "你"),
    ("あまり", "不太"),
    ("いつか", "总有一天"),
    ("お世話", "照料"),
    ("お互い", "互相"),
    ("お願い", "拜托"),
    ("お風呂", "泡澡"),
    ("かしら", "呢"),
    ("かなり", "相当"),
    ("ぎゅっ", "紧紧抱住"),
    ("こちら", "此处"),
    ("これは", "这是"),
    ("ごめん", "对不起"),
    ("しかし", "然而"),
    ("しない", "不"),
    ("すごい", "厉害"),
    ("ずっと", "一直"),
    ("そっと", "轻轻地"),
    ("だから", "所以"),
    ("ついに", "终于"),
    ("とても", "非常"),
    ("また、", "另外，"),
    ("もっと", "更加"),
    ("クラス", "职业"),
    ("ズボン", "裤子"),
    ("パンツ", "内裤"),
    ("ピンク", "粉色"),
    ("フェラ", "口交"),
    ("ペニス", "阴茎"),
    ("レイプ", "强奸"),
    ("丸出し", "完全暴露"),
    ("優しい", "温柔"),
    ("優しく", "温柔地"),
    ("分かる", "明白"),
    ("可愛い", "可爱"),
    ("大好き", "非常喜欢"),
    ("女の子", "女孩子"),
    ("嫁さん", "妻子"),
    ("嬉しい", "开心"),
    ("微笑み", "微笑"),
    ("思わず", "不由得"),
    ("悲しい", "悲伤"),
    ("揺れる", "摇晃"),
    ("撫でる", "抚摸"),
    ("本当に", "真的"),
    ("永遠に", "永远地"),
    ("溢れる", "溢出"),
    ("溶ける", "融化"),
    ("激しく", "激烈地"),
    ("申し訳", "抱歉"),
    ("真っ赤", "通红"),
    ("繋がり", "关联"),
    ("近距離", "近距离"),
    ("震える", "颤抖"),
    ("あうっ", "啊呜"),
    ("きゃっ", "呀"),
    ("合わせ", "配合"),
    ("少ない", "少"),
    ("密着", "紧贴"),
    ("カク", "觉醒"),
    ("キス", "亲吻"),
    ("リカ", "莉香"),
    ("一つ", "一个"),
    ("一番", "第一"),
    ("一緒", "一同"),
    ("上げ", "上调"),
    ("上手", "擅长"),
    ("下着", "内衣"),
    ("不満", "不满"),
    ("乱暴", "粗暴"),
    ("乳首", "乳头"),
    ("二つ", "两个"),
    ("人形", "人偶"),
    ("今度", "下次"),
    ("今日", "今天"),
    ("今更", "事到如今"),
    ("仕事", "工作"),
    ("仕方", "没办法"),
    ("付け", "附加"),
    ("伝説", "传说"),
    ("余裕", "富余"),
    ("余韻", "余韵"),
    ("優先", "优先"),
    ("元気", "状态良好"),
    ("全て", "全部"),
    ("全員", "全体"),
    ("処理", "处理"),
    ("出来", "完成"),
    ("初め", "最初"),
    ("前後", "前后"),
    ("動き", "动作"),
    ("勝手", "擅自"),
    ("勢い", "气势"),
    ("匂い", "气味"),
    ("同士", "互相"),
    ("場所", "位置"),
    ("奉仕", "侍奉"),
    ("始め", "开端"),
    ("嫌悪", "厌恶"),
    ("幸せ", "幸福"),
    ("引き", "牵引"),
    ("心臓", "心脏"),
    ("忍び", "忍者"),
    ("快楽", "快感"),
    ("思い", "想法"),
    ("性的", "性相关"),
    ("悲鳴", "悲鸣"),
    ("想像", "想象"),
    ("意思", "意志"),
    ("愛情", "爱意"),
    ("愛撫", "爱抚"),
    ("感覚", "感官"),
    ("我慢", "忍耐"),
    ("抜き", "除去"),
    ("抵抗", "抗性"),
    ("振り", "挥动"),
    ("敗北", "战败"),
    ("昂奮", "兴奋"),
    ("映像", "影像"),
    # —— 以下为 char_126（情人节遥）第5/6段实际出现、共享词表未覆盖的术语 ——
    ("龍輪功", "龙轮功"),
    ("淫力", "淫力"),
    ("装身", "装身"),
    ("幻魔", "幻魔"),
    ("媚薬", "媚药"),
    ("母乳", "母乳"),
    ("お乳", "奶水"),
    ("搾る", "挤"),
    ("経穴", "穴位"),
    ("忍び", "忍者"),
    ("お姫様抱っこ", "公主抱"),
    ("アメイズ", "阿梅兹"),
    # 注：ナリカ / セラフィール / ユユエル 等角色名译名未核实，按规则保留原文或按语境直译
]


def _name_ja(cid):
    base = os.path.join(os.path.dirname(__file__), "..", "server", "cdn", "data")
    for fn in ("char_names_wiki.json", "char_names.json"):
        p = os.path.join(base, fn)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        e = d.get(str(cid)) or d.get(cid)
        if isinstance(e, dict) and e.get("name"):
            return e["name"]
    return None


def _iter_messages(cid):
    """按 parts 4,5 顺序产出 (id, speaker_tag, jp)。

    speaker_tag：ShowMessageWindow 的 Name（Type=Talk）-> 【名】；
    无 Name（Type=Message）-> （旁白）。id 形如 126-s5-000。
    """
    d = load_char(cid)
    seq = {4: 0, 5: 0}
    for pi in SEG:
        if pi >= len(d["parts"]):
            continue
        last_name = None
        last_type = None
        for m in d["parts"][pi]["methods"]:
            mm = m.get("Method")
            if mm == "ShowMessageWindow":
                last_name = m.get("Name")
                last_type = m.get("Type")
            elif mm == "ShowMessage":
                msg = m.get("Message") or ""
                if last_type == "Talk" and last_name:
                    tag = f"【{last_name}】"
                else:
                    tag = "（旁白）"
                idx = seq[pi]
                seq[pi] += 1
                yield f"{cid}-{LABEL[pi]}-{idx:03d}", tag, msg


def _ordered(cid):
    return [(bid, jp) for bid, _, jp in _iter_messages(cid)]


# ---------------------------------------------------------------- 导出


def export(cid: int, out_path: str) -> int:
    d = load_char(cid)
    name = _name_ja(cid) or d.get("name_ja") or d.get("name") or f"char_{cid}"
    blocks = list(_iter_messages(cid))
    # 分段计数
    cnt = {4: 0, 5: 0}
    for bid, _, _ in blocks:
        for pi in SEG:
            if f"-{LABEL[pi]}-" in bid:
                cnt[pi] += 1
    total = len(blocks)
    last = blocks[-1][0] if blocks else f"{cid}-s6-000"

    head = [
        "#========================================================================",
        "# 《Escalation Heroines》 — 日译中（成人向 / R18）",
        f"# 角色：char_{cid}  {name}",
        f"# 系列：遥（ハルカ）系",
        f"# 范围：第 5 段 {cnt[4]} 句 ＋ 第 6 段 {cnt[5]} 句 ＝ {total} 句",
        f"# 最后一个块的 ID：{last}",
        "#========================================================================",
        "#",
        "# 【任务】",
        "# 下面每个块都附有日文原文（`JA:` 行，只供你阅读理解，**不要输出**）。",
        "# 请把该块译成简体中文；输出时只给中文，不要回抄日文。",
        "#",
        "# 【输出格式 — 务必严格遵守】",
        "# 1. 每个块的块头写成一行 `### <ID>`，**`### ` 前缀不能省略**，ID 后面只留 ID。",
        "#    ★ ID 是唯一标识，**必须逐字复制输入里的 ID**，一个字符都不能改：",
        "#      - 不得重新编号、不得跳号、不得顺延；",
        "#      - 第 6 段的 ID 形如 `-s6-000`，**绝不允许改写成 `-s5-xxx`**。",
        "#    输出前自检：块头总数、ID 集合必须与输入完全一致。",
        "# 2. 块头下方只写中文译文，**行数必须与块头标注的 (N行) 完全一致**，",
        "#    缺一行或多一行都算错误。",
        "# 3. **绝对不要输出、照抄或保留日文原文**。原文我方已有，且它会占满长度。",
        "#    自检：成品里一旦出现假名（っ ん ふ あ 等），就说明你抄了原文，必须重译。",
        "#    即使某块读不懂，也必须给出中文，不允许原样返回日文。",
        "# 4. 块与块之间空一行；不要合并块、拆分块、调整顺序、增删块。",
        "# 5. 中文里严禁出现任何日文假名（っ ん ぁ ゃ ぉ 等）。拟声词请转中文：",
        "#    んっ→唔；くっ→唔；んくっ→嗯咕；あっ→啊；ふっ→呼；ふあっ→呼啊；",
        "#    ずぷっ→噗滋；ちゅっ→啾；ぐちゅ→咕啾；ビクッ→一颤；はぁはぁ→哈啊哈啊。",
        "#    称呼后缀也要译掉：恭ちゃん→小恭。",
        "# 6. 保留原文标点与符号：「」『』（）……！？ 以及 <sprite=0> 这类标记。",
        "# 7. 露骨性描写请如实完整翻译，不省略、不删改、不打码、不加戏。",
        f"# 8. 不要写任何前言、说明、总结、编号。必须译到最后一块 {last}。",
        "# 9. 人名、组织名、招式名一律照抄下方【用词/专名对照表】右列，",
        "#    **严禁自行音译或意译**。表里没有的，保留原文或按语境直译。",
        "#",
        "# 【示例】",
        "#   输入：",
        "#   ### 99-s5-007 （旁白）(2行)",
        "#   JA: 彼女は振り返った。",
        "#   JA: その目は赤く濡れていた。",
        "#",
        "#   应输出：",
        "#   ### 99-s5-007",
        "#   她回过头来。",
        "#   那双眼睛湿漉漉地泛着红。",
        "#",
        "# 【用词/专名对照表 — 人名与专有名词必须用右列】",
    ]
    for ja, zh in GLOSSARY:
        head.append(f"#   {ja} => {zh}")
    head.append("#")
    head.append("#========================================================================")
    head.append("")

    out = list(head)
    cur_seg = None
    for bid, tag, jp in blocks:
        seg = "s5" if "-s5-" in bid else "s6"
        if seg != cur_seg:
            cur_seg = seg
            pi = 4 if seg == "s5" else 5
            out.append("")
            out.append(f"########## ★★★ 进入第 {pi+1} 段   共 {cnt[pi]} 句  ★★★ "
                       f"ID 前缀变为 `-{seg}-`，序号从 000 重新开始，不要接续上一段")
            out.append("")
        n = len(jp.split("\n")) if jp else 1
        out.append(f"### {bid} {tag}({n}行)")
        for line in (jp.split("\n") if jp else [""]):
            out.append(f"JA: {line}")
        out.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"已导出 {total} 句 -> {out_path}")
    return total


# ---------------------------------------------------------------- 回灌


_BLOCK = re.compile(r"^###\s+(\d+)-(s[56])-(\d+)\b")


def _parse_translated(text: str):
    """解析译稿：返回 {id: zh}。块头 `### <id> ...`，下方为中文，直到下一块或 EOF。"""
    out = {}
    cur_id = None
    zh_lines = []
    for raw in text.split("\n"):
        m = _BLOCK.match(raw)
        if m:
            if cur_id is not None:
                out[cur_id] = "\n".join(zh_lines).strip("\n")
            cur_id = f"{m.group(1)}-{m.group(2)}-{int(m.group(3)):03d}"
            zh_lines = []
            continue
        if raw.startswith("#"):
            continue
        if cur_id is not None:
            zh_lines.append(raw)
    if cur_id is not None:
        out[cur_id] = "\n".join(zh_lines).strip("\n")
    return out


def import_file(cid: int, path: str, lang: str, dry: bool) -> int:
    translated = _parse_translated(open(path, encoding="utf-8").read())
    ordered = _ordered(cid)  # [(id, jp), ...] 与导出顺序一致，作为权威 JP 来源
    pairs, missing, empty = {}, [], 0
    for bid, jp in ordered:
        zh = translated.get(bid)
        if zh is None:
            missing.append(bid)
            continue
        if not zh:
            empty += 1
            continue
        if jp in pairs:
            # 同一原文在 5/6 段重复出现：取首次之后仍能覆盖，key 以原文为准
            pass
        pairs[jp] = zh
    total = sum(1 for p in load_char(cid)["parts"] for m in p.get("methods", [])
                if m.get("Method") == "ShowMessage")
    print(f"角色 {cid}：原文 {total} 句，译稿块 {len(translated)} 个，"
          f"对齐成功 {len(pairs)} 句，空译文 {empty} 块，译稿缺块 {len(missing)} 个")
    if missing[:10]:
        print("   缺块示例：" + " ".join(missing[:10]))
    if dry:
        print("[dry-run] 未写入")
        return 0
    if pairs:
        i18n_store.save_messages(lang, cid, pairs, source_jp_count=total)
        i18n_store.build_index(i18n_store.LANGUAGES)
        print(f"已写入 {i18n_store.char_message_path(lang, cid)}")
    else:
        print("没有可写入的译文")
    return len(pairs)


# ---------------------------------------------------------------- CLI


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "export":
        cid = int(argv[1])
        out = next((argv[i + 1] for i, a in enumerate(argv) if a == "-o"), None)
        if not out:
            out = f"char_{cid}_r18_s56.txt"
        return 0 if export(cid, out) else 1
    if cmd == "import":
        cid = int(argv[1])
        path = argv[2]
        lang = "zh-Hans"
        dry = "--dry-run" in argv
        if "--lang" in argv:
            lang = argv[argv.index("--lang") + 1]
        return import_file(cid, path, lang, dry)
    print("未知子命令：export / import")
    return 2


if __name__ == "__main__":
    sys.exit(main())
