"""全量重导 SD 小人（多进程）。

``export_sd.py`` 单进程跑 821 个包大约 2.5 小时，其中绝大部分是逐帧栅格化
（19 个网格部件 + 骨骼蒙皮 + Deform）。这里只是把同一件事按编号切给多个
进程，最后统一合并 sdmap：

    python export_sd_all.py --workers 8 --force

注意：**只有本进程写 sdmap.js**，工作进程只返回条目，避免并发覆盖。
"""
import argparse
import os
import re
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import export_sd as E


def _work(job):
    num, force = job
    try:
        idx = E.manifest_index()
        r = E.export_unit(num, idx, force=force)
        return num, r, ""
    except Exception:                                            # noqa: BLE001
        return num, None, traceback.format_exc()[-600:]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个（调试用）")
    ap.add_argument("--units", default="")
    a = ap.parse_args(argv)

    idx = E.manifest_index()
    if a.units:
        nums = [p.zfill(5) for p in a.units.replace(" ", "").split(",") if p.isdigit()]
    else:
        nums = sorted({m.group(1) for n in idx
                       for m in [re.match(r"^bundle_unit_(\d+)_bc$", n)] if m})
    if a.limit:
        nums = nums[:a.limit]
    E._log(f"待重导 {len(nums)} 个，进程 {a.workers}，force={a.force}")

    jobs = [(n, a.force) for n in nums]
    done = []
    ok = fail = 0
    t0 = time.time()
    if a.workers <= 1:
        it = map(_work, jobs)
        pool = None
    else:
        from concurrent.futures import ProcessPoolExecutor
        pool = ProcessPoolExecutor(max_workers=a.workers)
        it = pool.map(_work, jobs, chunksize=2)

    for i, (num, r, err) in enumerate(it, 1):
        if r:
            done.append(r)
            ok += 1
        else:
            fail += 1
            if err:
                E._log(f"  ! {num} 失败：{err}")
        if done and (i % 40 == 0 or i == len(nums)):
            E.merge(done)
            done.clear()
        if i % 20 == 0 or i == len(nums):
            el = time.time() - t0
            eta = el / i * (len(nums) - i)
            E._log(f"  [{i}/{len(nums)}] ok {ok} fail {fail} "
                   f"{el / i:.1f}s/个 剩余约 {eta / 60:.0f} 分")
    if pool is not None:
        pool.shutdown()
    if done:
        E.merge(done)
    removed = E.cleanup_legacy()
    E._log(f"完成：成功 {ok}，失败 {fail}，清理旧文件 {removed} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
