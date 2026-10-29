"""官方 CDN 直连模式的路径与常量（整个 cdn 子包的唯一真源）。

和原 `_manifest_analysis/pipeline/config.py` 的区别
-------------------------------------------------
1. **产物根**：原来 `ROOT` 是 `_manifest_analysis`（构建工作区），`web/` 在它外面；
   现在 `ROOT` 就是本项目根，``web/``、``server/``、``userdata/`` 都在它下面。
2. **工作区下沉到 userdata**：bundle 缓存、清单缓存、旧脚本的临时产物全部放
   ``userdata/``，这样它们天然被 ``.gitignore`` 排除，不会污染仓库。
3. **旧脚本的 ``../web`` 约定被保留**：工作区设为 ``ROOT/userdata``，
   所以 cwd=工作区时 ``../web`` 正好等于 ``ROOT/web`` —— 那 5 个用相对路径的
   旧脚本（gen_imgmap / gen_charmeta / gen_names_* / extract_names / export_sfx）
   **一个字都不用改**。
4. **CDN 域名可被用户覆盖**：官方域名将来若变更，用户改 ``userdata/links.json``
   或设置页即可（见 :func:`cdn_base`）。仓库里默认值也是加密存放的。
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------- 根目录
# server/cdn/config.py -> server/cdn -> server -> <项目根>
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
WEB = os.path.join(ROOT, "web")
SERVER = os.path.join(ROOT, "server")
SCRIPTS = os.path.join(_HERE, "scripts")        # 从 _scripts 迁过来的解包脚本

# ---------------------------------------------------------------- 工作区
# 注意：必须是 ROOT 的**直接子目录**，旧脚本里的 "../web" 才能解析正确
WS = os.path.join(ROOT, "userdata")
FILES_STORY = os.path.join(WS, "char_story", "files")   # 剧本/语音/半身像
FILES_IMG = os.path.join(WS, "files_img")               # 立绘/背景/CG
FILES_AUDIO = os.path.join(WS, "files_audio")           # BGM/音效
MANIFEST_DIR = os.path.join(WS, "cdn")                  # 清单缓存（4.6 MB，可再生）
STATE_DIR = os.path.join(WS, "cdn", "state")            # 增量状态
I18N_SOURCE_DIR = os.path.join(WS, "trans")             # 待注入译稿
ARCHIVE_DIR = os.path.join(WS, "cdn", "archive")        # 旧脚本的备份/质检产物

# ---------------------------------------------------------------- 网页目录
DATA = os.path.join(WEB, "data")
MEDIA = os.path.join(WEB, "media")
IMG = os.path.join(MEDIA, "img")
IMG_X2 = os.path.join(MEDIA, "img_x2")
AUDIO = os.path.join(MEDIA, "audio")
ASSETS = os.path.join(WEB, "assets")
I18N = os.path.join(ASSETS, "i18n")
CSS = os.path.join(ASSETS, "css")
JS = os.path.join(ASSETS, "js")

STORY_SUBDIR = {"scenario": FILES_STORY, "image": FILES_IMG, "audio": FILES_AUDIO}

# ---------------------------------------------------------------- CDN
#: 官方 CDN 基础地址**不在这里写明文**。
#:
#: 需求原话："所有下载链接要做加密处理…为了不让在 github 时显示明文"。
#: 所以这里只放空串，真实地址来自 ``web/data/packs.links.js`` 的 RC4+base64
#: 密文（由 ``tools/encrypt_links.py`` 生成），或用户自己的
#: ``userdata/links.json``（明文，只在他本机）。
#:
#: 这样仓库源码里搜不到那个域名；用户想换镜像也不用改代码。
CDN_BASE_DEFAULT = ""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 清单名：Manifest1Ex = 新资产，Manifest2Ex = 旧资产
MANIFEST_NAMES = ("Manifest1Ex", "Manifest2Ex")

# AES-256-CBC（KEY/IV 是 CDN 侧固定值，社区公开，非本项目机密）
AES_KEY = b"}vq4p>cLPLpB6/Uk|dISq{jLg<o$G@Sr"
AES_IV = b"DGZv_YSCe0muI6%G"

#: 兼容旧代码的模块级常量（新代码请用 :func:`cdn_base`）
CDN_BASE = CDN_BASE_DEFAULT

# ---------------------------------------------------------------- 语言
LANG_NATIVE = "ja"
LANG_FALLBACK_ORDER = ("ja",)
LANGUAGES = (LANG_NATIVE, "zh-Hans")


def cdn_base() -> str:
    """生效的 CDN 基础地址（末尾保证有 ``/``）。

    优先级：``userdata/links.json`` 的 ``cdn`` > 内置默认值。
    用户自己改域名不需要动代码，也不用重新打包。
    """
    try:
        from .. import packs                       # server.packs
        cfg = packs.links() or {}
        v = (cfg.get("cdn") or "").strip()
        if v:
            return v if v.endswith("/") else v + "/"
    except Exception:                              # noqa: BLE001
        pass
    return CDN_BASE_DEFAULT


#: 随仓库分发的静态数据（角色名 / 元数据）。首次运行复制到工作区，
#: 因为那几个旧脚本都按 cwd 相对路径读它们。
#:
#: **资源清单（Manifest1Ex/2Ex）故意不在这里**：清单会随官方更新变化，
#: 打包进仓库只会让用户拿着过期数据。首次使用与控制台的「刷新清单」都会
#: 现拉最新的（约 5 MB，有进度条）。
SEED_DIR = os.path.join(_HERE, "data")
SEED_FILES = ("char_names.json", "char_names_wiki.json", "char_meta.json")


def ensure_dirs() -> None:
    """保证会写入的目录都存在，并把首次运行需要的静态数据播种到工作区（幂等）。"""
    for d in (FILES_STORY, FILES_IMG, FILES_AUDIO,
              DATA, os.path.join(IMG, "stand"), os.path.join(IMG, "back"),
              os.path.join(IMG, "cg"), os.path.join(IMG, "standfull"),
              os.path.join(IMG, "avatar"), os.path.join(IMG, "story"), AUDIO,
              I18N, MANIFEST_DIR, STATE_DIR, I18N_SOURCE_DIR, ARCHIVE_DIR):
        os.makedirs(d, exist_ok=True)
    # 静态数据进工作区根（旧脚本按 cwd 相对路径读）
    import shutil
    for name in SEED_FILES:
        src = os.path.join(SEED_DIR, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(WS, name)
        if not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass


def add_scripts_to_path() -> None:
    """把 scripts 目录加进 ``sys.path``，以复用 UnityFS/剧本解析实现。"""
    import sys
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
