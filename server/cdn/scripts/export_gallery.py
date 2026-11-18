"""解包「原声集」：曲目封面（jacket）+ 只补本机没有的音频 → ``web/data/gallery.js``。

原声集是什么
------------
游戏内「音楽」鉴赏页 = ``bundle_gallerymusic_music_00001…00165``（165 首，448 MB）
+ ``bundle_gallerymusicjackets``（165 张 447×411 的封面，12 MB）。

**和已有 BGM 的关系（省 375 MB 的关键）**
实测这 165 首里有 158 首与「公共素材 → BGM 音乐」里的 ``bundle_music_*`` **是同一份
母带**（解码后的 PCM 波形相关度 1.00、时长精确到 0.001 秒一致），其中 138 首本机
``media/audio/sfx/`` 里已经有现成文件。所以这里**不重复下载音频**：

1. 先用 ``sfxmap.js`` 把"同曲的本地文件"找出来直接用；
2. 只有本地没有的那几首（各平台联动的独占曲等）才下载它自己的
   ``bundle_gallerymusic_music_*`` 并解到 ``media/audio/gallery/``。

官方上新曲怎么办（2026-11-16 改）
---------------------------------
列表里有**哪些**曲目以"本机已下载的音频包"为准，不再由随程序分发的
``gallery_tracks.json`` 决定。安装器会按官方清单把所有编号的包下下来；
这里扫 ``bundle_gallerymusic_music_*``，发现表里没有的编号就现场解析：

* 名字：读包里 ``AudioClip`` 的内部占位名（公开曲名只在服务端 MasterData 里）；
* 时长 / 日期：读解出来的 m4a 容器 ``mvhd``（每首自带，全程不联网）。

新曲条目写进 ``<项目根>/userdata/gallery_tracks.local.json``（出厂表打底、
本地增量覆盖）—— **不写回** ``server/cdn/data/gallery_tracks.json``：那是随程序
分发的文件，会被程序更新覆盖，也会弄脏仓库。

用法（cwd = userdata/）
----------------------
    python export_gallery.py            # 解包 + 写 gallery.js
    python export_gallery.py --check    # 只看还差几首，不写文件
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import struct
import sys
import unicodedata

import UnityPy

WEB = os.path.join("..", "web")
OUT_JACKET = os.path.join(WEB, "media", "img", "jacket")
OUT_AUDIO = os.path.join(WEB, "media", "audio", "gallery")
OUT_JS = os.path.join(WEB, "data", "gallery.js")

#: 出厂曲目表（随程序分发）：告诉安装器哪些曲目与本机 BGM 是同一份母带。
TRACKS = os.path.join("..", "server", "cdn", "data", "gallery_tracks.json")
#: 运行期发现的官方新曲（用户数据，放 userdata；不随程序更新覆盖）。
ROOT_DIR = os.path.dirname(os.path.abspath(WEB))
WS = os.path.join(ROOT_DIR, "userdata")
LOCAL_TRACKS = os.path.join(WS, "gallery_tracks.local.json")

#: 包可能落在三处（``net.bundle_path`` 按前缀分目录，这里全找一遍）
BUNDLE_DIRS = ("char_story/files", "files_audio", "files_img")

#: 原声集音频包名 → 编号
AUDIO_RE = re.compile(r"^bundle_gallerymusic_music_(\d+)$")
#: 封面包前缀（主包 ``bundle_gallerymusicjackets`` + 官方"增量补图"包
#: ``bundle_image_gallerymusicjackets``；将来再发新的也一并收）
JACKET_PREFIXES = ("bundle_gallerymusicjacket",
                   "bundle_image_gallerymusicjacket")

#: 本机已解密清单（``userdata/cdn/`` 与旧工作区根）—— 给"复用本地 BGM"的曲目
#: 查资源包体积（那些包本机根本没下，拿不到文件大小）
MANIFEST_FILES = ("Manifest1Ex.json", "Manifest2Ex.json")
MANIFEST_DIRS = (os.path.join(ROOT_DIR, "userdata", "cdn"), ROOT_DIR)


def _log(msg):
    print(msg, flush=True)


PROGRESS_FILE = os.path.join(WS, "_archive", "gallery_progress.json")


def _progress(stage: str, done: int, total: int) -> None:
    """把真实进度写给外层任务读（原子写，半截 JSON 不会让读方崩）。

    子进程自己最清楚分母（封面包数 / 曲目数），由它报告比外层"数目录里的文件"
    准得多。外层心跳线程只读这个文件，得到 ``done/total`` 与阶段名。
    """
    try:
        os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
        frac = (float(done) / total) if total else 0.0
        # 阶段加权：封面 0–50%、曲目 50–95%、写表 95%+。曲目里大量是复用本地
        # 音频（瞬间完成），只有几首独占曲要解包 —— 纯按曲目数会让进度飞到
        # 95% 再卡住，加权后观感更贴近实际耗时。
        span = {"解包封面": (0.0, 50.0), "解析曲目": (50.0, 95.0)}.get(stage)
        pct = (span[0] + frac * (span[1] - span[0])) if span else frac * 100.0
        tmp = PROGRESS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"stage": stage, "done": done, "total": total,
                       "pct": round(pct, 1)}, f, ensure_ascii=False)
        os.replace(tmp, PROGRESS_FILE)
    except OSError:
        pass


def find_bundle(name: str):
    for d in BUNDLE_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def load_sfxmap() -> dict:
    p = os.path.join(WEB, "data", "sfxmap.js")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8-sig") as f:
            s = f.read()
        return json.loads(s[s.index("=") + 1:].strip().rstrip(";"))
    except Exception:                                            # noqa: BLE001
        return {}


def manifest_sizes() -> dict:
    """``{bundle_name: size}``：从本机清单取包体积（不必下包也能显示大小）。"""
    out = {}
    for d in MANIFEST_DIRS:
        for nm in MANIFEST_FILES:
            p = os.path.join(d, nm)
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f).get("data") or []
            except (OSError, ValueError):
                continue
            for x in data:
                out.setdefault(str(x.get("bundle_name") or ""),
                               int(x.get("size") or 0))
    return out


# ---------------------------------------------------------------- 曲目表
def load_builtin_tracks() -> list:
    try:
        with open(TRACKS, encoding="utf-8") as f:
            return list((json.load(f) or {}).get("tracks") or [])
    except (OSError, ValueError):
        return []


def load_local_tracks() -> dict:
    """本机增量表：``{编号: 条目}``（官方新曲解析出来的结果）。"""
    try:
        with open(LOCAL_TRACKS, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        return {}
    out = {}
    for t in (data.get("tracks") or []):
        try:
            no = int(t.get("no") or 0)
        except (TypeError, ValueError):
            continue
        if no:
            out[no] = t
    return out


def save_local_tracks(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOCAL_TRACKS), exist_ok=True)
        with open(LOCAL_TRACKS, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"v": 1, "tracks": [d[k] for k in sorted(d)]},
                      f, ensure_ascii=False, indent=1)
    except OSError as e:
        _log(f"! 新曲条目写不进去（只影响下次少解析一遍）：{e}")


def local_audio_bundles() -> dict:
    """本机已下载的 ``bundle_gallerymusic_music_NNNNN`` → ``{编号: 路径}``。"""
    out = {}
    for d in BUNDLE_DIRS:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for nm in names:
            m = AUDIO_RE.match(nm)
            if not m:
                continue
            p = os.path.join(d, nm)
            if os.path.isfile(p):
                out[int(m.group(1))] = p
    return out


def jacket_bundles() -> list:
    """本机所有原声集封面包（含官方后来单独发的增量补图包）。"""
    out = []
    for d in BUNDLE_DIRS:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for nm in names:
            if nm.startswith(JACKET_PREFIXES):
                out.append(os.path.join(d, nm))
    return sorted(set(out))


def jacket_file(no: int) -> str:
    """封面 web 路径。

    官方纹理名是 ``msc_0001``（曲目表按 ``%04d`` 拼），但也见过不补零的
    ``msc_166`` —— 两种都认，免得新曲有封面却显示成空。
    """
    for nm in ("msc_%04d.webp" % no, "msc_%d.webp" % no):
        if os.path.exists(os.path.join(OUT_JACKET, nm)):
            return "media/img/jacket/" + nm
    return ""


# ---------------------------------------------------------------- 封面
def export_jackets(total_hint: int = 0) -> int:
    """``msc_NNNN`` → ``media/img/jacket/msc_NNNN.webp``。

    **分母必须是"张数"，不能是"包数"**：清单里封面包只有 2 个（新旧各一），
    而每个包里有 82 张纹理。以前拿 ``len(jacket_bundles())`` 当分母，
    第 89 张时报的是 ``89/2`` → pct 算成 2225%，外层 clamp 到 99% 后进度条
    就钉死在 99.x% 一动不动，用户看到的是「解包封面… 89/2」+「卡了几分钟」
    （2026-11-17 实测）。

    所以分两趟：先把所有包的对象名收集一遍（只读名字、不解码像素，很便宜），
    拿到真实总张数，再逐张解码导出 —— 期间 ``n/total`` 始终是可信的。
    """
    os.makedirs(OUT_JACKET, exist_ok=True)
    pending = []
    for p in jacket_bundles():
        try:
            env = UnityPy.load(p)
        except Exception as e:                                   # noqa: BLE001
            _log(f"  ! {os.path.basename(p)} 打不开：{str(e)[:100]}")
            continue
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            try:
                d = obj.read()          # 读一次并持有：第二趟直接取 .image，
                name = str(d.m_Name or "")   # 不要重新 read（那会白解一遍包）
            except Exception:                                    # noqa: BLE001
                continue
            if name.startswith("msc_"):
                pending.append((d, name))
    total = total_hint or len(pending)
    _progress("解包封面", 0, total)
    n = 0
    for d, name in pending:
        try:
            out = os.path.join(OUT_JACKET, name + ".webp")
            if not os.path.exists(out):
                d.image.save(out, "WEBP", quality=92, method=4)
        except Exception:                                        # noqa: BLE001
            continue
        n += 1
        _progress("解包封面", n, total)
    return n


# ---------------------------------------------------------------- 音频
def split_mp4(res: bytes):
    """切出 resource 里的 MP4（与 export_sfx.py 同一套 box 解析）。"""
    out = []
    pos, n = 0, len(res)
    while pos + 8 <= n:
        size = struct.unpack(">I", res[pos:pos + 4])[0]
        typ = res[pos + 4:pos + 8]
        if size == 1 and pos + 16 <= n:
            size = struct.unpack(">Q", res[pos + 8:pos + 16])[0]
        if size < 8 or pos + size > n:
            break
        if typ == b"ftyp" or not out:
            out.append([pos, pos + size])
        else:
            out[-1][1] = pos + size
        pos += size
    return [res[a:b] for a, b in out] if out else [res]


def export_one_audio(no: int) -> bool:
    """解一首画廊独占曲：``bundle_gallerymusic_music_000NN`` → ``media/audio/gallery``。"""
    name = "bundle_gallerymusic_music_%05d" % no
    p = find_bundle(name)
    if not p:
        return False
    out = os.path.join(OUT_AUDIO, "music_%05d.m4a" % no)
    if os.path.exists(out) and os.path.getsize(out) > 1024:
        return True
    os.makedirs(OUT_AUDIO, exist_ok=True)
    try:
        payload, nodes, *_ = _parse_unityfs(open(p, "rb").read())
        res = None
        for off, sz, _nf, nm in nodes:
            if nm.endswith(".resource"):
                res = payload[off:off + sz]
                break
        if res is None:
            return False
        clip = split_mp4(res)[0]
        with open(out, "wb") as f:
            f.write(clip)
        return True
    except Exception as e:                                       # noqa: BLE001
        _log(f"  ! {name} 解不开：{str(e)[:100]}")
        return False


def _parse_unityfs(blob: bytes):
    from unityfs_probe import parse_unityfs
    return parse_unityfs(blob)


def probe_orig(bundle_path: str) -> str:
    """包里 ``AudioClip`` 的内部占位名（如 ``music_00166``）。

    公开曲名只在服务端 MasterData 里，包里没有。能拿到什么就显示什么，
    拿不到留空，前端显示「曲目 166」—— 不编造。
    """
    try:
        env = UnityPy.load(bundle_path)
        for obj in env.objects:
            if obj.type.name == "AudioClip":
                return str(obj.read().m_Name or "")
    except Exception as e:                                       # noqa: BLE001
        _log(f"  ! {os.path.basename(bundle_path)} 读不到内部名：{str(e)[:100]}")
    return ""


# ---------------------------------------------------------------- 分类
#: 曲名 → 分组。**不按官方表分类**（官方界面只是按编号排），这一层是给人用的：
#: 165 首平铺着翻很累，按"听感用途"分成 7 类能少翻一大半。
#:
#: 顺序有意义：先认「○○のテーマ」再认「戦」，否则「戦士のテーマ」会被丢进战斗。
GROUP_RULES = (
    ("系统", ("system", "システム", "メニュー", "タイトル", "画面", "ショップ", "ガチャ")),
    ("角色曲", ("のテーマ",)),
    ("日常", ("日常", "優し", "癒", "のどか", "昼下がり", "ほのぼの", "アットホーム",
             "団欒", "休日", "平和", "楽園", "陽だまり", "安ら", "安樂", "穏やか",
             "元気", "おしゃれ", "デート", "ギャグ", "home", "small happiness",
             "warm light", "in the daytime", "afternoon", "木漏れ日", "午後",
             "まどろみ", "微睡", "ひととき", "気まま", "靴跡", "はにわ", "陶器市",
             "かわいい", "キミに届け", "乙女")),
    ("战斗", ("battle", "バトル", "決戦", "乱戦", "ボス", "レイド", "強敵", "熱血",
             "最終章", "襲来", "戦", "boss", "fight", "duel", "combat", "raid",
             "prepare", "death game", "venom", "strain", "stray brave",
             "beat valkyrie", "beat witch", "command phase", "brutal",
             "闘", "撃", "拳", "牙", "煉獄", "修羅", "危機", "逃走", "突撃")),
    ("严肃", ("悲壮", "勇壮", "不穏", "不安", "ピンチ", "疑惑", "思索", "哀し", "切な",
             "悲しみ", "敗北", "絶望", "シリアス", "真面目", "真剣", "闇", "陰",
             "緊迫", "決意", "別れ", "涙", "怪", "夜", "堕", "悪", "栄光")),
)


def classify(name: str) -> str:
    """曲名 → 分组。

    先把全角字母折成 ASCII（``ＯＰオルゴール`` / ``ランス９ＥＤ`` 都是全角），
    否则「按 OP/ED 找主题曲」这条永远不命中。
    """
    n = unicodedata.normalize("NFKC", name or "")
    low = n.lower()
    if not low:
        return "其他"
    if "主題歌" in n or "テーマソング" in n:
        return "主题歌"
    if low.strip() in ("title", "ending", "op", "ed"):
        return "主题歌"
    # ＯＰ / ＥＤ 要按"独立单词"认（避开 Shop 这类词里的 op）
    if re.search(r"(^|[^A-Za-z])(OP|ED)([^A-Za-z]|$)", n):
        return "主题歌"
    for tag, keys in GROUP_RULES:
        for k in keys:
            if k.lower() in low:
                return tag
    return "其他"


# ---------------------------------------------------------------- 曲名
#: 人工补录的曲名（编号 → 游戏内显示的曲名）。
#:
#: 客户端包里只有**内部占位名**（``Home 3`` / ``Treasure Rance C`` / ``battle``），
#: 真正的曲名在服务端 MasterData 里，包里没有。所以只能人工补；
#: 补一条就多一首能认出来的曲子，没补的继续显示内部名（不编造）。
NAMES = os.path.join("..", "server", "cdn", "data", "gallery_names.json")


def load_names() -> dict:
    try:
        with open(NAMES, encoding="utf-8") as f:
            d = json.load(f)
        return {str(k): str(v) for k, v in (d.get("names") or {}).items() if v}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- 音频元信息
def audio_meta(rel: str) -> tuple:
    """读 m4a 容器 ``mvhd``：``(封装日期 YYYY-MM-DD, 时长秒)``。

    日期**不取** CDN 的 Last-Modified：那是整批包一次性上传的时间，165 首
    全是同一天，跟曲目本身没关系（2026-11-15 用户指出）。这些 m4a 没有
    ID3 / ©day 标签，但 ``mvhd`` 的 creation_time 是官方封装这段音频的时间，
    每首不同、就写在正在播放的那个文件里。全程只读本机文件，不联网。

    时长也一起读：官方新曲在出厂表里没有条目，时长只能从文件本身拿。
    只顺着跳 box 头、跳过 mdat（几 MB 的音频本体），moov 才整段读进来。
    """
    if not rel:
        return "", 0.0
    path = os.path.join(WEB, rel.replace("/", os.sep))
    moov = b""
    try:
        with open(path, "rb") as f:
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                size = struct.unpack(">I", hdr[:4])[0]
                typ = hdr[4:8]
                off = 8
                if size == 1:
                    size = struct.unpack(">Q", f.read(8))[0]
                    off = 16
                if size and size < off:
                    break
                if typ == b"moov":
                    moov = f.read(size - off) if size else f.read()
                    break
                if not size:
                    break
                f.seek(size - off, 1)
        i = moov.find(b"mvhd")
        if i < 0:
            return "", 0.0
        p = i + 4                       # 'mvhd' 后面紧跟 version/flags
        q = p + 4
        if moov[p] == 1:
            created = struct.unpack(">Q", moov[q:q + 8])[0]
            timescale = struct.unpack(">I", moov[q + 16:q + 20])[0]
            dur = struct.unpack(">Q", moov[q + 20:q + 28])[0]
        else:
            created = struct.unpack(">I", moov[q:q + 4])[0]
            timescale = struct.unpack(">I", moov[q + 8:q + 12])[0]
            dur = struct.unpack(">I", moov[q + 12:q + 16])[0]
        dt = ""
        if created:
            epoch = datetime.datetime(1904, 1, 1)
            dt = (epoch + datetime.timedelta(seconds=created)).strftime("%Y-%m-%d")
        sec = round(dur / timescale, 1) if timescale else 0.0
        return dt, sec
    except (OSError, ValueError, struct.error, OverflowError, IndexError):
        return "", 0.0


def probe_new_track(no: int, bundle: str) -> dict:
    """官方新曲：读内部名 + 解出音频 + 读时长，得到一条可写进增量表的条目。"""
    orig = probe_orig(bundle)
    src = ""
    if export_one_audio(no):
        src = "media/audio/gallery/music_%05d.m4a" % no
    _dt, dur = audio_meta(src)
    return {"no": no, "dur": dur or 0, "clip": None, "name": "",
            "orig": orig or ("music_%05d" % no)}


def _write_back_builtin(builtin: list, known: dict) -> None:
    """把重算过的分类写回出厂表（只限出厂表里原有的编号）。

    新曲**不写**：那是 userdata 的增量表管的事，写进程序目录会被更新覆盖。
    """
    try:
        with open(TRACKS, encoding="utf-8") as f:
            note = (json.load(f) or {}).get("note") or ""
    except (OSError, ValueError):
        note = ""
    keep = {int(t.get("no") or 0) for t in builtin}
    tracks = []
    for no in sorted(keep):
        t = known.get(no)
        if not t:
            continue
        t = dict(t)
        t["orig"] = t.get("orig") or (t.get("name") or "")
        t["group"] = classify(t.get("orig") or "")
        tracks.append({"no": no, "dur": t.get("dur") or 0,
                       "clip": t.get("clip"), "name": t.get("name") or None,
                       "group": t["group"], "orig": t["orig"]})
    try:
        with open(TRACKS, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"v": 1, "note": note, "tracks": tracks},
                      f, ensure_ascii=False, indent=1)
    except OSError as e:
        _log(f"! 曲目表写不回去（分类只影响本次生成的 gallery.js）：{e}")


# ---------------------------------------------------------------- 主流程
def build(check_only: bool = False) -> int:
    builtin = load_builtin_tracks()
    audio = local_audio_bundles()
    if not builtin and not audio:
        _log(f"缺 {os.path.normpath(TRACKS)}，本机也没有已下载的原声集音频包")
        return 1

    local = load_local_tracks()
    sfx = load_sfxmap()
    sizes = manifest_sizes()
    over = load_names()

    # 曲目全集 = 出厂表 ∪ 本地增量表 ∪ 本机已下的音频包编号
    known: dict = {}
    for t in builtin:
        no = int(t.get("no") or 0)
        if no:
            known[no] = dict(t)
    for no, t in local.items():
        known[no] = dict(t)

    # 官方上新曲：本机有音频包、两份表都没登记 → 现场解析补进增量表。
    # 这就是"资源更新不必等开发者手补"的实现：用户点一次「补全」即可。
    fresh = [no for no in sorted(audio) if no not in known]
    if fresh:
        _log("发现 %d 首表里没有的新曲：%s%s" % (
            len(fresh), "、".join("#%d" % n for n in fresh[:12]),
            "…" if len(fresh) > 12 else ""))
        if not check_only:
            for no in fresh:
                t = probe_new_track(no, audio[no])
                local[no] = t
                known[no] = dict(t)
            save_local_tracks(local)

    jackets = 0
    if not check_only:
        # 分母（总张数）由 export_jackets 自己先数一遍再报，这里不要再用包数占位
        jackets = export_jackets()
        _log(f"封面 {jackets} 张")

    rows, missing_audio, no_jacket = [], 0, []
    total_tracks = len(known)
    for _ti, no in enumerate(sorted(known)):
        _progress("解析曲目", _ti, total_tracks)
        t = dict(known[no])
        # 包里的内部名（battle / 悲壮 / Home …）。已经有了就别覆盖 ——
        # 生成时 name 可能是人工补的公开名，覆盖掉就再也分不出用途了。
        t["orig"] = t.get("orig") or (t.get("name") or "")
        if str(no) in over:
            t["name"] = over[str(no)]
        # 分类用**内部名**算、显示用官方名：
        # 官方名大多是英文标题（Calm / Usual / Brave），看不出用途；
        # 内部名是官方自己打的用途标签，分出来更贴合听感。
        t["group"] = classify(t.get("orig") or "")

        jrel = jacket_file(no)
        if not jrel and not check_only:
            no_jacket.append(no)

        src, reused = "", False
        clip = t.get("clip")
        if clip and clip in sfx:
            cand = os.path.join(WEB, sfx[clip])
            if os.path.exists(cand):
                src, reused = sfx[clip], True
        if not src:
            gpath = os.path.join(OUT_AUDIO, "music_%05d.m4a" % no)
            if check_only:
                if os.path.exists(gpath):
                    src = "media/audio/gallery/music_%05d.m4a" % no
            elif export_one_audio(no):
                src = "media/audio/gallery/music_%05d.m4a" % no
            if not src:
                missing_audio += 1

        # 时长 / 日期取「正在播放的这一段音频」自己的 mvhd；
        # 拿不到（没音频文件）才退回表里的时长。
        dt, dur = audio_meta(src)
        bname = "bundle_gallerymusic_music_%05d" % no
        sz = os.path.getsize(audio[no]) if no in audio else 0
        if not sz:
            sz = int(sizes.get(bname) or 0)
        rows.append({
            "no": no,
            "d": dur or float(t.get("dur") or 0),
            "j": jrel,
            "s": src,
            "t": t.get("name") or "",
            "g": t.get("group") or "其他",
            # ↓ 详细浮窗用的元信息：包侧的事实 + 音频自身的时间戳
            "o": t.get("orig") or "",                     # 包内名（占位名）
            "bn": bname,                                  # 官方包名
            "sz": sz,                                     # 资源包大小（字节）
            "dt": dt,                                     # 音频自带日期
            "re": 1 if (reused and src) else 0,
        })

    _log(f"曲目 {len(rows)} 首：有音频 {sum(1 for r in rows if r['s'])}，"
         f"缺 {missing_audio}；封面缺 {len(no_jacket)}")

    if check_only:
        return 0

    # 不写回出厂表 server/cdn/data/gallery_tracks.json：那是随程序分发、
    # 会被程序更新覆盖的文件（见模块头部说明）。本机重算的 group 分类只影响
    # 本次生成的 gallery.js；写出厂表既弄脏仓库，又会把人工修正的曲名冲掉。
    _progress("写入曲目表", total_tracks, total_tracks)
    data = {"v": 1, "n": len(rows), "tracks": rows}
    os.makedirs(os.path.dirname(OUT_JS), exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tmp = OUT_JS + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("window.__GALLERY=" + body + ";")
    os.replace(tmp, OUT_JS)
    _log(f"→ {os.path.normpath(OUT_JS)}（{os.path.getsize(OUT_JS) / 1024:.0f} KB）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="解包原声集（封面 + 缺的音频 + 官方新曲）")
    ap.add_argument("--check", action="store_true", help="只统计，不写文件")
    a = ap.parse_args(argv)
    return build(a.check)


if __name__ == "__main__":
    sys.exit(main())
