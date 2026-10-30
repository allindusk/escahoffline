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
                     FILES_STORY, IMG, ROOT, SCRIPTS, WEB, WS,
                     add_scripts_to_path)
from .net import fetch_many

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
          done_offset: int = 0) -> dict:
    """下载一批资源包（``entries`` 是清单条目 dict 列表）。默认跳过已完成的。

    ``pause_event`` 由任务传入，用来支持「暂停 / 继续」。
    """
    return fetch_many(entries, workers=workers, desc=desc, skip_existing=skip,
                      pause_event=pause_event, job=job,
                      done_offset=done_offset)


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
        tail = (p.stdout or "").strip().splitlines()[-8:]
        for ln in tail:
            print("      " + ln)
        if p.returncode != 0 and p.stderr:
            print("      stderr: " + p.stderr.strip()[-400:])
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


def sync_advindex() -> int:
    """把已接入的非角色剧情汇总成 ``web/data/advindex.js``（侧栏列表数据源）。"""
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
        # 封面：章节标题横幅（游戏里标题本身就是图，没有文本资源）
        cover = ""
        if n is not None:
            rel = f"media/img/story/{kind}_{int(n)}.webp"
            if os.path.exists(os.path.join(WEB, rel)):
                cover = rel
        rows.append({
            "key": key,
            "kind": kind,
            # tutorial / op 无编号，no = None（前端不显示「第X话」）
            "no": n,
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
    rows.sort(key=lambda r: (select.ADV_KINDS.index(r["kind"])
                             if r["kind"] in select.ADV_KINDS else 9, r["no"] or 0))
    p = os.path.join(DATA, "advindex.js")
    with open(p, "w", encoding="utf-8") as f:
        f.write("window.__ADV=" + json.dumps(rows, ensure_ascii=False,
                                             separators=(",", ":")) + ";")
    print(f"  advindex.js：{len(rows)} 部剧情")
    return len(rows)


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
                m = re.fullmatch(r"topic_([a-z]+)_(\d+)", sp.m_Name or "")
                if not m:
                    continue
                base = name_map.get(m.group(1), m.group(1))
                dst = os.path.join(out_dir, f"{base}_{int(m.group(2))}.webp")
                if os.path.exists(dst):
                    n_skip += 1
                    continue
                sp.image.save(dst, "WEBP", quality=88, method=4)
                n_new += 1
            except Exception:
                continue
    print(f"  剧情封面：新增 {n_new} 跳过 {n_skip}")
    return {"new": n_new, "skip": n_skip}


def export_avatar(cid, size: int = 96) -> bool:
    """从 c-panel 半身像左上角裁正方形做头像 -> ``web/img/avatar/<cid>.png``。"""
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
    if os.path.exists(out) or not src:
        return os.path.exists(out)
    try:
        env = UnityPy.load(src)
        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            im = obj.read().image
            side = min(im.width, im.height)
            half = min(im.width, side)            # 半身像取左侧正方形
            im.crop((0, 0, half, min(im.height, half))).resize(
                (size, size), Image.LANCZOS).save(out, optimize=True)
            return True
    except Exception as e:
        print(f"    头像导出失败 {cid}: {e}")
    return False


# ================================================================ 立绘合成


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
        print(f"  compose_stand_all 退出码 {rc}")

    # merge：旧值为底，本次结果覆盖
    if backup and os.path.exists(slot_path):
        old = json.load(open(backup, encoding="utf-8"))
        new = json.load(open(slot_path, encoding="utf-8"))
        merged = dict(old)
        merged.update(new)
        with open(slot_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, separators=(",", ":"))
        print(f"  stand_slot.json merge：旧 {len(old)} + 新 {len(new)}"
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
            print(f"  ⚠ 立绘落点可疑 {len(mine)} 组（表情/底图对不上，见 "
                  f"_archive/stand_slot_qc.json）")
            for b in mine[:5]:
                print(f"    {b['key']} slot={b['slot']} diff={b['diff']}")
    print(f"  合成用时 {time.time() - t0:.0f}s")


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
    if not os.path.exists(src):
        print("  没有 sfxmap.json（先跑 export_sfx.py music/sound）")
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
    return res
