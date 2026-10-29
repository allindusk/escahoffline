# -*- coding: utf-8 -*-
"""临时：给用户自己仓库的 GitHub Release 资产实测下载线路。

用法：
  python tools/_gh_route_bench.py            # 全量测
  python tools/_gh_route_bench.py quick      # 只探活
输出：tools/_route_bench.json + 控制台表格
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import ssl
import sys
import threading
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

UPDATE_URL = ("https://github.com/allindusk/escahoffline/releases/download/"
              "v1.0.0/ESCAH-Offline-v1.0.0-update.zip")
PORTABLE_URL = ("https://github.com/allindusk/escahoffline/releases/download/"
                "v1.0.0/ESCAH-Offline-v1.0.0-win64-portable.zip")

# 候选线路：templates 里 {url} 换成原始 GitHub URL
ROUTES = [
    ("direct",        "GitHub 直连",              "{url}"),
    ("edgeone",       "edgeone.gh-proxy.org",     "https://edgeone.gh-proxy.org/{url}"),
    ("yylx",          "git.yylx.win",             "https://git.yylx.win/{url}"),
    ("ghfast",        "ghfast.top",               "https://ghfast.top/{url}"),
    ("boki",          "github.boki.moe",          "https://github.boki.moe/{url}"),
    ("ghproxy_com",   "gh-proxy.com",             "https://gh-proxy.com/{url}"),
    ("ghproxy_net",   "ghproxy.net",              "https://ghproxy.net/{url}"),
    ("gh_proxy_net",  "gh-proxy.net",             "https://gh-proxy.net/{url}"),
    ("filegear",      "gh.filegear.me",           "https://gh.filegear.me/{url}"),
    ("llkk",          "gh.llkk.cc",               "https://gh.llkk.cc/{url}"),
    ("gitmirror",     "hub.gitmirror.com",        "https://hub.gitmirror.com/{url}"),
    ("conp",          "gh.conp.cc",               "https://gh.conp.cc/{url}"),
    ("futils",        "ghproxy.futils.com",       "https://ghproxy.futils.com/{url}"),
    ("zzut",          "ghdl.zzut.us.to",          "https://ghdl.zzut.us.to/{url}"),
    ("xiu2",          "gh.xiu.workers.dev",       "https://gh.xiu.workers.dev/{url}"),
    ("ghproxy_cc",    "ghproxy.cc",               "https://ghproxy.cc/{url}"),
    ("ygxz",          "gh-proxy.ygxz.in",         "https://gh-proxy.ygxz.in/{url}"),
    ("fastgit",       "hub.fastgit.org",          "https://hub.fastgit.org/{url}"),
]

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def apply(tpl: str, url: str) -> str:
    return tpl.replace("{url}", url)


def _req(real: str, start: int, end: int) -> urllib.request.Request:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if end >= 0:
        h["Range"] = f"bytes={start}-{end}"
    return urllib.request.Request(real, headers=h)


def probe_head(url: str) -> dict:
    """拿文件真实大小/是否支持 Range（只用能通的线路）。"""
    for _id, _n, tpl in ROUTES:
        try:
            with urllib.request.urlopen(_req(apply(tpl, url), 0, 0),
                                        timeout=10, context=CTX) as r:
                cr = r.headers.get("Content-Range", "")
                size = int(cr.split("/")[-1]) if "/" in cr else \
                    int(r.headers.get("Content-Length") or 0)
                return {"via": _id, "size": size,
                        "range": getattr(r, "status", 0) == 206}
        except Exception:
            continue
    return {"via": "", "size": 0, "range": False}


def stream_test(real: str, nbytes: int, timeout: float) -> dict:
    """单连接读 nbytes：测 TTFB + 持续吞吐。"""
    t0 = time.time()
    got = 0
    try:
        with urllib.request.urlopen(_req(real, 0, nbytes - 1),
                                    timeout=timeout, context=CTX) as r:
            code = getattr(r, "status", 200)
            ttfb = time.time() - t0
            while got < nbytes:
                buf = r.read(65536)
                if not buf:
                    break
                got += len(buf)
        dt = max(time.time() - t0 - ttfb, 1e-3)
        return {"ok": True, "ttfb_ms": round(ttfb * 1000),
                "mbps": round(got / 1048576.0 / dt, 2), "bytes": got, "code": code}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__ + ": " + str(e)[:70],
                "ttfb_ms": 0, "mbps": 0.0, "bytes": 0}


def parallel_test(real: str, size: int, threads: int, total: int,
                  timeout: float) -> dict:
    """模拟 net.download 的多段并发（每线程各取一段 Range）。"""
    per = max(total // threads, 1)
    got = [0] * threads
    err = []
    done = threading.Event()

    def seg(i: int):
        # 每段从文件不同偏移取，避免都读同一落在缓存里的数据
        s = min(i * (size // max(threads, 1)), max(size - per, 0))
        e = min(s + per - 1, size - 1)
        try:
            with urllib.request.urlopen(_req(real, s, e),
                                        timeout=timeout, context=CTX) as r:
                while True:
                    b = r.read(65536)
                    if not b:
                        break
                    got[i] += len(b)
        except Exception as ex:
            err.append(type(ex).__name__ + ": " + str(ex)[:60])
        finally:
            done.set() if False else None

    t0 = time.time()
    ths = [threading.Thread(target=seg, args=(i,), daemon=True)
           for i in range(threads)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout + 5)
    dt = max(time.time() - t0, 1e-3)
    total_got = sum(got)
    return {"mbps": round(total_got / 1048576.0 / dt, 2), "bytes": total_got,
            "errors": err[:2]}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    print("探测资产信息…")
    info = {k: probe_head(v) for k, v in
            (("update", UPDATE_URL), ("portable", PORTABLE_URL))}
    for k, v in info.items():
        print(f"  {k}: size={v['size']/1048576:.2f} MB via={v['via']} "
              f"range={v['range']}")
    if not info["portable"]["size"]:
        print("两个资产都不可达，直接 abort")
        return 1

    target = PORTABLE_URL
    size = info["portable"]["size"]
    # 阶段一：全部候选并发小窗测（4 MB）
    print("\n阶段一：候选线路 4MB 单连接测速…")

    def one(r):
        rid, name, tpl = r
        out = stream_test(apply(tpl, target), 4 << 20, 15)
        out["id"], out["name"], out["tpl"] = rid, name, tpl
        print(f"  {name:26s} {'OK ' if out['ok'] else 'FAIL'} "
              f"ttfb={out['ttfb_ms']:>5}ms  {out['mbps']:>7.2f} MB/s "
              f"{'' if out['ok'] else out.get('error','')}")
        return out

    with cf.ThreadPoolExecutor(max_workers=len(ROUTES)) as ex:
        res = list(ex.map(one, ROUTES))
    ok = sorted([r for r in res if r["ok"] and r["mbps"] > 0.05],
                key=lambda x: -x["mbps"])
    for r in res:
        if r not in ok:
            print(f"  剔除：{r['name']}（{r.get('error','太慢')}）")

    if mode == "quick":
        json.dump({"info": info, "stage1": res},
                  open("tools/_route_bench.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        return 0

    # 阶段二：前 6 名做 16 MB 单连接 + 4 线程并行
    print("\n阶段二：Top 线路 16MB 单连接 / 4 线程并行…")
    for r in ok[:6]:
        real = apply(r["tpl"], target)
        s = stream_test(real, 16 << 20, 60)
        p = parallel_test(real, size, 4, 16 << 20, 60)
        r["mbps16"] = s.get("mbps", 0)
        r["mbps4t"] = p["mbps"]
        r["p4_err"] = p["errors"]
        print(f"  {r['name']:26s} 4MB={r['mbps']:>6.2f}  16MB={r['mbps16']:>6.2f} "
              f" 4线程={r['mbps4t']:>6.2f} MB/s")

    # 阶段三：update 包（小文件）单独测，确认同一条路对小文件也成立
    print("\n阶段三：update 包 4MB 单连接…")
    for r in ok[:6]:
        real = apply(r["tpl"], UPDATE_URL)
        s = stream_test(real, 4 << 20, 30)
        r["update_mbps"] = s.get("mbps", 0)
        r["update_ttfb"] = s.get("ttfb_ms", 0)
        print(f"  {r['name']:26s} ttfb={s.get('ttfb_ms',0):>5}ms  "
              f"{s.get('mbps',0):>6.2f} MB/s")

    json.dump({"info": info, "stage1": res, "top": ok[:6]},
              open("tools/_route_bench.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n结果已写入 tools/_route_bench.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
