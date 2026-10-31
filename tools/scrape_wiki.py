"""从公开 Wiki 抓角色元信息，写入 server/cdn/data/char_meta.json。

替代以前的本地 ``escah`` WIKI 镜像（``extract_meta.py`` 读 ``escah/data/parsed/characters``）。
公开页面只有日文，也契合"全量包纯日文"的诉求。

**只请求 1 个页面**：角色一览（按分节标题拿稀有度）；只收 **SSR / SR / R** 三种角色。
NPC / サポーター 既不是稀有度、也没有独立剧情，不抓（用户 2026-11-01 明确）。
不逐页抓 439 个子页面。

典型用法：
* 本地/CI 手动跑一次：``python tools/scrape_wiki.py``
* GitHub Actions 每天定时跑（见 ``.github/workflows/wiki-daily.yml``），
  产物 ``char_meta.json`` 提交回仓库，客户端"更新剧情"时从 GitHub 拉取，
  用户侧走普通网络即可，不需要代理。

已有字段（如旧表里的 ``d`` 实装日、``zh`` 中文名）在重名匹配时会保留，不会丢。
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from server.cdn import wiki  # noqa: E402


def main() -> int:
    out = os.path.join(ROOT, "server", "cdn", "data", "char_meta.json")
    existing = {}
    if os.path.exists(out):
        try:
            with io.open(out, encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:                                      # noqa: BLE001
            existing = {}
    try:
        scraped = wiki.scrape()
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
