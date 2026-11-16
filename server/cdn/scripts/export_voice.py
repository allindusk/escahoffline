"""导出每个单位的**战斗语音**（音声）→ ``web/media/audio/voice/<编号>/`` + ``web/data/voicemap.js``。

语音在哪个包
------------
``bundle_audioclipsprefab_unit_<5 位编号>``（821 个：角色 1–416、NPC 1xxxx、敌 2xxxx，
每个约 1.0–1.4 MB）。一个包里 29~31 段，混着两类名字：

* ``voice_00011_attack02_01`` —— **配音**（登场/开战/攻击/技能/必杀/受伤/撤退/
  战斗不能/胜利/被捕获），这就是用户说的「音声」；
* ``sound_00011_attack02_01`` —— 同一个动作的**武器音效**，不是配音，导出时跳过。

包里没有曲名之类的元数据，能拿到的就是这套内部名，界面按动作名分组显示。

怎么解
------
AudioClip 的 ``m_Resource`` 是 StreamedResource（``m_Source`` 指向同包的
``.resource``，配 ``m_Offset``/``m_Size``）。UnityPy 的 ``clip.samples`` 会自己把
那段切出来并命名成 ``<名字>.m4a``，所以不用像 ``export_sfx.py`` 那样手写 MP4 box
解析 —— 直接写文件即可。

用法（cwd = userdata/）
----------------------
    python export_voice.py --chars 11,12     # 指定角色
    python export_voice.py --npc             # 清单里全部 NPC / 敌人
    python export_voice.py --all             # 全部 821 个
    python export_voice.py --force --chars 11
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import time
import contextlib

#: UnityPy 只在真正读包时要；``--repair`` 仅按已落盘的 m4a 补清单，
#: 那台机器上就算没装 UnityPy 也该能用（懒加载，见 :func:`_unitypy`）


def _unitypy():
    import UnityPy
    return UnityPy

WEB = os.path.join("..", "web")
OUT = os.path.join(WEB, "media", "audio", "voice")
OUT_JS = os.path.join(WEB, "data", "voicemap.js")
SRC_DIRS = ("char_story/files", "files_audio", "files_img")

#: 动作名 → **游戏里显示的名字**。2026-11-15 用用户抄下来的「エスカ・ルビー」音声
#: 列表逐条对照确认：参戦 / 戦闘開始 / 攻撃 / 強化 / 必殺技 / ダメージ / 勝利 /
#: 戦闘不能 / エロコマンダー捕獲。界面直接用这套，跟游戏里对得上。
#:
#: 注意：游戏那 39 条里另有「取得・覚醒・ホーム画面クリック・放置・タイトルコール」
#: 约 20 条 —— 它们**不在** ``audioclipsprefab_unit_*`` 里，而在全站共用语音库
#: （``bundle_systemvoice_*``，499 个包、名字只有 ``80000_00121`` 这种流水号），
#: 要归到具体角色必须靠服务端 MasterData（与曲名同一个加密接口），离线做不到。
ACT_JP = {
    "deploy": "参戦", "start": "戦闘開始", "attack": "攻撃", "ability": "強化",
    "special": "必殺技", "damage": "ダメージ", "dropout": "撤退",
    "knockout": "戦闘不能", "victory": "勝利", "caught": "エロコマンダー捕獲",
}

RE_CLIP = re.compile(r"^voice_(\d+)_([a-z]+?)(\d*)(?:_(\d+))?$")


def _log(msg: str) -> None:
    print(msg, flush=True)


def find_bundle(name: str) -> str | None:
    for d in SRC_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def load_map() -> dict:
    if not os.path.exists(OUT_JS):
        return {"v": 1, "units": []}
    try:
        with open(OUT_JS, encoding="utf-8-sig") as f:
            s = f.read()
        return json.loads(s[s.find("{"):s.rindex("}") + 1]) or {"v": 1, "units": []}
    except (OSError, ValueError):
        return {"v": 1, "units": []}


def save_map(data: dict) -> None:
    os.makedirs(os.path.dirname(OUT_JS), exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tmp = OUT_JS + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("window.__VOICEMAP=" + body + ";")
    os.replace(tmp, OUT_JS)


# ---------------------------------------------------------------- 并发写保护
#: ``voicemap.js`` 是一份**整表**。安装任务是并行跑的，一台机器上常有两三个
#: ``export_voice.py`` 同时收尾：各自在启动时读入整表，各自导出自己那批，最后
#: 整表写回 —— 后写的会把先写的整段覆盖掉（2026-11-15 日志里出现过
#: 「694 个单位 → 682 个单位」这种越导越少的怪象）。
#: 丢掉的正是那 7 个角色（4/37/66/76/79/100）的音声条目：音频文件都导出来了，
#: 唯独清单里没有它们，于是页面显示 378/385，资源管理页又因为目录存在而判定
#: 「已安装」，补全按钮也不亮。
LOCK_FILE = OUT_JS + ".lock"


@contextlib.contextmanager
def _write_lock(timeout: float = 60.0):
    """跨进程互斥（用独占创建锁文件实现，Windows / POSIX 都通用）。"""
    t0 = time.time()
    fd = -1
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # 上一个进程可能崩了没删锁：超过 2 分钟就当僵尸清掉
            try:
                if time.time() - os.path.getmtime(LOCK_FILE) > 120:
                    os.remove(LOCK_FILE)
                    continue
            except OSError:
                pass
            if time.time() - t0 > timeout:
                break                      # 等不到也别把活儿卡死，直接写
            time.sleep(0.05)
        except OSError:
            break
    try:
        yield
    finally:
        if fd >= 0:
            try:
                os.close(fd)
                os.remove(LOCK_FILE)
            except OSError:
                pass


def merge_save(entries: dict) -> dict:
    """**只覆盖本次导出的那几个单位**，其余条目以磁盘上的最新版本为准。

    ``entries`` = ``{编号: voicemap 条目}``。读 → 改 → 写全程持锁，别的进程
    同时写完的单位不会被这次写回冲掉。
    """
    os.makedirs(os.path.dirname(OUT_JS), exist_ok=True)
    with _write_lock():
        data = load_map()
        have = {u.get("id"): u for u in (data.get("units") or []) if u.get("id") is not None}
        for k, v in entries.items():
            if v is None:
                continue
            have[int(k)] = v
        data["v"] = 1
        data["units"] = [have[k] for k in sorted(have)]
        save_map(data)
        return data


def act_of(name: str) -> str:
    """``voice_00011_attack02_01`` → ``攻撃``（编号另外统一编，见 :func:`number_labels`）"""
    m = RE_CLIP.match(name)
    if not m:
        return ""
    return ACT_JP.get(m.group(2), m.group(2))


def number_labels(rows: list) -> None:
    """按"同一个动作出现几次"编号，编号规则与游戏里一致（就地改 rows[i][3]）。

    为什么不能直接用名字里的数字：
      · ``start01_01`` / ``start02_01`` → 戦闘開始 1 / 2（数字在前段）
      · ``deploy01_01`` / ``deploy01_02`` → 参戦 1 / 2（数字在后段）
    两套命名混着用（是官方的历史包袱），只有"按动作出现的先后顺序编号"能同时对上。
    只出现一次的动作（必殺技 / 戦闘不能 / エロコマンダー捕獲）游戏里也不带编号。
    """
    seen: dict[str, int] = {}
    total: dict[str, int] = {}
    for r in rows:
        total[r[3]] = total.get(r[3], 0) + 1
    for r in rows:
        k = r[3]
        if total.get(k, 0) <= 1:
            continue
        seen[k] = seen.get(k, 0) + 1
        r[3] = f"{k} {seen[k]}"


#: systemvoice 的**槽位规律**（用 Qwen3-ASR 转写了エスカレイヤー/角色32的 32 条、
#: 再用角色 11、2 交叉验证得来，2026-11-15）。
#:
#: 包名里的 clip 是 ``80000_<角色3位><槽位2位>``，槽位对**所有角色通用**：
#:   01        加入（登场台词：32「只今参上！」/ 11「ただいま参上」/ 2「お世話して差し上げましょう」）
#:   02–15     ホーム画面クリック / 放置（変身前・変身後，具体哪句是前/后只能靠声音区分）
#:   16–36     战斗语音（与 audioclipsprefab 重复，导出时按名称时长剔掉）
#:   37        タイトルコール（三个角色都是同一句「超昂大戦エスカレーションヒロインズ」）
#:   38–52     追加台詞（会話/放置/アイテム使用/レベルアップ，各角色略有差异）
#:
#: 游戏里那些精确名字（会話1(変身後)…）是由服务端表 + 声线决定的，
#: 离线只能给到"槽位语义"这一层；宁可写「ホーム 03」也不乱编「会話1(変身前)」。
HOME_SLOT = {1: "加入", 37: "タイトルコール"}
RE_SLOT = re.compile(r"(\d{2})$")


def label_home(num: int, rows: list) -> None:
    """按通用槽位给「其他语音」命名（就地改 rows[i][3]）。

    已经有更精确名字的（例如用 ASR 转写后人工对过表的角色）不覆盖。
    """
    cnt: dict[str, int] = {}
    for r in rows:
        if r[3] and not r[3].startswith(("其他", "ホーム", "追加", "战斗")):
            continue                       # 人工/ASR 定好的名字，保留
        m = RE_SLOT.search(r[0])
        slot = int(m.group(1)) if m else 0
        if slot in HOME_SLOT:
            r[3] = HOME_SLOT[slot]
            continue
        if 2 <= slot <= 15:
            base = "ホーム"
        elif 38 <= slot <= 52:
            base = "追加"
        elif 16 <= slot <= 36:
            base = "战斗"                   # 通常为重复包，能留下来说明没匹配上
        else:
            base = "其他"
        cnt[base] = cnt.get(base, 0) + 1
        r[3] = "%s %02d" % (base, cnt[base])


def export_sys(num: int, force: bool = False) -> list:
    """导出 ``bundle_systemvoice_<5 位编号>`` 里的**其余语音**（主页那批）。

    游戏「音声」页那 39 条里，战斗那一批在 ``audioclipsprefab_unit_*``，
    另一批（取得 / 覚醒 / ホーム画面クリック / 放置 / タイトルコール）在这里：
    一个角色一包、48 段左右，**同时也重复存了战斗语音**，所以调用方按名称时长
    把重复的剔掉。

    包里只有 ``80000_01101`` 这种流水号，游戏里的显示名只存在于服务端 MasterData。
    """
    name = "bundle_systemvoice_%05d" % int(num)
    path = find_bundle(name)
    if not path:
        return []
    d = os.path.join(OUT, str(int(num)))
    os.makedirs(d, exist_ok=True)
    env = _unitypy().load(path)
    got = []
    for obj in env.objects:
        if obj.type.name != "AudioClip":
            continue
        try:
            clip = obj.read()
            got.append((str(clip.m_Name or ""), round(float(clip.m_Length or 0), 2), clip))
        except Exception:                                        # noqa: BLE001
            continue
    got.sort(key=lambda x: x[0])
    rows = []
    for cname, dur, clip in got:
        fp = os.path.join(d, "sys_%s.m4a" % cname)
        rel = "media/audio/voice/%d/sys_%s.m4a" % (int(num), cname)
        if force or not os.path.exists(fp) or os.path.getsize(fp) < 512:
            try:
                blob = next(iter(clip.samples.values()))
            except Exception:                                    # noqa: BLE001
                continue
            if not blob:
                continue
            with open(fp, "wb") as f:
                f.write(blob)
        rows.append([cname, rel, dur, ""])
    return rows


def export_unit(num: int, force: bool = False) -> dict | None:
    """导出一个单位的全部配音，返回 voicemap 条目（失败返回 None）。"""
    name = "bundle_audioclipsprefab_unit_%05d" % int(num)
    path = find_bundle(name)
    if not path:
        return None
    d = os.path.join(OUT, str(int(num)))
    os.makedirs(d, exist_ok=True)
    env = _unitypy().load(path)
    clips = []
    for obj in env.objects:
        if obj.type.name != "AudioClip":
            continue
        try:
            clip = obj.read()
            cname = str(clip.m_Name or "")
        except Exception:                                        # noqa: BLE001
            continue
        if not cname.startswith("voice_"):
            continue                      # sound_* 是武器音效，不是配音
        out = os.path.join(d, cname + ".m4a")
        rel = "media/audio/voice/%d/%s.m4a" % (int(num), cname)
        dur = round(float(getattr(clip, "m_Length", 0) or 0), 2)
        if force or not os.path.exists(out) or os.path.getsize(out) < 512:
            try:
                samples = clip.samples
                blob = next(iter(samples.values()))
            except Exception:                                    # noqa: BLE001
                continue
            if not blob:
                continue
            with open(out, "wb") as f:
                f.write(blob)
        clips.append([cname, rel, dur, act_of(cname)])
    if not clips:
        return None
    clips.sort(key=lambda c: c[0])
    number_labels(clips)
    # 「其余语音」（主页等）：从 systemvoice 包里取，剔掉与战斗语音重复的
    try:
        seen = {round(float(c[2]), 2) for c in clips}
        home = [r for r in export_sys(num, force) if round(float(r[2]), 2) not in seen]
    except Exception as e:                                       # noqa: BLE001
        _log(f"  ! {num} 其余语音导出失败：{str(e)[:80]}")
        home = []
    label_home(num, home)
    kind = "char" if num < 10000 else ("npc" if num < 20000 else "enemy")
    return {"id": int(num), "k": kind, "clips": clips, "home": home}


def mp4_duration(path: str) -> float:
    """读 m4a 自带的时长（moov/mvhd 的 duration / timescale），秒。

    补清单时没有 UnityPy 可用，但音频文件本身带时长 —— 解析几个 box 就够，
    不用为了补 6 个角色再拉一遍 UnityPy / 重建整个包。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return 0.0

    def walk(start: int, end: int) -> float:
        off = start
        while off + 8 <= end:
            size, typ = struct.unpack(">I4s", data[off:off + 8])
            hdr = 8
            if size == 1:
                size = struct.unpack(">Q", data[off + 8:off + 16])[0]
                hdr = 16
            elif size == 0:
                size = end - off
            if typ in (b"mvhd", b"mdhd"):
                ver = data[off + hdr]
                if ver == 1:
                    ts, dur = struct.unpack(">IQ", data[off + hdr + 20:off + hdr + 32])
                else:
                    ts, dur = struct.unpack(">II", data[off + hdr + 12:off + hdr + 20])
                return (dur / ts) if ts else 0.0
            if typ in (b"moov", b"trak", b"mdia"):
                got = walk(off + hdr, off + size)
                if got:
                    return got
            if size < 8:
                break
            off += size
        return 0.0

    return round(walk(0, len(data)), 2)


