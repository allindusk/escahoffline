"""增量更新的各个阶段实现。

每个 stage 只做一件事、可单独重跑、**幂等**（已存在且体积正确的产物直接跳过）。
上层编排见 :mod:`pipeline.cli`。

关于"要不要改旧脚本"的取舍
--------------------------
本模块对新写的能力采用**新实现**，对重活采用**委派子进程**（委派意味着零行为变更）：

=====================  ==========================================
能力                   实现
=====================  ==========================================
剧本提取（单角色）     新实现（旧版会全量重写所有 char_*.js）
语音导出（单角色）     新实现（逻辑来自 export_audio_exact.py）
图片导出（按需）       新实现（旧版按 bundle 全量跑）
立绘合成               委派 ``compose_stand_all.py``（重且有落点推导）
imgmap / sfxmap        委派 ``gen_imgmap.py`` + 新写 sfxmap.js 转换
角色名 / 元数据        委派 ``extract_names.py`` + ``gen_charmeta.py``
=====================  ==========================================
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time

from .. import proc

from . import select
from .config import (ARCHIVE_DIR, AUDIO, DATA, FILES_AUDIO, FILES_IMG,
                     FILES_STORY, IMG, IMG_X2, ROOT, SCRIPTS, WEB, WS,
                     add_scripts_to_path)
from .net import _log, fetch_many

add_scripts_to_path()          # 之后才能 import unityfs_probe / parse_script

# 下面两个脚本的顶层会 import lz4 / UnityPy。**不能让它们的缺失阻止服务启动** ——
# 否则用户没装依赖时双击 启动.bat 直接闪退，既看不到「自动安装依赖」按钮，
# 也不知道缺什么。所以这里兜住：真正调用时才抛一条能看懂的错。
try:
    from parse_script import extract_jsons      # noqa: E402  (在 _scripts 目录下)
except Exception as _e:                          # noqa: BLE001
    def extract_jsons(*a, **k):
        raise RuntimeError(f"解脚本组件不可用（{_e}）：pip install UnityPy")

try:
    from unityfs_probe import parse_unityfs     # noqa: E402
except Exception as _e:                          # noqa: BLE001
    def parse_unityfs(*a, **k):
        raise RuntimeError(f"UnityFS 解析组件不可用（{_e}）：pip install lz4")

# ================================================================ 通用工具


def fetch(entries, workers: int = 48, desc: str = "bundle",
          skip: bool = True, pause_event=None, job=None,
          done_offset: int = 0, count_skipped: bool = True) -> dict:
    """下载一批资源包（``entries`` 是清单条目 dict 列表）。默认跳过已完成的。

    ``pause_event`` 由任务传入，用来支持「暂停 / 继续」。
    ``count_skipped`` 见 :func:`server.cdn.net.fetch_many`。
    """
    return fetch_many(entries, workers=workers, desc=desc, skip_existing=skip,
                      pause_event=pause_event, job=job,
                      done_offset=done_offset, count_skipped=count_skipped)


def read_bundle(path: str) -> bytes:
    """读一个已下载的 UnityFS 包。"""
    with open(path, "rb") as f:
        return f.read()


def run_script(name: str, *args, cwd: str = WS, quiet: bool = False):
    """以子进程方式跑旧脚本，cwd 固定为 ``userdata/``。

    为什么是 ``userdata`` 而不是项目根：这些旧脚本里写的是相对路径
    ``../web/...`` 与 ``files_img`` / ``files_audio`` / ``char_story/files``。
    让 cwd 成为项目根的**直接子目录**，``../web`` 正好落到 ``<根>/web``，
    其余相对目录也正好落在 ``userdata/`` 下的缓存区 —— 于是 5 个旧脚本
    一行都不用改（这是迁移时特意保留的约定）。

    子进程强制 UTF-8：这些旧脚本里的 ``print(日文)`` 在 GBK 控制台下会抛
    ``UnicodeEncodeError``（而且往往发生在"写文件之后、打印报告时"，
    exit code 一坏，上层就会误判整步失败）。设 ``PYTHONUTF8=1`` 一并解决。
    """
    cmd = [sys.executable, os.path.join(SCRIPTS, name), *[str(a) for a in args]]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if quiet:
        # 走 proc.run：Windows 上不弹黑窗（否则装一部剧情会闪好几次命令行）
        p = proc.run(cmd, cwd=cwd, capture_output=True, text=True,
                     encoding="utf-8", errors="replace", env=env)
        print(f"  [{name}] exit={p.returncode}")
        lines = (p.stdout or "").strip().splitlines()
        for ln in lines[-8:]:
            print("      " + ln)
        # **必须落盘**：这些脚本打出来的正是"用了哪个包 / 缺哪一层 / 落点来源"
        # 之类的关键决策，只 print 到控制台等于没记录（GUI 用户看不到控制台，
        # 出事时只有 app.log 可查）。
        try:
            from .. import log as applog
            tag = "stand" if name.startswith("compose") else name.replace(".py", "")[:10]
            applog.log(f"  [{name}] exit={p.returncode}", tag=tag)
            for ln in lines[:200]:
                applog.log("    " + ln, tag=tag)
            if len(lines) > 200:
                applog.log(f"    …（其余 {len(lines) - 200} 行略）", tag=tag)
        except Exception:                                      # noqa: BLE001
            pass
        if p.returncode != 0 and p.stderr:
            print("      stderr: " + p.stderr.strip()[-400:])
            try:
                from .. import log as applog
                applog.log("    stderr: " + p.stderr.strip()[-1500:], tag="script")
            except Exception:                                  # noqa: BLE001
                pass
        return p.returncode
    return proc.run(cmd, cwd=cwd, env=env).returncode


# ================================================================ 剧本


def _parts_from_bundle(path: str) -> list:
    """把剧本包的 adv JSON 数组解成播放器用的 ``parts``。

    **角色剧情与主线/活动/其他剧情共用这一份实现**：实测三类包内的 JSON
    schema 完全相同（FileName/StandImages/BackImages/Voices/Methods），
    只有 FileName 前缀不同（``adv_char_<cid>_<n>`` vs ``adv_<kind>_<no>_<n>``）。
    """
    payload, *_ = parse_unityfs(open(path, "rb").read())
    parts = []
    for o in extract_jsons(payload):
        methods = []
        for m in o["Methods"]:
            # 保留全部演出指令（时序必需），只剔除空字符串字段压缩体积
            methods.append({k: v for k, v in m.items() if v != ""})
        parts.append({
            "file": o["FileName"],
            "stand": o.get("StandImages", []),
            "full": o.get("StandFullImages", []),
            "back": o.get("BackImages", []),
            "cg": o.get("EventImages", []),
            "music": o.get("Musics", []),
            "sound": o.get("Sounds", []),
            "voices": o.get("Voices", []),
            "methods": methods,
        })
    parts.sort(key=lambda p: int(p["file"].rsplit("_", 1)[1]))
    return parts


def _count_lines(parts: list) -> int:
    return sum(1 for p in parts for m in p["methods"] if m.get("Method") == "ShowMessage")


def extract_scenario(cid, force: bool = False) -> str | None:
    """从已下载的剧本包解出精简 JSON，写 ``web/data/char_<id>.js``。

    与 ``extract_scripts.py`` 完全等价的输出结构（连 key 顺序都一致），
    只是加了单角色粒度——旧脚本每次都会把 383 个角色的文件全部重写一遍，
    那样会把本地改过的东西冲掉，不能用在增量链路里。
    """
    src = os.path.join(FILES_STORY, f"bundle_scenarioscript_char_{cid}")
    if not os.path.exists(src):
        print(f"  缺少剧本包：{src}")
        return None
    out_path = os.path.join(DATA, f"char_{cid}.js")
    if os.path.exists(out_path) and not force:
        print(f"  已存在 {os.path.basename(out_path)}（--force 可重解）")
        return out_path

    parts = _parts_from_bundle(src)
    data = {"id": int(cid), "parts": parts}
    select.dump_char(cid, data)
    print(f"  char_{cid}.js：{len(parts)} 段 / {_count_lines(parts)} 句")
    return out_path


def extract_adv(kind: str, no, force: bool = False) -> str | None:
    """解非角色剧情 -> ``web/data/adv_<kind>[_<no>].js``。

    tutorial / op 是**单个整包**（``no=None``，包名不带编号，包内分别 28 / 5 段），
    其余类型按编号一部一包。
    输出结构与角色剧本**同构**（都有 ``parts[].methods``），播放器因此不需要
    第二套渲染逻辑；额外多出 ``id/kind/no`` 三个自描述字段，便于列表与译文命名。
    """
    key = select.story_key(kind, no)
    name = (f"bundle_scenarioscript_{kind}" if no is None
            else f"bundle_scenarioscript_{kind}_{no}")
    src = os.path.join(FILES_STORY, name)
    if not os.path.exists(src):
        print(f"  缺少剧本包：{src}")
        return None
    out_path = os.path.join(DATA, key + ".js")
    if os.path.exists(out_path) and not force:
        print(f"  已存在 {os.path.basename(out_path)}（--force 可重解）")
        return out_path

    parts = _parts_from_bundle(src)
    data = {"id": key, "kind": kind, "no": no, "parts": parts}
    select.dump_story(key, data)
    print(f"  {key}.js：{len(parts)} 段 / {_count_lines(parts)} 句")
    return out_path


def _first_line(parts: list, limit: int = 34) -> str:
    """取第一句「像台词」的话，做列表副标题（跳过「……」这类纯标点）。"""
    for p in parts[:3]:
        for m in p["methods"]:
            if m.get("Method") != "ShowMessage":
                continue
            s = " ".join(str(m.get("Message") or "").split())
            if len(s) < 5:
                continue
            if not any(ch.isalnum() for ch in s):
                continue
            return s[:limit] + ("…" if len(s) > limit else "")
    return ""


#: 主线三部的「序章」= ``bundle_scenarioscript_op`` 里的某几段（没有独立剧本包）。
#: 用**首句**定位段号，官方以后追加段也不会错位。
#: 首句来源：用户在游戏里逐部核对过的三句。
PROLOGUES = [
    ("0", "第1部 序章", 0.0, "トキサダ……トキサダ"),
    ("0b", "第2部 序章", 23.5, "はぁっ、はぁっ"),
    ("0c", "第3部 序章", 44.5, "あの激しい戦いから"),
]


#: ``_0`` 段里的「〇名札（角色）」说话人前缀 —— 标题显示时要剥掉
NAME_TAG_RE = re.compile(r"^[〇○]?名札（[^）]{1,24}）\s*")


def _squash(s: str) -> str:
    """去掉引号与空白，只用于「这几句是不是同一个标题的分解」的判断。"""
    return re.sub(r"[「」『』\s]", "", s)


#: 「UTF-8 被当 cp932 读」的乱码特征字（正常日文里几乎不会连出现）
MOJIBAKE_RE = re.compile(r"[縲繧繝繿譁隸隹]")


def _repair_mojibake(s: str) -> str:
    """官方数据里个别标题行是双重编码的乱码，能还原就还原，还原不了给空串。

    实测只有**两处**（``adv_main_47_0`` / ``adv_event_95_0`` 的标题行），其余
    2000+ 句正文都正常，属于官方自己的录入问题，不是解析错。原始字节丢了、
    还原出来还带 ``?`` 的，就返回空串让调用方回退（这两条的标题已在
    ``data/story_titles.json`` 里按封面横幅重新识读）。
    """
    if not MOJIBAKE_RE.search(s):
        return s
    try:
        fixed = s.encode("cp932").decode("utf-8", "replace")
    except Exception:                                          # noqa: BLE001
        return ""
    if "\ufffd" in fixed or "?" in fixed:
        return ""
    return fixed


def _part_title(parts: list) -> str:
    """``_0`` 段（标题段）里的文本就是**章节/活动的标题**。

    三条实测事实（都拿原始剧本包核对过）：

    1. 剧本 JSON 只有 9 个字段（FileName / 各类 Images / Methods…），**没有
       title 字段** —— 文字标题只可能来自 ``_0`` 段里 ``ShowMessage`` 的那句。
    2. 大量包**根本没有** ``_0`` 段（如 ``bundle_scenarioscript_event_41`` 里只有
       ``_1``..``_8``），或者 ``_0`` 段存在但 ``Methods`` 为空（``main_24_0``）。
       这些章节的标题在官方数据里是**图片**（活动 logo / 章节横幅 / 剧本内
       ``ShowStand`` 的 ``GroupId="タイトルロゴ"``），没有任何可读文本，
       不是提取漏了 —— 拿不出来就是拿不出来。
    3. 有 ``_0`` 段的包里台词**不一定只有一句**：有的标题由多名角色各念半句
       （「魔女が西向きゃ」「尾は東」），最后一句是拼好的完整标题；有的带
       「〇名札（角色）」前缀（event_79）。以前要求 ``== 1`` 句，这类标题被整体
       丢掉，于是侧栏退回「活动剧情 第N话」。

    例：``adv_main_1_0`` = 「超昂戦士、始動！」、``adv_event_1_0`` = 「別次元から来たあのこ」。
    """
    if len(parts) < 2:          # 单段包：那段是正文，不是标题段
        return ""
    p0 = next((p for p in parts if str(p.get("file", "")).endswith("_0")), None)
    if not p0:
        return ""
    raw = [" ".join(str(m.get("Message") or "").split())
           for m in p0.get("methods") or [] if m.get("Method") == "ShowMessage"]
    uniq = []
    for msg in raw:
        t = _repair_mojibake(NAME_TAG_RE.sub("", msg).strip())
        if len(t) >= 2 and t not in uniq:
            uniq.append(t)
    if not uniq:
        return ""
    if len(uniq) > 1:
        # 只接受「拆句」：最长那句必须把其它几句都包含进去（拼好的完整标题），
        # 否则就是正文（比如 adv_other_1_0 是 26 句人気投票说明），不能当标题。
        longest = max(uniq, key=len)
        flat = _squash(longest)
        if len(uniq) > 3 or len(longest) > 60 or \
                any(_squash(u) not in flat for u in uniq if u is not longest):
            return ""
        uniq = [longest]
    # 「A」「B」→ A B：多人各念半句的标题拼起来要留个分隔，别粘成一坨
    return re.sub(r"」\s*「", " ", uniq[0]).strip("「」\"' ")


def load_story_titles() -> dict:
    """读「封面横幅上识读出来的标题」表（``data/story_titles.json``）。

    为什么需要这张表：官方剧本包里没有 title 字段，很多章节连 ``_0`` 标题段都没有，
    标题只存在于封面横幅图的美术字里（见 :func:`_part_title`）。识读结果放在这里，
    与剧本包解耦 —— 改标题不用重新下载/解析资源。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "story_titles.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("titles") or {}
    except Exception:                                          # noqa: BLE001
        return {}


