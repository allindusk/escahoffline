"""下载线路：内置多条镜像。

两类来源，两套策略（2026-11-01 用户定版）
----------------------------------------
1. **pip 装依赖**：**测速选最快**。PyPI 官方直连经常只有几十 KB/s，而清华/阿里/
   腾讯的镜像能到几 MB/s，写死一条就等于赌用户的网络环境。测速结果缓存 30 分钟
   （`userdata/mirrors.json`）。
2. **GitHub（release / API / raw）**：**不测速，按固定顺序用**。
   依次尝试 gh-proxy 的四个节点，**只要这条线路有正常响应就用它，不管快慢**；
   四条反代全都没反应时，才落到表尾的 GitHub 官方直连。

   为什么 GitHub 不测速：反代背后是共享 CDN，速度一天抖好几次，测速本身要
   先花几 MB 流量和十几秒，选出来的"最快"十分钟后就不准了；而"能不能连上"
   才是真正的分水岭（官方直连在国内经常直接握手失败）。用户明确要求：
   有响应就用，不比较速度。

注意：这些都是**公开的公共服务**，随时可能限流或改规则。某条线路没响应就自动
跳过，继续下一条；全部不通时退回官方直连 —— 用户至少还能下，只是可能慢。
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import socket
import ssl
import time
import urllib.request

from . import net, paths

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CACHE = os.path.join(paths.USERDATA, "mirrors.json")
CACHE_TTL = 30 * 60

# ---------------------------------------------------------------- 线路表
#: pip 索引源。``probe`` 是一个体积可控的索引页，用来测 TTFB 与吞吐。
PIP_ROUTES = [
    {"id": "tuna", "name": "清华 TUNA", "index": "https://pypi.tuna.tsinghua.edu.cn/simple",
     "probe": "https://pypi.tuna.tsinghua.edu.cn/simple/six/"},
    {"id": "aliyun", "name": "阿里云", "index": "https://mirrors.aliyun.com/pypi/simple",
     "probe": "https://mirrors.aliyun.com/pypi/simple/six/"},
    {"id": "ustc", "name": "中科大 USTC", "index": "https://pypi.mirrors.ustc.edu.cn/simple",
     "probe": "https://pypi.mirrors.ustc.edu.cn/simple/six/"},
    {"id": "tencent", "name": "腾讯云", "index": "https://mirrors.cloud.tencent.com/pypi/simple",
     "probe": "https://mirrors.cloud.tencent.com/pypi/simple/six/"},
    {"id": "huawei", "name": "华为云", "index": "https://repo.huaweicloud.com/repository/pypi/simple",
     "probe": "https://repo.huaweicloud.com/repository/pypi/simple/six/"},
    {"id": "pypi", "name": "PyPI 官方", "index": "https://pypi.org/simple",
     "probe": "https://pypi.org/simple/six/"},
]

#: GitHub 下载反代。``{url}`` 会被替换成原始 GitHub URL。
#:
#: **顺序即优先级，不测速**（2026-11-01 用户定版）：
#:
#:   1. gh-proxy 主站（Cloudflare v4，全球高速分发）
#:   2. gh-proxy 优选 v4（仅 IPv4 智能解析）
#:   3. gh-proxy 优选 v4/v6（IPv6/IPv4 智能解析）
#:   4. gh-proxy Fastly（v4，Fastly CDN 节点）
#:   5. GitHub 官方直连 —— **只在上面四条全都没反应时**才用
#:
#: 判定标准是"**有没有正常响应**"，不是"快不快"：反代背后是共享 CDN，
#: 速度一天抖好几次，测速要白花几 MB 流量和十几秒，选出来十分钟后就不准了。
#: 因此**不要再往这里加测速/打分逻辑**，也别按速度重排这张表。
#:
#: 用法：整条 URL 粘在反代域名后面（模板里 ``{url}`` 就是原始 GitHub URL），
#: 例如 ``https://gh-proxy.org/https://github.com/<repo>/releases/download/...``。
GH_ROUTES = [
    {"id": "ghproxy", "name": "gh-proxy 主站（Cloudflare v4）",
     "tpl": "https://gh-proxy.org/{url}"},
    {"id": "ghproxy_v4", "name": "gh-proxy 优选 v4",
     "tpl": "https://v4.gh-proxy.org/{url}"},
    {"id": "ghproxy_v6", "name": "gh-proxy 优选 v4/v6",
     "tpl": "https://v6.gh-proxy.org/{url}"},
    {"id": "ghproxy_cdn", "name": "gh-proxy Fastly（v4）",
     "tpl": "https://cdn.gh-proxy.org/{url}"},
    {"id": "direct", "name": "GitHub 官方直连（反代全无响应时兜底）",
     "tpl": "{url}"},
]

#: 探测用：一个真实的 release 小资产（1.7 KB），能同时验证"可代理 release"
#: 与"返回的不是错误页"。**不要用 raw.githubusercontent 探测** ——
#: 反代对 raw 和 release 的支持经常不一样，测 raw 会选错线路。
GH_PROBE = ("https://github.com/cli/cli/releases/download/v2.40.0/"
            "gh_2.40.0_checksums.txt")
_ROUTES = {"pip": PIP_ROUTES, "github": GH_ROUTES}


# ---------------------------------------------------------------- 单点测速
def _probe_url(url: str, kind: str, timeout: float = 5.0,
               nbytes: int = 65536) -> dict:
    """测一条路的 TTFB 与吞吐。失败返回 ok=False（不抛异常）。"""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Range": f"bytes=0-{max(1024, nbytes) - 1}",   # 只取一小段，别真下整页
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ttfb = time.time() - t0
            body = r.read(nbytes)
        dt = max(time.time() - t0 - ttfb, 1e-3)
        kbps = (len(body) / 1024.0) / dt
        return {"ok": True, "ttfb": round(ttfb * 1000), "kbps": round(kbps)}
    except Exception as e:                                     # noqa: BLE001
        # HTTPS 证书链问题/超时/被墙都归到这里，只留一句人话
        return {"ok": False, "ttfb": 0, "kbps": 0,
                "error": type(e).__name__ + ": " + str(e)[:80]}


def _score(kind: str, res: dict) -> float:
    """pip 镜像打分：越高越好。吞吐权重更高（要下几十 MB），
    TTFB 用来排除"握手慢到离谱"的。

    GitHub 不用它 —— 那边的规则是"按顺序、有响应就用"（见 ``GH_ROUTES``）。
    """
    if not res.get("ok"):
        return -1.0
    ttfb = max(res["ttfb"], 1)
    kbps = max(res["kbps"], 1)
    return min(kbps, 20000) / 100.0 + 300.0 / ttfb


def _test_github(routes: list[dict], timeout: float, workers: int,
                 target: str | None = None, lite: bool = False) -> list[dict]:
    """**顺序探活，不测速、不排序**（2026-11-01 用户定版）。

    判定标准只有一条：这条线路对目标 URL **有没有正常响应**。有响应就是可用，
    快慢一律不管；所以结果**保持表内顺序**，第一条 ``ok=True`` 的就是会被
    选用的那条（四条反代都不可用时才轮到表尾的官方直连）。

    ``target`` 给了就拿真正要下的那个资产探（同一条线路对 release 与 raw
    的支持不一样，拿真地址探最准）；``workers/lite`` 仅为兼容旧调用保留 ——
    这里的探测是顺序的，故意不并发：优先级表本身就是答案，没必要并发打一遍。
    """
    url = target or GH_PROBE
    res = []
    for route in routes:
        r = _probe_url(apply_github(route, url), "github", timeout)
        out = dict(route)
        out.update(r)
        out["mbps"] = 0.0
        out["slow"] = False
        out["lite"] = True
        out["score"] = (round(200.0 / max(r["ttfb"], 1), 2)
                        if r.get("ok") else -1.0)
        res.append(out)
    return res


def test(kind: str = "pip", timeout: float = 5.0,
         workers: int = 8, rounds: int = 1,
         target: str | None = None, lite: bool = False) -> list[dict]:
    """探测所有路线（含不可用的，标 ok=False）。

    * ``kind="pip"``：并发测速，结果按分数降序（真的会比速度，PyPI 镜像差几十倍）。
    * ``kind="github"``：**只顺序探活、不测速**，结果保持表内优先级顺序
      （见 :data:`GH_ROUTES`）。``target`` 可指定真正要取的 URL。
    """
    routes = _ROUTES.get(kind)
    if not routes:
        raise ValueError(f"未知线路类型：{kind}")
    if kind == "github":
        return _test_github(routes, timeout, workers, target, lite)

    def one(route):
        best = None
        for _ in range(max(1, rounds)):
            r = _probe_url(route["probe"], kind, timeout)
            if best is None or (_score(kind, r) > _score(kind, best)):
                best = r
            if r.get("ok"):
                break
        out = dict(route)
        out.update(best or {"ok": False})
        out["score"] = round(_score(kind, out), 2)
        return out

    with cf.ThreadPoolExecutor(max_workers=min(workers, len(routes))) as ex:
        results = list(ex.map(one, routes))
    results.sort(key=lambda x: -x["score"])
    return results


def ordered(kind: str = "github") -> list[dict]:
    """按**固定优先级**返回线路（GitHub：照表顺序，不测速）。"""
    out = []
    for r in _ROUTES.get(kind, []):
        d = dict(r)
        d.update({"ok": True, "ttfb": 0, "kbps": 0, "mbps": 0.0,
                  "slow": False, "score": 0.0, "ordered": True})
        out.append(d)
    return out


def ranked(kind: str = "github", refresh: bool = False,
           job=None, target: str | None = None, lite: bool = False) -> list[dict]:
    """按**使用顺序**排好的线路列表（给"失败就换下一条"用）。

    * ``github``：**固定顺序、不测速**（2026-11-01 用户定版）——
      gh-proxy 四个节点依次用，最后才是官方直连。``refresh/target/lite``
      仍然接受但不再影响顺序（保留是为了不破坏既有调用方）。
    * ``pip``：仍旧测速排序，并缓存 30 分钟。
    """
    if kind == "github":
        res = ordered("github")
        if job is not None:
            job.log("GitHub 线路按固定顺序使用（不测速）："
                    + " → ".join(r["name"] for r in res))
        return res
    res = None if refresh else cached(kind)
    if not res:
        if job is not None:
            job.log(f"正在测速 {len(_ROUTES.get(kind, []))} 条{_label(kind)}线路…")
        res = test(kind, target=target, lite=lite)
        save_cache(kind, res)
        if job is not None:
            for r in res:
                if r.get("ok"):
                    extra = f" / {r['mbps']} MB/s" if r.get("mbps") else ""
                    job.log(f"  {r['name']}：{r['ttfb']} ms{extra}")
                else:
                    job.log(f"  {r['name']}：不可用（{r.get('error', '超时')}）")
    ok = [r for r in res if r.get("ok")]
    fast = [r for r in ok if not r.get("slow")]
    slow = [r for r in ok if r.get("slow")]
    return fast + slow


# ---------------------------------------------------------------- 缓存与选择
def _load_cache() -> dict:
    d = paths.load_json(CACHE, {}) or {}
    return d if isinstance(d, dict) else {}


def cached(kind: str) -> list[dict] | None:
    c = _load_cache().get(kind)
    if not c:
        return None
    if time.time() - (c.get("at") or 0) > CACHE_TTL:
        return None
    return c.get("results")


def save_cache(kind: str, results: list[dict]) -> None:
    d = _load_cache()
    d[kind] = {"at": time.time(), "results": results}
    try:
        paths.save_json(CACHE, d)
    except OSError:
        pass


def best(kind: str = "pip", refresh: bool = False, job=None,
         target: str | None = None) -> dict:
    """→ 选中的路线（含 ``id/name/index|tpl/score``）。

    * ``github``：按表顺序探活，**第一条有响应的就是选中的**；全无响应返回
      表尾的官方直连（标 ``ok=False``，让调用方自己决定报错）。
    * ``pip``：有新鲜缓存就用缓存；否则测速并把结果写进缓存。
    """
    if kind == "github":
        for r in test("github", target=target):
            if r.get("ok"):
                if job is not None:
                    job.log(f"选用：{r['name']}（线路顺序优先，不测速）")
                return r
        return dict(_ROUTES["github"][-1], ok=False, score=0,
                    error="所有线路都没有响应（含官方直连）")
    if not refresh:
        c = cached(kind)
        if c and c[0].get("ok"):
            return c[0]
    if job is not None:
        job.log(f"正在测速 {len(_ROUTES.get(kind, []))} 条{_label(kind)}线路…")
    results = test(kind, target=target)
    save_cache(kind, results)
    for r in results:
        if job is not None:
            if r.get("ok"):
                job.log(f"  {r['name']}：{r['ttfb']} ms / {r['kbps']} KB/s"
                        f"（评分 {r['score']}）")
            else:
                job.log(f"  {r['name']}：不可用（{r.get('error', '超时')}）")
    usable = [r for r in results if r.get("ok")]
    if not usable:
        # 全挂：退回官方源，至少让流程继续（由调用方决定是否报错）
        fallback = (_ROUTES[kind][-1])
        return dict(fallback, ok=False, score=0, error="所有线路都测不通")
    if job is not None:
        job.log(f"选用：{usable[0]['name']}")
    return usable[0]


def _label(kind: str) -> str:
    return {"pip": " PyPI ", "github": " GitHub "}.get(kind, " ")


def apply_github(route: dict, url: str) -> str:
    """把原始 GitHub URL 套到选中路线的模板上。"""
    return (route.get("tpl") or "{url}").replace("{url}", url)


def routes(kind: str = "github") -> list[dict]:
    """该类线路的**原始定义**（未测速、未排序）。

    所有「要从 GitHub 取东西」的功能都共用这一张表，别再各自硬编码一份。
    """
    return list(_ROUTES.get(kind, []))


def fetch(url: str, *, kind: str = "github", want: str = "text",
          timeout: float = 8.0, budget: float = 30.0,
          job=None, refresh: bool = False, extra: tuple | list = ()):
    """按线路顺序取一个 URL，返回 ``(内容, 命中的线路名)``。

    统一入口：``wiki`` 取 charmeta、``updater`` 取 release、以后任何要访问
    GitHub 的功能都用它，**只传 URL 和想要的类型**即可，不用各写一套回退。

    ``want``：``text`` / ``json`` / ``bytes``
    ``timeout``：单条线路超时；``budget``：全部线路的总预算（秒）
    ``refresh``：只对 pip 有意义（重新测速）；**GitHub 永远按表顺序走**
    （gh-proxy 四节点 → 官方直连），不测速也不用速度缓存排序。
    ``extra``：额外的兜底线路（``{"name":..,"tpl":"完整URL"}``）。用于那些
    **不能靠前缀代理**的线路（如 jsDelivr 要 ``gh/{repo}@{br}/{path}`` 形式），
    调用方自己把 URL 拼好传进来，排在所有内置线路之后。
    """
    deadline = time.monotonic() + max(3.0, float(budget))
    if kind == "github":
        base = routes(kind)                  # 固定顺序：顺序即优先级
    else:
        base = (None if refresh else cached(kind)) or routes(kind)
    ok = [r for r in base if r.get("ok")] or base
    if extra:
        ok = list(ok) + list(extra)
    seen: set[str] = set()
    last = ""
    for r in ok:
        left = deadline - time.monotonic()
        if left <= 1.0:
            last = last or "线路总预算用尽"
            break
        real = apply_github(r, url)
        if real in seen:
            continue
        seen.add(real)
        name = r.get("name") or r.get("id") or "?"
        try:
            if want == "json":
                return net.fetch_json(real, timeout=min(timeout, left)), name
            if want == "bytes":
                return net.fetch_bytes(real, timeout=min(timeout, left)), name
            return net.fetch_text(real, timeout=min(timeout, left)), name
        except Exception as e:                                 # noqa: BLE001
            last = f"{name}: {type(e).__name__}"
            if job is not None:
                job.log(f"  线路 {name} 不可用（{type(e).__name__}），换下一条")
    raise RuntimeError(last or "所有线路都不可用")


def quick_check(route: dict, url: str, timeout: float = 6.0) -> bool:
    """这条线路能不能在 ``timeout`` 秒内吐出前几百字节。

    为什么要这个：下载一个 70 MB 的包，如果线路不通，``net.download`` 会一直
    等到超时才报错（可能几分钟）。用户看到的就是"卡住不动"。
    所以下大文件之前先花最多 6 秒探一下，**不通就立刻跳过**，把时间留给下一条，
    实在都不通就直接引导用户去 GitHub 手动下载。
    """
    real = apply_github(route, url)
    try:
        req = urllib.request.Request(real, headers={
            "User-Agent": UA, "Range": "bytes=0-2047"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return len(r.read(512)) > 0
    except Exception:                                          # noqa: BLE001
        return False


def summary(kind: str = "pip") -> dict:
    """给前端展示的线路概况（不触发任何网络请求）。

    ``ordered=True`` 表示这类线路**不测速**、按表内顺序使用（GitHub）。
    """
    c = _load_cache().get(kind) or {}
    rs = _ROUTES.get(kind, [])
    return {
        "kind": kind,
        "ordered": kind == "github",
        "routes": [{"id": r["id"], "name": r["name"]} for r in rs],
        "tested": bool(c),
        "testedAt": c.get("at", 0),
        "results": c.get("results") or [],
        # GitHub 的"首选"永远是表头那条；pip 才是测速结果里的第一名
        "best": "" if kind == "github" else (
            (c.get("results") or [{}])[0].get("id", "") if c.get("results") else ""),
    }
