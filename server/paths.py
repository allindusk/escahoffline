"""路径、配置与本地状态文件。

目录约定（为什么不把程序和数据混在一起）
----------------------------------------
::

    <便携根目录>/
      web/            站点根（浏览器看到的目录，media/data 是用户下载的资源）
      server/         本程序（更新时整体替换，不影响资源）
      tools/          维护脚本 / 可选超分引擎
      userdata/       用户数据：配置、已装清单、下载缓存、备份、导出、日志
        config.json
        state.json
        downloads/    下载中的 .part 与完成后的 .zip
        backup/       超分前的原图备份
        export/       导出的视频 / CG 压缩包
        logs/
      VERSION         当前版本号（更新比对用）

更新只覆盖 ``server/`` 与 ``web/`` 里的**程序文件**（html/js/css），
``web/media``、``web/data``、``userdata`` 一律不动 —— 这是"便携应用不丢数据"
的关键。
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
SERVER = os.path.join(ROOT, "server")
TOOLS = os.path.join(ROOT, "tools")
#: 译名对照表目录（随包分发、不随更新覆盖）。导出译文时把**绝对路径**写进提示词，
#: 让 AI 先读这里再翻译，保证同一角色/术语全篇一致。
GLOSSARY = os.path.join(ROOT, "glossary")
USERDATA = os.path.join(ROOT, "userdata")
DOWNLOADS = os.path.join(USERDATA, "downloads")
BACKUP = os.path.join(USERDATA, "backup")
EXPORT = os.path.join(USERDATA, "export")
LOGS = os.path.join(USERDATA, "logs")

DATA = os.path.join(WEB, "data")
MEDIA = os.path.join(WEB, "media")
IMG = os.path.join(MEDIA, "img")
IMG_X2 = os.path.join(MEDIA, "img_x2")
AUDIO = os.path.join(MEDIA, "audio")

CFG_FILE = os.path.join(USERDATA, "config.json")
STATE_FILE = os.path.join(USERDATA, "state.json")
LINKS_FILE = os.path.join(USERDATA, "links.json")
#: 收藏夹 / 播放进度的本地存档（浏览器 localStorage 的备份，见 server/marks.py）
MARKS_FILE = os.path.join(USERDATA, "marks.json")
#: 界面偏好（播放器设置 / 筛选 / 语言）的本地副本，见 server/prefs.py
PREFS_FILE = os.path.join(USERDATA, "prefs.json")

#: 默认配置。新增键一律给默认值，老 config.json 缺键也不会炸。
DEFAULTS = {
    "threads": 4,              # 每个资源包的下载线程数
    "concurrency": 2,          # 同时下载几个包
    "upscale": False,          # 解包后是否自动超分
    "upscaleEngine": "cugan",    # 只有 Real-CUGAN（realcugan-ncnn-vulkan）
    "upscaleScale": 2,
    "convertWebp": True,       # 解包时把 png/jpg 转 webp
    "webpQuality": 88,
    "keepArchive": False,      # 安装后保留 zip
    "deleteOriginalsAfterUpscale": False,
    "mirror": "",              # 自定义下载源（覆盖内置加密链接）
    "autoCheckUpdate": True,
    "telemetry": False,        # 永远不联网上报：仅作声明占位
    "lastUnit": "",            # 上次播放的剧情，欢迎页用
    "onboarded": False,        # 是否看过新手引导
    "port": 8000,
}


def ensure_dirs() -> None:
    for d in (USERDATA, DOWNLOADS, BACKUP, EXPORT, LOGS):
        os.makedirs(d, exist_ok=True)


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CFG_FILE, {}) or {})
    return cfg


def save_config(cfg: dict) -> dict:
    """只保留已知键，避免用户乱写把配置带偏。"""
    out = {k: cfg[k] for k in DEFAULTS if k in cfg}
    for k in DEFAULTS:
        out.setdefault(k, DEFAULTS[k])
    save_json(CFG_FILE, out)
    return out


def load_state() -> dict:
    st = load_json(STATE_FILE, {}) or {}
    st.setdefault("schema", 1)
    st.setdefault("depots", {})        # depot_id -> {installed, files, bytes, at}
    st.setdefault("files", 0)
    st.setdefault("bytes", 0)
    st.setdefault("upscaled", {})      # kind/key -> path（超分覆盖层）
    st.setdefault("version", "")
    return st


def save_state(st: dict) -> None:
    save_json(STATE_FILE, st)


def app_version() -> str:
    for name in ("VERSION", os.path.join("web", "VERSION")):
        p = os.path.join(ROOT, name)
        try:
            with open(p, encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
    from . import __version__
    return __version__


def note_run_version() -> dict | None:
    """记录本次运行的版本；上次记录的版本与本次不同 → 返回 ``{"from": 旧, "to": 新}``。

    用途：判定「这次是**从旧版本升级上来**」，好让页面弹一次"旧版立绘可能有问题"的
    提示。新装用户没有旧记录，返回 None（不该打扰）。
    存在 ``state.json`` 的 ``lastRunVersion``：它不像 config 那样被白名单过滤掉。
    """
    cur = (app_version() or "").strip()
    if not cur:
        return None
    st = load_state()
    prev = (st.get("lastRunVersion") or "").strip()
    if prev == cur:
        return None
    st["lastRunVersion"] = cur
    try:
        save_state(st)
    except OSError:
        pass
    return {"from": prev, "to": cur} if prev else None


#: 本机立绘成品是用哪一版合成逻辑做出来的（compose_stand_all.py 跑完就写这个）
STAND_COMPOSE_VER_FILE = os.path.join(USERDATA, "_archive", "stand_compose_ver.txt")
#: 「升级后要不要体检立绘」的一次性标记，见 server/__main__.py
STAND_PROMPT_FILE = os.path.join(USERDATA, "_archive", "stand_repair_prompt.json")


def _gen_ver() -> int | None:
    """当前程序里 compose_stand_all.py 的 ``GEN_VER``。

    从脚本里读而不是再写一个常量：两处手改必然有一天不同步，读错的后果是
    「明明修过了还每次升级都问一遍」。
    """
    p = os.path.join(SERVER, "cdn", "scripts", "compose_stand_all.py")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("GEN_VER"):
                    return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return None


def stand_compose_ver() -> int | None:
    """本机立绘成品的合成逻辑版本；没装过立绘／旧版没记 → None。"""
    try:
        with open(STAND_COMPOSE_VER_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def stand_repair_needed() -> bool:
    """已经合成好的立绘是不是旧合成逻辑做的 —— 决定升级后要不要提示体检。

    为什么不用「程序版本 < 1.1.6」判：立绘是本机合成的产物，跟程序版本不是一回事。
    合成逻辑 1.1.6 才修好，但 1.1.6 用户完全可能当时点「以后再说」而仍是旧图；
    反过来「1.1.6 → 1.1.7」这种本身没改合成的升级不该再打扰（2026-11-18 反馈）。
    唯一准确的判据是成品自己的合成逻辑版本：与当前 ``GEN_VER`` 一致 = 不用查。

    另外：用户点过「以后再说」就**永久不再问**（``state.json`` 的 ``standPromptOff``）——
    他要修的时候资源管理页里那个按钮一直在，不需要靠弹窗提醒。
    """
    if load_state().get("standPromptOff"):
        return False
    d = os.path.join(IMG, "stand")
    try:
        with os.scandir(d) as it:
            if not any(e.name.endswith(".webp") for e in it):
                return False            # 一张成品都没有：还没装过立绘，没事可修
    except OSError:
        return False
    gen = _gen_ver()
    if gen is None:
        return True                     # 读不到当前逻辑版本：宁可问一句（点一下无害）
    return stand_compose_ver() != gen


def rel(path: str) -> str:
    """绝对路径 → 站点相对路径（正斜杠），给前端用。"""
    p = os.path.abspath(path)
    try:
        return os.path.relpath(p, WEB).replace("\\", "/")
    except ValueError:
        return p.replace("\\", "/")


def web_path(relpath: str) -> str:
    return os.path.join(WEB, relpath.replace("/", os.sep))


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def disk_free() -> int:
    try:
        return __import__("shutil").disk_usage(ROOT).free
    except OSError:
        return 0


def python_ok() -> tuple[bool, str]:
    return sys.version_info >= (3, 8), sys.version.split()[0]


# ---------------------------------------------------------------- 路径健康检查
def path_warning() -> str:
    """项目路径含中文/非 ASCII 时返回提示，否则返回空串。

    为什么不直接报错：Python 3 处理 Unicode 路径没问题，日常使用确实能跑。
    但外部组件（headless 浏览器截图、PyAV 编码、某些杀软）对非 ASCII 路径
    支持不稳，容易出"莫名其妙的失败"。所以是**强烈建议**而不是硬性禁止。
    """
    try:
        if all(ord(c) < 128 for c in ROOT):
            return ""
    except Exception:                                          # noqa: BLE001
        return ""
    return ("项目路径里有中文或特殊字符：" + ROOT + "\n"
            "建议整体挪到纯英文路径（例如 D:\\ESCAH）。"
            "视频导出、图片编码这类外部组件对中文路径支持不稳。")
