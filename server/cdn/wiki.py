"""从公开 Wiki（escalationheroines.wikiru.jp）抓取角色元信息。

为什么换数据源：以前角色名/稀有度/类型来自本地 ``escah`` WIKI 镜像
（``extract_meta.py`` 直接读 ``escah/data/parsed/characters``），那是开发者本机数据，
不该进发布包、也不该被运行时依赖。官方游戏 CDN 的资源包里**没有**这些文本
（名字是 logo 图、稀有度画在状态界面图上），只有这个公开 Wiki 页面有干净的
名前 / 所属(类型) / 区分(势力) / レアリティ(稀有度)，且是纯日文。

数据全在**角色一览页**：名前 / 所属 / 区分 / 属性 / 数值，而且页面已按稀有度
**分节**（``content_1_0``=SSR、``_1``=SR、``_2``=R；其后的 NPC / コメント 分节跳过），
稀有度直接读分节标题即可 —— **不需要逐页抓 439 个子页面**。
``?サポーター`` 页**不再抓**：サポーター 是职业不是稀有度，且没有独立剧情
（用户 2026-11-01 定：游戏里只有 SSR / SR / R）。

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
#: 历史上抓过 ``?サポーター``（SUPPORTER_URL），2026-11-01 起不再请求 —— 那一页
#: 只有职业/类型信息，没有稀有度，也没有独立剧情；留着常量只为兼容旧引用。
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
#: 名字键的中间产物：{日文名: {r,d,t,cat,…}}。
#: **新角色只有这张表里有** —— 见 :func:`fetch_remote_char_meta` 的说明。
_META_PATH = "server/cdn/data/char_meta.json"

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
    "実装日": "d",          # ← 表里有这一列，以前没映射，所以 d 永远刷不到
    "レアリティ": "r",
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

#: 游戏里只有这三种角色（SSR / SR / R）。「サポーター」是职业不是稀有度、
#: 「NPC」是没有个人剧情的路人组 —— 两者都不抓（用户 2026-11-01 明确）。
VALID_RARITIES = ("SSR", "SR", "R")

# 角色一览页按稀有度分节，标题形如 <h2 id="content_1_0"><a href="./?SSR"
# title="SSR">SSRキャラ</a>...</h2>。
_SECTION_RE = re.compile(r'<h2 id="content_1_\d+"[^>]*>(.*?)</h2>', re.S)
#: **只认这三档**（用户 2026-11-01 明确）：游戏里只有 SSR / SR / R 三种角色，
#: 「サポーター」「NPC」既不是稀有度、也没有独立剧情，抓了也用不上 → 直接不认。
_RARITY_KEYS = VALID_RARITIES


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
    """拉每日 CI 产出的 charmeta 成品（id 键）→ ``(data, route)``。

    **运行时别再拿它当主数据源**：这张表是「按 id」的成品，新角色在 CI 那边没有
    名字、整条缺失（详见 :func:`fetch_remote_char_meta`）。留着只是因为：
    它只有 17KB，线路/解析都现成，排查「哪个 id 缺元数据」时方便对照。

    **线路不在这里维护**：统一交给 :func:`server.mirrors.fetch`（与
    「检查程序更新」共用同一张线路表与模板），这里只给出仓库内路径。
    全部线路失败返回 ``(None, 最后一条错误描述)``。
    """
    return _fetch_json_payload(_PROD_PATH, timeout)


def _fetch_json_payload(path: str, timeout: int) -> tuple[dict | None, str]:
    """按线路表拉仓库里的一个 JSON 文件 → ``(data, route)``；失败 ``(None, 原因)``。"""
    from .. import mirrors                                    # 避免层间循环导入
    url = "https://raw.githubusercontent.com/%s/%s/%s" % (_REPO, _BRANCH, path)
    extra = [{"name": "jsDelivr",
              "tpl": "https://cdn.jsdelivr.net/gh/%s@%s/%s"
                     % (_REPO, _BRANCH, path)}]
    try:
        txt, route = mirrors.fetch(url, want="text", timeout=timeout,
                                   budget=timeout * 5, extra=extra)
        data = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
        if not data:
            return None, f"{route}: 空数据"
        return data, route
    except Exception as e:                                     # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def fetch_remote_char_meta(timeout: int = 8) -> tuple[dict | None, str]:
    """拉每日 CI 产出的**名字键**元信息表 → ``(data, route)``。

    为什么运行时必须拉这张，而不是只用 ``charmeta.js``
    -------------------------------------------------
    ``charmeta.js`` 是「**id 键**」的成品，由 CI 用自己的 ``names.js`` 把名字换成
    id；而 CI 里没有解包剧本、**根本不知道新角色叫什么**，于是新角色在成品里整条
    缺失。2026-11-18 实测：GitHub 上 ``char_meta.json`` 已经收录 4 个当天实装的新
    角色（SSR/SR + 实装日都在），``charmeta.js`` 里却一条都没有 —— 客户端点
    「更新剧情目录」后新角色仍是「ID 400 / 实装日期未收录」，稀有度也永远刷不到。

    所以名字键的表才是运行时该拉的那份：id 由客户端按本机 ``names.js`` 现配
    （见 :func:`match_meta_by_names`）。
    """
    return _fetch_json_payload(_META_PATH, timeout)


# ---------------------------------------------------------------- 名字 ↔ 元信息
#: 重名角色在 names.js 里带「(13)」这样的后缀，比对时必须抹掉
_NAME_SUFFIX_RE = re.compile(r"\s*\(\d+\)$")


def pure_name(nm) -> str:
    """去掉重名后缀的原始名字（不做归一化）。"""
    return _NAME_SUFFIX_RE.sub("", str(nm or "")).strip()


def name_key(nm) -> str:
    """名字比对口径：NFKC（全角数字/字母转半角）+ 去掉空白、中点、括号。
    不归一的话「妹の１０２４」和「妹の1024」会被当成两个角色。"""
    import unicodedata
    s = unicodedata.normalize("NFKC", pure_name(nm))
    return re.sub(r"[\s・·（）()]", "", s)


def core_name(nm, owner: str = "") -> str:
    """名字的「核心段」：去掉所属前缀、取第一个分隔段。

    例：``魔女チィコ``（所属=魔女）→ ``チィコ``；``チィコ・リョウ`` → ``チィコ``。
    Wiki 用「所属+名」，剧本里常是「名・姓」，两种写法只能靠这个口径对齐。
    """
    s = pure_name(nm)
    if owner and s.startswith(owner):
        s = s[len(owner):]
    return name_key(re.split(r"[・·]", s)[0])


def meta_index(meta: dict) -> dict:
    """把「名字键」元信息索引化，供 :func:`meta_for_name` 反复查。

    为什么要索引：装机一次要给 389 个 id 各查一遍，逐个 normalize 是 0.5~1 秒的
    白等（每次装剧情都要付）。
    """
    by_nk: dict = {}
    by_core: dict = {}
    for k, m in (meta or {}).items():
        by_nk.setdefault(name_key(k), []).append((k, m))
        by_core.setdefault(core_name(k, str((m or {}).get("t") or "")), []).append((k, m))
    return {"by_nk": by_nk, "by_core": by_core}


def meta_for_name(nm, index: dict) -> dict | None:
    """本地名字 → 元信息：先「归一后完全同名」，再「核心段唯一命中」。"""
    if not index or not nm:
        return None
    hits = index["by_nk"].get(name_key(nm)) or []
    if len(hits) == 1:
        return hits[0][1]
    hits = index["by_core"].get(core_name(nm)) or []
    return hits[0][1] if len(hits) == 1 else None


def match_meta_by_names(remote: dict, names: dict) -> tuple[dict, dict]:
    """远程「名字键」元信息 → ``({本地名字: 元信息}, {对不上的名字: 元信息})``。

    只把**能确定对应到本机 id** 的条目交出去（``gen_charmeta.py`` 按名字查表），
    对不上的原样返回，由调用方存进表里等名字到了再挂上。

    两步匹配：
      1. 归一后完全同名 —— 老角色几乎全走这条；
      2. 核心段唯一命中（Wiki「魔女チィコ」vs 剧本「チィコ・リョウ」）：
         **命中多个就放弃**，宁可先显示 ID，也不把名字/稀有度贴到别人头上。
    """
    out: dict = {}
    left: dict = {}
    nk_names: dict = {}
    core_names: dict = {}
    for cid, nm in (names or {}).items():
        nk_names.setdefault(name_key(nm), []).append(str(cid))
        core_names.setdefault(core_name(nm), []).append(str(cid))
    used: set = set()

    def _take(cids, m):
        """一个 id 只能被一条元信息占用（同一角色在 wiki 上可能有复姓条目）。"""
        cid = cids[0]
        if cid in used:
            return False
        used.add(cid)
        out[str(names[cid])] = m
        return True

    for rname, m in (remote or {}).items():
        cids = nk_names.get(name_key(rname)) or []
        if len(cids) == 1 and _take(cids, m):
            continue
        left[rname] = m
    if not left:
        return out, {}
    rest: dict = {}
    for rname, m in left.items():
        cids = core_names.get(core_name(rname, str((m or {}).get("t") or ""))) or []
        if not (len(cids) == 1 and _take(cids, m)):
            rest[rname] = m
    return out, rest


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
        # 三张表的列数并不相同（SSR 25 列含「限定」、SR/R 24 列、NPC 只有 4 列），
        # 而且数据行常比表头多一个空单元格 —— 以**表头**为准裁剪，多余单元格丢掉，
        # 否则按表头下标取值会整体错位一格。
        if len(tds) > len(ths):
            tds = tds[:len(ths)]
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


def parse_charlist(html: str, forced_rarity: str | None = None,
                   allow_blank: bool = False) -> list[dict]:
    """解析角色一览页（或 ?サポーター 页），返回角色列表（含 ``r`` 稀有度）。

    稀有度取自页面分节标题（``content_1_0``=SSR / ``_1``=SR / ``_2``=R /
    ``_3``=NPC），**无需再逐页抓子页面**。

    ``forced_rarity`` / ``allow_blank`` 用于**没有稀有度分节标题**的页面
    （``?サポーター``）：以前传 ``forced_rarity=""`` 会因为空串是假值被整页跳过，
    等于那一页白抓；现在用 ``allow_blank=True`` 表示"这一页的行照收，但稀有度留空"。
    """
    records: list[dict] = []
    seen = set()
    # 按 <h2 id="content_1_N"> 切分；每节内的表格都属于该节的稀有度
    for sec in re.split(r'(?=<h2 id="content_1_\d+")', html):
        m = _SECTION_RE.search(sec)
        rar = _section_rarity(m.group(1)) if m else None
        rar = rar or forced_rarity
        if not rar and not allow_blank:
            continue                                 # 跳过「コメントフォーム」等分节
        # 只收 SSR / SR / R：NPC 分节（4 列的表）是路人，没有个人剧情，不要
        if rar and rar not in VALID_RARITIES:
            continue
        for tbl in re.finditer(r"<table[^>]*>(.*?)</table>", sec, re.S):
            for row in _parse_table_rows(tbl.group(1)):
                if row["page"] in seen:
                    continue
                seen.add(row["page"])
                row["r"] = rar or ""
                records.append(row)
    return records


def scrape(proxy: str | None = None) -> dict:
    """抓全量角色元信息，返回 ``{日文名: {r,t,cat,special_attr,attack_attr,img,d}}``。

    **只请求 1 个页面**：角色一览。页面自己按 SSR / SR / R 分节（NPC 分节跳过），
    稀有度直接读分节标题，不逐页抓子页面。
    ``?サポーター`` 页**不再抓**（2026-11-01 用户明确：サポーター/NPC 没有独立剧情，
    抓来没用；以前抓了还会用空稀有度覆盖主表的值）。
    """
    rows = parse_charlist(fetch_html(LIST_URL, proxy))
    # 同一角色可能同时出现在主表和 ?サポーター 页。**主表在前、只补空字段**，
    # 不能像以前那样"后写入的整条覆盖" —— サポーター 页没有稀有度，一旦它排在后面
    # 就会把主表抓到的 SSR 覆盖成空（cid 148 八爪妖蠱ハツネ 之前就是这么丢的）。
    meta: dict = {}
    keys = ("t", "cat", "special_attr", "attack_attr", "img", "r", "d")
    for r in rows:
        cur = meta.setdefault(r["name"], {k: "" for k in keys})
        for k in keys:
            if (r.get(k) or "") and not cur.get(k):
                cur[k] = r[k]
    return meta


def merge_meta(existing: dict, scraped: dict) -> dict:
    """把抓取结果合并进现有 char_meta（保留 zh/d 等本地字段）。

    **只保留有 SSR / SR / R 稀有度的条目**（用户 2026-11-01 明确）：
    サポーター / NPC 这类既不是稀有度、也没有独立剧情，留在表里只会误导
    （历史上就有 6 个角色的稀有度被贴成 NPC）。所以：
      · wiki 给了合法稀有度 → 用 wiki 的；
      · wiki 没给（本次没收录）→ 沿用旧值，但旧值也必须是三档之一；
      · 谁的稀有度最终不在三档里 → **整条丢弃**。
    """
    valid_r = set(VALID_RARITIES)
    out = {}
    for name, m in scraped.items():
        merged = dict(existing.get(name) or {})
        for k in ("t", "d", "cat", "special_attr", "attack_attr", "img"):
            if m.get(k):
                merged[k] = m[k]
        _wr = (m.get("r") or "").strip()
        merged["r"] = _wr if _wr in valid_r else (
            merged.get("r") if merged.get("r") in valid_r else "")
        if m.get("d"):
            # wiki 给出真日期 → 之前"暂用源文件日期"/本机时间的标示全部作废
            merged["d"] = m["d"]
            merged.pop("dEst", None)
            merged.pop("dSrc", None)
        elif merged.get("dEst"):
            # 旧版本留下的是**本机下载时间**（不是实装日）：清掉，宁可空着 = 未知
            # （2026-11-25 用户报的错日期就是这么来的）。dSrc 是源文件日期，保留。
            merged.pop("dEst", None)
            merged.pop("d", None)
        if merged["r"] in valid_r:
            out[name] = merged
    for name, m in existing.items():
        if name in out:
            continue
        if (m or {}).get("r") in valid_r:            # 旧值合法的才保留
            out[name] = m
    return out


if __name__ == "__main__":  # pragma: no cover - 手动调试用
    print(json.dumps(scrape(), ensure_ascii=False, indent=1))
