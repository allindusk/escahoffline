"""从公开 Wiki（escalationheroines.wikiru.jp）抓取角色元信息。

为什么换数据源：以前角色名/稀有度/类型来自本地 ``escah`` WIKI 镜像
（``extract_meta.py`` 直接读 ``escah/data/parsed/characters``），那是开发者本机数据，
不该进发布包、也不该被运行时依赖。官方游戏 CDN 的资源包里**没有**这些文本
（名字是 logo 图、稀有度画在状态界面图上），只有这个公开 Wiki 页面有干净的
名前 / 区分(类型) / レアリティ(稀有度) 等字段，且是纯日文。

两份数据：
* 角色一览页（列表）：名前 + 区分 + 所属 + 属性 + 数值，一次请求拿全 439 个。
* 每个角色的子页面：レアリティ(稀有度) + 身長/誕生日/CV，需要逐页抓。

注意：这个 Wiki 在日本，国内访问通常要开代理。所有抓取都走
``resolve_proxy()``（环境变量 / config.WIKI_PROXY），连不上时抛
``WikiUnreachable``（带 ``proxy_hint=True``），上层据此提示用户开代理。
"""

import io
import json
import os
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config

LIST_URL = "https://escalationheroines.wikiru.jp/?%E3%82%AD%E3%83%A3%E3%83%A9%E3%82%AF%E3%82%BF%E3%83%BC%E4%B8%80%E8%A6%A7"
BASE = "https://escalationheroines.wikiru.jp/"
MEDIA_BASE = "https://escalationheroines.wikiru.jp"

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


def parse_charlist(html: str) -> list[dict]:
    """解析角色一览页，返回角色列表（含 name/type/affiliation/属性/数值/img/page）。"""
    tables = re.findall(r"<table.*?</table>", html, re.S)
    records: list[dict] = []
    seen_pages = set()
    for t in tables:
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S)
        if len(trs) < 2:
            continue
        # 表头 → 列下标
        ths = [_cell_text(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", trs[0], re.S)]
        col = {}
        for i, h in enumerate(ths):
            f = _COLMAP.get(h)
            if f:
                col[f] = i
        if "name" not in col:
            continue  # 不是角色表（如广告表）
        for tr in trs[1:]:
            tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
            if len(tds) <= col["name"]:
                continue
            name_cell = tds[col["name"]]
            am = re.search(r'href="([^"]+)"', name_cell)
            if not am:
                continue
            href = urllib.parse.urljoin(BASE, am.group(1))
            if href in seen_pages:
                continue
            seen_pages.add(href)
            row = {"name": _cell_text(name_cell), "page": href,
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
            if col.get("name") and tds:
                img = re.search(r'<img[^>]*src="([^"]+)"', tds[0])
                url = _abs(img.group(1)) if img else ""
                row["img"] = url if url.startswith("http") else ""
            records.append(row)
    return records


def parse_detail(html: str) -> dict:
    """解析单个角色页，取 レアリティ(稀有度)。

    一个角色可能列多种形态（如 ``SSR<br>SR（サポーター）`` 或带
    ``【正月ver】`` 备注），取**文档顺序第一个**稀有度码作为主稀有度。
    """
    out: dict = {}
    m = re.search(r"レアリティ\s*</th>\s*<td[^>]*>(.*?)</td>", html, re.S)
    if m:
        txt = re.sub(r"<[^>]+>", "", m.group(1))      # 去标签，得到 "SSRSR（サポーター）"
        mm = re.search(r"(SSR|SR|R|NPC)", txt)
        if mm:
            out["rarity"] = mm.group(1)
    return out


def scrape(proxy: str | None = None, deep: bool = False,
          detail_limit: int = 0, workers: int = 8) -> dict:
    """抓全量角色元信息，返回 ``{日文名: {r,t,affiliation,special_attr,attack_attr,img}}``。

    * ``deep=False``：只抓列表页（名前 + 区分/类型 + 所属 + 属性 + 数值 + 头像链接），一次请求。
    * ``deep=True``：再逐页抓 レアリティ(稀有度)，写进 ``r``。
    * ``detail_limit>0``：深度抓取只补前 N 个缺稀有度的角色（运行时用，避免 439 次请求）。
    """
    html = fetch_html(LIST_URL, proxy)
    rows = parse_charlist(html)
    meta: dict = {}
    for r in rows:
        meta[r["name"]] = {
            "t": r.get("t", ""),
            "cat": r.get("cat", ""),
            "special_attr": r.get("special_attr", ""),
            "attack_attr": r.get("attack_attr", ""),
            "img": r.get("img", ""),
            "r": "",
        }
    if not deep:
        return meta

    todo = [r for r in rows if r.get("page") and not meta[r["name"]]["r"]]
    if detail_limit:
        todo = todo[:detail_limit]
    lock = threading.Lock()

    def _one(r):
        try:
            d = parse_detail(fetch_html(r["page"], proxy))
            if d.get("rarity"):
                with lock:
                    meta[r["name"]]["r"] = d["rarity"]
        except WikiError:
            pass  # 单页失败不影响整体

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(as_completed(ex.submit(_one, r) for r in todo))
    return meta


def merge_meta(existing: dict, scraped: dict) -> dict:
    """把抓取结果合并进现有 char_meta（保留 zh/icon 等本地字段）。

    注意：旧 escah 数据曾把「サポーター」等误当成稀有度写入 ``r``；这里把不在
    合法集合里的旧 ``r`` 清空（wiki 给了就覆盖，没给则置空），避免脏数据残留。
    """
    valid_r = {"SSR", "SR", "R", "NPC"}
    out = {}
    for name, m in scraped.items():
        merged = dict(existing.get(name) or {})
        for k in ("r", "t", "cat", "special_attr", "attack_attr", "img"):
            if m.get(k):
                merged[k] = m[k]
        if merged.get("r") not in valid_r:
            merged["r"] = m.get("r") or ""    # wiki 没给且旧的非法 → 清空
        out[name] = merged
    for name, m in existing.items():
        if name not in out:
            out[name] = m
    return out


if __name__ == "__main__":  # pragma: no cover - 手动调试用
    import sys
    res = scrape(deep="--deep" in sys.argv)
    print(json.dumps(res, ensure_ascii=False, indent=1))
