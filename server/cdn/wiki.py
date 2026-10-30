"""从公开 Wiki（escalationheroines.wikiru.jp）抓取角色元信息。

为什么换数据源：以前角色名/稀有度/类型来自本地 ``escah`` WIKI 镜像
（``extract_meta.py`` 直接读 ``escah/data/parsed/characters``），那是开发者本机数据，
不该进发布包、也不该被运行时依赖。官方游戏 CDN 的资源包里**没有**这些文本
（名字是 logo 图、稀有度画在状态界面图上），只有这个公开 Wiki 页面有干净的
名前 / 所属(类型) / 区分(势力) / レアリティ(稀有度)，且是纯日文。

数据全在**角色一览页**：名前 / 所属 / 区分 / 属性 / 数值，而且页面已按稀有度
**分节**（``content_1_0``=SSR、``_1``=SR、``_2``=R、``_3``=NPC），稀有度直接读
分节标题即可 —— **不需要逐页抓 439 个子页面**。
（サポーター 不在这一页，在独立的 ``?サポーター`` 页，另抓一次。）

注意：这个 Wiki 在日本，国内访问通常要开代理。所有抓取都走
``resolve_proxy()``（环境变量 / config.WIKI_PROXY），连不上时抛
``WikiUnreachable``（带 ``proxy_hint=True``），上层据此提示用户开代理。
"""

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import config

LIST_URL = "https://escalationheroines.wikiru.jp/?%E3%82%AD%E3%83%A3%E3%83%A9%E3%82%AF%E3%82%BF%E3%83%BC%E4%B8%80%E8%A6%A7"
SUPPORTER_URL = ("https://escalationheroines.wikiru.jp/"
                 "?%E3%82%B5%E3%83%9D%E3%83%BC%E3%82%BF%E3%83%BC")
BASE = "https://escalationheroines.wikiru.jp/"
MEDIA_BASE = "https://escalationheroines.wikiru.jp"

# 每日 CI 产出的**成品**：web/data/charmeta.js（约 17KB，只含 角色id→{r,d,t}）。
# 比中间产物 server/cdn/data/char_meta.json（约 63KB，按日文名 + 十来个字段）小得多，
# 客户端拉这个是 3.6 倍的速度差。
_REPO = "allindusk/escahoffline"
_BRANCH = "main"
_PROD_PATH = "web/data/charmeta.js"
_RAW = f"https://raw.githubusercontent.com/{_REPO}/{_BRANCH}/{_PROD_PATH}"

# raw.githubusercontent.com 在国内时通时不通，所以按顺序多线路回退。
# **时效优先**：raw / github.com 直连和几个 gh 反代每次都回源，CI 刚提交就能取到；
# jsDelivr 对 @main 分支约缓存 12 小时，虽然国内可达性最好，但只能当兜底放最后。
#   实测可用：raw / github-raw / gh-proxy.com / ghfast.top / edgeone
#            / jsdelivr / fastly / gcore
#   实测不可用（已剔除）：cdn.statically.io、raw.gitmirror.com、ghproxy.net
CHAR_META_ROUTES = [
    ("raw", _RAW),
    ("github-raw", "https://github.com/{repo}/raw/{br}/{path}"),
    ("gh-proxy.com", "https://gh-proxy.com/" + _RAW),
    ("ghfast.top", "https://ghfast.top/" + _RAW),
    ("edgeone", "https://edgeone.gh-proxy.org/" + _RAW),
    # —— 以下 CDN 有 12 小时级缓存，放最后，只在国内直连全挂时兜底
    ("jsdelivr", "https://cdn.jsdelivr.net/gh/{repo}@{br}/{path}"),
    ("jsdelivr-fastly", "https://fastly.jsdelivr.net/gh/{repo}@{br}/{path}"),
    ("jsdelivr-gcore", "https://gcore.jsdelivr.net/gh/{repo}@{br}/{path}"),
]
_ROUTE_VARS = {"repo": _REPO, "br": _BRANCH, "path": _PROD_PATH}

# 列表页表头（日）→ 我们的字段名
# 注意：このゲームでは「所属」が職業/タイプ（戦士/閃忍/神騎/サポーター…）で、
# 「区分」は勢力/世界（異界/天界/想破/現界/地上/久世…）。ユーザーが言う「角色类型」
# は職業なので t=所属、cat=区分 として両方持つ。
_COLMAP = {
    "名前": "name",
    "所属": "t",
    "区分": "cat",
    "特殊属性": "special_attr",
    "通常攻撃属性": "attack_attr",
    "攻撃範囲": "range",
    "限定": "limited",
    "スタミナ": "stamina",
    "攻撃力": "atk",
    "防御力": "def",
    "魔法力": "mag",
    "魔法抵抗力": "mres",
    "命中力": "acc",
}
_STAT_COLS = ("stamina", "atk", "def", "mag", "mres", "acc")

