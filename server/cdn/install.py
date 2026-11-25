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
import threading
import time

from .. import facts
from ..facts import PUBLIC_ALL, PUBLIC_OPTIONAL, PUBLIC_PARTS
from . import config, manifest, net, select, stages

#: 从 net 的日志里抠出 "[123/456]" 当进度
_PROG = re.compile(r"\[(\d+)/(\d+)\]")


class CDNError(RuntimeError):
    """给用户看的错误（缺依赖、清单拉不到、角色不存在…）"""


class _StaleManifest(RuntimeError):
    """下载源上的包与本机**清单**对不上（大小不同 / 已经不在源上）。

    为什么会走到这里：``ensure_manifest`` 只在**首次**拉清单，之后一直用旧的那份。
    官方重新打包某个包、或把它从清单里撤掉之后，旧清单里的 ``size`` 就永远校验不过，
    表现是"这一部怎么装都失败"，而旧版报出来的话却是"包可能损坏" —— 用户只能一遍遍
    怀疑自己的磁盘和网络。``install_unit`` 捕获它 → 自动刷新清单并重跑一次。
    """


def _E(code: str, msg: str) -> "CDNError":
    """带**错误码**的报错：``[E-DISK] 磁盘空间不足…``。

    为什么要码：用户反馈时常常只截一张图、不愿发日志文件。码出现在
    「操作记录」里，我一眼就能分出是网络、磁盘、权限还是清单问题，
    不用让他再复现一次去翻文件（码表见 ``net.FAIL_REASONS``）。
    """
    return CDNError(f"[{code}] {msg}")


def _explain_download(err) -> str:
    """底层异常 → 「发生了什么」。归因词表在 :mod:`server.cdn.net`，全链路共用。"""
    return net.explain_error(err)[0]


def _is_stale(err) -> bool:
    """这条失败像是"清单过期"造成的吗（大小不符 / 源上已没有）。"""
    return net.is_stale_error(err)


def _scenario_fail(what: str, stat, name: str) -> Exception:
    """剧本包最终没落盘 → 给一条**说真话**的错（发生了什么 + 该怎么办）。

    以前这里只会说「剧本解析失败（包可能损坏，重试一次）」：那是从
    ``extract_scenario() -> None`` 反推出来的猜测，真实原因（下载失败/被删）
    被 ``fetch_many`` 收进 ``fail`` 列表后**没人看**，于是用户拿到的信息全错，
    我们这边也查不出问题（2026-11-06 用户反馈，UI 上只有那一行字）。
    归因词表在 :data:`server.cdn.net.FAIL_REASONS`，全链路共用同一套。
    """
    fails = (stat or {}).get("fail") or []
    err = next((e for n, e in fails if n == name), "")
    if not err:
        return _E("E-VANISH", (
            f"{what} 的剧本包没下下来，但下载阶段并没有报错 —— 文件下好之后又不见了"
            "（多半是同时跑了「清理空间 / 清理资源包」，或被安全软件、网盘同步客户端"
            "挪走了）。等清理任务结束再重试一次；仍然不行请重装这部剧"))
    happened, how = net.explain_error(err)
    code = net.error_code(err)
    tail = f"（原始错误：{str(err)[:120]}）"
    if net.is_stale_error(err):
        return _StaleManifest(f"[{code}] {what} 的剧本包下载失败：{happened}。{how}{tail}")
    return _E(code, f"{what} 的剧本包没下下来：{happened}。{how}{tail}")


def _warn_voice_fail(job, stat, what: str) -> None:
    """语音包解不开 → 明确说"会有什么后果"。

    语音不是致命项（剧本照样能读），但**一点声音都没有**会被用户当成播放器坏了，
    而旧代码只在控制台 print 一行就过去了（GUI 用户根本看不到）。
    """
    bad = int((stat or {}).get("fail") or 0)
    if not bad or job is None:
        return
    job.log(f"⚠ {bad} 个语音包解不开：{what}里这些句子会没有声音。"
            "多半是包不完整或解包组件异常 —— 重装这部剧会重新下载")


def _warn_stand_output(job, nums) -> None:
    """立绘合成后核实产物：一张都没出来就明说，别等播到那一句才发现没有脸。"""
    if job is None or not nums:
        return
    d = os.path.join(config.IMG, "stand")
    prefixes = tuple(f"stand_{n}_" for n in nums)
    n = 0
    try:
        for fn in os.listdir(d):
            if fn.endswith(".webp") and fn.startswith(prefixes):
                n += 1
    except OSError:
        return
    if not n:
        job.log("⚠ 立绘一张都没合成出来：剧情里的人物会是空白。"
                "多半是图片包没下全，或缺少 Pillow / numpy 组件 —— "
                "重装这部剧会重下图片包；仍不行请到「剧情安装」页装依赖")


# ---------------------------------------------------------------- 进度桥
@contextlib.contextmanager
def _capture(job):
    """把 cdn.net 的下载日志喂给 Job（进度条 + 任务详情），不再往启动窗口写。"""
    old = net._log

    def hook(msg):
        s = str(msg)
        if job is not None:
            m = _PROG.search(s)
            try:
                if m:
                    # 注意：Job.progress() 没有 total 参数（多传会被吃掉进 detail），
                    # 总量必须用 set_total() 单独设，否则进度条永远是 0%
                    if getattr(job, "sub_mode", False):
                        # 多单元安装：顶层进度由 items 驱动，这里把每批 bundle 的
                        # 包计数写进 sub，避免反复把顶层进度冲到 100%。
                        job.sub_progress(int(m.group(1)), int(m.group(2)),
                                        s[:160])
                    else:
                        job.set_total(int(m.group(2)), "包")
                        job.progress(done=int(m.group(1)), message=s[:200])
                else:
                    job.log(s[:200])
            except Exception:                                  # noqa: BLE001
                pass
            return
        # 没有 Job（少数维护路径调到这里）就只落 app.log。
        # **绝不 print**：启动窗口只当"启动了，地址在这"，下载细节一律在页面上看
        # （2026-11-18 用户要求；以前这里每批包都往 CMD 打一行，用户得来回切窗口）。
        try:
            from .. import log as applog
            applog.log(s[:400], "cdn")
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
        # 体积只说在界面上（facts.CATALOG_BYTES），日志里不重复写一遍数字
        job.log("下载并解密官方资源清单（首次才需要）…")
    try:
        manifest.refresh()
    except Exception as e:                                     # noqa: BLE001
        happened, how = net.explain_error(e)
        raise _E(net.error_code(e), f"拉取官方资源清单失败：{happened}。{how}"
                                     f"（原始错误：{str(e)[:120]}）") from e
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
    # 公共素材统计也在安装/卸载/清理后失效：否则装完剧情后 30 秒内
    # 横幅/头像完成度还是旧值，前端据此不发图请求（2026-11-17）。
    _PUBLIC_STATUS_CACHE["data"] = None
    _VOICE_STATE_CACHE["data"] = None


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


#: 公共素材的类别 / 包名前缀 / 体积登记在 ``server/facts.py``（唯一来源），
#: 这里只做 re-export：`/api/install/base` 用 ``PUBLIC_PARTS`` 做白名单过滤，
#: 界面显示的"约 835 MB"也是同一份数据算出来的。
#: **avatar / banner 必须留在字典里**：漏了 avatar 的后果见 facts.py 的注释。

#: 横幅类包的两种前缀
BANNER_PREFIXES = ("bundle_episodeicons_episode_character_", "bundle_image_story_")

#: 立绘源包（分层立绘，**单数** ``bundle_standimage_stand_<编号>_<服装>``）：
#: 一包 = 一个角色的**一套服装**，包内含这一套的全部表情图层。
#: 注意不能写成 ``bundle_standimage``（不带尾下划线）：那样会把复数的
#: ``bundle_standimages_stand_<编号>``（完整立绘，另一类素材）也算进来。
STAND_PACK_PREFIX = "bundle_standimage_"

#: 「全部立绘」做完后写的完成标记（``userdata/_archive/stand_extra_done.json``）。
#: 存"当时清单里有多少个立绘源包 + 合成出多少张"，界面上展示进度用：
#: 官方上新包 → 清单数变大 → 自动变回「补全」；用户清理过 bundle 缓存 →
#: 成品还在、标记也还在，不该让他再下一遍（所以判齐**不看**本地包数）。
#: **判「已就绪」别看它**，看 :func:`stand_extra_missing`。
STAND_EXTRA_STAMP = "stand_extra_done.json"

#: 合成脚本（``compose_stand_all.py --all-packs``）写的组登记表：
#: ``{"编号_服装": {"n": 表情数, "pack": 源包字节}}``。这是判「全部立绘」是否
#: 真齐的唯一依据 —— 包下齐 ≠ 每套服装都合成过（2026-11-20 反馈的根因）。
STAND_EXTRA_GROUPS = "stand_extra_groups.json"

#: 演出特效（advfx）要用到的包：
#:   · bundle_image_adv        —— 表情符号图集 + 画面特效逐帧参数表
#:   · bundle_font_66_heart_03 —— 台词行内图标（<sprite=0> 的那颗粉色心形，
#:     TMP SpriteAsset）。它归在 bundle_font 里，光看 bundle_image_adv
#:     是拿不到的，漏了它台词里「……❤❤❤」的心就会缺失。
ADVFX_PREFIXES = ("bundle_image_adv", "bundle_font_66_heart_03")

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
    "bundle_image_adv",                   # 演出特效（表情符号图集 + 逐帧参数表）
    "bundle_font_66_heart_03",            # 台词行内图标（<sprite=0> 心形）
)


# ---------------------------------------------------------------- SD 小人
# def sd_bundle_name(cid) -> str:
    # """角色 / 敌人 → 它的 SD 包名（``bundle_unit_00001_bc``）。

    # 编号是 5 位补零的；NPC / 敌人是 1xxxx / 2xxxx 段，同样补到 5 位。
    # """
    # return "bundle_unit_%05d_bc" % int(cid)


def _load_web_js(relpath: str):
    """读 ``web/`` 下 ``window.__X={...};`` 形式的纯数据文件（不存在返回 None）。"""
    p = os.path.join(config.WEB, relpath.replace("/", os.sep))
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8-sig") as f:
            s = f.read()
        i = s.find("=")
        return json.loads(s[i + 1:].strip().rstrip(";"))
    except Exception:                                            # noqa: BLE001
        return None


_SD_CACHE: dict = {"at": 0.0, "ids": None}


# def sd_image_exists(cid) -> bool:
    # """这个编号的 SD 待机图是否已落盘（比 mediaindex.js 准且零成本）。"""
    # return os.path.exists(os.path.join(config.IMG, "sd", "%sstand.webp" % int(cid)))


# def sd_mark_installed(cid) -> None:
    # """把刚导出的编号加进内存缓存，避免为了一个 id 重扫整个媒体目录。

    # 以前每装一个角色都调 ``regen_mediaindex()``（扫 1.9 万个文件、重写索引），
    # 366 部剧情就是 366 次全盘扫描 —— 纯浪费，所以改成末尾统一重建一次。
    # """
    # import time as _t
    # ids = sd_installed_ids()
    # ids.add(str(int(cid)))
    # _SD_CACHE["at"] = _t.time()


# def sd_installed_ids(force: bool = False) -> set:
    # """本机已经导出过 SD 的编号集合（来自 ``data/mediaindex.js`` 的 ``sd`` 清单）。

    # 为什么不扫 ``media/img/sd/``：一个角色 4 张图、敌人 435 个，扫目录要
    # 1700 次 stat；而 mediaindex 是启动 + 每次导出后重写的一份索引，读一次就够。
    # """
    # import time as _t
    # if not force and _SD_CACHE["ids"] is not None and _t.time() - _SD_CACHE["at"] < 8:
        # return _SD_CACHE["ids"]
    # data = _load_web_js("data/mediaindex.js") or {}
    # ids = set(str(x) for x in (data.get("sd") or []))
    # _SD_CACHE.update(at=_t.time(), ids=ids)
    # return ids


# def sd_state_map() -> dict:
    # """``{编号: True/False}`` —— 给安装页显示「缺 SD 素材」用。

    # 只列**清单里有 SD 包**的编号：有些角色（老卡、联动临时单位）官方压根没出
    # SD，不该让它们永远显示"缺"。
    # """
    # out = {}
    # try:
        # mf = select.Manifest(ensure_manifest())
    # except Exception:                                            # noqa: BLE001
        # return out
    # for name in mf.index:
        # if not name.startswith("bundle_unit_"):
            # continue
        # body = name[len("bundle_unit_"):]
        # num = body.split("_", 1)[0]
        # if not num.isdigit():
            # continue
        # try:
            # v = int(num)
        # except ValueError:
            # continue
        # 直接看磁盘（不像 mediaindex.js 那样可能滞后于刚导出的图）
        # out[str(v)] = sd_image_exists(v)
    # return out


# def _install_sd(mf, cid, job=None, workers: int = 32) -> dict:
    # """下载并导出某个编号的 SD 小人（跟着角色剧情一起装）。"""
    # name = sd_bundle_name(cid)
    # ent = mf.index.get(name)
    # if not ent:
        # if job is not None:
            # job.log(f"   官方没有 {cid} 的 SD 小人，跳过")
        # return {"ok": False, "note": "no bundle"}
    # stages.fetch(_entries(mf, [name]), workers, f"SD {cid}", skip=True,
                 # pause_event=_pe(job), job=job)
    # path = os.path.join(config.FILES_STORY, name)
    # if not os.path.exists(path):
        # raise _E("E-SD", f"SD 包没下下来：{name}（重装这个角色会重新下载）")
    # rc = stages.run_script("export_sd.py", "--chars", str(int(cid)), quiet=True)
    # got = os.path.exists(os.path.join(config.IMG, "sd"))
    # if got:
        # 不再每装一个就重扫全盘；整批结束由 run_install 统一重建一次
        # sd_mark_installed(cid)
    # return {"ok": bool(got), "exit": rc}


# ---------------------------------------------------------------- 原声集
#: 封面包名前缀。主包是 ``bundle_gallerymusicjackets``，官方"增量补图"包
#: 叫 ``bundle_image_gallerymusicjackets``（新曲目加进来时单独发）。
#: 按前缀收，将来再发一个新的补图包也不用改代码。
GALLERY_JACKET_PREFIXES = ("bundle_gallerymusicjacket",
                           "bundle_image_gallerymusicjacket")
#: 音频包名：``bundle_gallerymusic_music_00001``。
#: **编号以官方清单为准** —— 官方每上一批新曲，清单里就会多出几个编号；
#: 以前只认随程序分发的 ``gallery_tracks.json``，于是新曲永远进不了下载清单、
#: 也进不了播放列表，必须由开发者手补编号再发版（2026-11-16 用户指出）。
GALLERY_AUDIO_RE = re.compile(r"^bundle_gallerymusic_music_(\d+)$")


