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

# 线路不在这里维护 —— 统一用 ``mirrors.fetch()``（和「检查程序更新」同一张线路表）。
# 那张表里已按"时效优先"排好：raw / github.com 直连 → gh 反代 → jsDelivr 兜底
# （jsDelivr 对 @main 约缓存 12 小时，只能放最后）。

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


def fetch_remote_charmeta(timeout: int = 8) -> tuple[dict | None, str]:
    """拉每日 CI 产出的 charmeta 成品 → ``(data, route)``。

    **线路不再在这里维护**：统一交给 :func:`server.mirrors.fetch`（与
    「检查程序更新」共用同一张线路表与模板），这里只给出仓库内路径。
    全部线路失败返回 ``(None, 最后一条错误描述)``。
    """
    from .. import mirrors                                    # 避免层间循环导入
    url = ("https://raw.githubusercontent.com/%s/%s/%s"
           % (_REPO, _BRANCH, _PROD_PATH))
    # jsDelivr 不能靠"前缀代理"，得自己拼 gh/{repo}@{br}/{path} 形式；
    # 它有约 12 小时缓存（时效差），所以作为**最后兜底**追加，不进公共线路表。
    extra = [{"name": "jsDelivr",
              "tpl": "https://cdn.jsdelivr.net/gh/%s@%s/%s"
                     % (_REPO, _BRANCH, _PROD_PATH)}]
    try:
        txt, route = mirrors.fetch(url, want="text", timeout=timeout,
                                   budget=timeout * 5, extra=extra)
        body = txt[txt.index("{"): txt.rindex("}") + 1]
        data = json.loads(body)
        if not data:
            return None, f"{route}: 空数据"
        return data, route
    except Exception as e:                                     # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


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
        # サポーター 页**不设稀有度**：这个游戏里「サポーター」是职业/类型
        # （所属列），不是 SSR/SR/R/NPC 那样的稀有度。以前硬塞进 r，
        # 结果 charmeta 里出现 39 个 r="サポーター" 的脏值。
        rows += parse_charlist(fetch_html(SUPPORTER_URL, proxy),
                               forced_rarity="")
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
    valid_r = {"SSR", "SR", "R", "NPC"}
    out = {}
    for name, m in scraped.items():
        merged = dict(existing.get(name) or {})
        for k in ("r", "t", "d", "cat", "special_attr", "attack_attr", "img"):
            if m.get(k):
                merged[k] = m[k]
        # 只接受合法稀有度：wiki 的「サポーター」分节是**类型**不是稀有度，
        # 旧本地数据里也可能残留这种脏值 —— 统一清空，别让它漏进 charmeta。
        _wr = m.get("r") or ""
        merged["r"] = _wr if _wr in valid_r else ""
        if m.get("d"):
            # wiki 给出真日期 → 之前用资源包时间填的推定值作废
            merged.pop("dEst", None)
        out[name] = merged
    for name, m in existing.items():
        if name not in out:
            out[name] = m
    # 兜底清洗：wiki 这次没收录的角色会原样保留旧值，旧值里可能有
    # 「サポーター」这类非法稀有度 —— 统一清空，别让脏值继续传下去。
    for m in out.values():
        if m.get("r") not in valid_r:
            m["r"] = ""
    return out


if __name__ == "__main__":  # pragma: no cover - 手动调试用
    print(json.dumps(scrape(), ensure_ascii=False, indent=1))