def sync_advindex() -> int:
    """把已接入的非角色剧情汇总成 ``web/data/advindex.js``（侧栏列表数据源）。"""
    titles = load_story_titles()
    rows = []
    for fn in os.listdir(DATA):
        if not select.PAT_ADV_FILE.fullmatch(fn):
            continue
        key = fn[:-3]
        try:
            d = select.load_story(key)
        except Exception:
            continue
        vdir = os.path.join(AUDIO, key)
        kind = d.get("kind") or key.split("_")[1]
        n = d.get("no")
        # 空占位包（例如 event_51 / event_52：2 段、0 句，官方还没放内容，
        # 所以也没有 topic_event 横幅）不要进列表 —— 否则就是一行点不开的空条目。
        if _count_lines(d["parts"]) == 0:
            continue
        # 封面：章节标题横幅（游戏里标题本身就是图，没有文本资源）
        # 不用 int(n)：序章拆出来的编号是 0 / 0b / 0c，对应 main_0 / main_0b / main_0c.webp
        cover = ""
        if n is not None:
            rel = f"media/img/story/{kind}_{n}.webp"
            if os.path.exists(os.path.join(WEB, rel)):
                cover = rel
        rows.append({
            "key": key,
            "kind": kind,
            # tutorial / op 无编号，no = None（前端不显示「第X话」）
            "no": n,
            # 优先级：剧本自带的 title（序章这类）> 封面图识读表 > ``_0`` 段台词。
            # 识读表放中间：它只在剧本完全没有标题时补位，不会盖掉官方文本。
            "title": d.get("title") or titles.get(key) or _part_title(d["parts"]),
            # 排序用：序章插在部与部之间（0 / 23.5 / 44.5），普通章节就是章号
            "order": d.get("order") if d.get("order") is not None else (
                n if isinstance(n, (int, float)) else 0),
            "cover": cover,
            "first": _first_line(d["parts"]),
            "parts": len(d["parts"]),
            "lines": _count_lines(d["parts"]),
            # voices = 剧本引用数；voiceRes = 本地实际可播的语音文件数。
            # 主线/活动目前拿不到语音资源（见 README），播放器据 voiceRes 决定
            # 要不要发 audio 请求，避免每句都 404。
            "voices": sum(len(p.get("voices") or []) for p in d["parts"]),
            "voiceRes": len([f for f in os.listdir(vdir) if f.endswith(".m4a")])
            if os.path.isdir(vdir) else 0,
        })
    def _ord(r):
        v = r.get("order")
        return v if isinstance(v, (int, float)) else 0

    # ---- 主线三部的「序章」：不是独立剧本包，而是 op 包里的三段 ----
    # 以前我把它们拆成 adv_main_0 / 0b / 0c 三个独立数据文件，结果用户那边
    # **永远显示"未下载"且资源管理里也装不了**（清单里根本没有这个单元）。
    # 现在改成"指向 adv_op 的第 i 段"，状态与资源都跟着 adv_op 走。
    op = os.path.join(DATA, "adv_op.js")
    if os.path.exists(op):
        try:
            opd = select.load_story("adv_op")
            parts = opd["parts"]
            vdir = os.path.join(AUDIO, "adv_op")
            for no, title, order, needle in PROLOGUES:
                i = next((k for k, p in enumerate(parts)
                          if needle in _first_line([p], 60)), None)
                if i is None:
                    continue
                cover = f"media/img/story/main_{no}.webp"
                rows.append({
                    "key": "adv_op",
                    "kind": "main",
                    "no": no,
                    "part": i,                      # 从 op 包的第 i 段开始播
                    "title": title,
                    "order": order,
                    "cover": cover if os.path.exists(os.path.join(WEB, cover)) else "",
                    "first": _first_line([parts[i]]),
                    "parts": 1,
                    "lines": _count_lines([parts[i]]),
                    "voices": sum(len(m.get("voices") or []) for m in parts[i]["methods"]),
                    "voiceRes": len([f for f in os.listdir(vdir) if f.endswith(".m4a")])
                    if os.path.isdir(vdir) else 0,
                })
        except Exception:                                   # noqa: BLE001
            pass

    rows.sort(key=lambda r: (select.ADV_KINDS.index(r["kind"])
                             if r["kind"] in select.ADV_KINDS else 9, _ord(r)))
    p = os.path.join(DATA, "advindex.js")
    with open(p, "w", encoding="utf-8") as f:
        f.write("window.__ADV=" + json.dumps(rows, ensure_ascii=False,
                                             separators=(",", ":")) + ";")
    print(f"  advindex.js：{len(rows)} 部剧情")
    return len(rows)