def gallery_tracks() -> list:
    """``server/cdn/data/gallery_tracks.json`` 里的曲目表（随程序分发）。

    现在它只承担一件事：告诉安装器哪些曲目和本机已有的 BGM 是**同一份母带**
    （``clip`` 字段），从而省掉重复下载。列表里有哪些歌、共几首，都改由
    官方清单（:func:`gallery_audio_nos`）决定。
    """
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "gallery_tracks.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return (data or {}).get("tracks") or []


def gallery_audio_nos(index) -> list:
    """清单里原声集音频包的全部编号（升序）。

    ``index`` 可以是 :class:`server.cdn.select.Manifest`（有 ``.index``），
    也可以直接是 ``{bundle_name: entry}`` —— 状态接口手里只有后者。
    用清单而不是随程序分发的表：官方上新曲后这里自动多出编号。
    """
    idx = getattr(index, "index", index) or {}
    nos = {int(m.group(1)) for m in
           (GALLERY_AUDIO_RE.match(str(n)) for n in idx) if m}
    return sorted(nos)


def gallery_jacket_names(index) -> list:
    """清单里所有原声集封面包（含官方的"增量补图"包）。"""
    idx = getattr(index, "index", index) or {}
    return sorted(str(n) for n in idx
                  if str(n).startswith(GALLERY_JACKET_PREFIXES))


def gallery_missing_audio(mf) -> list:
    """原声集里"本机没有现成音频、必须下自己那份包"的曲目。

    实测 165 首里有 158 首与「BGM 音乐」的 ``bundle_music_*`` **是同一份母带**
    （解码 PCM 波形相关 1.000、时长精确到 0.001 秒一致），本机已有 m4a 的直接
    复用，只有画廊独占的那几首（各平台联动曲等）才下自己的包 —— 一首约 2.5 MB，
    相比把 165 首全下一遍（448 MB）省掉 95%。

    遍历的是**清单里的全部编号**：表里没有的编号（官方新曲）当作独占曲下载，
    名字等解包时从包里读（公开曲名只在服务端，拿不到就显示内部占位名）。
    """
    sfx = _load_web_js("data/sfxmap.js") or {}
    known = {int(t.get("no") or 0): t for t in gallery_tracks()}
    out = []
    for no in gallery_audio_nos(mf):
        t = known.get(no) or {}
        clip = str(t.get("clip") or "")
        if clip and clip in sfx:
            rel = str(sfx.get(clip) or "")
            if rel and os.path.exists(os.path.join(config.WEB, rel.replace("/", os.sep))):
                continue
        name = "bundle_gallerymusic_music_%05d" % no
        if name in mf.index:
            out.append(name)
    return out


def _run_script_progress(job, name: str, label: str, done0: float,
                         quota: int, progress_file: str, *args) -> int:
    """跑整包解包脚本，期间按脚本自报的真实进度推进任务条（不再假死在 0%）。

    ``export_gallery.py`` / ``export_adv_effects.py`` 是一次性子进程，跑完才落
    产物；解包阶段又是按"包数"记账，所以整个脚本运行期间 ``job.done`` 停在
    起点 —— 用户看到的就是"0% 卡了几分钟"。

    现在子进程会在关键节点把 ``{stage, done, total, pct}`` 写到 ``progress_file``
    （原子写）。这里的心跳线程每 2 秒读一次，把 pct 映射成"这一类配额的一部分"
    推进 ``job.done`` 到 ``done0 + quota * pct/100``：顶层百分比因此真实爬升。
    解包结束后调用方的 ``owe = quota - (done - _d0)`` 只会补剩余差额，不重复计。
    """
    import threading

    stop = threading.Event()

    def _beat():
        while not stop.wait(2):
            pct = None
            stage = label
            detail = ""
            sub_d = sub_t = 0
            try:
                with open(progress_file, encoding="utf-8") as f:
                    p = json.load(f)
                pct = float(p.get("pct") or 0)
                stage = p.get("stage") or label
                sub_d = int(p.get("done") or 0)
                sub_t = int(p.get("total") or 0)
                detail = f" {sub_d}/{sub_t}"
            except (OSError, ValueError, TypeError):
                pass
            if job is None:
                continue
            # 把子脚本自报的"张 / 首"计数也写进任务条（sub 档）：解包阶段原来的
            # 「解包 8/9 个」是按**包**计数的，一个封面包里有 82 张，8.91 四舍五入
            # 就成 9/9 —— 看着像已经完成，其实还在第 123 张（2026-11-17 用户问
            # 「解包 9/9 个什么意思，用户看到不会以为 100% 吗」）。
            if sub_t > 0 and not getattr(job, "sub_mode", False):
                job.sub_progress(sub_d, sub_t, stage)
            if pct is not None and quota > 0:
                # 只前进不后退（与 job 的高水位一致）；上限留到 quota，
                # 100% 由调用方补配额，避免这里先把 done 顶过 total。
                target = done0 + quota * min(99.0, pct) / 100.0
                # **不要再拼"解包"**：子进程报的 stage 本身就叫「解包封面」/
                # 「解析曲目」，以前外层再前缀一个 → 「解包解包封面… 89/2」
                # （2026-11-17 用户截图）。
                msg = f"{stage}…{detail}" if detail else f"{stage}…"
                if target > job.done:
                    job.progress(done=target, message=msg)
                else:
                    job.progress(message=msg)
            else:
                job.progress(message=f"{label}…")

    t = threading.Thread(target=_beat, name=f"hb-{name}", daemon=True)
    t.start()
    try:
        return stages.run_script(name, *args, quiet=True)
    finally:
        stop.set()
        t.join(timeout=2)


def gallery_have_audio() -> int:
    """``web/data/gallery.js`` 里"有音频"的曲目数。

    判原声集是否装齐用它，不用"封面张数 == 曲目数"：官方可能只上新曲、
    不发新封面（或封面包名换了），按封面判会永远差几首、按钮一直亮着。
    文件不存在/内容坏掉返回 0。
    """
    p = os.path.join(config.DATA, "gallery.js")
    try:
        with open(p, encoding="utf-8") as f:
            s = f.read()
        obj = json.loads(s[s.index("{"): s.rindex("}") + 1])
    except (OSError, ValueError):
        return 0
    return len([t for t in (obj.get("tracks") or []) if t.get("s")])


def stand_img_count() -> int:
    """``web/media/img/stand/`` 里的成品立绘张数（只数 webp）。

    这是"全部立绘"这类安装项的**结果指标**：合成成功多少张就看它，
    不看源包缓存（用户清理过缓存后图还在，不该显示成 0）。
    """
    d = os.path.join(config.IMG, "stand")
    try:
        return len([n for n in os.listdir(d) if n.lower().endswith(".webp")])
    except OSError:
        return 0


def stand_extra_stamp_path() -> str:
    return os.path.join(config.WS, "_archive", STAND_EXTRA_STAMP)