# 角色一览页按稀有度分节，标题形如 <h2 id="content_1_0"><a href="./?SSR"
# title="SSR">SSRキャラ</a>...</h2>。サポーター 是独立页，用 forced_rarity 标。
_SECTION_RE = re.compile(r'<h2 id="content_1_\d+"[^>]*>(.*?)</h2>', re.S)
_RARITY_KEYS = ("SSR", "SR", "R", "NPC", "サポーター")


class WikiError(Exception):
    """Wiki 抓取相关错误的基类。"""


class WikiUnreachable(WikiError):
    """连不上 Wiki（多半是没开代理）。

    ``proxy_hint`` 为 True 时，UI 应提示用户开启代理后重试。
    """

    def __init__(self, msg: str, proxy_hint: bool = False):
        super().__init__(msg)
        self.proxy_hint = proxy_hint


def resolve_proxy() -> str | None:
    """代理来源优先级：环境变量 ``ESC_WIKI_PROXY`` > ``HTTPS_PROXY`` /
    ``HTTP_PROXY`` > ``config.WIKI_PROXY``。返回 ``http(s)://host:port`` 或 None。
    """
    for key in ("ESC_WIKI_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    return getattr(config, "WIKI_PROXY", None) or None


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_html(url: str, proxy: str | None = None, timeout: int = 30) -> str:
    """抓取一个 Wiki 页面，返回解码后的 HTML。连不上抛 ``WikiUnreachable``。"""
    proxy = proxy or resolve_proxy()
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": config.UA})
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            raise WikiUnreachable(f"Wiki 返回 {e.code}，可能需要代理：{url}",
                                  proxy_hint=True) from e
        raise WikiUnreachable(f"Wiki 返回 {e.code}：{url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise WikiUnreachable(f"无法访问 Wiki（可能未开启代理）：{e}",
                              proxy_hint=True) from e
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def _http_get(url: str, timeout: int = 10) -> str:
    """普通 HTTP GET（走系统默认网络，不强制 Wiki 代理）。"""
    req = urllib.request.Request(url, headers={"User-Agent": config.UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_remote_charmeta(timeout: int = 8) -> tuple[dict | None, str]:
    """按 ``CHAR_META_ROUTES`` 顺序拉每日 CI 产出的 charmeta 成品。

    返回 ``(data, route)``：``data`` 形如 ``{角色id: {r,d,t}}``；
    全部线路失败返回 ``(None, 最后一条错误描述)``。
    """
    last = ""
    for name, tpl in CHAR_META_ROUTES:
        url = tpl.format(**_ROUTE_VARS)
        try:
            txt = _http_get(url, timeout=timeout)
            body = txt[txt.index("{"): txt.rindex("}") + 1]
            data = json.loads(body)
            if data:
                return data, name
            last = f"{name}: 空数据"
        except Exception as e:                                # noqa: BLE001
            last = f"{name}: {type(e).__name__}"
    return None, last


def _cell_text(html_frag: str) -> str:
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", html_frag)).strip()


def _abs(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return MEDIA_BASE + url
    return url


def _section_rarity(head_html: str) -> str | None:
    """从分节 <h2> 判断稀有度：<a href="./?SSR" title="SSR">SSRキャラ</a>。"""
    for key in _RARITY_KEYS:
        if (f'title="{key}"' in head_html
                or f">{key}キャラ<" in head_html
                or f">{key}<" in head_html):
            return key
    return None


def _parse_table_rows(tbl_html: str) -> list[dict]:
    """解析一张角色表，返回行（不含稀有度，由所在分节决定）。"""
    trs = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl_html, re.S)
    if len(trs) < 2:
        return []
    # 表头 → 列下标
    ths = [_cell_text(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", trs[0], re.S)]
    col = {}
    for i, h in enumerate(ths):
        f = _COLMAP.get(h)
        if f:
            col[f] = i
    if "name" not in col:
        return []                                    # 不是角色表（如广告表）
    rows = []
    for tr in trs[1:]:
        tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
        if len(tds) <= col["name"]:
            continue
        name_cell = tds[col["name"]]
        am = re.search(r'href="([^"]+)"', name_cell)
        if not am:
            continue
        row = {"name": _cell_text(name_cell),
               "page": urllib.parse.urljoin(BASE, am.group(1)),
               "t": "", "cat": "", "special_attr": "", "attack_attr": "",
               "range": "", "limited": False, "img": ""}
        for f, idx in col.items():
            if f == "name":
                continue
            val = tds[idx] if idx < len(tds) else ""
            if f in _STAT_COLS:
                m = re.search(r"\d+", _cell_text(val))
                row[f] = int(m.group()) if m else None
            elif f == "limited":
                row[f] = "限定" in _cell_text(val)
            else:
                row[f] = _cell_text(val)
        # 头像：列表页是懒加载占位图（data:image/gif...），没有真实地址，
        # 只在拿到真 http(s) 链接时才记（多数情况为空，CG 页会回退首字色块）
        img = re.search(r'<img[^>]*src="([^"]+)"', tds[0])
        url = _abs(img.group(1)) if img else ""
        row["img"] = url if url.startswith("http") else ""
        rows.append(row)
    return rows


def parse_charlist(html: str, forced_rarity: str | None = None) -> list[dict]:
    """解析角色一览页（或 ?サポーター 页），返回角色列表（含 ``r`` 稀有度）。

    稀有度取自页面分节标题（``content_1_0``=SSR / ``_1``=SR / ``_2``=R /
    ``_3``=NPC），**无需再逐页抓子页面**。``forced_rarity`` 用于没有稀有度
    分节标题的页面（?サポーター）。
    """
    records: list[dict] = []
    seen = set()
    # 按 <h2 id="content_1_N"> 切分；每节内的表格都属于该节的稀有度
    for sec in re.split(r'(?=<h2 id="content_1_\d+")', html):
        m = _SECTION_RE.search(sec)
        rar = _section_rarity(m.group(1)) if m else None
        rar = rar or forced_rarity
        if not rar:
            continue                                 # 跳过「コメントフォーム」等分节
        for tbl in re.finditer(r"<table[^>]*>(.*?)</table>", sec, re.S):
            for row in _parse_table_rows(tbl.group(1)):
                if row["page"] in seen:
                    continue
                seen.add(row["page"])
                row["r"] = rar
                records.append(row)
    return records


def scrape(proxy: str | None = None) -> dict:
    """抓全量角色元信息，返回 ``{日文名: {r,t,cat,special_attr,attack_attr,img}}``。

    只抓两个页面：角色一览（按分节拿稀有度）+ ``?サポーター``。
    **不再逐页抓 439 个子页面** —— 稀有度就在分节标题里。
    """
    rows = parse_charlist(fetch_html(LIST_URL, proxy))
    try:
        rows += parse_charlist(fetch_html(SUPPORTER_URL, proxy),
                               forced_rarity="サポーター")
    except WikiError:
        pass                                         # サポーター 页失败不影响主表
    meta: dict = {}
    for r in rows:
        meta[r["name"]] = {
            "t": r.get("t", ""),
            "cat": r.get("cat", ""),
            "special_attr": r.get("special_attr", ""),
            "attack_attr": r.get("attack_attr", ""),
            "img": r.get("img", ""),
            "r": r.get("r", ""),
        }
    return meta


def merge_meta(existing: dict, scraped: dict) -> dict:
    """把抓取结果合并进现有 char_meta（保留 zh/d 等本地字段）。

    注意：旧 escah 数据曾把「サポーター」等误当成稀有度写入 ``r``；这里把不在
    合法集合里的旧 ``r`` 清空（wiki 给了就覆盖，没给则置空），避免脏数据残留。
    """
    valid_r = {"SSR", "SR", "R", "NPC", "サポーター"}
    out = {}
    for name, m in scraped.items():
        merged = dict(existing.get(name) or {})
        for k in ("r", "t", "d", "cat", "special_attr", "attack_attr", "img"):
            if m.get(k):
                merged[k] = m[k]
        if merged.get("r") not in valid_r:
            merged["r"] = m.get("r") or ""           # wiki 没给且旧的非法 → 清空
        if m.get("d"):
            # wiki 给出真日期 → 之前用资源包时间填的推定值作废
            merged.pop("dEst", None)
        out[name] = merged
    for name, m in existing.items():
        if name not in out:
            out[name] = m
    return out


if __name__ == "__main__":  # pragma: no cover - 手动调试用
    print(json.dumps(scrape(), ensure_ascii=False, indent=1))