def _hex_label(seg: str) -> str:
    """CG 文件名里的十六进制段 → 人类可读标签。

    ``83438378`` 是按 Shift-JIS 编码的字节串（= ``イベ``），``3032`` 是 ASCII
    （= ``02``）。CG 页卡片角上显示的「イベ_02」就是这么来的。
    """
    try:
        return bytes.fromhex(seg).decode("cp932", "replace")
    except ValueError:
        return seg


#: 占位转场图（与 build_cgmap.py 一致），不属于任何角色的画廊。
PLACEHOLDER = {"暗転", "黒画面", "黒", "暗転（黒）"}


def sync_cgmap(cid: int) -> int:
    """把角色剧本里引用、而 ``cgmap.js`` 里还没有的 CG 组合并进去。

    为什么需要它：``cgmap.js`` 以前只在打包时由旧工作区生成（仓库里没有生成器），
    所以官方上了新角色后，即使剧情装了、图也解出来了，CG 页（读 cgmap.js）里
    仍然是空的 —— 只能等下一次发版。这里按"剧本引用了哪些 CG"增量合并，
    **只加不删、可重复跑**（老条目原样保留，连顺序都不动）。

    分组规则与出厂数据一致：key ``イベ_02_００`` 拆成组标签 ``イベ_02``，
    文件名是整串 + ``.jpg``。
    """
    try:
        assets = select.collect_assets(select.load_char(cid))
    except Exception as e:                                     # noqa: BLE001
        print(f"  cgmap 跳过：读不到 char_{cid}（{e}）")
        return 0
    keys = sorted(assets.get("cg") or ())
    if not keys:
        return 0

    path = os.path.join(DATA, "cgmap.js")
    groups: list = []
    if os.path.exists(path):
        raw = open(path, encoding="utf-8-sig").read()
        try:
            groups = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
        except Exception:                                      # noqa: BLE001
            groups = []
    have = {(g.get("c"), g.get("k")) for g in groups}

    # ① 自愈：同一角色内，若某组的文件被**其它组完全覆盖**（超集组），删掉它。
    #    旧版本这里按"前两段"分组，会把 イベ_0126_純愛 / イベ_0126_陵辱 并成
    #    一个大组 イベ_0126（12 张），CG 页于是显示 3 组、其中一组与另两组重复。
    mine = [g for g in groups if g.get("c") == int(cid)]
    dropped = 0
    if len(mine) > 1:
        drop = set()
        for a in mine:
            fa = set(a.get("f") or [])
            if not fa:
                continue
            # 只跟**更小的组**比：大组被几个小组的并集覆盖才算重复。
            # 反过来会误杀子组（純愛/陵辱 都会被那个 12 张的合集组"覆盖"）。
            others: set = set()
            for b in mine:
                fb = set(b.get("f") or [])
                if b is not a and len(fb) < len(fa):
                    others |= fb
            if fa <= others:                     # 全部文件别处都有 → 重复合集组
                drop.add(id(a))
        if drop:
            groups = [g for g in groups if id(g) not in drop]
            have = {(g.get("c"), g.get("k")) for g in groups}
            dropped = len(drop)
            print(f"  cgmap.js：清理 {dropped} 个重复组（角色 {cid}）")

    # ② 分组规则与出厂数据一致：**去掉最后一段**（表情/序号），保留前面的组名。
    #    以前取前两段，会把带后缀的子组并成一个合集组。
    covered: set = set()
    for g in groups:
        if g.get("c") == int(cid):
            covered.update(g.get("f") or [])
    by_group: dict[str, list[str]] = {}
    for k in keys:
        fn = k + ".jpg"
        if fn in covered:                        # 已经被某组收录 → 不重复加
            continue
        seg = k.split("_")
        # 与出厂生成器（build_cgmap.py）一致：只收**本角色名下**的图。
        # 段数不足 3（如 83438378_黒画面 这种共享转场图）或第 2 段不是数字
        # （无归属角色）的键不属于任何角色，跳过；否则会生成「以文件名当组名」
        # 的幽灵组（黒画面 被 259 个角色剧本引用，每同步一次就污染一个角色）。
        if len(seg) < 3 or not seg[1].isdigit():
            continue
        if any(x in PLACEHOLDER for x in seg):
            continue
        label = "_".join(_hex_label(s) for s in seg[:-1])
        by_group.setdefault(label, []).append(fn)

    added = 0
    for label in sorted(by_group):
        if (int(cid), label) in have:
            continue
        groups.append({"c": int(cid), "k": label, "f": by_group[label],
                       "n": len(by_group[label])})
        added += 1
    # 注意：**只清理没新增也要写回**，否则自愈结果留在内存里，文件还是脏的
    if added or dropped:
        with open(path, "w", encoding="utf-8") as f:
            f.write("window.__CGMAP=" + json.dumps(groups, ensure_ascii=False,
                                                   separators=(",", ":")) + ";")
        print(f"  cgmap.js：合并 {added} 组、清理 {dropped} 组（角色 {cid}）")
    return added