def read_stand_extra_stamp() -> dict:
    """读「全部立绘」完成标记（没做过返回空 dict）。"""
    try:
        with open(stand_extra_stamp_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def write_stand_extra_stamp(packs: int, files: int) -> None:
    """写完成标记：``packs`` = 当时清单里的立绘源包数。

    **不要再拿它判「已就绪」**：合成的实际单位是"一套服装"，而按编号分 120 个
    一批跑，某一批失败时包全下齐了、标记也照写 —— 用户会看到「已就绪」但
    角色整组缺张（2026-11-20 反馈）。真正的判据是
    :func:`stand_extra_missing`（按合成脚本写的组登记表对账）。
    这里保留只是为了兼容旧版本的读取与展示。
    """
    p = stand_extra_stamp_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"packs": int(packs), "files": int(files),
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
    except OSError:
        pass


# ---------------------------------------------------------------- 全部立绘：是否真的合成过
#: 一套服装 = 清单里的一个 ``bundle_standimage_stand_<编号>_<服装>``。
_STAND_GROUP_RE = re.compile(r"^bundle_standimage_stand_(\d+)_([0-9a-zA-Z]+)$")


def stand_extra_groups_path() -> str:
    """``compose_stand_all.py --all-packs`` 写的组登记表。"""
    return os.path.join(config.WS, "_archive", STAND_EXTRA_GROUPS)


def read_stand_extra_groups() -> dict:
    """``{"编号_服装": {"n": 表情数, "pack": 源包字节}}``（没跑过返回空 dict）。"""
    try:
        with open(stand_extra_groups_path(), encoding="utf-8") as f:
            d = json.load(f) or {}
        g = d.get("groups")
        return g if isinstance(g, dict) else {}
    except (OSError, ValueError):
        return {}


def stand_extra_expected(index: dict) -> dict:
    """清单里每套服装 → 源包字节数（key 与组登记表同形：``编号_服装``）。"""
    out = {}
    for name, e in index.items():
        m = _STAND_GROUP_RE.match(name)
        if m:
            # 服装段转大写：包名是小写 hex，成品文件名/登记表用大写
            out[f"{m.group(1)}_{m.group(2).upper()}"] = int((e or {}).get("size") or 0)
    return out


def stand_extra_missing(index: dict = None) -> list:
    """还没按"包内全套表情"合成过的服装（key 列表）。

    判据不是文档里的完成标记，而是**合成脚本真正写出成品的组**：
      · 组登记表里没有 → 这套服装从没跑过全套合成（剧本模式只写被引用的几张）；
      · 登记的源包字节数与当前清单不符 → 官方换过这套素材，表情可能变了。
    这样任何一次"某一批没跑成"都会在下一次「补全」被精确补上，而不是
    永久停在"已就绪 + 整组缺张"。
    """
    if index is None:
        try:
            index = manifest.load_index()
        except Exception:                                      # noqa: BLE001
            return []
    exp = stand_extra_expected(index)
    rec = read_stand_extra_groups()
    miss = []
    for key, size in exp.items():
        r = rec.get(key)
        if not isinstance(r, dict) or not r:
            miss.append(key)
        elif size and int(r.get("pack") or 0) != size:
            miss.append(key)
    return miss


def _compose_retry(chunk: list, job=None, tries: int = 2) -> int:
    """跑一批「全部立绘」合成；退出码非 0 再试一次（返回最后一次的退出码）。

    为什么必须重试：一批是 120 个编号、用 8 个进程跑，一次原生层崩溃就能让
    这一批后面几十套服装一张图都不写（脚本自己会记下哪几组失败）。合成是
    幂等的 —— 已写好的直接跳过，重试只补剩下的，代价很小。
    """
    rc = 1
    for i in range(max(1, tries)):
        try:
            rc = int(stages.compose_stands_all_packs(chunk) or 0)
        except Exception as ex:                                # noqa: BLE001
            rc = -1
            if job is not None:
                job.log(f"   立绘合成异常（第 {i + 1} 次）：{str(ex)[:100]}")
        if rc == 0:
            return 0
    return rc


# def _is_npc_sd(name: str) -> bool:
    # """``bundle_unit_10001_bc`` 这种是不是 NPC / 敌人（编号 ≥ 10000）。"""
    # if not name.startswith("bundle_unit_"):
        # return False
    # num = name[len("bundle_unit_"):].split("_", 1)[0]
    # return num.isdigit() and int(num) >= 10000


# def _is_npc_voice(name: str) -> bool:
    # """NPC / 敌人的战斗语音包（``bundle_audioclipsprefab_unit_10001``）。"""
    # if not name.startswith("bundle_audioclipsprefab_unit_"):
        # return False
    # num = name[len("bundle_audioclipsprefab_unit_"):]
    # return num.isdigit() and int(num) >= 10000


# def sd_unavailable_ids() -> set[str]:
    # """本机**导不出 SD 小人**的编号（由 export_sd.py 导出失败时登记）。

    # 有些包只有特效 / Instance 骨架，当前解析器合不出图。这些编号既不进
    # 「应有 435 个」的分母，也不进「补全」清单：否则页面永远显示差几个、
    # 补全按钮永远亮着，而反复重导也变不出图（2026-11-15 反馈）。
    # 导出成功会自动撤销登记，将来解析能力提升后无需手工维护。
    # 见 ``server/cdn/scripts/export_sd.py`` 的 ``STATE_FILE``。
    # """
    # p = os.path.join(config.STATE_DIR, "sd_unavailable.json")
    # try:
        # with open(p, encoding="utf-8") as f:
            # return {str(int(x)) for x in (json.load(f) or [])}
    # except (OSError, ValueError, TypeError):
        # return set()


# def _npc_missing(mf) -> tuple[set[int], set[int]]:
    # """NPC / 敌人里**产物确实缺**的编号，返回 ``(缺 SD, 缺战斗语音)``。

    # 「补全」只该补这几个，而不是把 435 个 SD + 435 个语音包全量重导一遍 ——
    # 用户 2026-11-15 反馈「明明只差几个，解包却从 1/870 开始、进度条 60% 起」。

    # SD 判据必须和页面 ``public_status`` **同源**（mediaindex 的 sd 清单）：
    # 它按「这个编号有没有任意动作图」登记（见 media.write_mediaindex），
    # NPC / 敌人里有少数几个只有 ``<编号>attack1.webp``、根本没有 stand 待机图。
    # 若这里改用 ``sd_image_exists()``（只判 stand.webp），会把那 10 个当成缺，
    # 而且导完还是没有 stand 图 → 每次点补全都重补、页面数字却永远不变。
    # 解完会 regen_mediaindex()，所以按索引补完，页面数字必然变齐。
    # 已登记为导不出 SD 的编号由 ``sd_unavailable_ids()`` 排除。
    # 语音判 ``media/audio/voice/<编号>/`` 目录（与 public_status 同口径）。
    # 首次安装时所有编号都缺，结果与全量一致。
    # """
    # p_sd, p_vc = "bundle_unit_", "bundle_audioclipsprefab_unit_"
    # sd_all = {int(n[len(p_sd):].split("_", 1)[0])
              # for n in mf.index if _is_npc_sd(n)}
    # sd_all -= {int(x) for x in sd_unavailable_ids()}
    # vc_all = {int(n[len(p_vc):]) for n in mf.index if _is_npc_voice(n)}
    # have_idx = set()
    # for x in sd_installed_ids():
        # try:
            # have_idx.add(int(x))
        # except (TypeError, ValueError):
            # pass
    # sd_miss = sd_all - have_idx
    # vmap = _voice_map_ids()
    # vc_miss = {x for x in vc_all
               # if not _voice_installed(x, vmap)}
    # return sd_miss, vc_miss


# ---------------------------------------------------------------- 战斗语音
def _voice_map_ids(force: bool = False) -> set[str] | None:
    """``voicemap.js`` 里**已登记**的单位编号（``{"11","10001",...}``）。

    为什么要看清单而不是只看目录：音声的产物有**两份** —— 音频文件在
    ``media/audio/voice/<编号>/``，清单条目在 ``web/data/voicemap.js``。
    以前只判目录，于是目录在、清单里却没有条目的单位（并发写覆盖 / 导到一半
    中断）被当成"已安装"：页面显示 378/385，点补全也不亮（2026-11-15 反馈）。
    文件不存在（老版本 / 纯静态包）时返回 ``None``，调用方退回"只看目录"的
    旧口径，避免把全部 821 个包判成缺、触发一次全量重导。
    """
    p = os.path.join(config.DATA, "voicemap.js")
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    c = _voice_map_ids
    if not force and getattr(c, "_key", None) == key and getattr(c, "_ids", None) is not None:
        return c._ids
    try:
        with open(p, encoding="utf-8-sig") as f:
            s = f.read()
        i, j = s.find("{"), s.rfind("}")
        data = json.loads(s[i:j + 1])
        ids = {str(int(u["id"])) for u in (data.get("units") or []) if u.get("id") is not None}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    c._key, c._ids = key, ids
    return ids


def _voice_installed(cid, vmap) -> bool:
    """这个单位算不算"音声已安装"：目录有音频 **且** 清单里有条目。"""
    vdir = os.path.join(config.AUDIO, "voice", str(int(cid)))
    if not os.path.isdir(vdir):
        return False
    if vmap is None:                 # 没有清单可查 → 退回旧口径
        return True
    return str(int(cid)) in vmap


def voice_bundle_name(cid) -> str:
    """单位 → 它的语音包名（``bundle_audioclipsprefab_unit_00011``）。"""
    return "bundle_audioclipsprefab_unit_%05d" % int(cid)


_VOICE_STATE_CACHE: dict = {"at": 0.0, "data": None}
_VOICE_STATE_TTL = 30.0
_VOICE_STATE_LOCK = threading.Lock()


def voice_state_map(force: bool = False) -> dict:
    """``{编号: 本机有没有战斗语音}`` —— 形状与 :func:`sd_state_map` 一致。

    只列**清单里真有语音包**的编号，官方没出的单位不会被标成"永远缺"。
    带 30 秒缓存 + single-flight：``/api/cdn/units`` 每次都调它（基准 80 ms），
    多 iframe 并发时只算一次；装/卸语音后由 invalidate_units() 清掉。
    """
    now = time.time()
    c = _VOICE_STATE_CACHE
    if not force and c["data"] is not None and now - c["at"] < _VOICE_STATE_TTL:
        return c["data"]
    with _VOICE_STATE_LOCK:
        now = time.time()
        if not force and c["data"] is not None and now - c["at"] < _VOICE_STATE_TTL:
            return c["data"]
        out = {}
        try:
            mf = select.Manifest(ensure_manifest())
        except Exception:                                        # noqa: BLE001
            return out
        vdir = os.path.join(config.AUDIO, "voice")
        vmap = _voice_map_ids()
        for name in mf.index:
            if not name.startswith("bundle_audioclipsprefab_unit_"):
                continue
            num = name[len("bundle_audioclipsprefab_unit_"):]
            if not num.isdigit():
                continue
            # 包名是 5 位补零（00002），导出目录是去零的（2）—— 别拿包名直接拼路径
            cid = str(int(num))
            out[cid] = _voice_installed(cid, vmap)
        c["data"] = out
        c["at"] = now
        return out


def _char_id(unit: str):
    if not unit.startswith("char_"):
        return None
    try:
        return int(unit[5:])
    except ValueError:
        return None


def unit_extras_missing(unit: str) -> list[str]:
    """这部剧情**已经装好**、但还缺哪些"附加素材"（``sd`` / ``voice``）。

    为什么需要它：``run_install`` 碰到 ``ready`` 就直接跳过（免得重跑 20 分钟），
    可老用户的 SD / 音声是这两个版本才有的 —— 于是"勾上 381 部补 SD"变成了
    全部跳过、界面一直显示「缺 SD 素材」（2026-11-15 反馈）。
    """
    cid = _char_id(unit)
    if cid is None:
        return []
    out = []
    try:
        mf = select.Manifest(ensure_manifest())
        # 2026-11-16：SD 整体停用，不再判定/上报「缺 SD」
        # if sd_bundle_name(cid) in mf.index and not sd_image_exists(cid):
        #     out.append("sd")
        if voice_bundle_name(cid) in mf.index:
            # 目录在、voicemap.js 里却没有条目：音频导出来了但清单被覆盖掉，
            # 页面就是「378/385」那种数字，补全也不会亮（2026-11-15 反馈）。
            if not _voice_installed(cid, _voice_map_ids()):
                out.append("voice")
    except Exception:                                            # noqa: BLE001
        return out
    return out


def rebuild_indexes() -> None:
    """整批安装结束后**统一重建一次**媒体 / 立绘索引。

    以前每个角色装完都各跑一次 ``regen_mediaindex()`` + ``regen_standmap()``：
    前者扫全部媒体文件、后者扫 1.9 万张立绘并重写 standmap.js，
    366 部剧情就是 366 次全盘扫描（日志里 87 次 export_standmap.py）。
    这两个索引只影响"这个素材在不在"的显示，中途不刷新也无所谓。
    """
    for fn in (stages.regen_mediaindex, stages.regen_standmap):
        try:
            fn()
        except Exception:                                        # noqa: BLE001
            pass
    # 2026-11-16：SD 停用，不再刷新 SD 安装状态
    # sd_installed_ids(force=True)


def install_extras(unit: str, job=None, workers: int = 32) -> dict:
    """只补这部剧情的附加素材（SD 小人 / 战斗语音），不重跑剧本与图片解包。"""
    cid = _char_id(unit)
    if cid is None:
        return {"unit": unit, "extras": []}
    mf = select.Manifest(ensure_manifest())
    done = []
    # 2026-11-16：SD 小人整体停用，install_extras 不再补 SD
    # try:
    #     if sd_bundle_name(cid) in mf.index and not sd_image_exists(cid):
    #         _install_sd(mf, cid, job, workers)
    #         done.append("sd")
    # except Exception as e:
    #     ...
    try:
        if voice_bundle_name(cid) in mf.index and not _voice_installed(
                cid, _voice_map_ids()):
            if job is not None:
                job.log("   战斗语音")
            _install_voice(mf, cid, job, workers)
            done.append("voice")
    except Exception as e:                                       # noqa: BLE001
        if job is not None:
            job.log(f"   战斗语音失败：{str(e)[:120]}")
    # 立绘分组表跟 SD / 音声无关，补附加素材时不用重扫 1.9 万张立绘；
    # 整批结束由 run_install 统一重建一次即可。
    return {"unit": unit, "extras": done}


def _install_voice(mf, cid, job=None, workers: int = 32) -> dict:
    """下载并导出某个单位的语音（战斗语音 + 主页那批），各 1~2 MB。

    两个包：``audioclipsprefab_unit_*``（战斗）与 ``systemvoice_<编号>``（主页/其余，
    一个角色一整包）。后者也重复存了战斗语音，导出脚本会按名称时长去重。
    """
    names = [n for n in (voice_bundle_name(cid), "bundle_systemvoice_%05d" % int(cid))
             if n in mf.index]
    if not names:
        return {"ok": False, "note": "no bundle"}
    stages.fetch(_entries(mf, names), workers, f"语音 {cid}", skip=True,
                 pause_event=_pe(job), job=job)
    if not os.path.exists(net.bundle_path(names[0])):
        raise _E("E-VOICE", f"语音包没下下来：{names[0]}（重装这个角色会重新下载）")
    stages.run_script("export_voice.py", "--chars", str(int(cid)), quiet=True)
    got = os.path.isdir(os.path.join(config.AUDIO, "voice", str(int(cid))))
    return {"ok": bool(got)}


def install_base(job=None, workers: int = 48, parts=None,
                 force_export: bool = True) -> dict:
    """下载并解包「公共素材」：背景 / 音乐 / 音效 / 头像 / 剧情横幅。

    默认类别取 :data:`server.facts.PUBLIC_PARTS` 的键 —— 以前把默认值抄成
    ``("bg","bgm","sfx","avatar")``，漏了 banner，跟 ``/api/install/base`` 的
    白名单与界面上写的类别对不上；现在只有一处真源，加类别不会再漏。

    ``force_export``：已存在的成品图要不要重做。
      · **手动点「补全」→ True**：保留"旧版本 cpanel 头像有错图，重导一次"的修复能力；
      · **刷新清单顺手补 → False**（见 ``api._auto_fill_public``）：官方只多了几个包，
        没理由把 1086 张头像全部 force 重裁 —— 那是"几分钟"的直接来源（2026-11-18）。

    为什么值得单独做一个入口
    ------------------------
    这些是**跨剧情共享**的：每部剧情都会用到背景和 BGM，按剧情重复下载
    既慢又占地方。而且剧本里的 ``PlayMusic {"Music": "89B98A79_..."}``
    能精确反查到 ``bundle_music_89b98a79_...``（实测 10/10 命中），
    所以**全量下齐之后，所有剧情的 BGM 都能响**——这是"没音乐"的根治办法。

    体积（清单实测）按类别登记在 ``facts.PUBLIC_PARTS``，合计与分项文案由
    ``facts.public_bytes()`` / ``facts.public_breakdown()`` 生成 —— 界面上的
    "约 835 MB"和这里算的是同一份数据。相对全量素材只占几个百分点，
    但之后装任何剧情都不用再补。

    关于头像与剧情横幅
    ------------------
    侧栏头像来自 ``bundle_chara_c-panel_<cid>``（角色面板的半身像）、剧情列表的
    标题横幅来自 ``bundle_episodeicons_episode_character_<cid>``。剧本**都不引用**
    它们，所以按剧本引用下载是拿不到的；但数量固定、体积小（头像 ~20 MB、
    横幅 ~40 MB）、dependencies 为空（不跟任何包绑定），因此归入公共素材最合适：
      · 体积可以忽略
      · **没装的角色也能显示头像与横幅**，方便认人（若按剧情下载，未装角色就是空白）

    可选类别 ``stand_extra``（「全部立绘」）
    ---------------------------------------
    官方立绘是「一套服装一个源包」（``bundle_standimage_stand_<编号>_<服装>``），
    包内 7~12 张表情图层，剧本每套只用其中几张。按剧情分发时只把**被引用的**
    那几张合成出来，剩下那批「剧情里永远不会出现」的表情永远不会落盘 ——
    用户看到的现象就是立绘页整组缺几张、装遍全部剧情也补不齐。
    这一项把官方全部立绘源包下齐，再按包内 ``ImageGroup.ExpressionImages``
    合成全套表情（见 ``stages.compose_stands_all_packs``）。已存在的成品走幂等
    守卫，重复点「补全」不会白跑。
    """
    parts = tuple(parts) if parts else tuple(PUBLIC_PARTS)

    config.ensure_dirs()
    mf = select.Manifest(ensure_manifest(job))
    out: dict = {"bytes": 0}

    # 原声集 / NPC·敌 SD 的包清单要现算（前者取决于本机已有哪些 BGM，
    # 后者是一大票 unit 包），算一次复用，别在 _total 与 plan 里各扫一遍。
    # 封面按前缀收（官方新增补图包也一并下）；音频按清单编号收（含官方新曲）。
    _gal_names = (gallery_jacket_names(mf) + gallery_missing_audio(mf)) \
        if "gallery" in parts else []
    _gal_names = [n for n in _gal_names if n in mf.index]
    # 「全部立绘」：官方全部立绘源包（2623 个 / 约 589 MB）。剧本只引用到其中
    # 一部分，剩下那些包里的表情（同一套服装的其它表情）永远不会有成品 ——
    # 这就是「立绘页一半是灰的」的根因。已下过的包会 skip，不重下。
    _stand_names = ([n for n in mf.index if n.startswith(STAND_PACK_PREFIX)]
                    if "stand_extra" in parts else [])
    # 合成是按**角色编号**一套服装跑一次（清单里 2623 个包只对应 992 个编号），
    # 所以这一项在进度条上的"工作单位"是编号、不是包。分母用包数会算错：
    # 解包阶段按包记账，而这一项实际只推进 992 次 → 进度条永远差一截（2026-11-17 审计）。
    _stand_nums: list[str] = []
    _stand_seen: set[str] = set()
    for _n in _stand_names:
        _mm = re.match(r"^bundle_standimage_stand_(\d+)_", _n)
        if _mm and _mm.group(1) not in _stand_seen:
            _stand_seen.add(_mm.group(1))
            _stand_nums.append(_mm.group(1))   # 补零宽度必须原样保留：036 ≠ 36
    # 已经按「包内全套表情」合成过的服装不必再跑一遍（幂等守卫还是要解包读图，
    # 全量 1002 个编号要几分钟）。判据是合成脚本自己写的组登记表 ——
    # 包下齐了但没合成过的组、官方换过素材的组，都会留在这里被补上。
    _stand_todo: list[str] = []
    if _stand_nums:
        try:
            _miss = set(stand_extra_missing(mf.index))
        except Exception:                                      # noqa: BLE001
            _miss = set()
        _miss_nums = {k.split("_", 1)[0] for k in _miss}
        _stand_todo = [n for n in _stand_nums if n in _miss_nums]
        if job is not None:
            job.log(f"   全部立绘：{len(_stand_todo)} 个编号要合成"
                    f"（清单共 {len(_stand_nums)} 个；已合成过的不重做）")
    # 2026-11-16：NPC / 敌人的 SD 小人 + 音声整体停用。
    # 原来这里在 parts 含 "sd_npc" 时收集 435 个 bundle_unit_* 与
    # 435 个 bundle_audioclipsprefab_unit_*（1xxxx/2xxxx），现在全部注释。
    # _sd_names, _vc_names = [], []
    # if "sd_npc" in parts:
    #     _sd_miss, _vc_miss = _npc_missing(mf)
    #     _sd_names = [n for n in mf.index if _is_npc_sd(n) ...]
    #     _vc_names = [n for n in mf.index if _is_npc_voice(n) ...]

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
        _total += len([n for n in mf.index if stages.avatar_bundle_applicable(n)])
    if "banner" in parts:
        _total += len([n for n in mf.index if n.startswith(BANNER_PREFIXES)])
    if "advfx" in parts:
        _total += len([n for n in mf.index if n.startswith(ADVFX_PREFIXES)])
    if "gallery" in parts:
        _total += len(_gal_names)
    if _stand_todo:
        # 按**编号**记账（不是包数）：这一项在解包阶段做的是「一套服装合成一次」，
        # 分母若按 2623 个包算，进度条到最后只会走到约 73%（见上面 _stand_nums）。
        # 只算**这次真要跑**的编号：已合成过的不重做，进度条才不会走不到头。
        _total += len(_stand_todo)
    # 2026-11-16：sd_npc 已停用
    # if "sd_npc" in parts:
    #     _total += len(_sd_names) + len(_vc_names)
    if not _total:
        # 页面已就绪时（或自动补的流程里）这类可能一个都不缺：别再走空的
        # 下载 / 解包循环，进度条也不用经历一次「60% → 100%」的空转。
        if job is not None:
            job.log("这些类别已经齐了，没有要补的；已装好的不重做")
            job.set_total(1, "项")
            job.progress(done=1, message="已经齐了，无需补全")
        return out
    fails: list[str] = []
    hints: list[str] = []

    def _collect(tag: str, res: dict) -> None:
        """记录下载失败：按**原因归类**汇总，最后统一抛 CDNError。

        逐包贴原始异常（"…（HTTP Error 404: Not Found）"）是以前的写法：
        同一句话既可能是"官方撤包"也可能是"磁盘满"，用户根本不知该改哪。
        归类 + "怎么处理"由 :func:`server.cdn.net.summarize_fails` 统一给，
        逐包的告警已经由 ``stages.fetch`` 打在任务日志里，这里只留汇总。
        """
        bad = (res or {}).get("fail") or []
        if not bad:
            return
        fails.append(f"{tag}：{net.summarize_fails(bad, limit=160)}")
        hint = net.hint_for(bad)
        if hint:
            hints.append(hint)

    done_base = 0
    if job is not None and _total:
        # 一个任务里可能"先刷新清单、再顺手补公共素材"（见 api._auto_fill_public）：
        # 那条流程走到这里时进度已经是 100%（2/2 份清单），不清高水位的话
        # 后面整段下载 + 解包都会顶在 100% 上不动。独立点「补全」时无副作用。
        job.restart_percent()
        job.set_total(_total, "个")
        # 总字节也一起给出来，进度条才能显示"12.3 / 775 MB"
        _bytes = 0
        if "bg" in parts:
            _bytes += sum(int((mf.index.get(mf.back_bundle(k)) or {}).get("size") or 0)
                          for k in ((_m.load_imgmap().get("back") or {})))
        if "avatar" in parts:
            _bytes += sum(int(e.get("size") or 0)
                          for n, e in mf.index.items()
                          if stages.avatar_bundle_applicable(n))
        if "banner" in parts:
            _bytes += sum(int(e.get("size") or 0)
                          for n, e in mf.index.items()
                          if n.startswith(BANNER_PREFIXES))
        if "advfx" in parts:
            _bytes += sum(int(e.get("size") or 0)
                          for n, e in mf.index.items()
                          if n.startswith(ADVFX_PREFIXES))
        if _gal_names:
            _bytes += sum(int((mf.index.get(n) or {}).get("size") or 0) for n in _gal_names)
        if _stand_names:
            _bytes += sum(int((mf.index.get(n) or {}).get("size") or 0)
                          for n in _stand_names)
        # 2026-11-16：NPC / 敌人 SD + 音声已整体停用，_sd_names/_vc_names 不再定义，
        # 这段遗留的字节统计必须一起删掉（否则带 job 装公共素材时 NameError）。
        for _pref in ("bundle_music_", "bundle_sound_"):
            if (_pref == "bundle_music_" and "bgm" not in parts) or \
               (_pref == "bundle_sound_" and "sfx" not in parts):
                continue
            _bytes += sum(int(e.get("size") or 0) for n, e in mf.index.items()
                          if n.startswith(_pref))
        job.bytes_total = _bytes
        # **下载阶段只占 0–60%**：解包（背景逐包开图、头像 385 张重裁）常常比
        # 下载还慢，而以前这里没设阶段映射，默认就是 0–100% —— 下载一完
        # bytes_done 就撞到 bytes_total，进度条顶到 100%（高水位还会把它钉死），
        # 后面几十分钟的解包全程显示 100%，用户以为已经完成（2026-11-10 反馈）。
        # keep_bytes=True：这一阶段仍然要按字节算进度，不能把分母清掉。
        job.phase(0.0, 60.0, keep_bytes=True)

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
                if stages.avatar_bundle_applicable(n)]
        sz = sum(int(e.get("size") or 0) for e in ents)
        plan.append({"tag": "avatar", "label": "角色/NPC/敌人头像",
                     "ents": ents, "sz": sz})
    if "banner" in parts:
        names = [n for n in mf.index if n.startswith(BANNER_PREFIXES)]
        ents = _entries(mf, names)
        sz = sum(int(e.get("size") or 0) for e in ents)
        plan.append({"tag": "banner", "label": "剧情横幅", "ents": ents, "sz": sz})
    if "advfx" in parts:
        ents = [e for n, e in mf.index.items() if n.startswith(ADVFX_PREFIXES)]
        sz = sum(int(e.get("size") or 0) for e in ents)
        plan.append({"tag": "advfx", "label": "演出特效", "ents": ents, "sz": sz})
    if "gallery" in parts:
        ents = _entries(mf, _gal_names)
        sz = sum(int(e.get("size") or 0) for e in ents)
        plan.append({"tag": "gallery", "label": "原声集", "ents": ents, "sz": sz})
    if _stand_names:
        ents = _entries(mf, _stand_names)
        sz = sum(int(e.get("size") or 0) for e in ents)
        plan.append({"tag": "stand_extra", "label": "全部立绘",
                     "ents": ents, "sz": sz})
    # 2026-11-16：sd_npc 下载计划已删除
    # if "sd_npc" in parts:
    #     ents = _entries(mf, _sd_names) + _entries(mf, _vc_names)
    #     plan.append({"tag": "sd_npc", ...})

    # ---- 进度条的起点：先把"磁盘上已有、这次不会重下"的字节扫进来 ----
    # 必须是全量已完成字节，而不是本次新下的字节：用户第二次点「补全」时，
    # 背景/音乐/音效大半已经在本地（一个字节都不用传），而总量仍是全量 893 MB。
    # 以前只累加新下的量，于是任务条上出现「70.9% · 8.40 MB / 893.01 MB」——
    # 8 MB 按总量算才 1%，两个数字互相打脸（用户就是这么报的）。
    _have = 0
    _need_dl = 0
    #: 每个类别真正**要下载**的包数 / 字节（预扫时算出来）。
    #: 日志里必须报这个，不能报"计划里的包数"：头像类计划 1554 个包（全部），
    #: 实际往往只要 7 个 —— 直接打 1554 会让人以为"已经装好了还要重下"（2026-11-18）。
    _need_by_tag: dict = {}
    if job is not None:
        for step in plan:
            n_need = 0
            b_need = 0
            for e in step["ents"]:
                name = str(e.get("bundle_name") or "")
                sz = int(e.get("size") or 0)
                if not name or not sz:
                    _need_dl += 1        # 信息不全，保守当作要下
                    n_need += 1
                    continue
                try:
                    if net.need_download(net.bundle_path(name), sz):
                        _need_dl += 1
                        n_need += 1
                        b_need += sz
                    else:
                        _have += sz
                except OSError:
                    _need_dl += 1
                    n_need += 1
            _need_by_tag[step["tag"]] = (n_need, b_need)
        if _have:
            job.bytes_done = max(job.bytes_done, float(_have))
            # 预扫进来的字节不是"这次下载的量"：不设基线的话，任务开头两秒
            # 会按 800 MB / 2 秒算出几百 MB/s 的假网速（用户会以为中了木马）。
            job.bytes_base = float(_have)
            job.log(f"本地已有 {_have / 1048576:.0f} MB 不会重下，进度从这部分开始算")

    # 一个包都不用下载（「补全」最常见：只缺几个产物，源包早就在本地）。
    # 这时别再用「下载占前 60%」的划分：预扫的字节会把那 60% 瞬间填满，
    # 用户一打开任务条就是 60%（2026-11-15 反馈「为什么进度条从 60% 开始」）。
    # 整条 0–100% 都给解包，进度从 0 起；下载循环也整段跳过，省掉上千次 stat。
    no_download = bool(job is not None and _total > 0 and _need_dl == 0)
    if no_download:
        job.restart_percent()          # 清掉预扫阶段钉住的 60% 高水位
        job.phase(0.0, 100.0, total=max(1, _total), unit="个", label="解包")
        job.log(f"本地已有全部资源包，这次不下载；进度条从 0% 起，"
                f"只补 {_total} 个缺的产物")

    # ---- 阶段一：下载全部资源包 ----
    dl_plan = [] if no_download else plan
    done_base = 0
    for step in dl_plan:
        ents = step["ents"]
        if job is not None and ents:
            # 报"真要下的量"，顺带说明本地跳过了多少（见 _need_by_tag 的说明）
            n_need, b_need = _need_by_tag.get(step["tag"], (len(ents), step["sz"]))
            skip = len(ents) - n_need
            if n_need == 0:
                job.log(f"{step['label']}：本地已有全部 {len(ents)} 个包，跳过下载")
            elif skip > 0:
                job.log(f"下载{step['label']} {n_need} 个包 / "
                        f"{b_need / 1048576:.1f} MB（本地已有 {skip} 个，跳过）")
            else:
                job.log(f"下载{step['label']} {n_need} 个包 "
                        f"/ {b_need / 1048576:.0f} MB")
        _collect(step["label"],
                 stages.fetch(ents, workers, f"{step['label']}素材", skip=True,
                              pause_event=_pe(job), job=job,
                              done_offset=done_base,
                              # 上面已经预扫过"已存在"的字节，别再重复累加
                              count_skipped=False))
        done_base += len(ents)

    # ---- 阶段二：逐个类别解包 ----
    # 有下载时分两段：下载 0–60%、解包 60–100%。解包（尤其背景）常常比下载还久，
    # 如果仍按下载字节算，下载一完进度条就顶到 100% 干等十几分钟，像卡死了。
    # 没下载时（no_download）上面已把 0–100% 整段交给解包，这里不再覆盖。
    #
    # 解包阶段的计数单位是**包**（不是"类"）：只按 6 个类推进的话，背景那一类
    # 要跑十几分钟而进度条纹丝不动，用户还是会当成卡死。背景与头像逐包推进，
    # 其余类别（子进程导出）只能整类补足配额。
    phase_txt = "占整条进度条" if no_download else "占进度条后 40%"
    if job is not None and not no_download:
        # 这句要留在"操作记录"里：进度条只有 60% 时最容易让人以为算错了
        # （明明 1 GB 的素材已经下到 900 MB），所以必须说清"下载只占前 60%"。
        # **必须短**：任务条中间那句只有约 190px（标题与数字区各占一侧），
        # 以前写「下载阶段结束（进度条 60%）。接下来解包：不再联网，把资源包…」
        # 会被截成「下载阶段结束（进…」，关键的 60% 反而看不见（2026-11-17 截图实测）。
        job.log("下载完成（进度条 60%），接着解包资源包到 100%（不再联网）")
        job.phase(60.0, 40.0, total=max(1, _total), unit="个", label="解包")
    if job is not None:
        job.progress(done=0)
    for step in plan:
        tag = step["tag"]
        ents = step["ents"]
        sz = step["sz"]
        quota = len(ents)          # 与 phase(total=_total) 同一口径：按包记账
        _d0 = job.done if job is not None else 0.0
        # 清掉上一类的"子脚本进度"（见 _run_script_progress 的 sub_progress）：
        # 不清的话，解包头像时任务条还挂着「解包封面 165/165」，看着像没动过。
        if job is not None and not getattr(job, "sub_mode", False):
            try:
                job.sub_progress(0, 0, "")
            except Exception:                                  # noqa: BLE001
                pass
        if job is not None and ents:
            extra = "，这步比下载还久" if tag == "bg" else ""
            job.progress(message=f"解包{step['label']}（{len(ents)} 个包{extra}，"
                                 f"{phase_txt}）…")
        if tag == "bg":
            stages.export_images(mf, {"back": set(step["keys"]),
                                      "stand": set(), "cg": set()},
                                 on_bundle=(lambda: job.progress(add=1))
                                 if job is not None else None)
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
            # 4xxx 官方头像里混着活动/皮肤 NPC，剧情脚本没引用到的**根本不导出**。
            # 以前是先全量导出 246 张、再删掉没引用的一批 —— 下次「补全」又重复
            # 一遍同样的导出→删除（2026-11-17 审计）。没有装任何剧情时 used_npc
            # 为空，此时不做过滤（宁可多留也不要把 NPC 全删）。
            try:
                used_npc = set(stages._story_stand_ids(4000, 4399))
            except Exception:                                  # noqa: BLE001
                used_npc = set()
            for i, e in enumerate(ents, 1):
                # 官方头像有 2000 多个包，逐个导出要好几分钟；任务条必须有进度
                if job is not None and (i % 20 == 0 or i == len(ents)):
                    job.progress(message=f"解包头像 {i}/{len(ents)}…")
                name = str(e.get("bundle_name") or "")
                mm = re.match(r"^bundle_usericon_user_icon_(\d{4})$", name)
                if used_npc and mm and 4001 <= int(mm.group(1)) <= 4399 \
                        and int(mm.group(1)) not in used_npc:
                    if job is not None:
                        job.progress(add=1)      # 跳过也要推进，进度条不能卡住
                    continue
                try:
                    # force：手动「补全」时 True（旧版本导出的 cpanel 头像有错图、
                    # 官方更新后也要覆盖旧文件）；刷新清单顺手补时传 False —— 只补
                    # 真正缺的，不把 1086 张重裁一遍。
                    # regen=False：整批结束后统一重建一次映射，别每张都重扫媒体树。
                    if stages.export_avatar_bundle(name, force=force_export,
                                                   regen=False):
                        n_ok += 1
                    else:
                        # 不是每张官方头像都有对应文件（编号空洞/包结构异常），
                        # 这不算致命错误，继续导下一张。
                        n_bad += 1
                        if job is not None and n_bad <= 3:
                            job.log(f"   头像跳过 {name}")
                except Exception as ex:                        # noqa: BLE001
                    n_bad += 1
                    if job is not None and n_bad <= 3:
                        job.log(f"   头像导出失败 {name}：{str(ex)[:80]}")
                if job is not None:
                    job.progress(add=1)
            # 收尾只做**一轮**：先按剧情引用删掉多余的 NPC 头像，再从磁盘重建
            # 全部映射。以前这里是「导完重建一轮 → 删图 → 再重建一轮」，每个
            # regen_* 都是扫一两万文件的子进程，等于白跑一遍。
            try:
                stages.cleanup_unused_npc_avatars()
            except Exception:                                  # noqa: BLE001
                pass
            # 敌人头像来源核验：官方 bundle_usericon_user_icon_5xxx 是活动/皮肤图标，
            # 编号和剧情编号无关，里面还夹着周年之类的**宣传图**（整张脸特写 + 活动
            # logo）。旧版本拿它当剧情敌人头像导出过一批，对话框里就出现"脸部特写"
            # （2026-11-20 用户反馈 e5145/e5193）。有 2xxx / 15xxx 官方图标的编号一律
            # 不动，其余从立绘按官方脸框重裁（幂等：立绘没变不重做）。
            try:
                stages.fix_enemy_avatars(getattr(mf, "index", None))
            except Exception:                                  # noqa: BLE001
                pass
            # imgmap.x2.js 是本地超分覆盖层，必须按 img_x2 磁盘重建；
            # 否则旧版本会留下指向已删文件的 src，页面继续 404。
            try:
                from .. import media
                media.rebuild_x2_overlay()
            except Exception:                                  # noqa: BLE001
                pass
            try:
                stages.regen_imgmap()
            except Exception:                                  # noqa: BLE001
                pass
            try:
                stages.regen_avatarmap()
            except Exception as ex:                            # noqa: BLE001
                if job is not None:
                    job.log(f"   头像映射生成失败：{str(ex)[:80]}")
            stages.regen_mediaindex()
            out["avatar"] = n_ok
            out["bytes"] += sz
            if job is not None:
                job.log(f"   头像生成完毕：{n_ok} 张可用"
                        + (f"，{n_bad} 个包没有可用图标" if n_bad else ""))
            if n_bad:
                fails.append(f"头像：{n_bad} 个包没有导出图标"
                             "（官方编号空洞或包不完整）")
                hints.append("可重装一次公共素材；仍失败请反馈 app.log 的头像段落")
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
        elif tag == "gallery":
            # 封面 + 少数几首画廊独占曲：解包 + 写 web/data/gallery.js
            # 子进程写真实进度文件，这里按它推进 job.done —— 顶层百分比会
            # 随"解包封面 N/165 → 解析曲目 N/165"真实爬升，不再假死在 0%。
            cnt = _run_script_progress(
                job, "export_gallery.py", "解包原声集（封面 + 独占曲）",
                _d0, quota,
                os.path.join(config.WS, "_archive", "gallery_progress.json"))
            ok = os.path.exists(os.path.join(config.DATA, "gallery.js"))
            if not ok:
                fails.append("原声集：包下到了但没能解出曲目表"
                             f"（导出脚本 exit={cnt}，详见日志）")
                hints.append("重试一次「补全缺失」；仍失败请反馈日志里的 gallery 段落")
            out["gallery"] = 1 if ok else 0
            out["bytes"] += sz
        elif tag == "stand_extra":
            # 下载只是第一步。普通合成路径（``collect_tasks``）只做**剧本引用到**的
            # 那几张表情，所以这里走 ``--all-packs``：按包内
            # ``ImageGroup.ExpressionImages`` 把每套服装的全部表情都合成出来
            # （已存在的成品走幂等守卫，不重写、也不重复占空间）。
            #
            # 进度口径：`_total` 里这一项记的是**编号数**（`_stand_todo`），
            # 与这里 `add=len(chunk)` 一一对应，所以整条进度条单调爬到 100%，
            # 合成阶段自己占掉它该占的那一段（不再「下载完就 100%、合成像卡死」）。
            nums = _stand_todo        # 只跑"还没按全套表情合成过"的编号
            if job is not None and not nums:
                job.log("   全部立绘：清单里每一套服装都合成过，跳过合成")
            BATCH = 120                          # 一批约 3~5 分钟，进度条不至于冻住
            bad = 0
            done_sets = 0
            cancelled = False
            for bi in range(0, len(nums), BATCH):
                if job is not None and job.cancelled:
                    cancelled = True
                    break
                chunk = nums[bi:bi + BATCH]
                if job is not None:
                    job.progress(message=f"合成全部立绘 {bi + 1}-{bi + len(chunk)}"
                                         f"/{len(nums)} 套服装（含剧情没用到的表情）…")
                rc = _compose_retry(chunk, job)
                if rc != 0:
                    bad += 1
                    if job is not None and bad <= 3:
                        job.log(f"   立绘合成失败（{chunk[0]} 起 {len(chunk)} 套，"
                                f"退出码 {rc}）：详见 app.log 的 compose_stand_all 段落")
                else:
                    done_sets += len(chunk)
                if job is not None:
                    job.progress(add=len(chunk))
            # 立绘页的分组表 / "这组有没有"清单都靠扫磁盘，必须跟着重建，
            # 否则图在本机但页面还是灰的（等同没装）。
            stages.regen_standmap()
            stages.regen_mediaindex()
            n_img = stand_img_count()
            out["stand_extra"] = n_img
            out["bytes"] += sz
            if job is not None:
                job.log(f"   立绘总张数 {n_img}（含剧情里没用到的表情）")
            # **判「已就绪」按"每套服装都真合成过"对账，不看包数**：
            # 包下齐 ≠ 合成过（剧本模式只写被引用的几张），以前只看包数就盖章，
            # 于是装完全部剧情 + 全部立绘仍可能整组缺张（2026-11-20 反馈）。
            try:
                left = stand_extra_missing(mf.index)
            except Exception:                                  # noqa: BLE001
                left = []
            if cancelled:
                fails.append("全部立绘：已取消，没有标记为完成")
                hints.append("再点一次「补全」会接着做（已合成的不会重做）")
            elif bad or left:
                why = []
                if bad:
                    why.append(f"{bad} 批合成失败")
                if left:
                    why.append(f"还有 {len(left)} 套服装没合成")
                fails.append("全部立绘：" + "、".join(why)
                             + "（详见日志），未标记为完成")
                hints.append("再点一次「补全」只会补没合成的那几套；"
                             "仍失败请反馈日志里的 compose_stand_all 段落")
            else:
                write_stand_extra_stamp(len(_stand_names), n_img)
                if job is not None:
                    job.log(f"   已合成 {done_sets}/{len(nums)} 个编号的服装，"
                            f"{len(_stand_names)} 个源包全部对账通过，标记为已就绪")
        # 2026-11-16：sd_npc 解包/导出分支已删除（NPC 敌 SD + 音声整体停用）
        # elif tag == "sd_npc":
        # elif tag == "sd_npc":
            # NPC / 敌人的 SD 小人 + 战斗语音：只合成 _sd_names/_vc_names 里
            # 那些「产物真缺」的编号（补全时可能只有几个，首次安装才是全 435）。
            # 以前一律按清单全量重跑，差 8 个也要把 870 个包重导一遍。
            # sd_nums = sorted({int(mm.group(1)) for mm in
                              # (re.match(r"^bundle_unit_(\d+)_bc$", n) for n in _sd_names)
                              # if mm})
            # vc_nums = sorted({int(n.rsplit("_", 1)[-1]) for n in _vc_names
                              # if n.rsplit("_", 1)[-1].isdigit()})
            # 一批 8 个：一个单位约 2 秒（UnityPy 解包 + 合成），整包一次跑完
            # 435 个会让进度条四十多分钟纹丝不动（2026-11-15 又反馈「卡在 60%」）。
            # 8 个 → 每 15~20 秒走一格；每批**先记一半、跑完再记一半**，
            # 这样连"这一批正在跑"的那十几秒里进度也有变化，不会看着像死机。
            # BATCH = 8
            # bad_batches = 0
            # for label, nums, script in (("SD 小人", sd_nums, "export_sd.py"),
                                        # ("战斗语音", vc_nums, "export_voice.py")):
                # for bi in range(0, len(nums), BATCH):
                    # if job is not None and job.cancelled:
                        # break
                    # chunk = nums[bi:bi + BATCH]
                    # half = len(chunk) / 2.0
                    # if job is not None:
                        # job.progress(add=half,
                                     # message=f"合成 NPC / 敌人 {label} "
                                             # f"{bi + 1}-{bi + len(chunk)}/{len(nums)}"
                                             # f"（{phase_txt}）…")
                    # rc = stages.run_script(script, "--chars",
                                           # ",".join(str(x) for x in chunk), quiet=True)
                    # if rc != 0:
                        # bad_batches += 1
                    # if job is not None:
                        # job.progress(add=half)
            # try:
                # stages.regen_mediaindex()
            # except Exception:                                    # noqa: BLE001
                # pass
            # sd_installed_ids(force=True)
            # d = os.path.join(config.IMG, "sd")
            # n_ok = 0
            # try:
                # n_ok = len([f for f in os.listdir(d) if f.endswith(".webp")])
            # except OSError:
                # pass
            # if not n_ok:
                # fails.append("NPC/敌人 SD：包下到了但一张图都没合成出来"
                             # f"（{bad_batches} 批导出失败，详见日志）")
                # hints.append("重试一次；仍失败请反馈日志里的 export_sd 段落")
            # out["sd_npc"] = n_ok
            # out["bytes"] += sz
        elif tag == "advfx":
            # 演出特效不能直接给前端：包里是 CPE 数据结构 + 一张 512² 图集，
            # 必须在本机解成「逐帧 JSON（web/data/advfx.js）+ 无损 webp」，
            # 播放器才能按 30fps 摆部件。逻辑全在 export_adv_effects.py。
            # 特效脚本暂不写进度文件：传空路径让心跳只更新 message
            # （它只有 1 个包配额、耗时也比原声集短，不需要百分比映射）。
            cnt = _run_script_progress(
                job, "export_adv_effects.py", "解包演出特效（表情符号逐帧数据）",
                _d0, quota,
                os.path.join(config.WS, "_archive", "_advfx_no_progress.json"))
            # 判定必须与 public_status() 一致（advfx_ok）：三样产物齐全才算装上。
            # 只查 advfx.js 会在缺 msg_heart.png（官方撤 heart 包）时误写 stamp，
            # 于是界面永远显示"演出特效缺 0/1"、自动补全又不再触发。
            oks = (os.path.exists(os.path.join(config.DATA, "advfx.js"))
                   and os.path.exists(os.path.join(config.IMG, "advfx", "mark.webp"))
                   and os.path.exists(os.path.join(config.IMG, "advfx", "msg_heart.png")))
            if not oks:
                fails.append("演出特效：包下到了但产物不齐（逐帧数据/图集/台词心形）"
                             f"（导出脚本 exit={cnt}，详见日志）")
                hints.append("重装一次公共素材；仍失败请反馈日志里的 advfx 段落")
            out["advfx"] = 1 if oks else 0
            out["bytes"] += sz

        # 每解包完一类，把"这一类该记的包数"补足：
        # 背景与头像是逐包推进的（上面已 add 过），背景/音效/横幅/特效是整块做完，
        # 这里一次性补上配额。统一按包记账，百分比才不会在背景那一类冻住十几分钟。
        if job is not None:
            owe = quota - (job.done - _d0)
            if owe > 0:
                job.progress(add=owe, message=f"{step['label']} 已就绪")
            else:
                job.progress(message=f"{step['label']} 已就绪")

    # 兜底：解包阶段的配额按"包数"记，个别类别的实际回调数可能与配额差一个
    # （一个包装着多张图/多张背景时只回调一次）。这里补到 100%，
    # 免得任务明明跑完了、进度条还停在 99.6%。
    if job is not None and job.total and job.done < job.total:
        job.progress(done=float(job.total))

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

    # ⑤ 收尾体检：把"还没齐"的类别点出来，别让用户对着一个 98% 的数字猜。
    # **只查这次真的要求装的类别**：原声集 / 全部立绘是可选按钮，用户没勾它们
    # 时本来就是空的 —— 报一句「仍未齐：原声集封面 0/165」只会让人以为这次失败了
    # （2026-11-17 用户看到这句后专门去补了一轮）。NPC/敌 SD 与战斗语音已于
    # 2026-11-16 停用（不再下载），更不该出现在这张体检表里。
    try:
        st = public_status()
        short = []
        for name, have_k, total_k, part in (
                ("背景", "back", "backTotal", "bg"),
                ("BGM", "music", "musicTotal", "bgm"),
                ("音效", "sfx", "sfxTotal", "sfx"),
                ("头像", "avatar", "avatarTotal", "avatar"),
                ("横幅", "banner", "bannerTotal", "banner"),
                ("演出特效", "advfx", "advfxTotal", "advfx"),
                ("原声集封面", "jacket", "jacketTotal", "gallery")):
            if part not in parts:
                continue
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
        # 文案 = 哪一类 + 什么原因（归类）+ 怎么处理，三者缺一不可。
        msg = "部分素材没装上 · " + fails[0]
        if len(fails) > 1:
            msg += f"（另有 {len(fails) - 1} 类失败，详见日志）"
        if hints:
            msg += f"。怎么处理：{hints[0]}"
        raise _E("E-PART", msg)

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
        "avatar": len([n for n in idx if stages.avatar_bundle_applicable(n)]),
        "banner": len([n for n in idx if n.startswith(BANNER_PREFIXES)]),
        # 演出特效：新类别。oldua 的 stamp 里没有这个键 → new_public() 判为
        # 「从没下过」，老用户刷新清单时会自动把它列进待补，不用手动勾。
        "advfx": len([n for n in idx if n.startswith(ADVFX_PREFIXES)]),
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


