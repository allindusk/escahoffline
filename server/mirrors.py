"""下载线路：内置多条镜像，**先测速再选最快的那条**。

为什么需要这个
--------------
两件事会受"线路"影响，而它们的官方源在国内都可能很慢：

1. **装依赖**（pip）：PyPI 官方直连经常只有几十 KB/s，而清华/阿里/腾讯的镜像
   能到几 MB/s。写死一条就等于赌用户的网络环境。
2. **程序更新**（GitHub Release）：github.com 与 objects.githubusercontent.com
   在国内常常不可达。项目本身就在用 ghproxy 这类反代（见 tools 里的旧脚本）。

所以策略是：**每个来源内置若干路线 → 实测 → 按实测结果排序 → 用最快的那条**。
测速结果缓存 30 分钟（`userdata/mirrors.json`），避免每次操作都先等一轮探测。

指标怎么算
----------
- pip 镜像：取一个小的索引页，测 **TTFB + 单位时间字节数**，综合打分
  （TTFB 决定"能不能连"，吞吐决定"下得动"，两者都要）。
- GitHub 反代：主要判断**可达性**（很多反代能连但很慢/被限流），所以看 TTFB，
  并给可达的直连更高权重（同等速度下优先直连，少一层中间人）。

注意：这些都是**公开的公共服务**，随时可能限流或改规则。某条路线测不通就自动跳过，
全部不通时退回官方源 —— 用户至少还能下，只是可能慢。
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import socket
import ssl
import time
import urllib.request

from . import paths

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
#: 这份名单是**实测**出来的（2026-10-29，46 条候选）：
#: 只有 edgeone 与 yylx 两条超过 1 MB/s，其余反代要么 <0.5 MB/s，要么
#: 已经挂了（SSL 证书错、超时、DNS 解析失败）。所以：
#:   · 前两条是主力；
#:   · ghfast / boki 作为"全挂时的保命渠道"（慢但能用，0.1–0.6 MB/s）；
#:   · 直连放最前——能连的话最省事（不经过第三方），连不上会自动跳过。
#:
#: 注意：**GitHub 官方没有中国大陆镜像**。release 资产实际落在
#: objects.githubusercontent.com（Fastly/Cloudflare），国内经常被限速，
#: 所以除了直连，只能靠社区反代，而这些节点生命周期很短 —— 本名单需要定期重测。
GH_ROUTES = [
    {"id": "direct", "name": "GitHub 直连", "tpl": "{url}"},
    {"id": "edgeone", "name": "gh-proxy.org（EdgeOne）",
     "tpl": "https://edgeone.gh-proxy.org/{url}"},
    {"id": "yylx", "name": "git.yylx.win", "tpl": "https://git.yylx.win/{url}"},
    {"id": "boki", "name": "github.boki.moe（慢速兜底）",
     "tpl": "https://github.boki.moe/{url}"},
    {"id": "ghfast", "name": "ghfast.top（慢速兜底）",
     "tpl": "https://ghfast.top/{url}"},
]

#: 探测用：一个真实的 release 小资产（1.7 KB），能同时验证"可代理 release"
#: 与"返回的不是错误页"。**不要用 raw.githubusercontent 探测** ——
#: 反代对 raw 和 release 的支持经常不一样，测 raw 会选错线路。
GH_PROBE = ("https://github.com/cli/cli/releases/download/v2.40.0/"
            "gh_2.40.0_checksums.txt")
#: 吞吐探测用的大一点的 release 资产（约 12 MB，只读前 2 MB）
GH_PROBE_BIG = ("https://github.com/cli/cli/releases/download/v2.40.0/"
                "gh_2.40.0_windows_amd64.zip")

#: 低于这个吞吐视为"慢线路"：仍可用，但只在快线路全挂时才选
MIN_GOOD_MBPS = 1.0

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
    """分数越高越好。TTFB 与吞吐都参与，避免"能连但极慢"被选中。"""
    if not res.get("ok"):
        return -1.0
    ttfb = max(res["ttfb"], 1)
    kbps = max(res["kbps"], 1)
    if kind == "github":
        # 反代主要看可达性：TTFB 越小越好，吞吐当加分项
        return 1000.0 / ttfb + min(kbps, 2000) / 400.0
    # pip：吞吐权重更高（要下几十 MB），TTFB 用来排除"握手慢到离谱"的
    return min(kbps, 20000) / 100.0 + 300.0 / ttfb


def _probe_mbps(url: str, timeout: float = 20.0, limit: int = 2 << 20) -> float:
    """读满 ``limit`` 字节测真实吞吐（MB/s）。失败返回 0。"""
    t0 = time.time()
    got = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ttfb = time.time() - t0
            while got < limit:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
        dt = max(time.time() - t0 - ttfb, 1e-3)
        return round(got / 1048576.0 / dt, 2)
    except Exception:                                          # noqa: BLE001
        return 0.0


def _test_github(routes: list[dict], timeout: float,
                 workers: int) -> list[dict]:
    """两阶段：先小文件筛可达（快），再对前几名测真实吞吐（准）。

    只测握手会选错：实测 ghfast.top 握手 0.87s 但吞吐只有 0.06 MB/s，
    而 edgeone 握手 0.82s、吞吐 13.85 MB/s —— 差 200 倍。
    """
    def one(route):
        r = _probe_url(apply_github(route, GH_PROBE), "github", timeout)
        out = dict(route)
        out.update(r)
        out["mbps"] = 0.0
        return out

    with cf.ThreadPoolExecutor(max_workers=min(workers, len(routes))) as ex:
        res = list(ex.map(one, routes))

    # 只对握手最快的 2 条测吞吐：测速本身要花用户十几秒，3 条会拖到 30 秒
    reachable = sorted([r for r in res if r.get("ok")], key=lambda x: x["ttfb"])[:2]
    for r in reachable:
        r["mbps"] = _probe_mbps(apply_github(r, GH_PROBE_BIG),
                                timeout=max(timeout * 2, 10), limit=512 << 10)
        # 吞吐定生死，握手只做同速时的次要依据
        r["score"] = round(r["mbps"] * 100 + 200.0 / max(r["ttfb"], 1), 2)
        r["slow"] = r["mbps"] < MIN_GOOD_MBPS
    for r in res:
        if r not in reachable:
            r["score"] = (round(200.0 / max(r["ttfb"], 1), 2)
                          if r.get("ok") else -1.0)
            r["slow"] = True                     # 没测到吞吐的一律按慢处理
    res.sort(key=lambda x: -x["score"])
    return res


def test(kind: str = "pip", timeout: float = 5.0,
         workers: int = 8, rounds: int = 1) -> list[dict]:
    """并发测所有路线，返回按分数降序排列的结果（含不可用的，标 ok=False）。

    ``rounds=2`` 会重复测取最好成绩（网络抖动时更准，但慢一倍）。
    """
    routes = _ROUTES.get(kind)
    if not routes:
        raise ValueError(f"未知线路类型：{kind}")
    if kind == "github":
        return _test_github(routes, timeout, workers)

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


def ranked(kind: str = "github", refresh: bool = False,
           job=None) -> list[dict]:
    """按速度排好序的**可用**线路列表（给"下载失败自动换下一条"用）。

    快线路（≥ MIN_GOOD_MBPS）排在前面；慢线路留在后面兜底 ——
    如果只保留快线路，两条都挂了用户就彻底下不了。
    """
    res = None if refresh else cached(kind)
    if not res:
        if job is not None:
            job.log(f"正在测速 {len(_ROUTES.get(kind, []))} 条{_label(kind)}线路…")
        res = test(kind)
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


def best(kind: str = "pip", refresh: bool = False, job=None) -> dict:
    """→ 选中的路线（含 ``id/name/index|tpl/score``）。

    有新鲜缓存就用缓存；否则测速并把结果写进缓存。
    ``job`` 给了就把每条路的成绩写进进度消息。
    """
    if not refresh:
        c = cached(kind)
        if c and c[0].get("ok"):
            return c[0]
    if job is not None:
        job.log(f"正在测速 {len(_ROUTES.get(kind, []))} 条{_label(kind)}线路…")
    results = test(kind)
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
    """给前端展示的线路概况（不触发测速）。"""
    c = _load_cache().get(kind) or {}
    return {
        "kind": kind,
        "routes": [{"id": r["id"], "name": r["name"]} for r in _ROUTES.get(kind, [])],
        "tested": bool(c),
        "testedAt": c.get("at", 0),
        "results": c.get("results") or [],
        "best": (c.get("results") or [{}])[0].get("id", "") if c.get("results") else "",
    }