def scan_unit_dir(num: int) -> dict | None:
    """**不读包，只按已落盘的音频文件**重建一个单位的 voicemap 条目。

    用在 ``--repair``：文件都导出来了，清单里却缺这一条（并发写覆盖 / 导出时
    异常中断），补回来只差"名字 + 时长"两样 —— 名字在文件名里，时长在 m4a 头里。
    命名规则与正式导出完全一致（:func:`act_of` / :func:`number_labels` /
    :func:`label_home`），所以补出来的条目和重新导出没有区别。
    """
    d = os.path.join(OUT, str(int(num)))
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return None
    clips = []
    for fn in names:
        if not fn.startswith("voice_") or not fn.endswith(".m4a"):
            continue
        cname = fn[:-4]
        rel = "media/audio/voice/%d/%s" % (int(num), fn)
        clips.append([cname, rel, mp4_duration(os.path.join(d, fn)), act_of(cname)])
    if not clips:
        # 战斗语音一段都没有 → 这个单位本来就没配这套，别造空条目
        return None
    number_labels(clips)
    seen = {round(float(c[2]), 2) for c in clips}
    home = []
    for fn in names:
        if not fn.startswith("sys_") or not fn.endswith(".m4a"):
            continue
        cname = fn[4:-4]                        # 80000_01101
        rel = "media/audio/voice/%d/%s" % (int(num), fn)
        dur = mp4_duration(os.path.join(d, fn))
        if round(dur, 2) in seen:               # 与战斗语音重复的那批剔掉
            continue
        home.append([cname, rel, dur, ""])
    label_home(num, home)
    kind = "char" if num < 10000 else ("npc" if num < 20000 else "enemy")
    return {"id": int(num), "k": kind, "clips": clips, "home": home}