def _public_img_ok(folder: str, base: str) -> bool:
    for ext in (".png", ".webp", ".jpg"):
        if os.path.exists(os.path.join(folder, base + ext)):
            return True
    return False


def _official_avatar_output(name: str) -> str:
    """官方头像包对应的产物主干；不是头像包 / 不产图的包返回空串。"""
    if name.startswith("bundle_usericon_user_icon_"):
        try:
            n = int(name.rsplit("_", 1)[-1])
        except ValueError:
            return ""
        if 1 <= n <= 416:
            return "d%03d" % n
        if 1001 <= n <= 1416:
            return "b%03d" % (n - 1000)
        if 2001 <= n <= 2203:
            return "e%04d" % (n + stages.ENEMY_ICON_OFFSET)
        if 4001 <= n <= 4399:
            return "n%04d" % n
        return ""
    if name.startswith("bundle_battle_character_icon_character_"):
        # 2026-11-23：这是**角色战斗图标**（20000 + 角色 ID），和剧情敌人
        # 5xxx 立绘不是一套编号，不产出 e####（否则补全会一直"缺图中"）。
        return ""
    return ""


#: 章节横幅所在的分包。这些包会**长大**：官方每上新一章 / 一个活动，
#: 就往里加一张 ``topic_<kind>_<n>`` Sprite（包体积随之变化）。
STORY_TOPIC_PREFIX = "bundle_image_story_"


