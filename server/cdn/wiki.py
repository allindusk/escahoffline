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

# 每日 CI 抓取的产物（char_meta.json 提交在仓库 main 分支）。客户端优先从这里
# 拉取：国内普通网络即可访问 GitHub raw，不需要代理；失败再退回直连 Wiki。
GITHUB_RAW = ("https://raw.githubusercontent.com/allindusk/escahoffline/"
              "main/server/cdn/data/char_meta.json")

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


def fetch_json(url: str, timeout: int = 30) -> dict:
    """抓一个 JSON（走系统默认网络，**不用** Wiki 代理）。

    用于从 GitHub raw 取每日 CI 抓好的 ``char_meta.json``。
    """
    req = urllib.request.Request(url, headers={"User-Agent": config.UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


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
        for k in ("r", "t", "cat", "special_attr", "attack_attr", "img"):
            if m.get(k):
                merged[k] = m[k]
        if merged.get("r") not in valid_r:
            merged["r"] = m.get("r") or ""           # wiki 没给且旧的非法 → 清空
        out[name] = merged
    for name, m in existing.items():
        if name not in out:
            out[name] = m
    return out


if __name__ == "__main__":  # pragma: no cover - 手动调试用
    print(json.dumps(scrape(), ensure_ascii=False, indent=1))