def sync_index(force: bool = False) -> int:
    """把 ``char_*.js`` 的现状合并进 ``web/data/index.js``。

    旧脚本是全量重建（会丢掉磁盘上已有的条目）；这里是按 id merge，
    所以只跑单个角色也不会把别人删掉。
    """
    idx_path = os.path.join(DATA, "index.js")
    index = {}
    if os.path.exists(idx_path):
        raw = open(idx_path, encoding="utf-8").read()
        for e in json.loads(raw[raw.index("["):raw.rindex("]") + 1]):
            index[e["id"]] = e

    changed = 0
    for fn in os.listdir(DATA):
        if not fn.startswith("char_") or not fn.endswith(".js"):
            continue
        try:
            cid = int(fn[5:-3])
            d = select.load_char(cid)
        except Exception:
            continue
        entry = {
            "id": cid,
            "parts": len(d["parts"]),
            "lines": sum(1 for p in d["parts"] for m in p["methods"]
                         if m.get("Method") == "ShowMessage"),
            "voices": sum(len(p.get("voices") or []) for p in d["parts"]),
        }
        if force or index.get(cid) != entry:
            index[cid] = entry
            changed += 1

    entries = [index[k] for k in sorted(index)]
    with open(idx_path, "w", encoding="utf-8") as f:
        f.write("window.__INDEX=" + json.dumps(entries, ensure_ascii=False,
                                               separators=(",", ":")) + ";")
    print(f"  index.js：{len(entries)} 个角色（更新 {changed} 条）")
    return len(entries)


# ================================================================ 语音


def extract_clips(path: str, out_dir: str, stat: dict, dry: bool = False) -> None:
    """把一个语音包里的 AudioClip 按 ``m_Name`` 精确写进 ``out_dir``。

    **绝对不要按顺序 zip**：包内 AudioClip 的顺序和剧本 Voices 的顺序完全不同，
    按序切会让每句台词配上别的台词的声音（12.7 万条全错位的事故）。
    """
    import UnityPy

    try:
        env = UnityPy.load(path)
        clips = []
        for obj in env.objects:
            if obj.type.name == "AudioClip":
                d = obj.read()
                r = d.m_Resource
                clips.append((d.m_Name, int(r.m_Offset), int(r.m_Size)))
        if not clips:
            return
        payload, nodes, *_ = parse_unityfs(open(path, "rb").read())
        res = None
        for off, sz, _nf, nm in nodes:
            if nm.endswith(".resource"):
                res = payload[off:off + sz]
                break
        if res is None:
            return
        for name, off, size in clips:
            p = os.path.join(out_dir, name + ".m4a")
            if os.path.exists(p) and os.path.getsize(p) == size:
                stat["skip"] += 1
                continue
            if dry:
                stat["new"] += 1
                continue
            with open(p, "wb") as f:
                f.write(res[off:off + size])
            stat["new"] += 1
            stat["bytes"] += size
    except Exception as e:
        stat["fail"] += 1
        print(f"    {os.path.basename(path)} 失败: {e}")


def export_voices(cid, workers: int = 32, dry: bool = False) -> dict:
    """导出角色剧情语音（``bundle_voice_adv_char_<cid>_<seg>``）-> ``audio/<cid>/``。"""
    out_dir = os.path.join(AUDIO, str(cid))
    os.makedirs(out_dir, exist_ok=True)
    stat = {"new": 0, "skip": 0, "fail": 0, "bytes": 0}
    for seg in range(12):
        path = os.path.join(FILES_STORY, f"bundle_voice_adv_char_{cid}_{seg}")
        if os.path.exists(path):
            extract_clips(path, out_dir, stat, dry)
    print(f"  语音 {cid}：新增 {stat['new']} 跳过 {stat['skip']} 失败 {stat['fail']} "
          f"{stat['bytes'] / 1048576:.1f} MB")
    return stat


