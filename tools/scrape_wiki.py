"""构建全量包时：从公开 Wiki 抓角色元信息，写入 server/cdn/data/char_meta.json。

替代以前的本地 ``escah`` WIKI 镜像（``extract_meta.py`` 读 ``escah/data/parsed/characters``）。
公开页面只有日文，也契合"全量包纯日文"的诉求；稀有度来自每个角色的子页面
（``deep=True`` 逐页抓，约 439 个请求，靠代理联网，构建机跑没问题）。

已有字段（如旧表里的 ``d`` 实装日）在重名匹配时会保留，不会丢。
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from server.cdn import wiki  # noqa: E402


def main() -> int:
    deep = "--deep" in sys.argv
    out = os.path.join(ROOT, "server", "cdn", "data", "char_meta.json")
    existing = {}
    if os.path.exists(out):
        try:
            with io.open(out, encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:                                      # noqa: BLE001
            existing = {}
    try:
        scraped = wiki.scrape(deep=deep)
    except wiki.WikiUnreachable as e:
        print(f"[跳过] 无法访问 Wiki（多半未开代理）：{e}")
        return 1
    merged = wiki.merge_meta(existing, scraped)
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
    n_r = sum(1 for v in merged.values() if v.get("r"))
    print(f"写入 {os.path.relpath(out, ROOT)}：{len(merged)} 条"
          f"（含稀有度 {n_r}，带类型 {sum(1 for v in merged.values() if v.get('t'))}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