def stale_story_topic_packs() -> list[str]:
    """本地已有、但和清单**体积对不上**的章节横幅包（= 官方加过新横幅）。

    为什么需要：``public_deficit`` 核对横幅时用的是 ``story_banners.json``
    里登记的"应有张数"，而那份登记是**上次导出时**写的。官方新上 ``event_99``
    后，登记里根本没有这张 → 判定"不缺" → 横幅包永远不重下 → 列表里那条
    活动永远没有横幅（2026-11-23 用户反馈：「更新剧情目录后第99话还是不显示
    横幅」）。这里改成按包体积核对：只要包里多了东西就视为过期，重下一次
    （5 个分包、几十 MB 以内），再由 ``export_story_topics`` 把新 Sprite 导出。

    只收**本地已存在**的包：完全没下过横幅的用户不在自动补的范围内
    （那属于"公共素材一点没装"，不该被这里悄悄拖几十 MB）。
    """
    idx = config_manifest_index()
    if not idx:
        return []
    out = []
    for n, e in idx.items():
        if not n.startswith(STORY_TOPIC_PREFIX):
            continue
        try:
            size = int((e or {}).get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        p = os.path.join(config.FILES_IMG, n)
        if not os.path.exists(p):
            continue
        try:
            if size and os.path.getsize(p) != size:
                out.append(n)
        except OSError:
            continue
    return sorted(out)


def public_deficit() -> dict:
    """清单里有、但**磁盘上还没有对应图**的公共素材包（精确到包名）。

    为什么不能只靠 :func:`new_public`：那套是拿 stamp（上次补完时的包数）跟现在的
    清单比。而**纯净全量包**里的 ``web/media`` 是预装的、从没"补全"过 → 没有 stamp
    → ``new_public()`` 永远返回空。于是官方一上新角色（1.0.9 带进来 char_416），
    侧栏头像与剧情横幅直接 404，而程序完全不知道（用户 2026-11-04 反馈）。

    这里改成按"包 ↔ 期望落盘的图"一对一核对，覆盖：
      · ``bundle_chara_c-panel_<n>``              → ``media/img/avatar/<n>.png``
      · ``bundle_usericon_user_icon_*`` / ``bundle_battle_character_icon_*``
                                                   → ``d/b/n/e`` 官方头像
      · ``bundle_episodeicons_episode_character_<n>`` → ``media/img/story/char_<cid>.webp``
    章节横幅（``bundle_image_story_*``，一个包里一批图）另按"应有张数"登记核对。

    → ``{"avatar": [包名…], "banner": [包名…]}``（没有缺口的类目不出现在结果里）
    """
    idx = config_manifest_index()
    if not idx:
        return {}
    adir = os.path.join(config.IMG, "avatar")
    sdir = os.path.join(config.IMG, "story")
    out = {"avatar": [], "banner": []}
    used_npc = stages._story_stand_ids(4000, 4399)
    for n in idx:
        if n.startswith("bundle_chara_c-panel_"):
            # 包名是**补零**的（_001 / _0416），落盘的图是**不补零**的（1.png / 416.png），
            # 直接比字符串会认为全都缺（实测 dev 环境误报 98 个）。
            try:
                tail = str(int(n.rsplit("_", 1)[-1]))
            except ValueError:
                continue
            if not _public_img_ok(adir, tail):
                out["avatar"].append(n)
        elif n.startswith("bundle_episodeicons_episode_character_"):
            try:
                cid = int(n.rsplit("_", 1)[-1])
            except ValueError:
                continue
            if not _public_img_ok(sdir, "char_%d" % cid):
                out["banner"].append(n)
        else:
            base = _official_avatar_output(n)
            if base and base.startswith("n"):
                # 活动/皮肤 NPC 与剧情 NPC 共用 4xxx；cleanup 只保留剧情用到的，
                # 这里也跟着只认这些，否则会把“刻意删掉的活动图”当成永远补不完的缺口。
                try:
                    nid = int(base[1:])
                except ValueError:
                    nid = 0
                if not used_npc or nid not in used_npc:
                    continue
            if base and not _public_img_ok(adir, base):
                out["avatar"].append(n)
    inv = _banner_inventory()
    if inv:
        try:
            have = {x for x in os.listdir(sdir)
                    if x.lower().endswith((".webp", ".png", ".jpg"))}
        except OSError:
            have = set()
        if inv - have:
            out["banner"] += [n for n in idx if n.startswith("bundle_image_story_")]
    return {k: v for k, v in out.items() if v}


#: 自动补公共素材的**最小**精确补额度（个包）。
#: 真正的判据见下面的 ``PUBLIC_DELTA_RATIO``（按占比）；这个数只是下限 ——
#: 清单还没下过 / 无法统计总数时，缺 12 个以内仍然精确补。
PUBLIC_AUTO_LIMIT = 12
#: 「精确补 / 整类重做」的分界（占该类包总数的比例）。
#:
#: 只看绝对个数会误判：2026-11-18 实测，用户头像 1086/1086、横幅 557/557 全齐，
#: 官方新增 6 部剧情只多了 16 个包 —— 但 16 > 12，于是被判成"缺口很大"、按类别
#: 整批重做，白下载/重导 1554 个头像包 + 394 个横幅包（几分钟）。
#: 真正该整批重做的只有"基本没装过"的用户，所以按**占比**判断。
PUBLIC_DELTA_RATIO = 0.2


def public_totals() -> dict:
    """清单里头像 / 横幅各有多少个包（判断"缺口算大还是算小"用）。"""
    try:
        c = _cur_counts()
    except Exception:                                          # noqa: BLE001
        return {"avatar": 0, "banner": 0}
    return {"avatar": int(c.get("avatar") or 0),
            "banner": int(c.get("banner") or 0)}


def install_public_delta(job, delta: dict) -> dict:
    """只补 ``delta`` 里列出的那几个公共素材包（下载 → 导出对应图）。

    与 :func:`install_base` 的区别：那个按**类别**全量重做（385 个头像 force 重导，
    要几分钟），这里只处理缺的那几个 —— 官方新增一个角色只是 2 个包。
    """
    names = list(delta.get("avatar") or []) + list(delta.get("banner") or [])
    if not names:
        return {"packs": 0}
    mf = index_manifest()
    ents = _entries(mf, names)
    if job is not None:
        job.log("公共素材补 %d 个包（头像 %d / 横幅 %d）…" % (
            len(names), len(delta.get("avatar") or []),
            len(delta.get("banner") or [])))
    stages.fetch(ents, 16, "公共素材", skip=True, pause_event=_pe(job), job=job)
    n_av = n_bn = 0
    for n in delta.get("avatar") or []:
        try:
            # 必须按**包名**导出：头像类有三族（cpanel / usericon d,b,n,e / 敌人战斗图标），
            # 以前统一当 cpanel 调 export_avatar(编号)，官方那几族一律导不出来 →
            # 缺口永远补不完（2026-11-18 排查）。export_avatar_bundle 认得全部三族。
            if stages.export_avatar_bundle(n, force=True, regen=False):
                n_av += 1
        except Exception:                                      # noqa: BLE001
            pass
    if n_av:
        try:
            stages.regen_mediaindex()     # 新头像落盘 → 前端立刻知道"这个有图了"
        except Exception:                                      # noqa: BLE001
            pass
    cids = []
    story_pkgs = False
    for n in delta.get("banner") or []:
        if n.startswith("bundle_episodeicons_episode_character_"):
            try:
                cids.append(int(n.rsplit("_", 1)[-1]))
            except ValueError:
                pass
        else:
            story_pkgs = True                 # bundle_image_story_*：章节横幅
    if cids:
        try:
            n_bn = (stages.export_char_banners(only=cids) or {}).get("new", 0)
        except Exception:                                      # noqa: BLE001
            pass
    if story_pkgs:
        try:
            stages.export_story_topics()
        except Exception:                                      # noqa: BLE001
            pass
    try:
        stages.regen_imgmap()
    except Exception:                                          # noqa: BLE001
        pass
    if job is not None:
        job.log(f"   公共素材补齐：头像 {n_av}、角色横幅 {n_bn}")
    # 补齐后把"已跟到清单的包数"记下来（和 install_base 末尾同一口径）：
    # 不写的话 new_public() 每次刷新清单都还会把这几类报成"有新增"。
    # 漏导出的个别图不靠 stamp 兜底 —— public_deficit() 是按磁盘上的图核对的。
    try:
        counts = _cur_counts()
        sub = {k: counts[k] for k in ("avatar", "banner") if k in counts}
        if sub:
            write_stamp(sub)
    except Exception:                                          # noqa: BLE001
        pass
    return {"packs": len(names), "avatars": n_av, "banners": n_bn}


#: 章节横幅"应有张数"的兜底种子（随仓库分发）。导出横幅时会另写一份到
#: ``userdata/cdn/story_banners.json``（官方新增章节后由它更新）；种子只用于
#: "图早就导出过、但没登记"的老用户 —— 否则界面会显示「章节 0/0」。
_BANNER_INV_SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "data", "story_banners.json")