def export_adv_voices(key: str, bundles: list, dry: bool = False) -> dict:
    """导出非角色剧情语音 -> ``audio/<story_key>/``（main_1 / event_3 / op …）。

    实测主线/活动的语音就在 ``bundle_voice_adv_{kind}_<n>_<seg>`` 里，
    段号与剧本 parts 一一对应（main_1 有 9 段 = 9 个语音包，570 个 clip 里
    568 个正好是剧本引用的名字）。
    """
    out_dir = os.path.join(AUDIO, key)
    os.makedirs(out_dir, exist_ok=True)
    stat = {"new": 0, "skip": 0, "fail": 0, "bytes": 0}
    for name in bundles:
        path = os.path.join(FILES_STORY, name)
        if os.path.exists(path):
            extract_clips(path, out_dir, stat, dry)
    print(f"  语音 {key}：{len(bundles)} 个包，新增 {stat['new']} 跳过 {stat['skip']} "
          f"失败 {stat['fail']} {stat['bytes'] / 1048576:.1f} MB")
    return stat


# ================================================================ 图片


def export_images(mf: select.Manifest, ids: dict, workers: int = 32) -> dict:
    """按需导出背景/CG/分层立绘部件。

    ``ids`` 是 :func:`pipeline.select.collect_assets` 的输出。调用方负责先下载包。
    """
    import UnityPy

    bybundle, miss = {}, []    # back
    for s in ids["back"]:
        b = mf.back_bundle(s)
        if not b:
            miss.append(("back", s))
            continue
        bybundle.setdefault(b, {})[select.norm(s)] = f"back/{s}.png"
    # cg
    for s in ids["cg"]:
        b = mf.cg_bundle(s)
        if not b:
            miss.append(("cg", s))
            continue
        np_ = select.norm(s).split("_")
        np_[0] = select.strip_prefix(np_[0])
        bybundle.setdefault(b, {})["_".join(np_)] = f"cg/{s}.jpg"
    # stand（分层部件 base）
    for s in ids["stand"]:
        m = select.PAT_STAND_KEY.match(s)
        if not m:
            miss.append(("stand", s))
            continue
        b = mf.stand_bundle(s)
        if not b:
            miss.append(("stand", s))
            continue
        base = f"stand_{m.group(1)}_{m.group(2).upper()}_base"
        bybundle.setdefault(b, {})[select.norm(base)] = f"stand/{base}.png"

    for sub in ("back", "cg", "stand"):
        os.makedirs(os.path.join(IMG, sub), exist_ok=True)

    n_new = n_skip = n_fail = 0
    for bname, wanted in sorted(bybundle.items()):
        path = os.path.join(FILES_IMG, bname)
        if not os.path.exists(path):
            n_fail += len(wanted)
            continue
        try:
            env = UnityPy.load(path)
        except Exception:
            n_fail += len(wanted)
            continue
        got = set()
        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            try:
                sp = obj.read()
                nm = select.norm(sp.m_Name)
                p = nm.split("_")
                p[0] = select.strip_prefix(p[0])
                nm2 = "_".join(p)
                nm = nm2 if nm2 in wanted else nm
                if nm not in wanted:
                    continue
                out = os.path.join(IMG, wanted[nm])
                if os.path.exists(out):
                    got.add(nm)
                    n_skip += 1
                    continue
                img = sp.image
                if out.endswith(".jpg"):
                    img.convert("RGB").save(out, quality=88, optimize=True)
                else:
                    img.save(out, optimize=True)
                got.add(nm)
                n_new += 1
            except Exception:
                continue
        n_fail += len(wanted) - len(got)
    print(f"  图片导出：新增 {n_new} 跳过 {n_skip} 失败 {n_fail} "
          f"（无 bundle 映射 {len(miss)}）")
    for k, s in miss[:8]:
        print(f"     缺 [{k}] {s}")
    return {"new": n_new, "skip": n_skip, "fail": n_fail, "miss": miss}


def export_standfull(mf: select.Manifest, nums, workers: int = 24) -> dict:
    """导出复数前缀 ``bundle_standimages_stand_<num>`` 的完整立绘（*base 的兜底）。"""
    import UnityPy

    out_dir = os.path.join(IMG, "standfull")
    os.makedirs(out_dir, exist_ok=True)
    n_new = n_skip = n_fail = 0
    for num in sorted(nums):
        b = mf.stand_full.get(num)
        out = os.path.join(out_dir, f"{num}.png")
        if os.path.exists(out):
            n_skip += 1
            continue
        if not b:
            # 有些编号只有分层部件包、没有完整立绘包，属正常情况
            n_skip += 1
            continue
        path = os.path.join(FILES_IMG, b)
        if not os.path.exists(path):
            n_fail += 1
            continue
        try:
            env = UnityPy.load(path)
            for obj in env.objects:
                if obj.type.name == "Sprite":
                    obj.read().image.save(out, optimize=True)
                    n_new += 1
                    break
            else:
                n_fail += 1
        except Exception:
            n_fail += 1
    print(f"  standfull：新增 {n_new} 跳过 {n_skip} 失败 {n_fail}")
    return {"new": n_new, "skip": n_skip, "fail": n_fail}


STORY_TOPIC_PKGS = ("bundle_image_story_main", "bundle_image_story_main2",
                    "bundle_image_story_main3", "bundle_image_story_event",
                    "bundle_image_story_etc")


def export_story_topics() -> dict:
    """导出章节标题横幅图 -> ``media/img/story/<kind>_<no>.webp``。

    游戏里主线/活动/其他的**章节标题本身是图片**（`topic_main_1`、
    `topic_event_001` 等 Sprite），没有对应的文本资源——所以网页上的剧情列表
    想显示封面只能用这些图。`main` / `main2` / `main3` 是同一分类的资源分包
    （编号 0–23 / 24–44 / 45–61 连续），导出时按 `topic_` 后面的名字归一。
    """
    import re
    import UnityPy

    out_dir = os.path.join(IMG, "story")
    os.makedirs(out_dir, exist_ok=True)
    # 包内 Sprite 名是 topic_chapter_<n>（主线）/ topic_event_<n> / topic_etc_<n>，
    # 统一落成 <剧情 kind>_<no>.webp，与 adv key 对齐（chapter -> main）
    name_map = {"chapter": "main", "event": "event", "etc": "etc"}
    n_new = n_skip = 0
    for bname in STORY_TOPIC_PKGS:
        path = os.path.join(FILES_IMG, bname)
        if not os.path.exists(path):
            continue
        try:
            env = UnityPy.load(path)
        except Exception:
            continue
        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            try:
                sp = obj.read()
                # 编号后面可能带字母后缀：topic_chapter_000 / 000b / 000c
                # 分别是第 1/2/3 部的「序章」横幅（游戏里确实有三张）。
                # 旧正则只认 \d+，把 000b/000c 当成不匹配直接丢掉，
                # 于是"第二三部序章横幅找不到"。
                m = re.fullmatch(r"topic_([a-z]+)_(\d+)([a-z]?)", sp.m_Name or "")
                if not m:
                    continue
                base = name_map.get(m.group(1), m.group(1))
                num = f"{int(m.group(2))}{m.group(3)}"
                dst = os.path.join(out_dir, f"{base}_{num}.webp")
                if os.path.exists(dst):
                    n_skip += 1
                    continue
                sp.image.save(dst, "WEBP", quality=88, method=4)
                n_new += 1
            except Exception:
                continue
    print(f"  剧情封面：新增 {n_new} 跳过 {n_skip}")
    return {"new": n_new, "skip": n_skip}