def repair_map(log=_log) -> int:
    """补齐 ``voicemap.js`` 里**文件已存在但清单缺条目**的单位。"""
    data = load_map()
    have = {u.get("id"): u for u in (data.get("units") or []) if u.get("id") is not None}
    added = []
    try:
        dirs = [d for d in os.listdir(OUT) if d.isdigit()]
    except OSError:
        dirs = []
    for d in sorted(dirs, key=int):
        num = int(d)
        cur = have.get(num)
        if cur and (cur.get("clips") or []):
            continue
        ent = scan_unit_dir(num)
        if not ent:
            continue
        have[num] = ent
        added.append(num)
    if added:
        merge_save({k: have[k] for k in added})
        log("补齐了 %d 个单位：%s" % (len(added), ",".join(str(x) for x in added)))
    else:
        log("清单已经齐了，没有可补的单位")
    data = load_map()
    total = sum(len(u.get("clips") or []) for u in data.get("units") or [])
    log("voicemap.js：%d 个单位 / 战斗 %d 段" % (len(data.get("units") or []), total))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="导出单位战斗语音")
    ap.add_argument("--chars", default="", help="逗号分隔的编号（1,11,10001）")
    ap.add_argument("--npc", action="store_true", help="清单里全部 NPC / 敌人")
    ap.add_argument("--all", action="store_true", help="全部")
    ap.add_argument("--force", action="store_true", help="重做已存在的音频")
    ap.add_argument("--relabel", action="store_true",
                    help="只按槽位规律给已导出的语音重新命名，不下载不转码")
    ap.add_argument("--repair", action="store_true",
                    help="不读包：按已落盘的 m4a 文件补齐清单里缺失的单位条目")
    a = ap.parse_args(argv)

    if a.repair:
        return repair_map()

    if a.relabel:
        # 版本升级时用：老条目还叫「其他 1..N」，按通用槽位规律重算一次。
        # 已经人工/ASR 对过表的精确名字由 label_home 自己保留。
        data = load_map()
        n = 0
        touched = {}
        for u in data.get("units") or []:
            if u.get("home"):
                label_home(u["id"], u["home"])
                touched[u["id"]] = u
                n += 1
        merge_save(touched)
        _log(f"重新命名了 {n} 个单位的「其他语音」")
        return 0

    nums: list[int] = []
    if a.chars:
        nums += [int(x) for x in a.chars.replace(" ", "").split(",") if x]
    if a.npc or a.all:
        import sys as _s
        _s.path.insert(0, os.path.abspath(os.path.join("..", "..")))
        try:
            from server.cdn import manifest
            idx = manifest.load_index()
        except Exception:                                        # noqa: BLE001
            idx = {}
        for n in idx:
            m = re.match(r"^bundle_audioclipsprefab_unit_(\d+)$", n)
            if not m:
                continue
            v = int(m.group(1))
            if a.all or v >= 10000:
                nums.append(v)
    nums = sorted(set(nums))
    if not nums:
        _log("没有指定要导出的单位（用 --chars / --npc / --all）")
        return 2

    data = load_map()
    have = {u.get("id"): u for u in data.get("units") or []}
    t0 = time.time()
    ok = miss = 0
    touched = {}
    for i, num in enumerate(nums, 1):
        try:
            ent = export_unit(num, force=a.force)
        except Exception as e:                                   # noqa: BLE001
            _log(f"  ! {num} 失败：{str(e)[:100]}")
            ent = None
        if ent:
            # 重新导出别把「已经人工/ASR 对过表的精确名字和台词」冲掉。
            # 通用槽位名（ホーム/追加/其他…）会被 label_home 重新算，无需保留。
            old = have.get(int(num)) or {}
            prev = {r[0]: r for r in (old.get("home") or []) if r}
            for r in ent.get("home") or []:
                p = prev.get(r[0])
                if not p:
                    continue
                if len(p) > 3 and p[3] and not p[3].startswith(
                        ("其他", "ホーム", "追加", "战斗")):
                    r[3] = p[3]
                if len(p) > 4 and p[4]:
                    while len(r) < 5:
                        r.append("")
                    r[4] = p[4]
            have[int(num)] = ent
            touched[int(num)] = ent
            ok += 1
        else:
            miss += 1
        if i % 20 == 0 or i == len(nums):
            _log(f"  [{i}/{len(nums)}] ok={ok} miss={miss} {time.time() - t0:.0f}s")
    # **只写本次碰过的单位**（持锁读改写）：并行跑的几个导出进程不会互相覆盖。
    data = merge_save(touched)
    total = sum(len(u.get("clips") or []) for u in data["units"])
    nhome = sum(len(u.get("home") or []) for u in data["units"])
    _log(f"voicemap.js：{len(data['units'])} 个单位 / 战斗 {total} 段"
         f" + 其余 {nhome} 段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