def _banner_inventory() -> set:
    """章节横幅应该有哪几张（返回文件名集合，如 ``main_1.webp``）。

    优先读导出时写的登记文件，其次读仓库种子；都没有就返回空集
    （= 还不知道总数，界面上只显示角色横幅那部分）。
    """
    for p in (stages.STORY_BANNER_INV, _BANNER_INV_SEED):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:                                      # noqa: BLE001
            continue
        names = d.get("names") if isinstance(d, dict) else d
        if names:
            return {str(x) for x in names}
    return set()


#: 公共素材统计缓存。``/api/cdn/public`` 挂在播放器/CG/原声/立绘每一页首屏，
#: 一次要读 500+ 个 char_*.js 收集 stand 编号（冷调 2.2 秒，src=stages.py 的
#: _story_stand_ids），多 iframe 并发时会放大成 3~6 秒（2026-11-17 反馈：
#: "文件没变却每次点页面都重复耗时读取"）。开服预热（api.py serve()）与首个
#: 请求共用这把锁，预热结果不再丢弃；装/卸/清理后由 invalidate_units() 清掉。
_PUBLIC_STATUS_CACHE: dict = {"at": 0.0, "data": None}
_PUBLIC_STATUS_TTL = 30.0
_PUBLIC_STATUS_LOCK = threading.Lock()


def public_status(force: bool = False) -> dict:
    """带 30 秒缓存的版本（single-flight：并发只算一次）。"""
    now = time.time()
    c = _PUBLIC_STATUS_CACHE
    if not force and c["data"] is not None and now - c["at"] < _PUBLIC_STATUS_TTL:
        return c["data"]
    with _PUBLIC_STATUS_LOCK:
        now = time.time()
        if not force and c["data"] is not None and now - c["at"] < _PUBLIC_STATUS_TTL:
            return c["data"]
        out = _public_status_calc()
        c["data"] = out
        c["at"] = now
        return out