def export_char_banners(only=None) -> dict:
    """导出角色剧情的"横幅"：``episode_character_<cid>`` → ``media/img/story/char_<cid>.webp``。

    为什么有这一节：主线/活动/其他在游戏里都有章节横幅图（topic_*），
    角色剧情没有文字横幅，但有 ``bundle_episodeicons_episode_character_<cid>``
    —— 游戏「角色剧情」入口上那张角色图。播放器要"用横幅当标题"，
    角色剧情只能用这张，所以统一落到 ``img/story/`` 下、与 adv 封面同一目录。

    包名是 **5 位补零**（``episode_character_00303``），别直接拼 cid。
    """
    import UnityPy

    out_dir = os.path.join(IMG, "story")
    os.makedirs(out_dir, exist_ok=True)
    names = [n for n in os.listdir(FILES_STORY)
             if n.startswith("bundle_episodeicons_episode_character_")]
    if only:
        want = {f"{int(c):05d}" for c in only}
        names = [n for n in names if n.rsplit("_", 1)[1] in want]
    n_new = n_skip = n_fail = 0
    for bname in sorted(names):
        try:
            cid = int(bname.rsplit("_", 1)[1])
        except ValueError:
            continue
        dst = os.path.join(out_dir, f"char_{cid}.webp")
        if os.path.exists(dst):
            n_skip += 1
            continue
        try:
            env = UnityPy.load(os.path.join(FILES_STORY, bname))
            for obj in env.objects:
                if obj.type.name != "Sprite":
                    continue
                obj.read().image.save(dst, "WEBP", quality=90, method=4)
                n_new += 1
                break
            else:
                n_fail += 1
        except Exception:                                   # noqa: BLE001
            n_fail += 1
    print(f"  角色横幅：新增 {n_new} 跳过 {n_skip} 失败 {n_fail}")
    return {"new": n_new, "skip": n_skip, "fail": n_fail}


