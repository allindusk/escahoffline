"""按需安装：从官方 CDN 直接拉 AssetBundle → 本地解包 → 应用到播放器。

这是整个「官方直连」模式的门面，UI 只跟这里打交道。

一次安装做了什么（以角色为例）
------------------------------
1. 确保资源清单在本地（``Manifest1Ex/2Ex``，AES 加密，拉一次约 3.5 MB）
2. 按清单算出这个角色**真正需要**的 bundle：
   剧本包 + 语音包（按段）+ 头像/半身像包
3. 并发下载这些 bundle（默认 32 并连，CDN 单连接只有 60–100 KB/s，
   并发是唯一有效手段）
4. 解剧本 → ``web/data/char_<id>.js``（播放器的数据源）
5. 解语音 → ``web/media/audio/<id>/*.m4a``
6. 从剧本里读出引用的背景/CG/立绘，**只下缺的那些**图片包
7. 立绘要合成表情差分（底图 + 脸部件，落点靠像素匹配算），产出
   ``web/media/img/stand/*.webp``
8. 刷新索引与映射表（``index.js`` / ``imgmap.js`` / ``sfxmap.js`` / ``names.js``）

为什么不在浏览器里做：要 UnityPy 解 Unity 对象、numpy 做贴图落点匹配，
这套东西必须跑在本机的 Python 里。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time

from . import config, manifest, net, select, stages

#: 从 net 的日志里抠出 "[123/456]" 当进度
_PROG = re.compile(r"\[(\d+)/(\d+)\]")


class CDNError(RuntimeError):
    """给用户看的错误（缺依赖、清单拉不到、角色不存在…）"""


# ---------------------------------------------------------------- 进度桥
@contextlib.contextmanager
def _capture(job):
    """把 cdn.net 的下载日志同时喂给 Job（进度条）和 stdout（日志）。"""
    old = net._log

    def hook(msg):
        s = str(msg)
        if job is not None:
            m = _PROG.search(s)
            try:
                if m:
                    # 注意：Job.progress() 没有 total 参数（多传会被吃掉进 detail），
                    # 总量必须用 set_total() 单独设，否则进度条永远是 0%
                    job.set_total(int(m.group(2)), "包")
                    job.progress(done=int(m.group(1)), message=s[:200])
                else:
                    job.log(s[:200])
            except Exception:                                  # noqa: BLE001
                pass
        try:
            print(s, flush=True)
        except Exception:                                      # noqa: BLE001
            pass

    net._log = hook
    try:
        yield
    finally:
        net._log = old


# ---------------------------------------------------------------- 依赖自检
#: 解包必需的三方库（模块名, 用途）
_REQUIRED_MODS = (("UnityPy", "解 Unity 资源对象"),
                  ("lz4", "解压 UnityFS 容器"),
                  ("PIL", "写出 webp"),
                  ("numpy", "立绘落点匹配"))


def missing_deps() -> list[str]:
    """解包必需的三方库。缺任何一个都不能安装。

    **真导入**一次：能发现"装了但装坏了"（位数 / ABI 不匹配）这种隐性故障。
    只用在真正要解包之前（装剧情）；不要用在 ``/api/state`` 这种热路径上。
    """
    out = []
    for mod, note in _REQUIRED_MODS:
        try:
            __import__(mod)
        except Exception:                                      # noqa: BLE001
            out.append(f"{mod}（{note}）")
    return out


def _spec_ok(name: str) -> bool:
    """只查"有没有装"，**不执行模块代码**。"""
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:                                          # noqa: BLE001
        return False


def missing_deps_quick() -> list[str]:
    """``status()`` 专用的轻量版：只 find_spec，不 import。

    为什么必须分开：``import UnityPy`` 首次要 **850 ms**，而 ``status()`` 挂在
    ``/api/state`` 上、页面一打开就会被调用 → 那一次请求从 20 ms 变成 1 秒
    （日志里就是 ``GET /api/state 200 2099ms``）。
    """
    return [f"{mod}（{note}）" for mod, note in _REQUIRED_MODS
            if not _spec_ok(mod)]


_STATUS_CACHE: dict = {"at": 0.0, "data": None}
_STATUS_TTL = 30.0


def status(force: bool = False) -> dict:
    """给控制台用的现状：CDN、清单、可安装单元数量。

    带 30 秒缓存：``/api/state`` 被控制台每秒轮询，而这里面要解析 4.6 MB 的
    清单 JSON，不缓存会把接口拖到 100 ms 以上。
    """
    if not force and _STATUS_CACHE["data"] is not None \
            and time.time() - _STATUS_CACHE["at"] < _STATUS_TTL:
        return _STATUS_CACHE["data"]
    config.ensure_dirs()
    # 用 quick 版：这里只给前端显示"缺什么"，不能为了它去 import UnityPy（850 ms）
    missing = missing_deps_quick()
    info = {
        "cdn": config.cdn_base(),
        "manifestReady": manifest.has_manifest(),
        "manifest": "",
        "units": {"char": 0, "adv": 0},
        "missingDeps": missing,
        "ready": not missing,
        "bundleCache": _cache_size(),
    }
    if info["manifestReady"]:
        try:
            # 别用 manifest.summary()：它按 .raw 是否存在判断，
            # 而 .raw 现在解密完就删，会误报"Manifest1Ex=缺失"。
            info["manifest"] = "已就绪 · {} 个资源包".format(
                len(manifest.load_index()))
            u = list_units()
            info["units"] = {"char": len(u["char"]), "adv": len(u["adv"])}
            info["bundles"] = len(manifest.load_index())
        except Exception as e:                                 # noqa: BLE001
            info["manifest"] = f"清单读取失败：{e}"
    _STATUS_CACHE.update(at=time.time(), data=info)
    return info


_SIZE_CACHE: dict = {"at": 0.0, "data": None}
_SIZE_TTL = 60.0


def invalidate_cache_size() -> None:
    """清 bundle 缓存体积缓存（清理/安装之后调用，否则 /api/state 还报旧值）。"""
    _SIZE_CACHE.update(at=0.0, data=None)


def _cache_size(force: bool = False) -> dict:
    """bundle 缓存体积。带 60 秒缓存。

    这里要对缓存目录 scandir 上万个文件，而它同时挂在 ``/api/state``（被轮询）
    与 ``coverage()`` 上，不缓存会持续拖慢接口。
    """
    if not force and _SIZE_CACHE["data"] is not None \
            and time.time() - _SIZE_CACHE["at"] < _SIZE_TTL:
        return _SIZE_CACHE["data"]
    n = b = 0
    for root in (config.FILES_STORY, config.FILES_IMG, config.FILES_AUDIO):
        if not os.path.isdir(root):
            continue
        for entry in os.scandir(root):
            try:
                if entry.is_file():
                    n += 1
                    b += entry.stat().st_size
            except OSError:
                pass
    out = {"files": n, "bytes": b}
    _SIZE_CACHE.update(at=time.time(), data=out)
    return out


# ---------------------------------------------------------------- 清单
def ensure_manifest(job=None, force: bool = False) -> dict:
    config.ensure_dirs()
    if not force and manifest.has_manifest():
        return manifest.load_index()
    if job is not None:
        job.log("下载并解密官方资源清单（约 3.5 MB，首次才需要）…")
    try:
        manifest.refresh()
    except Exception as e:                                     # noqa: BLE001
        raise CDNError(f"拉取资源清单失败：{e}") from e
    if job is not None:
        job.log(manifest.summary())
    return manifest.load_index()


def index_manifest() -> select.Manifest:
    return select.Manifest(ensure_manifest())


_UNITS_CACHE: dict = {"at": 0.0, "data": None}
_UNITS_TTL = 120.0


def list_units(job=None) -> dict:
    """云端可安装的单元：``{"char": ["char_1", ...], "adv": ["adv_main_1", ...]}``

    结果带 2 分钟缓存：``/api/state`` 会被控制台轮询，每次都全量扫 2.5 万个
    bundle 名没必要（实测单次约 60 ms，但轮询 ×2 就是纯浪费）。
    """
    if _UNITS_CACHE["data"] is not None and time.time() - _UNITS_CACHE["at"] < _UNITS_TTL:
        return _UNITS_CACHE["data"]
    idx = ensure_manifest(job)
    chars = [f"char_{c}" for c in select.char_ids(idx)]
    advs = []
    for item in select.adv_ids(idx):
        kind, no = item if isinstance(item, (tuple, list)) else (item, None)
        advs.append(select.story_key(kind, no))
    out = {"char": chars, "adv": advs}
    _UNITS_CACHE.update(at=time.time(), data=out)
    return out


def invalidate_units() -> None:
    """清单/安装状态变了之后清缓存，让界面立刻看到最新状态。"""
    _UNITS_CACHE["data"] = None
    _LOCAL_CACHE["data"] = None
    _STATUS_CACHE["data"] = None


def unit_state(key: str) -> str:
    """本地某部剧情的状态（以**磁盘文件**为证据，不依赖任何记账文件）。

    ``ready``        剧本 + 语音都在
    ``soft-missing`` 只有剧本，没语音（能播，静音）
    ``partial``      剧本文件异常小（解包中断）
    ``missing``      没装
    """
    data = os.path.join(config.DATA, f"{key}.js")
    if not os.path.exists(data):
        return "missing"
    try:
        if os.path.getsize(data) < 500:
            return "partial"
    except OSError:
        return "missing"
    vdir = os.path.join(config.AUDIO, key[5:] if key.startswith("char_") else key)
    # 只判目录存在，不进去数文件：611 个单元逐个 scandir 要 1.5 秒，
    # 而语音目录只有"安装时创建"这一条来源，存在即视为有语音。
    if os.path.isdir(vdir):
        return "ready"
    return "soft-missing"


_LOCAL_CACHE: dict = {"at": 0.0, "data": None}
_LOCAL_TTL = 8.0


def local_units(force: bool = False) -> dict[str, str]:
    """本地已安装的剧情 → 状态。

    带 8 秒缓存：``/api/state`` 被轮询，而这里要对每个单元 stat 一次剧本文件
    再 scandir 语音目录（611 个单元 ≈ 200 ms），不缓存会明显拖慢接口。
    """
    if not force and _LOCAL_CACHE["data"] is not None \
            and time.time() - _LOCAL_CACHE["at"] < _LOCAL_TTL:
        return _LOCAL_CACHE["data"]
    out = {}
    if os.path.isdir(config.DATA):
        for name in os.listdir(config.DATA):
            if not name.endswith(".js"):
                continue
            key = name[:-3]
            if key.startswith("char_") or key.startswith("adv_"):
                out[key] = unit_state(key)
    _LOCAL_CACHE.update(at=time.time(), data=out)
    return out


def unit_status_map() -> dict[str, str]:
    """全部单元的状态：本地已有的用实际状态，云端有但本地没有的记 missing。

    **清单不存在时只返回本地状态**：``list_units()`` 会触发 ``ensure_manifest()``
    也就是"发现没清单就下载 5 MB"。而本函数挂在 ``/api/state`` 上被页面一打开就
    调用 —— 放在这里会让打开控制台先卡 16 秒，还让"请先下载清单"的引导永远出不来。
    """
    out = local_units()
    if not manifest.has_manifest():
        return out
    try:
        u = list_units()
        for k in u.get("char", []) + u.get("adv", []):
            out.setdefault(k, "missing")
    except Exception:                                          # noqa: BLE001
        pass
    return out


#: 公共素材的类别与体积（来自清单实测，2026-10-29）
#:
#: **avatar / banner 必须在这里登记**：`/api/install/base` 会用本字典做白名单
#: 过滤（`parts = [p for p in raw if p in PUBLIC_PARTS]`），以前漏了 avatar，
#: 前端又没显式传 parts → 头像分支永远不执行 → 用户"下完公共素材还是没头像"
#: （侧栏与 CG 页都靠 `avatarsReady()`，于是整片首字色块）。
PUBLIC_PARTS = {
    "bg": ("背景", "bundle_bgimage_", 205),
    "bgm": ("BGM 音乐", "bundle_music_", 539),
    "sfx": ("音效", "bundle_sound_", 31),
    "avatar": ("角色头像", "bundle_chara_c-panel_", 20),
    # 剧情横幅：角色剧情入口图 + 主线/活动/其他的章节横幅（播放器当标题用）
    "banner": ("剧情横幅", "bundle_episodeicons_episode_character_|bundle_image_story_", 40),
}

#: 横幅类包的两种前缀
BANNER_PREFIXES = ("bundle_episodeicons_episode_character_", "bundle_image_story_")

#: **清理缓存时必须保留**的包前缀。
#: 这些包不会被任何剧本引用（头像/横幅/背景/BGM/音效都是"全局共用"），
#: 若交给"按引用差集删"，用户清理一次就会把它们全删掉 ——
#: 表现就是「补全缺失」永远补不完（补完 → 清理 → 又缺）。
PROTECTED_PREFIXES = (
    "bundle_chara_c-panel_",              # 角色头像
    "bundle_statusbase_",                 # 状态图
    "bundle_episodeicons_",               # 角色剧情入口横幅
    "bundle_image_story_",                # 主线/活动/其他的章节横幅
    "bundle_miniicons_", "bundle_profileimages_",
    "bundle_home2charathumbnails_",
    "bundle_bgimage_",                    # 背景（公共素材）
    "bundle_music_", "bundle_sound_",     # BGM / 音效（公共素材）
)


def install_base(job=None, workers: int = 48,
                 parts=("bg", "bgm", "sfx", "avatar")) -> dict:
    """下载并解包「公共素材」：背景 / BGM / 音效。

    为什么值得单独做一个入口
    ------------------------
    这三类是**跨剧情共享**的：每部剧情都会用到背景和 BGM，按剧情重复下载
    既慢又占地方。而且剧本里的 ``PlayMusic {"Music": "89B98A79_..."}``
    能精确反查到 ``bundle_music_89b98a79_...``（实测 10/10 命中），
    所以**全量下齐之后，所有剧情的 BGM 都能响**——这是"没音乐"的根治办法。

    体积（清单实测）：背景 205 MB + BGM 539 MB + 音效 31 MB + 头像 20 MB ≈ **795 MB**。
    相对全量素材 20.75 GB 只占 3.8%，但之后装任何剧情都不用再补。

    关于头像（avatar）
    ------------------
    侧栏头像来自 ``bundle_chara_c-panel_<cid>``（角色面板的半身像）。剧本**不引用**
    它，所以按剧本引用下载是拿不到的；但它只有 384 个 / 19.8 MB，且 dependencies 为空
    （不跟任何包绑定），因此归入公共素材最合适：
       · 体积可以忽略
       · **没装的角色也能显示头像**，方便认人（若按剧情下载，未装角色就是空白）
    """
    config.ensure_dirs()
    mf = select.Manifest(ensure_manifest(job))
    out: dict = {"bytes": 0}

    # 先把"总共要下多少个包"算出来 —— 否则任务刚启动时 total 是 0，
    # 界面会显示成 0 B/0 B（用户完全看不出在干嘛）。
    _total = 0
    if "bg" in parts:
        from .. import media as _m
        _total += len((_m.load_imgmap().get("back") or {}))
    if "bgm" in parts:
        _total += len([n for n in mf.index if n.startswith("bundle_music_")])
    if "sfx" in parts:
        _total += len([n for n in mf.index if n.startswith("bundle_sound_")])
    if "avatar" in parts:
        _total += len([n for n in mf.index if n.startswith("bundle_chara_c-panel_")])
    if "banner" in parts:
        _total += len([n for n in mf.index if n.startswith(BANNER_PREFIXES)])
    fails: list[str] = []

    def _collect(tag: str, res: dict) -> None:
        """记录下载失败：写进任务日志（带包名），最后统一抛 CDNError。"""
        bad = (res or {}).get("fail") or []
        if not bad:
            return
        sample = "、".join(f"{n}（{str(e)[:40]}）" for n, e in bad[:5])
        fails.append(f"{tag} {len(bad)} 个：{sample}")
        if job is not None:
            job.log(f"⚠ {tag} 有 {len(bad)} 个包下载失败：{sample}")

    done_base = 0
    if job is not None and _total:
        job.set_total(_total, "个")
        # 总字节也一起给出来，进度条才能显示"12.3 / 775 MB"
        _bytes = 0
        if "bg" in parts:
            _bytes += sum(int((mf.index.get(mf.back_bundle(k)) or {}).get("size") or 0)
                          for k in ((_m.load_imgmap().get("back") or {})))
        if "avatar" in parts:
            _bytes += sum(int(e.get("size") or 0)
                          for n, e in mf.index.items()
                          if n.startswith("bundle_chara_c-panel_"))
        if "banner" in parts:
            _bytes += sum(int(e.get("size") or 0)
                          for n, e in mf.index.items()
                          if n.startswith(BANNER_PREFIXES))
        for _pref in ("bundle_music_", "bundle_sound_"):
            if (_pref == "bundle_music_" and "bgm" not in parts) or \
               (_pref == "bundle_sound_" and "sfx" not in parts):
                continue
            _bytes += sum(int(e.get("size") or 0) for n, e in mf.index.items()
                          if n.startswith(_pref))
        job.bytes_total = _bytes

    # 先把「要下的全部包」按类别登记好，再分两个阶段执行：
    #   阶段一：一股脑把全部资源包下载完（网络瓶颈，默认 32 并发）
    #   阶段二：逐个类别解包（CPU/磁盘瓶颈）
    # 这样背景解包（比下载慢）与 BGM 下载（长板）能错峰，
    # 而不是「下完一批、解完一批、再下一批」地串行浪费时间。
    plan = []
    if "bg" in parts:
        from .. import media as _media
        keys = list((_media.load_imgmap().get("back") or {}))
        ents = _entries(mf, [mf.back_bundle(k) for k in keys])
        sz = sum(int(e.get("size") or 0) for e in ents)
        plan.append({"tag": "bg", "label": "背景", "ents": ents, "sz": sz,
                     "keys": keys})
    if "bgm" in parts:
        ents = [e for n, e in mf.index.items() if n.startswith("bundle_music_")]
        sz = sum(int(e.get("size") or 0) for e in ents)
        plan.append({"tag": "bgm", "label": "BGM", "ents": ents, "sz": sz})
    if "sfx" in parts:
        ents = [e for n, e in mf.index.items() if n.startswith("bundle_sound_")]
        sz = sum(int(e.get("size") or 0) for e in ents)
        plan.append({"tag": "sfx", "label": "音效", "ents": ents, "sz": sz})
    if "avatar" in parts:
        ents = [e for n, e in mf.index.items()
                if n.startswith("bundle_chara_c-panel_")]
        sz = sum(int(e.get("size") or 0) for e in ents)
        plan.append({"tag": "avatar", "label": "角色头像", "ents": ents, "sz": sz})
    if "banner" in parts:
        names = [n for n in mf.index if n.startswith(BANNER_PREFIXES)]
        ents = _entries(mf, names)
        sz = sum(int(e.get("size") or 0) for e in ents)
        plan.append({"tag": "banner", "label": "剧情横幅", "ents": ents, "sz": sz})

    # ---- 进度条的起点：先把"磁盘上已有、这次不会重下"的字节扫进来 ----
    # 必须是全量已完成字节，而不是本次新下的字节：用户第二次点「补全」时，
    # 背景/音乐/音效大半已经在本地（一个字节都不用传），而总量仍是全量 893 MB。
    # 以前只累加新下的量，于是任务条上出现「70.9% · 8.40 MB / 893.01 MB」——
    # 8 MB 按总量算才 1%，两个数字互相打脸（用户就是这么报的）。
    _have = 0
    if job is not None and job.bytes_total:
        for step in plan:
            for e in step["ents"]:
                name = str(e.get("bundle_name") or "")
                sz = int(e.get("size") or 0)
                if not name or not sz:
                    continue
                try:
                    if not net.need_download(net.bundle_path(name), sz):
                        _have += sz
                except OSError:
                    pass
        if _have:
            job.bytes_done = max(job.bytes_done, float(_have))
            job.log(f"本地已有 {_have / 1048576:.0f} MB 不会重下，进度从这部分开始算")

    # ---- 阶段一：下载全部资源包 ----
    done_base = 0
    for step in plan:
        ents = step["ents"]
        if job is not None and ents:
            job.log(f"下载{step['label']} {len(ents)} 个包 "
                    f"/ {step['sz'] / 1048576:.0f} MB")
        _collect(step["label"],
                 stages.fetch(ents, workers, f"{step['label']}素材", skip=True,
                              pause_event=_pe(job), job=job,
                              done_offset=done_base,
                              # 上面已经预扫过"已存在"的字节，别再重复累加
                              count_skipped=False))
        done_base += len(ents)

    # ---- 阶段二：逐个类别解包 ----
    # 百分比分两段：下载 0–60%、解包 60–100%。解包（尤其背景）常常比下载还久，
    # 如果仍按下载字节算，下载一完进度条就顶到 100% 干等十几分钟，像卡死了。
    if job is not None:
        job.phase(60.0, 40.0, total=max(1, len(plan)), unit="类")
        job.progress(done=0)
    for step in plan:
        tag = step["tag"]
        ents = step["ents"]
        sz = step["sz"]
        if job is not None and ents:
            extra = "，这步比下载还久" if tag == "bg" else ""
            job.progress(message=f"解包{step['label']}（{len(ents)} 个包{extra}）…")
        if tag == "bg":
            stages.export_images(mf, {"back": set(step["keys"]),
                                      "stand": set(), "cg": set()})
            out["bg"] = len(ents)
            out["bytes"] += sz
        elif tag in ("bgm", "sfx"):
            kind = "music" if tag == "bgm" else "sound"
            cnt = stages.run_script("export_sfx.py", kind, quiet=True)
            if cnt != 0 and job is not None:
                job.log(f"   {step['label']}导出异常（exit={cnt}），已跳过")
            stages.regen_sfxmap()
            out[kind] = len(ents)
            out["bytes"] += sz
        elif tag == "avatar":
            n_ok = n_bad = 0
            for i, e in enumerate(ents, 1):
                # 384 个头像逐个导出要好几分钟，中途得让任务条有话说
                if job is not None and (i % 20 == 0 or i == len(ents)):
                    job.progress(message=f"解包角色头像 {i}/{len(ents)}…")
                name = str(e.get("bundle_name") or "")
                tail = name.rsplit("_", 1)[-1]
                try:
                    # force=True：旧版本生成过一批错头像（同一张图贴到多个 id），
                    # 必须重做才能修好；x2 一起重做，否则播放器优先读的还是旧超分图。
                    if stages.export_avatar(int(tail), force=True):
                        n_ok += 1
                    else:
                        n_bad += 1
                except Exception as ex:                        # noqa: BLE001
                    n_bad += 1
                    if job is not None and n_bad <= 3:
                        job.log(f"   头像导出失败 cid={tail}：{str(ex)[:80]}")
            # 让 imgmap.avatar 带上这些头像，前端就不用靠试扩展名
            try:
                stages.regen_imgmap()
            except Exception:                                  # noqa: BLE001
                pass
            out["avatar"] = n_ok
            out["bytes"] += sz
            if job is not None:
                job.log(f"   头像生成完毕：{n_ok}/{len(ents)}"
                        + (f"，失败 {n_bad} 个" if n_bad else ""))
            if n_bad:
                fails.append(f"头像导出失败 {n_bad} 个"
                             "（多为包损坏或缺少解码组件）")
        elif tag == "banner":
            try:
                stages.export_char_banners()
            except Exception as e:                             # noqa: BLE001
                if job is not None:
                    job.log(f"   角色横幅导出失败：{e}")
            try:
                stages.export_story_topics()
            except Exception as e:                             # noqa: BLE001
                if job is not None:
                    job.log(f"   章节横幅导出失败：{e}")
            out["banner"] = len(ents)
            out["bytes"] += sz

        # 每解包完一类推一格（解包阶段百分比由"类"决定，见上面 job.phase）
        if job is not None:
            job.progress(add=1, message=f"{step['label']} 已就绪")

    # 记录本次各类的清单包数，下次刷新清单据此判断"有没有新增"
    # **有失败就不写**：写了等于"这一类已经跟上清单"，下次刷新就不会自动补，
    # 用户只能一直看到"补全缺失"却不知道为啥不自动补。
    if not fails:
        try:
            counts = _cur_counts()
            sub = {k: v for k, v in counts.items() if k in parts}
            if sub:
                write_stamp(sub)
        except Exception:                                      # noqa: BLE001
            pass

    # ⑤ 收尾体检：把"还没齐"的类别点出来，别让用户对着一个 98% 的数字猜
    try:
        st = public_status()
        short = []
        for name, have_k, total_k in (("背景", "back", "backTotal"),
                                      ("BGM", "music", "musicTotal"),
                                      ("音效", "sfx", "sfxTotal"),
                                      ("头像", "avatar", "avatarTotal"),
                                      ("横幅", "banner", "bannerTotal")):
            have, total = int(st.get(have_k) or 0), int(st.get(total_k) or 0)
            if total and have < total * 0.98:
                short.append(f"{name} {have}/{total}")
        out["public"] = st
        out["short"] = short
        if job is not None:
            if short:
                job.log("⚠ 仍未齐：" + "、".join(short)
                        + "（官方源本身可能缺图；若是导出失败，日志里有原因）")
            job.detail["base"] = out
            job.log(f"公共素材就绪：约 {out['bytes'] / 1048576:.0f} MB"
                    f"（以后装任何剧情都不用再补背景和音乐）")
    except Exception:                                          # noqa: BLE001
        if job is not None:
            job.detail["base"] = out

    if fails:
        # 有失败就抛错：前端会弹提示（任务条显示 j.error），用户能看到"卡在哪"，
        # 而不是对着一个永远 97% 的数字猜。完整清单在任务日志里。
        raise CDNError("部分素材没装上 · " + fails[0]
                       + (f"（另有 {len(fails) - 1} 类失败，详见日志）"
                          if len(fails) > 1 else ""))

    return out


def _stamp_path() -> str:
    return os.path.join(config.MANIFEST_DIR, "public.json")


def read_stamp() -> dict:
    """上次装完公共素材时，清单里各分类的包数。

    用途：官方上新后，刷新清单能算出"这次多了几首 BGM"，从而只补新增的。
    没有它就只能比"本地已有 vs 清单总数"，而背景永远差 11 个（官方包里就没图），
    会变成每次刷新都触发一次全量检查。
    """
    try:
        with open(_stamp_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                          # noqa: BLE001
        return {}


def write_stamp(counts: dict) -> None:
    """只覆盖传入的键：单独补过 BGM 不该把背景也标成已跟上。"""
    st = read_stamp()
    st.update(counts)
    try:
        os.makedirs(config.MANIFEST_DIR, exist_ok=True)
        with open(_stamp_path(), "w", encoding="utf-8", newline="\n") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:                                          # noqa: BLE001
        pass


def _cur_counts() -> dict:
    idx = config_manifest_index()
    return {
        "bg": len([n for n in idx if n.startswith("bundle_bgimage_")]),
        "bgm": len([n for n in idx if n.startswith("bundle_music_")]),
        "sfx": len([n for n in idx if n.startswith("bundle_sound_")]),
        "avatar": len([n for n in idx if n.startswith("bundle_chara_c-panel_")]),
        "banner": len([n for n in idx if n.startswith(BANNER_PREFIXES)]),
    }


def config_manifest_index() -> dict:
    try:
        return manifest.load_index()
    except Exception:                                          # noqa: BLE001
        return {}


def new_public() -> dict:
    """相对上次装完时，清单里新增了多少个公共素材包（只返回 > 0 的分类）。

    **新增的类别也要算进来**：旧版本的 stamp 只有 bg/bgm/sfx（avatar/banner
    是这次才加进公共素材的），如果按"have == 0 就跳过"处理，老用户永远补不上
    头像和横幅。所以「stamp 里没有这个键」= 这个类别从没下过 → 全量补。
    """
    st = read_stamp()
    if not st:
        return {}                     # 从没装过公共素材 → 不自动下 800 MB
    cur = _cur_counts()
    out = {}
    for k, v in cur.items():
        if k not in st:               # 老版本 stamp 没这个类别 → 全量补
            if v:
                out[k] = v
            continue
        have = int(st.get(k) or 0)
        # have == 0 表示这一类用户明确装过 0 个，别替他决定
        if have and v > have:
            out[k] = v - have
    return out


def public_status() -> dict:
    """公共素材完成度（按**解包产物**算，不是按 bundle 缓存）。

    之前的实现有三处错，导致界面出现"BGM 0/217（其实已下完）"
    "音效 722/505（分母比分子还小）"这种荒唐数字：
      · BGM 导出到 media/audio/sfx/（和音效同目录），却去 audio/music 里数
      · 音效分子用整个 sfx 目录的文件数，把 BGM 也算进去了
      · 背景总数取 imgmap 条目数，而 imgmap 只映射"已装剧情"用到的图
    """
    config.ensure_dirs()

    def _count(d, exts):
        if not os.path.isdir(d):
            return 0
        return len([n for n in os.listdir(d)
                    if not n.startswith(".") and n.lower().endswith(exts)])

    # 背景：清单里有多少个 bgimage 包，就应该是多少张
    try:
        idx = manifest.load_index()
    except Exception:                                          # noqa: BLE001
        idx = {}
    back_total = len([n for n in idx if n.startswith("bundle_bgimage_")]) or 612
    n_back = min(_count(os.path.join(config.IMG, "back"),
                        (".webp", ".png", ".jpg")), back_total)

    # BGM / 音效：都在 media/audio/sfx/ 里，用清单的包名反推文件名来区分
    music_total = len([n for n in idx if n.startswith("bundle_music_")]) or 217
    sfx_total = len([n for n in idx if n.startswith("bundle_sound_")]) or 505
    adir = os.path.join(config.AUDIO, "sfx")
    files = set()
    if os.path.isdir(adir):
        files = {os.path.splitext(n)[0] for n in os.listdir(adir)}
    # 用**小写**对比：BGM 里既有 "89B98A79_905E96CA96DA"（全大写）
    # 也有 "BGMDate"（英文混合大小写），统一转小写才不会漏算一半。
    music_names = {n[len("bundle_music_"):].lower()
                   for n in idx if n.startswith("bundle_music_")}
    files_low = {f.lower() for f in files}
    n_music = len(files_low & music_names) if music_names else 0
    n_sfx = len(files_low - music_names)

    # 注：不要按"bundle 缓存是否还在"算待下载 —— 老用户可能是早期解包、
    # 后来清过缓存，按缓存算会得出几百个虚假的"待下载"。判断是否齐全一律看
    # **解包产物**（上面的 back/music/sfx），官方新增时 backTotal 等会变大，
    # 前端自然显示成 601/620，用户点「更新」即可，不需要额外数字。
    adir2 = os.path.join(config.IMG, "avatar")
    n_avatar = _count(adir2, (".png", ".webp", ".jpg"))
    avatar_total = len([n2 for n2 in idx if n2.startswith("bundle_chara_c-panel_")]) or 384

    # 剧情横幅：按**包是否已下载**算（导出产物是 384 张角色图 + 若干章节图，
    # 而分母用"清单里这两类包的数量"，官方新增时会自然变大 → 前端显示成
    # 553/560 这类，用户点「补全」即可）
    from .net import bundle_path as _bp
    banner_names = [n for n in idx if n.startswith(BANNER_PREFIXES)]
    banner_total = len(banner_names) or 389
    n_banner = len([n for n in banner_names if os.path.exists(_bp(n))])
    # 已导出的横幅**图**。列表要不要显示横幅看的是这个，不是 bundle 缓存：
    # 用户清过「资源包缓存」后上面的 n_banner 会归零，但图还在磁盘上，
    # 控制台列表照样应该显示横幅（否则会莫名退回纯文字，看着像功能没了）。
    try:
        n_banner_img = _count(os.path.join(config.IMG, "story"),
                              (".webp", ".png", ".jpg"))
    except Exception:                                          # noqa: BLE001
        n_banner_img = 0

    return {"back": n_back, "backTotal": back_total,
            "music": min(n_music, music_total), "musicTotal": music_total,
            "sfx": min(n_sfx, sfx_total), "sfxTotal": sfx_total,
            "avatar": min(n_avatar, avatar_total), "avatarTotal": avatar_total,
            "banner": min(n_banner, banner_total), "bannerTotal": banner_total,
            "bannerImg": n_banner_img,
            # 结构版本：前端据此判断"后端是不是还没重启/还是旧代码"。
            # 加 banner 这一批字段时是 2 —— 前端看到 v<2 就不能把缺字段当成 0/0，
            # 否则界面会显示「横幅 0/0」这种看起来像"没有横幅"的假象。
            "v": 2,
            "bytes": 0}


def uninstall(unit: str, drop_cache: bool = False, job=None) -> dict:
    """卸载一部剧情的**已解包资源**（不碰 bundle 缓存，除非 ``drop_cache``）。

    删什么
    ------
    · ``web/data/<unit>.js``            剧本
    · ``web/media/audio/<id>/``         该剧情独占的语音
    · ``web/media/img*/avatar/<id>.*``  角色头像
    · ``web/media/img*/stand/stand_<n>_*``  该角色独占的立绘（按剧本引用的编号前缀）

    为什么背景 / CG 不删：它们按"被谁引用"共享，删了会让别的剧情黑图。
    要彻底回收那些，用「清理未引用的缓存」（它按全局引用关系算差集，安全）。

    ``drop_cache=True`` 时顺带跑一次 prune_cache —— 它只删"没有任何已装剧情
    引用"的包，且有"解析失败就不删"的护栏。
    """
    import glob
    import re

    config.ensure_dirs()
    if not (unit.startswith("char_") or unit.startswith("adv_")):
        raise ValueError(f"无法识别的剧情标识：{unit}")

    files = 0
    freed = 0

    def _rm(p):
        nonlocal files, freed
        try:
            sz = os.path.getsize(p)
        except OSError:
            return
        try:
            os.remove(p)
            files += 1
            freed += sz
        except OSError:
            pass

    # 先读剧本，拿到它引用的立绘编号（删了文件就读不到了）
    js = os.path.join(config.DATA, f"{unit}.js")
    nums = set()
    try:
        with open(js, encoding="utf-8") as f:
            raw = f.read()
        d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        for part in d.get("parts") or []:
            for key in part.get("stand") or []:
                m = re.match(r"(\d+)_", str(key))
                if m:
                    nums.add(int(m.group(1)))
    except Exception:                                          # noqa: BLE001
        pass
    _rm(js)

    # 语音目录（角色 / 章节独占）
    vname = unit[5:] if unit.startswith("char_") else unit
    vdir = os.path.join(config.AUDIO, vname)
    if os.path.isdir(vdir):
        for n in os.listdir(vdir):
            _rm(os.path.join(vdir, n))
        try:
            os.rmdir(vdir)
        except OSError:
            pass

    # 角色独占：头像 + 立绘（含超分版）
    if unit.startswith("char_"):
        cid = int(unit[5:])
        media_root = os.path.dirname(config.IMG)               # web/media
        for sub in ("img", "img_x2"):
            for ext in (".webp", ".png", ".jpg"):
                _rm(os.path.join(media_root, sub, "avatar", f"{cid}{ext}"))
            for n in nums:
                for p in glob.glob(os.path.join(media_root, sub, "stand",
                                                f"stand_{n}_*")):
                    _rm(p)

    out = {"unit": unit, "files": files, "bytes": freed}
    if job is not None:
        job.log(f"[{unit}] 删除 {files} 个文件 / 释放 {freed / 1048576:.1f} MB")
    if drop_cache:
        out["cache"] = prune_cache(job, dry=False)
    invalidate_units()
    return out


def _bname(x):
    """清单条目 / 包名 → 包名。``select.Manifest`` 的返回值两种都有。"""
    if x is None:
        return None
    if isinstance(x, dict):
        return x.get("bundle_name")
    return x if isinstance(x, str) and x else None


def _used_bundles(mf, key: str) -> set:
    """某部剧情实际用到的 bundle 名集合（和安装时的算法保持一致）。

    **不吞异常**：这个方法的结果直接决定"哪些缓存可以删"，一旦解析失败就返回空集，
    会让调用方以为"这个剧情什么都没用到" → 真删模式会把它的包全删掉。
    所以失败一律抛给上层，由上层决定放弃删除。
    """
    names = set()
    if key.startswith("char_"):
        cid = int(key[5:])
        names.add(_bname(mf.scenario(cid)))
        names.update(_bname(b) for b in mf.voices(cid))
        names.update(_bname(b) for b in mf.meta_images(cid))
        assets = select.collect_assets(select.load_char(cid))
    else:
        m = _UNIT_RE.match(key)
        if not m:
            raise ValueError(f"无法识别的剧情标识：{key}")
        kind = m.group(1)
        no = int(m.group(2)) if m.group(2) else None
        names.add(_bname(mf.adv_scenario(kind, no)))
        names.update(_bname(b) for b in
                     select.adv_voice_bundles(mf.index, kind, no))
        assets = select.collect_assets(select.load_story(key))
    for k in ("back", "cg", "stand"):
        for s in assets.get(k) or ():
            b = (mf.back_bundle(s) if k == "back"
                 else mf.cg_bundle(s) if k == "cg"
                 else mf.stand_bundle(s))
            names.add(_bname(b))
            mk = select.PAT_STAND_KEY.match(s) if k == "stand" else None
            if mk:
                names.add(_bname(mf.stand_full.get(int(mk.group(1)))))
    return {n for n in names if n}


def prune_cache(job=None, dry: bool = True) -> dict:
    """找出 bundle 缓存里**没有任何已装剧情引用**的包。

    什么时候会多下：
      1. 装了剧情后来又删了 `web/data/<unit>.js`（文件走了，bundle 还在）
      2. 官方换过命名，旧包没人引用了
      3. 立绘合成只用了部分分层包
    默认 ``dry=True`` 只统计不删，用户确认后再删。
    """
    config.ensure_dirs()
    mf = select.Manifest(ensure_manifest(job))
    units = list(local_units())
    if job is not None:
        job.log(f"扫描 {len(units)} 部已装剧情，统计它们引用的 bundle…")
        job.set_total(max(1, len(units)))
    used = set()
    failed = []
    for i, k in enumerate(units, 1):
        if job is not None and job.cancelled:
            break
        try:
            used |= _used_bundles(mf, k)
        except Exception as e:                                 # noqa: BLE001
            failed.append(f"{k}: {str(e)[:60]}")
        if job is not None and i % 20 == 0:
            job.progress(done=i, message=f"统计引用 {i}/{len(units)}")
    if failed:
        # 有剧情解析不出来 → 它的包会被误判成"没人用"，宁可不删
        dry = True
        if job is not None:
            job.log(f"⚠ {len(failed)} 部剧情解析失败（如 {failed[0]}），"
                    f"为避免误删，本次只统计不删除")

    have = []
    for root in (config.FILES_STORY, config.FILES_IMG, config.FILES_AUDIO):
        if not os.path.isdir(root):
            continue
        for e in os.scandir(root):
            try:
                if e.is_file():
                    have.append((e.name, e.stat().st_size, e.path))
            except OSError:
                pass
    keep = [n for n, _s2, _p2 in have if n.startswith(PROTECTED_PREFIXES)]
    unused = [(n, s, p) for n, s, p in have
              if n not in used and not n.startswith(PROTECTED_PREFIXES)]
    if job is not None and keep:
        job.log(f"其中 {len(keep)} 个是公共素材（头像/横幅/背景/BGM/音效），"
                f"不按剧情引用判断，一律保留")
    total = sum(s for _n, s, _p in unused)
    if job is not None:
        job.log(f"缓存 {len(have)} 个包，其中 {len(unused)} 个没被引用"
                f"（{total / 1048576:.1f} MB）")
    deleted = 0
    if not dry:
        for _n, _s, p in unused:
            try:
                os.remove(p)
                deleted += 1
            except OSError:
                pass
        if job is not None:
            job.log(f"已删除 {deleted} 个未被引用的包，释放约 {total / 1048576:.1f} MB")
    res = {"cached": len(have), "used": len(used), "unused": len(unused),
           "bytes": total, "deleted": deleted, "dry": dry,
           "failed": len(failed),
           "sample": [n for n, _s, _p in unused[:30]]}
    if job is not None:
        try:
            job.detail["prune"] = res
        except Exception:                                      # noqa: BLE001
            pass
    return res


def coverage() -> dict:
    """整体完成度：已装剧情 / 云端剧情总数 + bundle 缓存体积。"""
    st = unit_status_map()
    done = sum(1 for v in st.values() if v in ("ready", "soft-missing"))
    total = len(st) or 1
    return {
        "units": done, "unitsTotal": len(st),
        "percent": round(100.0 * done / total, 1),
        "bundleCache": _cache_size(),
    }


# ---------------------------------------------------------------- 安装入口
_UNIT_RE = re.compile(r"^adv_([a-z]+)(?:_(\d+))?$")


def install_unit(key: str, job=None, workers: int = 32, voice: bool = True,
                 image: bool = True, maps: bool = True,
                 force: bool = False, upscale: bool = False,
                 upscale_scale: int = 2,
                 upscale_engine: str = "cugan") -> dict:
    """安装一部剧情。``key`` 形如 ``char_12`` / ``adv_main_3`` / ``adv_op``。

    ``upscale=True`` 时，装完顺手把**这部剧用到的图**超分（只处理这些 key，
    不会把全站几千张图跑一遍）。原图不覆盖，产出在 ``media/img_x2/``。
    """
    config.ensure_dirs()
    miss = missing_deps()
    if miss:
        raise CDNError("缺少依赖：" + "、".join(miss)
                       + "。请先 pip install -r requirements.txt")
    with _capture(job):
        mf = select.Manifest(ensure_manifest(job))
        if key.startswith("char_"):
            try:
                cid = int(key[5:])
            except ValueError as e:
                raise CDNError(f"角色标识不对：{key}") from e
            return _install_char(mf, cid, job, workers, voice, image, maps, force,
                                 upscale, upscale_scale, upscale_engine)
        m = _UNIT_RE.match(key)
        if not m:
            raise CDNError(f"无法识别的剧情标识：{key}（应为 char_12 或 adv_main_3）")
        kind = m.group(1)
        no = int(m.group(2)) if m.group(2) else None
        return _install_adv(mf, kind, no, job, workers, voice, image, maps, force,
                            upscale, upscale_scale, upscale_engine)


def _self_check(job, key: str) -> dict:
    """装完自检：剧本 / 语音 / 图片引用是否齐全。

    为什么必须做：安装是"按剧本引用反查 bundle"算出来的，如果某个图片 key
    在官方清单里找不到对应包（命名例外、源资源缺失），现在会**静默跳过**，
    用户直到播到那一句才发现黑图。自检把这类缺口在装完时就摆出来。
    """
    try:
        if key.startswith("char_"):
            r = stages.check_char(int(key[5:]))
            miss = []
            if not r.get("scenario"):
                miss.append("剧本缺失")
            if r.get("voice_miss"):
                miss.append(f"语音缺 {r['voice_miss']}/{r['voices_ref']} 个")
            for k, label in (("stand", "立绘"), ("back", "背景"), ("cg", "CG")):
                keys = [str(x) for x in (r.get(f"{k}_miss") or [])]
                if keys:
                    # 带上具体 key：只说"缺 2 个"没法判断是源资源缺还是合成漏了，
                    # 排查还得让用户自己翻剧本。
                    miss.append(f"{label}引用缺 {len(keys)} 个"
                                f"（{', '.join(keys[:3])}"
                                + ("…" if len(keys) > 3 else "") + "）")
            nb = [str(x) for x in (r.get("stand_bundle_miss") or [])]
            if nb:
                miss.append(f"立绘包未下载 {len(nb)} 个"
                            f"（{', '.join(nb[:2])}"
                            + ("…" if len(nb) > 2 else "") + "）"
                            "——重装这部剧即可补齐")
            out = {"unit": key, "missing": miss, "detail": r}
        else:
            data = os.path.join(config.DATA, f"{key}.js")
            vdir = os.path.join(config.AUDIO, key)
            miss = []
            if not os.path.exists(data):
                miss.append("剧本缺失")
            if not os.path.isdir(vdir):
                miss.append("语音未导出")
            out = {"unit": key, "missing": miss}
    except Exception as e:                                     # noqa: BLE001
        out = {"unit": key, "missing": [f"自检失败：{e}"]}
    if job is not None:
        if out["missing"]:
            job.log("⚠ 自检：" + "；".join(out["missing"])
                    + "（多半是源资源本身缺，播放时会静音/无图跳过）")
        else:
            job.log("✓ 自检通过：剧本 / 语音 / 图片引用齐全")
    return out


def _upscale_assets(job, assets: dict, scale: int, engine: str) -> int:
    """只超分这部剧引用到的图片键。返回处理张数（0 表示没得做/失败）。"""
    from .. import media
    pairs = set()
    for kind in ("back", "cg", "stand"):
        for key in assets.get(kind) or ():
            pairs.add((kind, key))
    if not pairs:
        return 0
    if job is not None:
        job.log(f"超分本剧情的图（{len(pairs)} 张，2 倍放大，原图保留）…")
    try:
        r = media.upscale(job, only=pairs, scale=scale, engine=engine)
        return int(r.get("done") or 0)
    except Exception as e:                                     # noqa: BLE001
        if job is not None:
            job.log(f"  超分失败（不影响播放）：{e}")
        return 0


def _entries(mf, items):
    """把"清单条目"与"bundle 名"混合的列表统一成条目列表。

    ``select.Manifest`` 的接口并不一致（历史原因）：``scenario()/voices()``
    直接返回**条目 dict**，而 ``back_bundle()/stand_bundle()`` 返回**包名字符串**。
    这里两种都吃，避免上层到处判断。
    """
    out = []
    for it in items:
        if not it:
            continue
        if isinstance(it, dict):
            if it.get("bundle_name"):
                out.append(it)
        elif isinstance(it, str) and it in mf.index:
            out.append(mf.index[it])
    return out


def _install_char(mf, cid, job, workers, voice, image, maps, force,
                  upscale=False, scale=2, engine="cugan") -> dict:
    t0 = time.time()
    scen = mf.scenario(cid)
    if not scen:
        raise CDNError(f"官方清单里找不到角色 {cid} 的剧本包"
                       f"（可能是新角色还没上架，或清单过期）")
    names = [scen]
    if voice:
        names += mf.voices(cid)
    names += mf.meta_images(cid)
    names = [n for n in names if n]
    # 注意：names 里混着"清单条目 dict"（scenario/voices 的返回值）和"包名字符串"，
    # 必须先过 _entries() 归一化，否则拿 dict 当 key 会抛 unhashable。
    base_entries = _entries(mf, names)
    _sz0 = sum(int(e.get("size") or 0) for e in base_entries)
    if job is not None:
        job.log(f"① 剧本 / 语音 / 头像：{len(base_entries)} 个包 / "
                f"{_sz0 / 1048576:.1f} MB")
    stages.fetch(base_entries, workers, f"char{cid} 基础包", skip=True, pause_event=_pe(job), job=job)

    if job is not None:
        job.log("② 解析剧本 → data/char_%d.js" % cid)
    if not stages.extract_scenario(cid, force=force):
        raise CDNError(f"角色 {cid} 的剧本解析失败（包可能损坏，重试一次）")

    if voice:
        if job is not None:
            job.log("③ 导出语音")
        stages.export_voices(cid, workers=workers)

    # 头像源：剧本**不引用** c-panel 包（它是角色面板的半身像），
    # 但侧栏头像要靠它裁出来，所以这里单独下一个（几 KB ~ 几百 KB）。
    # 名字同样要按补零格式试，否则清单里匹配不到。
    cp_cands = [f"bundle_chara_c-panel_{int(cid):03d}",
                f"bundle_chara_c-panel_{int(cid):02d}",
                f"bundle_chara_c-panel_{cid}"]
    cp_ents = _entries(mf, [n for n in cp_cands if n in mf.index])
    if cp_ents:
        stages.fetch(cp_ents, workers, "角色头像", skip=True,
                     pause_event=_pe(job), job=job)
    stages.export_avatar(cid)

    nums = set()
    assets = {"stand": set(), "back": set(), "cg": set()}
    if image:
        if job is not None:
            job.log("④ 按剧本实际引用下载图片包")
        try:
            assets = select.collect_assets(select.load_char(cid))
        except Exception as e:                                 # noqa: BLE001
            assets = {"stand": set(), "back": set(), "cg": set()}
            if job is not None:
                job.log(f"   读剧本失败（跳过图片）：{e}")
        need = []
        for kind in ("back", "cg", "stand"):
            for s in assets.get(kind) or ():
                b = (mf.back_bundle(s) if kind == "back"
                     else mf.cg_bundle(s) if kind == "cg"
                     else mf.stand_bundle(s))
                if b:
                    need.append(b)
                mk = select.PAT_STAND_KEY.match(s) if kind == "stand" else None
                if mk:
                    nums.add(mk.group(1))
                    full = mf.stand_full.get(int(mk.group(1)))
                    if full:
                        need.append(full)
        need = sorted({n for n in need if n})
        _sz = sum(int((mf.index.get(n) or {}).get("size") or 0) for n in need)
        if job is not None:
            job.log(f"   图片包 {len(need)} 个 / {_sz / 1048576:.1f} MB"
                    f"（已存在的会跳过，不会重下）")
        stages.fetch(_entries(mf, need), workers, f"char{cid} 图片包", skip=True, pause_event=_pe(job), job=job)
        ex = stages.export_images(mf, assets)
        if job is not None:
            # export_images 的 print 只在控制台；这里把"缺哪些 key / 失败多少张"
            # 落进日志，否则事后只能看到"合成 N 套成功"，查不出缺图原因。
            if ex.get("miss"):
                head = ", ".join(f"{k}:{s}" for k, s in ex["miss"][:5])
                job.log(f"   ⚠ 无对应包的图片 key {len(ex['miss'])} 个（{head}）")
            if ex.get("fail"):
                job.log(f"   ⚠ 图片导出失败 {ex['fail']} 张（包缺失或损坏）")
        stages.export_standfull(mf, nums)
        if nums:
            if job is not None:
                job.log(f"⑤ 合成表情差分立绘（{len(nums)} 套）")
            stages.compose_stands(nums)
        # 剧本引用了、但包里没有对应表情的立绘（表情码其实是文本）→ 素体顶替
        stages.fill_missing_stands(assets.get("stand") or (), job)

    if upscale and image:
        _upscale_assets(job, assets, scale, engine)

    if maps:
        if job is not None:
            job.log("⑥ 刷新索引与映射表")
        stages.sync_index()
        stages.sync_cgmap(cid)
        stages.regen_maps()
        stages.regen_names()
        # 新角色 Wiki 可能还没收录：先把实装日期用剧本包创建时间顶上，
        # 之后用户点「更新角色信息」拿到 wiki 数据会被自动覆盖。
        d_est = stages.ensure_char_date(cid)
        if d_est and job is not None:
            job.log(f"   实装日期暂用资源包日期 {d_est}"
                    "（Wiki 收录后会自动覆盖）")
        stages.regen_charmeta()

    chk = _self_check(job, f"char_{cid}")
    return {"unit": f"char_{cid}", "seconds": round(time.time() - t0, 1),
            "stands": len(nums), "check": chk}


def _install_adv(mf, kind, no, job, workers, voice, image, maps, force,
                 upscale=False, scale=2, engine="cugan") -> dict:
    t0 = time.time()
    key = select.story_key(kind, no)
    b = mf.adv_scenario(kind, no)
    if not b:
        raise CDNError(f"官方清单里找不到 {key} 的剧本包")
    if job is not None:
        job.log(f"① 剧本包：{b}")
    stages.fetch(_entries(mf, [b]), workers, f"{key} 剧本", skip=True, pause_event=_pe(job), job=job)
    if not stages.extract_adv(kind, no, force=force):
        raise CDNError(f"{key} 的剧本解析失败")

    if voice:
        if job is not None:
            job.log("② 语音包")
        vbundles = select.adv_voice_bundles(mf.index, kind, no)
        if vbundles:
            stages.fetch(_entries(mf, vbundles), workers, f"{key} 语音", skip=True, pause_event=_pe(job), job=job)
            stages.export_adv_voices(key, vbundles)

    nums = set()
    assets = {"stand": set(), "back": set(), "cg": set()}
    if image:
        if job is not None:
            job.log("③ 按剧本引用下载图片包")
        try:
            assets = select.collect_assets(select.load_story(key))
        except Exception as e:                                 # noqa: BLE001
            assets = {"stand": set(), "back": set(), "cg": set()}
            if job is not None:
                job.log(f"   读剧本失败（跳过图片）：{e}")
        need = []
        for k in ("back", "cg", "stand"):
            for s in assets.get(k) or ():
                bb = (mf.back_bundle(s) if k == "back"
                      else mf.cg_bundle(s) if k == "cg"
                      else mf.stand_bundle(s))
                if bb:
                    need.append(bb)
                mk = select.PAT_STAND_KEY.match(s) if k == "stand" else None
                if mk:
                    nums.add(mk.group(1))
                    full = mf.stand_full.get(int(mk.group(1)))
                    if full:
                        need.append(full)
        need = sorted({n for n in need if n})
        _sz2 = sum(int((mf.index.get(n) or {}).get("size") or 0) for n in need)
        if job is not None:
            job.log(f"   图片包 {len(need)} 个 / {_sz2 / 1048576:.1f} MB")
        stages.fetch(_entries(mf, need), workers, f"{key} 图片包", skip=True, pause_event=_pe(job), job=job)
        ex = stages.export_images(mf, assets)
        if job is not None:
            if ex.get("miss"):
                head = ", ".join(f"{k}:{s}" for k, s in ex["miss"][:5])
                job.log(f"   ⚠ 无对应包的图片 key {len(ex['miss'])} 个（{head}）")
            if ex.get("fail"):
                job.log(f"   ⚠ 图片导出失败 {ex['fail']} 张（包缺失或损坏）")
        stages.export_standfull(mf, nums)
        if nums:
            if job is not None:
                job.log(f"④ 合成表情差分立绘（{len(nums)} 套）")
            stages.compose_stands(nums)
            stages.fill_missing_stands(assets.get("stand") or (), job)
        else:
            # 非角色剧情也要章节横幅
            stages.export_story_topics()

    if upscale and image:
        _upscale_assets(job, assets, scale, engine)

    if maps:
        if job is not None:
            job.log("⑤ 刷新索引与映射表")
        stages.sync_advindex()
        stages.sync_index()
        stages.regen_maps()
        stages.regen_names()
        stages.regen_charmeta()

    chk = _self_check(job, key)
    return {"unit": key, "seconds": round(time.time() - t0, 1), "check": chk}


# ---------------------------------------------------------------- 自检
def check_unit(key: str) -> dict:
    """某部剧情在本地是否齐全（缺哪一步一目了然）。"""
    config.ensure_dirs()
    if key.startswith("char_"):
        try:
            return stages.check_char(int(key[5:]))
        except Exception as e:                                 # noqa: BLE001
            return {"error": str(e)}
    data = os.path.join(config.DATA, f"{key}.js")
    voice_dir = os.path.join(config.AUDIO, key)
    return {
        "key": key,
        "data": os.path.exists(data),
        "voices": len(os.listdir(voice_dir)) if os.path.isdir(voice_dir) else 0,
    }


def _pe(job):
    """取任务的暂停信号；没有 job（脚本里直接调用）就当没有暂停。"""
    return getattr(job, "pause_event", None)