def _public_status_calc() -> dict:
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

    # 背景：分母按**引用视角**算（见下），先拿清单包数与磁盘图数
    try:
        idx = manifest.load_index()
    except Exception:                                          # noqa: BLE001
        idx = {}
    back_packs = len([n for n in idx if n.startswith("bundle_bgimage_")]) or 612
    back_files = _count(os.path.join(config.IMG, "back"),
                        (".webp", ".png", ".jpg"))
    # 「引用了但没有图」的张数由 gen_imgmap 写进 cdn/imgmap_miss.json。
    # 为什么要它：清单里的 bgimage 包**不等于**该有的图 —— 少数包没有任何已装
    # 剧情引用（官方遗留/未使用），导出时不会产生图。以前分母固定用包数，
    # 界面就是「背景 601/612 … 已就绪」这种自相矛盾的组合（2026-11-04 用户反馈）。
    # 现在：分母 = 引用到的张数 = 已导出 + 引用但缺 → 缺图会被算成不齐（该补全），
    # 没被引用的包不进分母，只在提示里说明有几个包没用上。
    miss_path = os.path.join(config.MANIFEST_DIR, "imgmap_miss.json")
    back_ref_miss, has_miss = 0, False
    try:
        with open(miss_path, encoding="utf-8") as f:
            back_ref_miss = len(json.load(f).get("back") or [])
        has_miss = True
    except Exception:                                          # noqa: BLE001
        back_ref_miss, has_miss = 0, False
    back_ref_total = back_files + back_ref_miss
    back_total = back_ref_total if has_miss else back_packs
    n_back = min(back_files, back_total)

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
    # 本轮头像库产物：d### 日常 / b### 战斗 / n#### NPC / e#### 敌人。
    # 旧的 cpanel <编号>.png 仍然保留作兜底，但不混进“官方头像”计数。
    avatar_files = [n for n in os.listdir(adir2)
                    if re.match(r"^[dbne]\d+\.(png|webp|jpg)$", n, re.I)]
    n_avatar = len(avatar_files)
    # 分母按**清单里官方头像包的应产物数**算，不能等于当前文件数：
    # 以前 avatar_total = n_avatar，用户一张都没下时 0/1 尚可，
    # 但下了一半时会出现 500/500 的假“齐全”，侧栏与播放器照样去请求缺图。
    # NPC 4xxx 只算剧情真正用到的（cleanup_unused_npc_avatars 会删掉活动图），
    # 避免把刻意没导出的活动 NPC 算成缺口；已有多出来的文件不惩罚。
    used_npc = stages._story_stand_ids(4000, 4399)
    expected_avatar = {
        b for b in (_official_avatar_output(n) for n in idx)
        if b and (not b.startswith("n") or int(b[1:]) in used_npc)
    }
    avatar_total = max(len(expected_avatar), n_avatar) or 1

    # 剧情横幅：按**导出的图片张数**算，角色 / 章节分开数。
    #
    # 以前按"包是否已下载"算：384 个角色横幅包 + 5 个章节横幅包 = 389，界面就
    # 写成「横幅 389/389」。可那 5 个章节包里装着主线/活动/其他的**全部**章节
    # 标题图（一批 Sprite，实测 172 张），所以 389 既不是图数、也不是横幅数
    # （用户 2026-11-02 反馈：「角色剧情横幅都 384 个，活动主线其他呢」）。
    # 列表要不要显示横幅看的是 bannerImg（磁盘上有没有图），不是 bundle 缓存 ——
    # 用户清过缓存后图还在，不该莫名退回纯文字。
    story_dir = os.path.join(config.IMG, "story")
    try:
        story_files = {n for n in os.listdir(story_dir)
                       if n.lower().endswith((".webp", ".png", ".jpg"))}
    except OSError:
        story_files = set()
    char_total = len([n for n in idx
                      if n.startswith("bundle_episodeicons_episode_character_")]) or 384
    n_char_banner = len([f for f in story_files if f.startswith("char_")])
    # 章节横幅的"应有张数"只能读登记文件（图是包里的 Sprite，index 里没有条目）
    inv = _banner_inventory()
    story_total = len(inv)
    n_story_banner = len(story_files & inv) if inv else 0
    banner_total = char_total + story_total
    banner_have = (min(n_char_banner, char_total)
                   + min(n_story_banner, story_total))
    n_banner_img = len(story_files)

    # 演出特效：产物是「逐帧 JSON + 图集 webp + 台词行内图标」，齐了才算装上。
    # 判的是产物不是 bundle 缓存：用户清过缓存后特效仍然能播，不该显示成缺。
    # msg_heart.png（<sprite=0> 的粉心）是这一版新加的产物：老用户这里会判成
    # 未完成 → 点「补全」重装一次就会连 bundle_font_66_heart_03 一起拉下来重导。
    advfx_ok = (os.path.exists(os.path.join(config.DATA, "advfx.js"))
                and os.path.exists(os.path.join(config.IMG, "advfx", "mark.webp"))
                and os.path.exists(os.path.join(config.IMG, "advfx", "msg_heart.png")))

    # 原声集：产物 = gallery.js 里"有音频"的曲目。分母按**官方清单编号**算，
    # 官方上新曲后分母自动变大 → 按钮从「已就绪」变回「补全」，点一下就能听到。
    # （2026-11-16：以前分母是随程序分发的表，新曲永远不出现也不提示。）
    gal_have = gallery_have_audio()
    n_gal_jacket = _count(os.path.join(config.IMG, "jacket"), (".webp", ".png"))
    gal_total = len(gallery_audio_nos(idx)) or len(gallery_tracks()) or 165

    # 全部立绘（可选）：分子是**磁盘成品张数**（展示用）；判齐看
    # **每套服装是否真的按包内全套表情合成过**（合成脚本写的组登记表）——
    # 只看"源包数"会在"包下齐了但某一批没合成"时误报「已就绪」
    # （2026-11-20 反馈：装完全部剧情 + 全部立绘，部分角色仍只有 3~4 组）。
    stand_packs = len([n for n in idx if n.startswith(STAND_PACK_PREFIX)]) or 2623
    n_stand_img = stand_img_count()
    try:
        _stand_left = stand_extra_missing(idx)
    except Exception:                                          # noqa: BLE001
        _stand_left = []
    _stand_checked = bool(read_stand_extra_groups())
    stand_extra_ok = not _stand_left and n_stand_img > 0
    # 「没有任何剧情包包含」的张数 = 磁盘总数 − imgmap 里被剧本引用到的键数。
    # 界面上要写清这部分才是这一项独有的价值（不然用户觉得"我装了剧情怎么还缺"）。
    try:
        from .. import media as _media
        _referenced = len((_media.load_imgmap().get("stand") or {}))
    except Exception:                                          # noqa: BLE001
        _referenced = 0
    stand_extra_only = max(0, n_stand_img - _referenced)
    # NPC / 敌 SD：清单里编号 ≥ 10000 的那批，齐了才算装上。
    # 扣掉「本机导不出 SD」的编号：它们不该占分母，否则界面永远
    # 2026-11-16：SD 小人 + NPC/敌音声整体停用，这几项统计不再需要。
    # 前端已删除对应展示，这里固定返回 0，避免别的调用方 KeyError。
    # sd_ids = sd_installed_ids()
    # npc_nums = {...}; n_sd_npc = len(npc_nums & sd_ids)
    # sd_npc_total = len(npc_nums) or 435
    # npc_voice_nums = {...}; n_voice = ...
    # voice_total = len(npc_voice_nums) or 435
    n_sd_npc = 0
    sd_npc_total = 0
    n_voice = 0
    voice_total = 0

    return {"back": n_back, "backTotal": back_total,
            # 背景的三个补充口径：
            #   backPacks   = 清单里的 bgimage 包数（612）
            #   backRefMiss = 被已装剧情引用、但磁盘上没有的图（>0 才是真缺）
            #   backUnused  = 清单里有、但没有任何已装剧情引用的包（导出时不会有图）
            "backPacks": back_packs, "backRefMiss": back_ref_miss,
            "backUnused": max(0, back_packs - back_ref_total) if has_miss else 0,
            # 背景是否已按"引用视角"核对过（读过 imgmap_miss.json）。
            # 没核对过时分子分母都是**包数口径**（601/612），差的那几张其实
            # 是"没有任何已装剧情引用"的包 —— 前端据此决定要不要按 1:1 严格判齐。
            "backVerified": has_miss,
            "music": min(n_music, music_total), "musicTotal": music_total,
            "sfx": min(n_sfx, sfx_total), "sfxTotal": sfx_total,
            "avatar": min(n_avatar, avatar_total), "avatarTotal": avatar_total,
            "banner": banner_have, "bannerTotal": banner_total,
            # 角色横幅（一包一图）与章节横幅（一个包一批图）分开给：界面上
            # 「横幅 556/556（角色 384 · 章节 172）」里的两个数就是它们。
            "bannerChar": min(n_char_banner, char_total),
            "bannerCharTotal": char_total,
            "bannerStory": min(n_story_banner, story_total),
            "bannerStoryTotal": story_total,
            "bannerImg": n_banner_img,
            # 结构版本：前端据此判断"后端是不是还没重启/还是旧代码"。
            # 2 = 加了 banner 字段；3 = 横幅改成按图数（角色/章节分开）；
            # 4 = 背景改成"按引用算" + backPacks/backRefMiss/backUnused；
            # 5 = 加 backVerified（前端据此对"已核对过"的背景按 1:1 严格判齐）；
            # 6 = 加 advfx（演出特效：表情符号逐帧数据 + 图集）。
            # 7 = advfx 追加台词行内图标 msg_heart.png（<sprite=0>）。
            # 10 = voice/voiceTotal 只统计 NPC / 敌人（不再把角色音声混进分母）。
            # 11 = 音声是否装上 = 目录有音频 **且** voicemap.js 里有条目
            #      （以前只判目录，清单被并发覆盖掉的单位会显示"已安装"却听不到）。
            # 12 = 原声集改成"有音频的曲目数 / 清单曲目数"（gallery/galleryTotal
            #      不再是 0-1 布尔值）；分母含官方新曲，上新后按钮会变回「补全」。
            # 13 = 加 standExtra（可选「全部立绘」：含剧情没用到的表情）+
            #      standExtraPacks / standExtraOk。
            # 14 = 加 standExtraMissing（还有多少套服装没合成过）：判齐改成按
            #      合成脚本写的组登记表对账，不再信"源包数 >= 清单包数"。
            # 15 = 加 standExtraChecked（台账在不在）：老用户升级后第一次缺的
            #      数量等于"全部服装"，界面据此只提示"点一次补全核对"。
            # 前端看到 v 低于自己需要的版本，就不能把缺字段当成 0/0，
            # 否则界面会显示「横幅 0/0」这种看起来像"没有横幅"的假象。
            "advfx": 1 if advfx_ok else 0, "advfxTotal": 1,
            # 原声集：已导出音频的曲目数 / 清单里的曲目数。v12 起按真实曲目算
            # （以前是 1/1，只看 gallery.js 在不在 → 官方新曲不会被发现）。
            "gallery": min(gal_have, gal_total), "galleryTotal": gal_total,
            # 封面数单独给，只用于展示（缺封面不该判成"没装好"）
            "jacket": min(n_gal_jacket, gal_total), "jacketTotal": gal_total,
            # 全部立绘（v13）：磁盘成品张数 + 是否已经按最新清单跑完过。
            # standExtraPacks 是清单里的立绘源包数（不是本地包数 —— 清理过缓存
            # 的用户本地是 0，但成品和标记都在，不该让他重下）。
            "standExtra": n_stand_img, "standExtraPacks": stand_packs,
            "standExtraOnly": stand_extra_only,
            "standExtraOk": bool(stand_extra_ok),
            # 还有多少套服装没按全套表情合成过（0 = 真的齐了）
            "standExtraMissing": len(_stand_left),
            # 组登记表在不在：0 = 这台机器还没用新判据核对过（老用户升级后
            # 第一次，standExtraMissing 会等于全部服装数，此时界面不要写成
            # "还有 2650 套要补"，那会把人吓一跳 —— 实际可能只缺几十套）。
            "standExtraChecked": 1 if _stand_checked else 0,
            # NPC / 敌人 SD：已合成的编号数 / 清单里的编号数
            "sdNpc": n_sd_npc, "sdNpcTotal": sd_npc_total,
            # 战斗语音：已导出的 NPC 单位数 / 清单里的 NPC 单位数
            "voice": n_voice, "voiceTotal": voice_total,
            "v": 15,
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
        job.set_total(max(1, len(units)), "部")
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
        raise _E("E-DEP", "缺少安装组件：" + "、".join(miss)
                            + "。到「剧情安装」页点「自动安装依赖」，装完再重试")
    with _capture(job):
        mf = select.Manifest(ensure_manifest(job))

        def _run(m):
            if key.startswith("char_"):
                try:
                    cid = int(key[5:])
                except ValueError as e:
                    raise _E("E-ID", f"角色标识不对：{key}") from e
                return _install_char(m, cid, job, workers, voice, image, maps, force,
                                     upscale, upscale_scale, upscale_engine)
            mm = _UNIT_RE.match(key)
            if not mm:
                raise _E("E-ID", f"无法识别的剧情标识：{key}"
                                 "（应为 char_12 或 adv_main_3）")
            kind = mm.group(1)
            no = int(mm.group(2)) if mm.group(2) else None
            return _install_adv(m, kind, no, job, workers, voice, image, maps, force,
                                upscale, upscale_scale, upscale_engine)

        for attempt in (0, 1):
            try:
                return _run(mf)
            except _StaleManifest as e:
                # 清单过期自愈：下载源上的包和本机清单里的 size/条目对不上。
                # 刷新一次清单（约 30–60 秒）再重跑 —— 各 stage 都是幂等的，
                # 已经下好/解好的部分不会重做。
                if attempt:
                    raise _E("E-STALE", f"{e}（刷新清单后仍然对不上，当前的下载源"
                                        "可能需要更换，或官方正在更新资源）") from None
                if job is not None:
                    job.log(str(e))
                    job.log("本机资源清单可能是旧的，正在刷新清单"
                            "（约 30–60 秒，这段时间进度条不动是正常的），"
                            "随后自动重试…")
                try:
                    manifest.refresh()
                except Exception as e2:                        # noqa: BLE001
                    raise _E(net.error_code(e2),
                             f"{e}；刷新清单也失败了：{str(e2)[:120]}"
                             "（请检查网络后重试）") from e2
                mf = select.Manifest(manifest.load_index())
                if job is not None:
                    job.log("清单已更新，重新安装…")


def _bname(n) -> str:
    """清单条目 → 包名字符串。

    官方清单（Manifest2Ex）里的条目是 ``{"bundle_name": ..., "size": ...}`` 这种
    **dict**，而 ``index`` 的 key 是包名字符串。直接拿 dict 当 key 会抛
    ``TypeError: unhashable type: 'dict'``，而 ``_unit_bytes`` 外面那层
    ``except: return 0`` 会把它吞成"0 字节"（adv 剧情实测全为 0）。
    """
    if isinstance(n, dict):
        return str(n.get("bundle_name") or "")
    return str(n or "")


def _sz_of(mf, names, pending: bool = False) -> int:
    """一批包名 -> 字节数。

    ``pending=True`` 时**只算本机还缺的**：点「补全」时绝大多数包早就在盘上，
    按"全部包的体积"当分母会算出「要下 13.6 GB / 剩 66 小时」这种吓人的数字
    （2026-11-15 反馈：补 382 个角色的 SD，界面写着 13.61 GB）。
    """
    tot = 0
    for n in names or ():
        if not n:
            continue
        ent = mf.index.get(n) or {}
        size = int(ent.get("size") or 0)
        if pending and not net.need_download(net.bundle_path(n), size):
            continue
        tot += size
    return tot


def _unit_bytes(mf, key: str, pending: bool = False) -> int:
    """预估某部剧情的全部 bundle 字节数（剧情 + 语音 + 头像 + 图片包）。

    多单元安装时要在**开始前**就知道总字节，才能给进度条一个稳定的分母
    （否则每下一批就改分母，会出现「done 28 / total 2」这种自相矛盾的进度）。
    只读清单索引，不下载；和 ``_install_char`` / ``_install_adv`` 里实际下的包一致。
    """
    try:
        if key.startswith("char_"):
            cid = int(key[5:])
            raw = [mf.scenario(cid)] + list(mf.voices(cid)) \
                + list(mf.meta_images(cid))
            # 清单条目可能是 dict（带 bundle_name）或包名字符串，统一成名字
            names = [nm for nm in (_bname(x) for x in raw) if nm]
            # 战斗语音 + 主页语音：装角色剧情时会一并下（第 ⑧ 步），保留。
            # 2026-11-16：SD 小人已停用，不再把 sd_bundle_name(cid) 计进来。
            for extra in (voice_bundle_name(cid),
                          "bundle_systemvoice_%05d" % cid):
                if extra in mf.index:
                    names.append(extra)
            sz = _sz_of(mf, names, pending)
            try:
                assets = select.collect_assets(select.load_char(cid))
            except Exception:                                  # noqa: BLE001
                return sz
            need = []
            for kind in ("back", "cg", "stand"):
                for s in assets.get(kind) or ():
                    b = (mf.back_bundle(s) if kind == "back"
                         else mf.cg_bundle(s) if kind == "cg"
                         else mf.stand_bundle(s))
                    if b:
                        need.append(_bname(b))
                    mk = select.PAT_STAND_KEY.match(s) if kind == "stand" else None
                    if mk:
                        full = mf.stand_full.get(int(mk.group(1)))
                        if full:
                            need.append(_bname(full))
            need = sorted({n for n in need if n})
            sz += _sz_of(mf, need, pending)
            return sz
        m = _UNIT_RE.match(key)
        if not m:
            return 0
        kind, no = m.group(1), (int(m.group(2)) if m.group(2) else None)
        # 与 _install_adv 保持一致：剧本包 + 语音包（语音是它真正下的大头）
        names = [_bname(mf.adv_scenario(kind, no))]
        try:
            names += [_bname(x) for x in
                      (select.adv_voice_bundles(mf.index, kind, no) or ())]
        except Exception:                                      # noqa: BLE001
            pass
        sz = _sz_of(mf, names, pending)
        try:
            assets = select.collect_assets(select.load_story(key))
        except Exception:                                      # noqa: BLE001
            return sz
        need = []
        for k in ("back", "cg", "stand"):
            for s in assets.get(k) or ():
                bb = (mf.back_bundle(s) if k == "back"
                      else mf.cg_bundle(s) if k == "cg"
                      else mf.stand_bundle(s))
                if bb:
                    need.append(_bname(bb))
                mk = select.PAT_STAND_KEY.match(s) if k == "stand" else None
                if mk:
                    full = mf.stand_full.get(int(mk.group(1)))
                    if full:
                        need.append(_bname(full))
        need = sorted({n for n in need if n})
        sz += _sz_of(mf, need, pending)
        return sz
    except Exception:                                          # noqa: BLE001
        return 0


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
        raise _E("E-NOBUNDLE", f"官方清单里找不到角色 {cid} 的剧本包"
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
    st0 = stages.fetch(base_entries, workers, f"char{cid} 基础包", skip=True, pause_event=_pe(job), job=job)

    # 剧本包是整条链路的地基：它没落盘，后面一律没意义。这里先看**文件在不在**，
    # 再决定要不要解析 —— 失败时用下载层记下的真实原因报错（见 _scenario_fail）。
    scen_name = _bname(scen)
    scen_path = os.path.join(config.FILES_STORY, f"bundle_scenarioscript_char_{cid}")
    if not os.path.exists(scen_path):
        raise _scenario_fail(f"角色 {cid}", st0, scen_name)

    if job is not None:
        job.log("② 解析剧本 → data/char_%d.js" % cid)
    try:
        ok = stages.extract_scenario(cid, force=force)
    except OSError as e:
        # 解出来了但写不进去（磁盘满 / 目录只读 / 被占用）：别说成"包坏了"。
        happened, how = net.explain_error(e)
        code = net.error_code(e)
        raise _E(code if code != "E-UNKNOWN" else "E-WRITE",
                 f"角色 {cid} 的剧本已经解出来，但保存失败：{happened}。"
                 f"{how}（原始错误：{str(e)[:120]}）") from e
    except Exception as e:                                     # noqa: BLE001
        # 文件在、但内容解不开：和"没下下来"是两回事，必须分开说。
        raise _E("E-UNPACK", f"角色 {cid} 的剧本包解不开：{str(e)[:160]}。"
                             "两种情况：① 文件只下了一半（重装这部剧会重新下载）；"
                             "② 官方换了打包格式，当前版本还不认识（升级到最新版）") from e
    if not ok:
        raise _E("E-VANISH", f"角色 {cid} 的剧本包解不开：本机缺少 "
                             f"{os.path.basename(scen_path)}，重装这部剧会重新下载")

    if voice:
        if job is not None:
            job.log("③ 导出语音")
        vs = stages.export_voices(cid, workers=workers)
        _warn_voice_fail(job, vs, "这部剧")

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
    if not stages.export_avatar(cid) and job is not None:
        # 头像不是播放必需品（剧情/立绘都在），但左侧列表会一直显示首字色块，
        # 用户会当成"没装好"。这一行必须落日志。
        job.log("⚠ 角色头像没裁出来（左侧列表会显示首字色块）：多半是半身像包"
                "没下到，或包的格式不认识 —— 重装这部剧会重新下载")

    # 官方独立头像（日常 / 战斗）也一起拿：播放器现在优先用这两张，
    # 不再依赖 cpanel 裁图。装单部剧情也能立刻看到官方头像。
    off_names = [f"bundle_usericon_user_icon_{int(cid):04d}",
                 f"bundle_usericon_user_icon_{1000 + int(cid):04d}"]
    off_ents = _entries(mf, [n for n in off_names if n in mf.index])
    if off_ents:
        stages.fetch(off_ents, workers, "官方头像", skip=True,
                     pause_event=_pe(job), job=job)
        stages.export_avatar_official("daily", cid)
        stages.export_avatar_official("battle", cid)
        try:
            stages.regen_avatarmap()
            stages.regen_mediaindex()
        except Exception:                                      # noqa: BLE001
            pass

    # 角色剧情的**横幅**（游戏「角色剧情」入口那张图）也在公共素材里：
    # `bundle_episodeicons_episode_character_<cid:05d>` → media/img/story/char_<cid>.webp。
    # 以前装剧情只拿头像、不拿横幅，于是官方新实装的角色装完剧情后，侧栏横幅
    # 还是 404（用户 2026-11-04 报「纯净全量包更新后横幅和头像都 404」）。
    # 名字是 5 位补零，和 c-panel 的 3 位补零不是一回事。
    bn_name = f"bundle_episodeicons_episode_character_{int(cid):05d}"
    bn_ents = _entries(mf, [bn_name] if bn_name in mf.index else [])
    if bn_ents:
        stages.fetch(bn_ents, workers, "角色横幅", skip=True,
                     pause_event=_pe(job), job=job)
    try:
        stages.export_char_banners(only=[cid])
    except Exception as e:                                     # noqa: BLE001
        if job is not None:
            job.log(f"   角色横幅导出失败（不影响播放）：{str(e)[:80]}")

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
            # 「清单里没这个图」和「图解不出来」是两回事，分开说：
            if ex.get("miss"):
                head = ", ".join(f"{k}:{s}" for k, s in ex["miss"][:5])
                job.log(f"   ⚠ {len(ex['miss'])} 个图片在官方清单里找不到对应包"
                        f"（{head}）—— 官方源本身缺图，播放到那里会跳过不出图")
            if ex.get("fail"):
                job.log(f"   ⚠ {ex['fail']} 张图解不出来：包不完整，或图片格式"
                        "当前版本不认识 —— 重装这部剧会重新下载这些包")
        stages.export_standfull(mf, nums)
        if nums:
            if job is not None:
                job.log(f"⑤ 合成表情差分立绘（{len(nums)} 套）")
            stages.compose_stands(nums)
            _warn_stand_output(job, nums)
        # 剧本引用了、但包里没有对应表情的立绘（表情码其实是文本）→ 素体顶替
        stages.fill_missing_stands(assets.get("stand") or (), job)
        # 本剧出现的 NPC / 敌人也要有对话框头像（官方 2xxx 有就导；活动限定没有
        # 2xxx 的，从刚合成好的立绘按官方脸框现裁）。
        _install_story_avatars(mf, assets.get("stand") or (), job, workers)

    # 2026-11-16：SD 小人整体停用，原 ⑦ 步已删除
    # if image:
    #     _install_sd(mf, cid, job, workers)
    if image:
        # 战斗语音（音声）：同一个单位的另一类素材，约 1 MB，装好后
        # 「原声集 → 角色音声」里就能听。角色音声保留，不受 SD 停用影响。
        if job is not None:
            job.log("⑦ 战斗语音（音声）")
        try:
            _install_voice(mf, cid, job, workers)
        except Exception as e:                                   # noqa: BLE001
            if job is not None:
                job.log(f"   战斗语音没装上（不影响剧情播放）：{str(e)[:120]}")

    if upscale and image:
        _upscale_assets(job, assets, scale, engine)

    if maps:
        if job is not None:
            job.log("⑥ 刷新索引与映射表")
        try:
            stages.sync_index()
            stages.sync_cgmap(cid)
            stages.regen_maps()
        except Exception as e:                                 # noqa: BLE001
            # 素材已经落盘，只是列表/索引没刷新：说清"东西在，别重下"，
            # 而不是扔一段英文堆栈让用户以为整部都白装了。
            raise _E("E-INDEX", "剧情和素材已经装好，但刷新列表 / 图片索引失败："
                                f"{str(e)[:160]}。重启程序后重试即可"
                                "（已下载的资源不会重下）") from e
        stages.regen_names()
        # Wiki 还没收录实装日时，记一个「包日期」= 资源包**源文件**日期
        # （CDN Last-Modified，对所有人一致）。它不写进实装日字段，页面上显示成
        # 「包日期」；编号 < 100 的老角色只拿它排序、不显示。
        p_src = stages.ensure_pack_date(cid)
        if p_src and job is not None:
            job.log(f"   包日期 {p_src}（资源包源文件日期；Wiki 收录实装日后以 Wiki 为准）")
        stages.regen_charmeta()

    chk = _self_check(job, f"char_{cid}")
    return {"unit": f"char_{cid}", "seconds": round(time.time() - t0, 1),
            "stands": len(nums), "check": chk}


def _install_story_avatars(mf, stand_ids, job=None, workers: int = 16) -> dict:
    """把这部剧用到的**角色 / NPC / 敌人**头像补齐（``d/b`` / ``n`` / ``e``）。

    需求来自剧本引用到的立绘编号（``stand_<编号>_...``）：
      · ``1-416`` → ``d###`` / ``b###``（usericon 0001.. / 1001..，日常/战斗）
      · ``4xxx`` → ``n####``（usericon 4xxx，剧情 NPC）
      · ``5xxx`` → ``e####``：**只认官方 usericon 2xxx**（剧情敌人标准半身像）。
        官方没有就保持无头像 —— 不从立绘现裁（用户 2026-11-23 要求：官方原本
        没头像的（老人乙 / ハニー 这种）对话框里就不该出现头像）。
    已存在的文件一律跳过（幂等），所以角色剧情（自己那张已由 ④ 步导好）不会重复。

    为什么放在剧情安装里：以前只有「公共素材 → 补全」才会导这批图，新剧情里
    一出新敌人，对话框头像就是空的，用户以为坏了（2026-11-20 要求自动化）。
    体积很小（2xxx 包约 12 KB/个），已下过的包 fetch(skip=True) 不会重下；
    任何异常都只记日志，绝不让剧情安装失败。
    """
    adir = os.path.join(config.IMG, "avatar")

    def has(key: str) -> bool:
        return os.path.exists(os.path.join(adir, key + ".png"))

    want_c, want_n, want_e = set(), set(), set()
    for raw in stand_ids or ():
        m = re.match(r"^stand_(\d+)_", str(raw))
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= 416:                      # 角色：日常 / 战斗两张
            if not (has(f"d{n:03d}") and has(f"b{n:03d}")):
                want_c.add(n)
        elif 4000 <= n <= 4399:                # 剧情 NPC
            if not has(f"n{n:04d}"):
                want_n.add(n)
        elif 5000 <= n <= 5999:                # 剧情敌人 / 活动限定
            if not has(f"e{n:04d}"):
                want_e.add(n)
    if not want_c and not want_n and not want_e:
        return {"char": 0, "npc": 0, "enemy": 0, "cropped": 0}
    pkgs = []
    for n in sorted(want_c):
        for name in (f"bundle_usericon_user_icon_{n:04d}",
                     f"bundle_usericon_user_icon_{1000 + n:04d}"):
            if name in mf.index:
                pkgs.append(name)
    for n in sorted(want_n):
        name = f"bundle_usericon_user_icon_{n:04d}"
        if name in mf.index:
            pkgs.append(name)
    for n in sorted(want_e):
        # 只补官方 usericon 2xxx；battle_character_icon 200xx 是角色图标，
        # 与 5xxx 剧情敌人无关（见 stages.enemy_avatar_has_official）。
        name = f"bundle_usericon_user_icon_{n - 3000:04d}"
        if name in mf.index:
            pkgs.append(name)
    pkgs = sorted(set(pkgs))
    if pkgs:
        if job is not None:
            job.log(f"   剧情头像：{len(pkgs)} 个包（角色 {len(want_c)} / "
                    f"NPC {len(want_n)} / 敌人 {len(want_e)}）")
        try:
            stages.fetch(_entries(mf, pkgs), workers, "剧情头像", skip=True,
                         pause_event=_pe(job), job=job)
        except Exception:                                      # noqa: BLE001
            pass
    c_ok = n_ok = e_ok = dropped = 0
    for n in sorted(want_c):
        for kind in ("daily", "battle"):
            if has(f"d{n:03d}" if kind == "daily" else f"b{n:03d}"):
                continue
            try:
                if stages.export_avatar_official(kind, n, force=False, regen=False):
                    c_ok += 1
            except Exception:                                  # noqa: BLE001
                pass
    for n in sorted(want_n):
        try:
            if stages.export_avatar_official("npc", n, force=True, regen=False):
                n_ok += 1
        except Exception:                                      # noqa: BLE001
            pass
    for n in sorted(want_e):
        # 只导官方 usericon 2xxx；官方没有 → 这一位就保持没有头像。
        try:
            if stages.export_avatar_official("enemy", n, force=True, regen=False):
                e_ok += 1
        except Exception:                                      # noqa: BLE001
            pass
    # 顺带清掉"来源不可靠"的历史 e 头像（旧版把活动宣传图 / 别的角色的战斗
    # 图标 / 从立绘现裁的图写成了 e####）：有官方 usericon 2xxx 的保留，
    # 其余删掉、保持无头像（2026-11-23 用户要求）。
    cropped = 0
    try:
        cropped, dropped = stages.fix_enemy_avatars(mf.index)
    except Exception:                                          # noqa: BLE001
        dropped = 0
    if c_ok or n_ok or e_ok or cropped or dropped:
        try:
            from .. import media
            stages.regen_avatarmap()
            stages.regen_mediaindex()
            media.rebuild_x2_overlay()
        except Exception:                                      # noqa: BLE001
            pass
        if job is not None:
            job.log(f"   头像补齐：角色 {c_ok} 张、NPC {n_ok} 张、敌人 {e_ok} 张"
                    + (f"、立绘裁 {cropped} 张" if cropped else "")
                    + (f"、清理无官方来源 {dropped} 张" if dropped else ""))
    return {"char": c_ok, "npc": n_ok, "enemy": e_ok, "cropped": cropped}


def _install_adv(mf, kind, no, job, workers, voice, image, maps, force,
                 upscale=False, scale=2, engine="cugan") -> dict:
    t0 = time.time()
    key = select.story_key(kind, no)
    b = mf.adv_scenario(kind, no)
    if not b:
        raise _E("E-NOBUNDLE", f"官方清单里找不到 {key} 的剧本包"
                               "（多半是清单过期：点「更新」刷新资源清单后重试）")
    if job is not None:
        # 只打包名：b 是清单条目 dict，直接插值会把
        # "{'bundle_name': ..., 'crc': ..., 'hash': ...}" 整段写进操作记录
        job.log(f"① 剧本包：{_bname(b)}")
    st0 = stages.fetch(_entries(mf, [b]), workers, f"{key} 剧本", skip=True, pause_event=_pe(job), job=job)
    # 与角色剧情同一条判据：先看包里文件在不在，再用下载层的真实原因报错。
    scen_name = _bname(b)
    scen_path = os.path.join(config.FILES_STORY, scen_name)
    if not os.path.exists(scen_path):
        raise _scenario_fail(key, st0, scen_name)
    try:
        ok = stages.extract_adv(kind, no, force=force)
    except Exception as e:                                     # noqa: BLE001
        raise _E("E-UNPACK", f"{key} 的剧本包解不开：{str(e)[:160]}。"
                             "两种情况：① 文件只下了一半（重装这一部会重新下载）；"
                             "② 官方换了打包格式，当前版本还不认识（升级到最新版）") from e
    if not ok:
        raise _E("E-VANISH", f"{key} 的剧本包解不开：本机缺少 {scen_name}，"
                             "重装这一部会重新下载")

    if voice:
        if job is not None:
            job.log("② 语音包")
        vbundles = select.adv_voice_bundles(mf.index, kind, no)
        if vbundles:
            stages.fetch(_entries(mf, vbundles), workers, f"{key} 语音", skip=True, pause_event=_pe(job), job=job)
            vs = stages.export_adv_voices(key, vbundles)
            _warn_voice_fail(job, vs, "这一部")

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
                job.log(f"   ⚠ {len(ex['miss'])} 个图片在官方清单里找不到对应包"
                        f"（{head}）—— 官方源本身缺图，播放到那里会跳过不出图")
            if ex.get("fail"):
                job.log(f"   ⚠ {ex['fail']} 张图解不出来：包不完整，或图片格式"
                        "当前版本不认识 —— 重装这一部会重新下载这些包")
        stages.export_standfull(mf, nums)
        if nums:
            if job is not None:
                job.log(f"④ 合成表情差分立绘（{len(nums)} 套）")
            stages.compose_stands(nums)
            _warn_stand_output(job, nums)
            stages.fill_missing_stands(assets.get("stand") or (), job)
        else:
            # 非角色剧情也要章节横幅
            stages.export_story_topics()
        # 本剧出现的 NPC / 敌人头像：官方 2xxx/15xxx 有就导，活动限定没有的就
        # 从立绘按官方脸框现裁（2026-11-20：新剧情不再需要手动点「补全」）。
        _install_story_avatars(mf, assets.get("stand") or (), job, workers)

    if upscale and image:
        _upscale_assets(job, assets, scale, engine)

    if maps:
        if job is not None:
            job.log("⑤ 刷新索引与映射表")
        try:
            stages.sync_advindex()
            # 「其他」剧情：刚装上的这一包按**正文内容**判定属于哪个游戏入口
            # （殲滅戦 / RAID / 広域戦 / 纪念活动…），补进 othereps.js —— 不依赖人工
            # 关联表，官方下次更新照样能归（判定规则见 stages.sync_othereps）。
            stages.sync_othereps(job=job)
            stages.sync_index()
            stages.regen_maps()
        except Exception as e:                                 # noqa: BLE001
            raise _E("E-INDEX", "剧情和素材已经装好，但刷新列表 / 图片索引失败："
                                f"{str(e)[:160]}。重启程序后重试即可"
                                "（已下载的资源不会重下）") from e
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