def export_avatar(cid, size: int = 96, force: bool = False,
                  x2: bool = True) -> bool:
    """从 c-panel 半身像左上角裁正方形做头像 -> ``web/media/img/avatar/<cid>.png``。

    ``force``：即使已存在也重做。**必须支持强刷**——旧版本导出的头像里
    有整批错图（13/61/70 三个 id 字节完全相同、413 是别人的脸），
    用户点了「补全」如果按"已存在就跳过"，永远是错的那张。
    ``x2``：顺带把 ``media/img_x2/avatar/<cid>.webp``（256px）也重做，
    否则播放器优先读超分层，仍显示旧图。
    """
    from PIL import Image
    import UnityPy

    # **包名是补零的**：清单里叫 bundle_chara_c-panel_002，而 cid 是 2。
    # 直接拼不补零的名字会永远找不到源文件（和立绘合成脚本同一个坑），
    # 于是头像一个都导不出来 → 侧栏 384 个角色全是空白占位。
    src = None
    for n in (f"bundle_chara_c-panel_{int(cid):03d}",
              f"bundle_chara_c-panel_{int(cid):02d}",
              f"bundle_chara_c-panel_{cid}"):
        p = os.path.join(FILES_STORY, n)
        if os.path.exists(p):
            src = p
            break
    out = os.path.join(IMG, "avatar", f"{cid}.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if (os.path.exists(out) and not force) or not src:
        return os.path.exists(out)
    try:
        env = UnityPy.load(src)
        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            im = obj.read().image
            side = min(im.width, im.height)
            half = min(im.width, side)            # 半身像取左侧正方形
            im = im.crop((0, 0, half, min(im.height, half)))
            im.resize((size, size), Image.LANCZOS).save(out, optimize=True)
            if x2:
                x2dir = os.path.join(IMG_X2, "avatar")
                os.makedirs(x2dir, exist_ok=True)
                im.resize((size * 2, size * 2), Image.LANCZOS).save(
                    os.path.join(x2dir, f"{cid}.webp"), "WEBP",
                    quality=92, method=4)
            return True
    except Exception as e:
        _log(f"    头像导出失败 {cid}: {e}")
    return False


# ================================================================ 立绘合成


def _say(msg: str, tag: str = "stand") -> None:
    """既打到控制台也写进 app.log。

    装剧情是后台任务（GUI 用户看不到控制台），出问题时只有 app.log 可查，
    所以「关键决策 / 异常」必须落盘，不能只 print。
    """
    print(msg)
    try:
        from .. import log as applog
        applog.log(msg, tag=tag)
    except Exception:                                          # noqa: BLE001
        pass


def stand_size_report(nums) -> list[str]:
    """检查同一 (编号, 服装) 下所有立绘的尺寸是否一致。

    不一致通常意味着**串了另一套素材**：CDN 上 ``standimage_stand_36_*`` 与
    ``standimage_stand_036_*`` 是两个不同角色/形态，一旦解析错，合成出来的图
    尺寸就和同组其它表情对不上（历史上 stand_36 整组被 036 的包覆盖过，
    表现就是「同一个地方立绘不对」）。
    """
    from PIL import Image
    d = os.path.join(IMG, "stand")
    want = {str(n) for n in nums}
    bygrp: dict = {}
    try:
        files = os.listdir(d)
    except OSError:
        return []
    for fn in files:
        m = re.fullmatch(r"stand_(\d+)_([0-9A-F]+)_([0-9A-F]+)\.webp", fn)
        if not m or m.group(1) not in want:
            continue
        bygrp.setdefault((m.group(1), m.group(2)), []).append(fn)
    out = []
    for (num, cloth), fns in sorted(bygrp.items()):
        sizes: dict = {}
        for fn in fns:
            try:
                with Image.open(os.path.join(d, fn)) as im:
                    sizes.setdefault(im.size, []).append(fn)
            except Exception:                                  # noqa: BLE001
                continue
        if len(sizes) > 1:
            detail = "，".join(f"{w}x{h}×{len(v)}"
                              for (w, h), v in sorted(sizes.items()))
            out.append(f"{num}/{cloth}: {detail}")
    return out


def compose_stands(nums) -> None:
    """委派 ``compose_stand_all.py`` 合成表情差分，并**合并**落点文件。

    坑：该脚本每次都会用本次结果**整体覆盖** ``stand_slot.json``。
    按角色过滤跑一次，就会把别的角色的落点记录抹掉。所以这里跑完立即与旧值
    merge，保证增量更新不破坏全量。
    """
    slot_path = os.path.join(DATA, "stand_slot.json")
    backup = None
    if os.path.exists(slot_path):
        # 备份放工作区的 _archive/，别污染交付目录 web/data
        arch = ARCHIVE_DIR
        os.makedirs(arch, exist_ok=True)
        backup = os.path.join(arch, "stand_slot.json.prev")
        shutil.copy2(slot_path, backup)

    args = [str(n) for n in sorted(nums)]
    t0 = time.time()
    rc = run_script("compose_stand_all.py", *args, quiet=True)
    if rc != 0:
        _say(f"  compose_stand_all 退出码 {rc}")

    # merge：旧值为底，本次结果覆盖
    if backup and os.path.exists(slot_path):
        old = json.load(open(backup, encoding="utf-8"))
        new = json.load(open(slot_path, encoding="utf-8"))
        merged = dict(old)
        merged.update(new)
        with open(slot_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, separators=(",", ":"))
        _say(f"  stand_slot.json merge：旧 {len(old)} + 新 {len(new)}"
             f" -> {len(merged)}")
    # 落点质量自检结果（compose_stand_all.py 写在 _archive/stand_slot_qc.json）：
    # diff 大 = 表情层与底图在该落点对不上（历史"总有几张错位"就是靠这个兜住的）
    qc = os.path.join(ARCHIVE_DIR, "stand_slot_qc.json")
    if os.path.exists(qc):
        try:
            bad = json.load(open(qc, encoding="utf-8"))
        except Exception:
            bad = []
        ids = set()
        for n in nums:
            ids.add(str(n))
            ids.add(f"{int(n):04d}")
        mine = [b for b in bad if b.get("key", "").split("_")[0] in ids]
        if mine:
            _say(f"  ⚠ 立绘落点可疑 {len(mine)} 组（表情/底图对不上，见 "
                 f"_archive/stand_slot_qc.json）")
            for b in mine[:5]:
                _say(f"    {b['key']} slot={b['slot']} diff={b['diff']}")

    # 尺寸一致性：串了另一套素材时，同组表情尺寸会对不上（这次立绘串号的直接症状）
    bad_sz = stand_size_report(nums)
    if bad_sz:
        _say(f"  ⚠ 立绘尺寸不一致 {len(bad_sz)} 组（可能串了另一套素材）")
        for b in bad_sz[:8]:
            _say("    " + b)
    else:
        _say("  立绘尺寸一致 ✓")
    _say(f"  合成用时 {time.time() - t0:.0f}s")


def fill_missing_stands(keys, job=None) -> list[str]:
    """给"剧本引用了、但合成不出对应表情"的立绘 key 兜一张同套素体立绘。

    为什么需要：少数 key 的第三个字段不是脸部件编号，而是把文本（如 ボイス）
    当成表情码在用，分层包里根本没有这个表情 —— 合成阶段会跳过它，播放到那句
    就是**整张立绘消失**（e2e 里 char_2 装完自检报的"立绘引用缺 2 个"就是这种）。
    这里用同一套衣服的素体表情（8AEE967B）复制一份顶上：表情未必对，但比空着强，
    而且补了哪些 key 会写进日志，日后能核对。

    只在 ``regen_maps`` 之前调用才有意义（imgmap 是按目录里的文件扫出来的）。
    """
    stand_dir = os.path.join(IMG, "stand")
    out: list[str] = []
    if not os.path.isdir(stand_dir):
        return out
    have = set(os.listdir(stand_dir))
    for key in sorted(keys or ()):
        fn = f"{key}.webp"
        if fn in have:
            continue
        m = re.match(r"stand_(\d+)_([0-9A-Fa-f]+)_([0-9A-Za-z]+)$", key)
        if not m:
            continue
        num, cloth = m.group(1), m.group(2)
        prefix = f"stand_{int(num)}_{cloth}_"
        cands = [f"{prefix}8AEE967B.webp"] + [
            n for n in sorted(have) if n.startswith(prefix) and n.endswith(".webp")]
        src = next((c for c in cands if c in have), None)
        if not src:
            continue
        shutil.copy2(os.path.join(stand_dir, src),
                     os.path.join(stand_dir, fn))
        have.add(fn)
        out.append(key)
    if out and job is not None:
        job.log(f"⑤b 立绘兜底：{len(out)} 个 key 没有对应表情，用素体顶替"
                f"（{', '.join(out[:3])}{'…' if len(out) > 3 else ''}）")
    return out


# ================================================================ 映射表


def regen_imgmap() -> None:
    """委派旧脚本重生成 ``imgmap.js``（全量、幂等、只读磁盘，秒级）。"""
    run_script("gen_imgmap.py", quiet=True)


def regen_sfxmap() -> int:
    """把 ``export_sfx.py`` 产出的 ``sfxmap.json`` 转成网页要的 ``sfxmap.js``。

    这是旧链路的断口：网页加载的是 ``sfxmap.js``，但脚本只写 ``sfxmap.json``，
    以前靠手动转换。这里补上。
    """
    src = os.path.join(DATA, "sfxmap.json")
    out = os.path.join(DATA, "sfxmap.js")
    if not os.path.exists(src):
        # 没装公共素材（BGM / 音效）时也要留一份**空表**：
        # 播放器三个页面都无条件加载 data/sfxmap.js，视频导出（tools/video/
        # story.py）也直接读它 —— 缺文件就是 404 + 导出直接崩。
        if not os.path.exists(out):
            with open(out, "w", encoding="utf-8") as f:
                f.write("window.__SFX={};")
            print("  sfxmap.js：空表（还没装公共素材的音效）")
        return 0
    mp = json.load(open(src, encoding="utf-8"))
    # 网页里的媒体统一在 media/ 下；sfxmap.json 记的是相对 web 根的 audio/...
    mp = {k: ("media/" + v if v.startswith("audio/") else v)
          for k, v in mp.items()}
    out = os.path.join(DATA, "sfxmap.js")
    with open(out, "w", encoding="utf-8") as f:
        f.write("window.__SFX=" + json.dumps(mp, ensure_ascii=False,
                                            separators=(",", ":")) + ";")
    print(f"  sfxmap.js：{len(mp)} 条")
    return len(mp)


def regen_maps() -> None:
    regen_imgmap()
    regen_sfxmap()


# ================================================================ 角色名 / 元数据


def regen_names(use_wiki: bool = True) -> None:
    """先 ``extract_names.py`` 刷新 ``char_names.json``，再生成 ``names.js``。

    两个生成者的取舍：``gen_names_final.py`` 用 WIKI 校对过的人名
    （``char_names_wiki.json``），是终版；``gen_names_js.py`` 直接用
    ``char_names.json``。有 WIKI 数据就用前者，否则后者。

    覆盖前先留一份备份到 ``_archive/``：这两个脚本是"整体重写"，万一哪天上游
    判定逻辑改了，还能立刻回退到旧名单。
    """
    arch = ARCHIVE_DIR
    os.makedirs(arch, exist_ok=True)
    for f in ("char_names.json",):
        p = os.path.join(WS, f)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(arch, f + ".prev"))
    out = os.path.join(DATA, "names.js")
    if os.path.exists(out):
        shutil.copy2(out, os.path.join(arch, "names.js.prev"))
    run_script("extract_names.py", quiet=True)
    wiki = os.path.join(WS, "char_names_wiki.json")
    if use_wiki and os.path.exists(wiki):
        run_script("gen_names_final.py", quiet=True)
    else:
        run_script("gen_names_js.py", quiet=True)


def regen_charmeta() -> None:
    """稀有度/实装日期/头像。依赖 ``names.js`` 已存在，所以必须排在 names 之后。"""
    if not os.path.exists(os.path.join(WS, "char_meta.json")):
        print("  跳过 gen_charmeta.py：缺少 char_meta.json")
        return
    run_script("gen_charmeta.py", quiet=True)


def _pack_birth(path: str) -> str:
    """文件创建时间（Windows 创建时间，退化到 mtime），格式 ``YYYY/MM/DD``。"""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    ts = getattr(st, "st_birthtime", None) or st.st_mtime
    return time.strftime("%Y/%m/%d", time.localtime(ts))


def ensure_char_date(cid: int) -> str:
    """给「还没有实装日期」的新角色补一个**推定日期**（= 剧本资源包的创建时间）。

    场景：新角色刚上架，用户点「更新剧情」能装到包，但 Wiki 是第三方、通常延后
    一两天才收录这个角色。这段时间稀有度/类型只能空着，日期也不必干等 ——
    先用包自己的时间顶上，并用 ``dEst`` 标记它是推定值。

    之后用户再点「更新角色信息」，wiki 给出真日期时由
    :func:`wiki.merge_meta` 覆盖（它只在 wiki 有值时才覆盖），并清掉 ``dEst``。

    只对**本次安装的角色**调用，不会给历史角色乱填。
    """
    names = _load_names()
    nm = names.get(str(cid))
    if not nm:
        return ""
    base = re.sub(r"\s*\(\d+\)$", "", nm)
    path = os.path.join(WS, "char_meta.json")
    meta = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                meta = json.load(f) or {}
        except Exception:                                      # noqa: BLE001
            meta = {}
    cur = dict(meta.get(base) or {})
    if cur.get("d"):
        return ""                       # 已有日期（wiki 给的或上次推定），不覆盖
    d = _pack_birth(os.path.join(FILES_STORY,
                                 f"bundle_scenarioscript_char_{cid}"))
    if not d:
        return ""
    cur["d"] = d
    cur["dEst"] = 1
    meta[base] = cur
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return d


def _load_names() -> dict:
    """读 ``{角色id: 日文名}``。

    注意：``names.js`` 实际在 ``web/data``（= ``DATA``），userdata 里没有，
    所以先试 DATA，再退回 WS。
    """
    for p in (os.path.join(DATA, "names.js"), os.path.join(WS, "names.js")):
        try:
            with open(p, encoding="utf-8") as f:
                raw = f.read()
            return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        except Exception:                                      # noqa: BLE001
            continue
    return {}


def sync_wiki(job=None, proxy: str | None = None) -> dict:
    """拉角色元信息（稀有度/类型/实装日），合并进本机 ``userdata/char_meta.json``
    并重建成 ``charmeta.js``。

    数据来源优先级：
    1. 每日 CI 产出的**成品** ``web/data/charmeta.js``（约 17KB，只含
       ``{id: {r,d,t}}``），用 ``mirrors.fetch()`` 的线路表回退拉取
       （与「检查程序更新」共用同一张表），**不需要代理**；
    2. 直连 Wiki（只抓 2 个页面）——需要代理，失败抛 ``WikiUnreachable``。

    ``proxy_needed`` 只在上面的第 2 步也失败时才可能为 True。
    """
    from . import wiki as wiki_mod
    scraped: dict = {}
    source = ""
    data, route = wiki_mod.fetch_remote_charmeta()
    if data:
        names = _load_names()
        for cid, m in data.items():
            nm = names.get(str(cid))
            if nm:
                scraped[nm] = {"r": m.get("r", ""), "d": m.get("d", ""),
                               "t": m.get("t", "")}
        if scraped:
            source = "github:" + route
            if job is not None:
                job.log(f"已从 GitHub（{route}）取到 {len(scraped)} 个角色")
    if not scraped:
        source = "wiki"
        if job is not None:
            job.log(f"GitHub 各线路均失败（{route}），改直连 Wiki（需代理）")
        scraped = wiki_mod.scrape(proxy=proxy)
    path = os.path.join(WS, "char_meta.json")
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:                                      # noqa: BLE001
            existing = {}
    merged = wiki_mod.merge_meta(existing, scraped)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
    if job is not None:
        n_r = sum(1 for v in scraped.values() if v.get("r"))
        job.log(f"角色信息（来源 {source}）：合并 {len(scraped)} 条（含稀有度 {n_r}）")
    regen_charmeta()
    return {"merged": len(merged), "scraped": len(scraped),
            "with_rarity": sum(1 for v in merged.values() if v.get("r")),
            "source": source}


# ================================================================ 自检


def check_char(cid) -> dict:
    """检查某个角色在本地的完备程度（缺哪一阶段一目了然）。"""
    try:
        d = select.load_char(cid)
    except Exception:
        d = None

    imgmap = {}
    p = os.path.join(DATA, "imgmap.js")
    if os.path.exists(p):
        raw = open(p, encoding="utf-8").read()
        imgmap = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])

    res = {"char": cid, "scenario": bool(d), "parts": 0, "lines": 0,
           "voices_ref": 0, "voice_files": 0, "voice_miss": 0,
           "stand_miss": [], "back_miss": [], "cg_miss": []}
    if not d:
        return res
    vdir = os.path.join(AUDIO, str(cid))
    all_voices = set()
    for part in d["parts"]:
        all_voices.update(part.get("voices") or [])
    have = set(os.listdir(vdir)) if os.path.isdir(vdir) else set()
    res["parts"] = len(d["parts"])
    res["lines"] = sum(1 for p in d["parts"] for m in p["methods"]
                       if m.get("Method") == "ShowMessage")
    res["voices_ref"] = len(all_voices)
    res["voice_files"] = len(have)
    res["voice_miss"] = len({v for v in all_voices if v + ".m4a" not in have})

    assets = select.collect_assets(d)
    for key, kind in (("stand", "stand"), ("back", "back"), ("cg", "cg")):
        miss = [s for s in assets[kind] if s not in imgmap.get(kind, {})]
        res[f"{kind}_miss"] = miss

    # 包级检查：imgmap 缺可能只是"没合成"，但也可能是"包根本没下载"。
    # 后者重装一次就能补（fetch 的 skip 只跳过已存在的），必须单独说清楚，
    # 否则用户只看到"立绘缺 N 个"不知道该做什么。
    try:
        from . import manifest as _mf
        _M = select.Manifest(_mf.load_index())
    except Exception:                                          # noqa: BLE001
        _M = None
    bundle_miss = []
    if _M is not None:
        for s in assets["stand"]:
            b = _M.stand_bundle(s)
            if b and not os.path.exists(os.path.join(FILES_IMG, b)):
                bundle_miss.append(b)
    res["stand_bundle_miss"] = sorted(set(bundle_miss))
    return res
