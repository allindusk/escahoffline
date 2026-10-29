"""统一命令行入口。

用法（**任意目录都能跑**，路径由 :mod:`pipeline.config` 推算）::

    cd "d:/.../escahoffline/_manifest_analysis"
    python -m pipeline.cli <command> [options]

常用命令
--------
``doctor``      环境自检（依赖/路径/清单/网页数据是否齐）
``refresh``     重新下载并解密两份清单
``diff``        与上次清单对比，列出增删改的包
``new``         云上有剧本、本地还没有的角色（= 待接入的新角色）
``update``      增量接入角色：下载→解剧本→语音→图片→立绘→刷新映射表
``check``       某个角色本地资源是否齐全
``rename``      重生成 names.js / charmeta.js（新增角色后）
``maps``        重生成 imgmap.js / sfxmap.js
``i18n-*``      译文仓库：导入译稿、迁移旧的 Message_zh、看覆盖率
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

from . import i18n_store, manifest, select, stages
from .config import (DATA, FILES_STORY, LANGUAGES, ROOT, WEB, add_scripts_to_path,
                     ensure_dirs)

if hasattr(sys.stdout, "buffer"):        # Windows 控制台默认 GBK，统一 UTF-8
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ================================================================ 命令实现


def _index(refresh_local: bool = False) -> select.Manifest:
    if not manifest.has_manifest():
        print("还没有清单，请先跑：python -m pipeline.cli refresh")
        sys.exit(1)
    return select.Manifest(manifest.load_index())


def cmd_doctor(a) -> int:
    print("=== 环境 ===")
    print(f"  Python      {sys.version.split()[0]}  ({sys.executable})")
    for m, note in (("UnityPy", "解 Unity 对象"), ("PIL", "图片"),
                    ("numpy", "立绘合成"), ("lz4", "UnityFS 解压")):
        try:
            __import__(m)
            print(f"  {m:<11} ✓ ({note})")
        except Exception:
            print(f"  {m:<11} ✗ 缺失 —— {note} 相关阶段不可用")
    try:
        from . import aes
        print(f"  AES 自检     {'✓' if aes.selftest() else '✗'}")
    except Exception as e:
        print(f"  AES 自检     ✗ {e}")

    print("=== 路径 ===")
    for label, p in (("项目根", ROOT), ("网页", WEB), ("数据中心", DATA),
                     ("剧情包", FILES_STORY)):
        flag = "✓" if os.path.isdir(p) else "✗"
        print(f"  {label:<6} {flag} {p}")

    print("=== 清单 ===")
    print("  " + manifest.summary())

    print("=== 网页数据 ===")
    chars = [f for f in os.listdir(DATA) if f.startswith("char_") and f.endswith(".js")]
    print(f"  char_*.js   {len(chars)} 个")
    for fn in ("index.js", "imgmap.js", "names.js", "charmeta.js", "sfxmap.js"):
        p = os.path.join(DATA, fn)
        print(f"  {fn:<12} {'✓' if os.path.exists(p) else '✗'}")
    return 0


def cmd_refresh(a) -> int:
    print("拉取清单…")
    manifest.refresh(install=not a.no_install)
    if a.diff and not a.no_install:
        return cmd_diff(a)
    return 0


def cmd_diff(a) -> int:
    d = manifest.diff_snapshots()
    tot = {k: sum(len(v) for v in d[k].values()) for k in d}
    print(f"对比结果：新增 {tot['added']} / 删除 {tot['removed']} / "
          f"内容变更 {tot['changed']}")
    for k in ("added", "removed", "changed"):
        for name, items in d[k].items():
            if not items:
                continue
            print(f"\n  [{k}] {name}  {len(items)} 条")
            for it in items[:a.limit]:
                print(f"    {it if isinstance(it, str) else it[0]}")
    return 0


def cmd_new(a) -> int:
    """云上有剧本、本地还没变成 char_*.js 的角色。"""
    mf = _index()
    remote = set(select.char_ids(mf.index))
    local = set()
    for fn in os.listdir(DATA):
        if fn.startswith("char_") and fn.endswith(".js"):
            local.add(int(fn[5:-3]))
    missing = sorted(remote - local)
    print(f"云端 {len(remote)} 个角色，本地 {len(local)} 个，未接入 {len(missing)} 个")
    if missing:
        print("  ID: " + ", ".join(map(str, missing[:60])) +
              (" …" if len(missing) > 60 else ""))
        print("\n  接入一个：python -m pipeline.cli update <id>")
    return 0


def cmd_update(a) -> int:
    """增量接入一个角色：这是「资源更新」的主链路。"""
    ensure_dirs()
    mf = _index()
    cids = [int(x) for x in a.char]
    print(f"=== 接入角色 {cids} ===")
    t0 = time.time()

    for cid in cids:
        print(f"\n--- 角色 {cid} ---")
        # 1) 下载剧本 / 语音 / 半身像包
        bundles = [mf.scenario(cid)] + mf.voices(cid) + mf.meta_images(cid)
        bundles = [b for b in bundles if b]
        if not bundles:
            print("  清单里没有这个角色的剧本，跳过")
            continue
        stages.fetch(list(bundles), a.workers, f"char{cid} 基础包", skip=True)

        # 2) 解剧本 -> char_<id>.js
        stages.extract_scenario(cid, force=a.force)

        # 3) 语音导出
        if not a.no_voice:
            stages.export_voices(cid)

        # 4) 头像
        stages.export_avatar(cid)

        # 5) 图片：按剧本实际引用，只下缺的那些 bundle
        if not a.no_image:
            try:
                d = select.load_char(cid)
                assets = select.collect_assets(d)
            except Exception as e:
                print(f"  读剧本失败 {e}")
                assets = {"stand": set(), "back": set(), "cg": set()}
            need = []
            for kind in ("back", "cg", "stand"):
                miss = [s for s in assets[kind]]
                if kind == "back":
                    need += [mf.back_bundle(s) for s in miss]
                elif kind == "cg":
                    need += [mf.cg_bundle(s) for s in miss]
                else:
                    need += [mf.stand_bundle(s) for s in miss]
                need += [mf.stand_full.get(int(select.PAT_STAND_KEY.match(s).group(1)))
                         for s in miss if kind == "stand"
                         and select.PAT_STAND_KEY.match(s)]
            need = sorted({n for n in need if n})
            entries = [mf.index[n] for n in need if n in mf.index]
            stages.fetch(entries, a.workers, f"char{cid} 图片包", skip=True)
            stages.export_images(mf, assets)
            stages.export_standfull(
                mf, {int(select.PAT_STAND_KEY.match(s).group(1))
                     for s in assets["stand"] if select.PAT_STAND_KEY.match(s)})

        # 6) 立绘合成（含 stand_slot.json 合并保护）
        nums = set()
        if not a.no_image:
            try:
                nums = {select.PAT_STAND_KEY.match(s).group(1)
                        for s in select.collect_assets(select.load_char(cid))["stand"]
                        if select.PAT_STAND_KEY.match(s)}
            except Exception as e:
                print(f"  收集立绘编号失败：{e}")
            if nums:
                stages.compose_stands(nums)

    # 7) 全站映射表 / 索引 / 角色名
    if not a.no_map:
        print("\n--- 刷新映射表 ---")
        stages.sync_index()
        stages.regen_maps()
        stages.regen_names(use_wiki=not a.no_wiki)
        stages.regen_charmeta()

    print(f"\n完成，用时 {time.time() - t0:.0f}s")
    print("验证：启动 web/启动.bat，或 python -m pipeline.cli check <id>")
    return 0


def cmd_download_all(a) -> int:
    """把清单里的资源包**全量**拉到本地（已存在且 size 一致的一律跳过）。

    为什么要全量：清单 24.7 GB 里 17.9 GB 是语音，剩下是立绘/背景/CG/头像；
    将来做 GUI 的"选择资源包下载"时，本地有一份完整镜像才好按需裁剪。
    单个 CDN 连接只有 ~0.1 MB/s（实测），所以并发才是吞吐来源。
    """
    from . import net

    ensure_dirs()
    mf = _index()
    items = list(mf.index.values())
    if a.kind:
        pre = tuple(x.strip() for x in a.kind.split(",") if x.strip())
        items = [e for e in items if e["bundle_name"].startswith(pre)]
        print(f"过滤前缀 {pre}")
    total_b = sum(int(e["size"]) for e in items)

    def local_ok(e) -> bool:
        p = net.bundle_path(e["bundle_name"])
        return os.path.exists(p) and os.path.getsize(p) == int(e["size"])

    todo = [e for e in items if not local_ok(e)]
    done_b = total_b - sum(int(e["size"]) for e in todo)
    print(f"清单 {len(items)} 包 / {total_b / 1073741824:.2f} GB")
    print(f"本地已完整 {len(items) - len(todo)} 包 / {done_b / 1073741824:.2f} GB")
    print(f"待下载 {len(todo)} 包 / "
          f"{sum(int(e['size']) for e in todo) / 1073741824:.2f} GB")
    if a.dry_run or not todo:
        return 0
    if a.limit:
        todo = todo[:a.limit]
        print(f"--limit 生效：本轮只下前 {len(todo)} 个")

    t0 = time.time()
    pending = todo
    for r in range(1, a.rounds + 1):
        if not pending:
            break
        left_b = sum(int(e["size"]) for e in pending)
        print(f"\n--- 第 {r}/{a.rounds} 轮：{len(pending)} 包 / "
              f"{left_b / 1073741824:.2f} GB ---")
        stat = net.fetch_many(pending, a.workers, f"全量下载 R{r}", skip_existing=True)
        bad = {n for n, _ in stat["fail"]}
        pending = [e for e in pending if e["bundle_name"] in bad]
        if pending:
            print(f"  本轮失败 {len(pending)} 个，准备重试")
            time.sleep(3)

    still = [e for e in todo if not local_ok(e)]
    el = max(time.time() - t0, 1e-6)
    got = sum(int(e["size"]) for e in todo) - sum(int(e["size"]) for e in still)
    print(f"\n完成：用时 {el / 60:.1f} 分钟，新下 {got / 1073741824:.2f} GB，"
          f"平均 {got / el / 1048576:.2f} MB/s")
    print(f"  成功 {len(todo) - len(still)} / {len(todo)} 包")
    if still:
        print(f"  ⚠ 仍有 {len(still)} 包未完成（重跑本命令即可续传）：")
        for e in still[:10]:
            print(f"    {e['bundle_name']}  {int(e['size']) / 1048576:.1f} MB")
        return 1
    return 0


def cmd_adv(a) -> int:
    """接入非角色剧情：``adv main_1 event_3 other_2``。

    与 ``update`` 同构（下载→解剧本→按需图片→立绘合成→刷新映射表），
    差别只在剧本包命名与输出文件 key：``adv_<kind>_<no>.js``。
    实测三类包的内部 schema 与角色剧本完全一致，所以阶段实现全部复用。
    """
    ensure_dirs()
    mf = _index()
    specs = []
    for s in a.story:
        p = select.parse_adv_spec(s)
        if p:
            specs.append(p)
        else:
            print(f"  无法解析 {s}（写法：main_1 / event_3 / other_2 / adv_main_1）")
    if not specs:
        return 1
    print(f"=== 接入剧情 {[select.story_key(k, n) for k, n in specs]} ===")
    t0 = time.time()

    for kind, no in specs:
        key = select.story_key(kind, no)
        print(f"\n--- {key}（{select.ADV_LABEL.get(kind, kind)}）---")
        b = mf.adv_scenario(kind, no)
        if not b:
            print("  清单里没有这部剧情，跳过")
            continue
        # 1) 剧本包
        stages.fetch([b], a.workers, f"{key} 剧本", skip=True)
        # 2) 解剧本 -> adv_<kind>_<no>.js
        if not stages.extract_adv(kind, no, force=a.force):
            continue
        # 2.5) 语音：bundle_voice_adv_<kind>_<no>_<seg>，段号与 parts 顺序对应
        if not a.no_voice:
            vbundles = select.adv_voice_bundles(mf.index, kind, no)
            if vbundles:
                vent = [mf.index[n] for n in vbundles if n in mf.index]
                stages.fetch(vent, a.workers, f"{key} 语音包", skip=True)
                stages.export_adv_voices(key, vbundles)
        if a.no_image:
            continue
        # 3) 图片：按剧本实际引用下载
        try:
            assets = select.collect_assets(select.load_story(key))
        except Exception as e:
            print(f"  读剧本失败 {e}")
            continue
        need, nums = [], set()
        for kindname in ("back", "cg", "stand"):
            for s in assets[kindname]:
                if kindname == "back":
                    need.append(mf.back_bundle(s))
                elif kindname == "cg":
                    need.append(mf.cg_bundle(s))
                else:
                    need.append(mf.stand_bundle(s))
                    m = select.PAT_STAND_KEY.match(s)
                    if m:
                        nums.add(m.group(1))
                        need.append(mf.stand_full.get(int(m.group(1))))
        entries = [mf.index[n] for n in {x for x in need if x} if n in mf.index]
        stages.fetch(entries, a.workers, f"{key} 图片包", skip=True)
        stages.export_images(mf, assets)
        stages.export_standfull(mf, {int(n) for n in nums})
        # 4) 立绘差分合成（与角色剧情同一套落点推导）
        if nums:
            stages.compose_stands(nums)

    if not a.no_map:
        print("\n--- 刷新映射表 ---")
        stages.sync_advindex()
        stages.regen_maps()

    print(f"\n完成，用时 {time.time() - t0:.0f}s")
    print("验证：启动 web/启动.bat，URL 加 ?adv=<key>（如 ?adv=main_1&autoplay=1）")
    return 0


def cmd_adv_all(a) -> int:
    """把清单里**所有**非角色剧情一次性接入。

    与逐部跑 ``adv`` 的区别是**阶段批量化**：图片/立绘/语音包合并成一次下载、
    ``export_images`` 一次跑完、``compose_stands`` 一次跑完（避免 227 次子进程），
    语音则按部导出到各自的 ``media/audio/<key>/``。全程幂等，已存在的产物跳过。
    """
    ensure_dirs()
    mf = _index()
    remote = select.adv_ids(mf.index)
    if a.kind:
        kinds = {x.strip() for x in a.kind.split(",") if x.strip()}
        remote = [t for t in remote if t[0] in kinds]
    print(f"=== 全量接入 {len(remote)} 部剧情 ===")
    t0 = time.time()

    # 1) 剧本包 + 解包
    scripts = [e for e in (mf.adv_scenario(k, n) for k, n in remote) if e]
    stages.fetch(scripts, a.workers, "剧本包", skip=True)
    for kind, no in remote:
        stages.extract_adv(kind, no, force=a.force)

    # 2) 汇总所有剧情的资源引用
    assets = {"stand": set(), "back": set(), "cg": set()}
    nums, voice_jobs, ok = set(), [], 0
    for kind, no in remote:
        key = select.story_key(kind, no)
        try:
            d = select.load_story(key)
        except Exception:
            continue
        ok += 1
        one = select.collect_assets(d)
        for k in assets:
            assets[k] |= one[k]
        for s in one["stand"]:
            m = select.PAT_STAND_KEY.match(s)
            if m:
                nums.add(m.group(1))
        vb = select.adv_voice_bundles(mf.index, kind, no)
        if vb:
            voice_jobs.append((key, vb))
    print(f"  已解 {ok} 部；立绘 {len(assets['stand'])} / 背景 {len(assets['back'])} / "
          f"CG {len(assets['cg'])} / 立绘编号 {len(nums)} / 有语音 {len(voice_jobs)} 部")

    # 3) 一次性下载缺失的图片/立绘/语音包
    need = []
    for s in assets["back"]:
        need.append(mf.back_bundle(s))
    for s in assets["cg"]:
        need.append(mf.cg_bundle(s))
    for s in assets["stand"]:
        need.append(mf.stand_bundle(s))
    for n_ in nums:
        need.append(mf.stand_full.get(int(n_)))
    for _k, vb in voice_jobs:
        need.extend(vb)
    entries = [mf.index[x] for x in {x for x in need if x} if x in mf.index]
    stages.fetch(entries, a.workers, "图片+语音包", skip=True)

    # 4) 导出（批量）
    stages.export_images(mf, assets)
    stages.export_standfull(mf, {int(x) for x in nums})
    if nums:
        stages.compose_stands(nums)
    if not a.no_voice:
        for key, vb in voice_jobs:
            stages.export_adv_voices(key, vb)

    # 5) 剧情封面 + 索引
    stages.export_story_topics()
    if not a.no_map:
        print("\n--- 刷新索引 ---")
        stages.sync_advindex()
        stages.regen_maps()

    print(f"\n完成，用时 {(time.time() - t0) / 60:.1f} 分钟")
    print("验证：python -m pipeline.cli adv-list  /  浏览器 ?adv=main_12")
    return 0


def cmd_adv_list(a) -> int:
    """列出云端有哪些非角色剧情、本地接了哪些。

    ``--json`` 输出机器可读清单（GUI 的「资源包选择」直接消费）：
    每项含 kind/no/key/bundle/剧本包字节数/本地是否已接入。
    """
    mf = _index()
    remote = select.adv_ids(mf.index)
    local = set()
    for fn in os.listdir(DATA):
        if select.PAT_ADV_FILE.fullmatch(fn):
            local.add(fn[:-3])

    if a.json:
        rows = []
        for kind, no in remote:
            key = select.story_key(kind, no)
            ent = mf.adv_scenario(kind, no) or {}      # 清单条目 dict
            vb = select.adv_voice_bundles(mf.index, kind, no)
            rows.append({
                "kind": kind, "kindLabel": select.ADV_LABEL[kind], "no": no,
                "key": key,
                "bundle": ent.get("bundle_name"),
                "size": int(ent.get("size") or 0),
                "hash": ent.get("hash"),
                # 语音是体积大头（占清单 72%），GUI 要能按「含不含语音」让用户选
                "voiceBundles": len(vb),
                "voiceSize": sum(int(mf.index[b]["size"]) for b in vb if b in mf.index),
                "local": key in local,
            })
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    for kind in select.ADV_KINDS:
        nos = [n for k, n in remote if k == kind]
        have = [n for n in nos if select.story_key(kind, n) in local]
        names = ", ".join("整包" if n is None else str(n) for n in have[:20])
        print(f"{select.ADV_LABEL[kind]:<6} 云端 {len(nos):>3} 部  本地 {len(have):>3} 部"
              + (f"  （本地：{names}" + (" …" if len(have) > 20 else "") + "）"
                 if have else ""))
    print("\n接入一部：python -m pipeline.cli adv main_1")
    print("机器可读：python -m pipeline.cli adv-list --json")
    return 0


def cmd_fetch(a) -> int:
    """按 bundle 名下载资源包（名字必须能在清单里查到，否则无从校验 size）。"""
    ensure_dirs()
    mf = _index()
    items, unknown = [], []
    for name in a.bundle:
        (items.append(mf.index[name]) if name in mf.index else unknown.append(name))
    for n in unknown:
        print(f"  清单里没有 {n}")
    if items:
        stages.fetch(items, workers=a.workers, desc="手动下载")
    return 0 if not unknown else 1


def cmd_check(a) -> int:
    for cid in a.char:
        r = stages.check_char(int(cid))
        if not r["scenario"]:
            print(f"角色 {cid}：本地还没有剧情数据")
            continue
        print(f"角色 {cid}：{r['parts']} 段 / {r['lines']} 句 / "
              f"语音 {r['voice_files']}/{r['voices_ref']}"
              f"（缺 {r['voice_miss']}）")
        for k in ("stand", "back", "cg"):
            if r[f"{k}_miss"]:
                print(f"   缺 {k}：{len(r[f'{k}_miss'])} 个 "
                      f"例如 {r[f'{k}_miss'][:3]}")
        cov = i18n_store.coverage(int(cid))
        for lang, info in cov.get("langs", {}).items():
            if lang == "ja":
                continue
            if info.get("translated"):
                print(f"   {lang} 译文 {info['translated']}/{cov['total']} "
                      f"({info['pct']}%)  失效 {info.get('stale', 0)}")
    return 0


def cmd_extract(a) -> int:
    for cid in a.char:
        stages.extract_scenario(int(cid), force=a.force)
    stages.sync_index(force=False)
    return 0


def cmd_maps(a) -> int:
    stages.sync_index()
    stages.regen_maps()
    i18n_store.build_index(LANGUAGES)
    return 0


def cmd_rename(a) -> int:
    stages.regen_names(use_wiki=not a.no_wiki)
    stages.regen_charmeta()
    return 0


def cmd_i18n_import(a) -> int:
    """把人工译稿对齐注入 i18n 仓库（对齐算法复用 inject_char_story）。"""
    add_scripts_to_path()
    import inject_char_story as inj          # 无 import 副作用，可安全复用

    cid = int(a.char)
    tparts = inj.parse_translation(a.file)
    try:
        data = select.load_char(cid)
    except Exception as e:
        print(f"读不到 char_{cid}.js：{e}")
        return 1

    total_src = 0
    pairs, drift = {}, []
    for pi, part in enumerate(data["parts"]):
        smm = inj.src_messages(part)
        zh = tparts.get(pi, [])
        if not smm and not zh:
            continue
        assigned, unassigned, orphan = inj.align(smm, zh)
        for i, text in assigned.items():
            pairs[smm[i][1].get("Message") or ""] = text
        total_src += len(smm)
        drift += [("未匹配", i, (smm[i][1].get("Message") or "")[:24])
                  for i in unassigned]
        drift += [("多余译稿", j, (zh[j][1] or "")[:24]) for j in orphan]

    print(f"角色 {cid}：原文 {total_src} 句，对齐成功 {len(pairs)} 句 "
          f"({100.0 * len(pairs) / total_src:.1f}%)" if total_src else "无台词")
    for kind, i, txt in drift[:a.show]:
        print(f"   ! {kind} #{i} {txt}")
    if drift:
        print(f"   共 {len(drift)} 处漂移（对不上的保持日文，绝不错配）")

    if a.dry_run:
        print("[dry-run] 未写入")
        return 0
    p = i18n_store.save_messages(a.lang, cid, pairs, source_jp_count=total_src)
    print(f"已写入 {p}")
    i18n_store.build_index(LANGUAGES)
    return 0


def cmd_i18n_migrate(a) -> int:
    i18n_store.migrate_from_message_zh(dry=a.dry_run)
    if not a.dry_run:
        i18n_store.build_index(LANGUAGES)
    return 0


def cmd_i18n_restore(a) -> int:
    n = i18n_store.import_backup(a.file, int(a.char), lang=a.lang)
    i18n_store.build_index(LANGUAGES)
    return 0 if n else 1


def cmd_i18n_cov(a) -> int:
    cids = [int(c) for c in a.char] if a.char else sorted(
        int(f[5:-3]) for f in os.listdir(DATA)
        if f.startswith("char_") and f.endswith(".js"))
    rows = []
    for cid in cids:
        c = i18n_store.coverage(cid)
        if "error" in c:
            continue
        rows.append(c)
    if a.lang != "all":
        rows = [r for r in rows if (r["langs"].get(a.lang) or {}).get("translated")]
    tot = sum(r["total"] for r in rows)
    for lang in LANGUAGES:
        if lang == "ja":
            continue
        hit = sum(r["langs"].get(lang, {}).get("translated", 0) for r in rows)
        print(f"{lang}：{hit}/{tot} 句 ({100.0 * hit / tot:.1f}%)  "
              f"覆盖 {len(rows)} 个角色" if tot else "无数据")
    if a.verbose:
        for r in rows:
            zh = r["langs"].get("zh-Hans", {})
            if zh.get("translated"):
                print(f"   角色 {r['char']:>4}  {zh['translated']:>5}/{r['total']:<5} "
                      f"{zh['pct']:>5}%")
    return 0


def cmd_i18n_prune(a) -> int:
    for cid in a.char:
        n = i18n_store.prune_missing(a.lang, int(cid))
        print(f"  角色 {cid}：清理失效译文 {n} 条")
    i18n_store.build_index(LANGUAGES)
    return 0


# ================================================================ 参数


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境与数据自检").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("refresh", help="重新下载并解密清单")
    p.add_argument("--diff", action="store_true", help="顺手与上一版对比")
    p.add_argument("--no-install", action="store_true",
                   help="只存快照，不覆盖根目录下的 Manifest*.json")
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("diff", help="清单对比")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_diff)

    sub.add_parser("new", help="列出云端有、本地未接入的角色").set_defaults(fn=cmd_new)

    p = sub.add_parser("update", help="增量接入角色（完整链路）")
    p.add_argument("char", nargs="+", help="角色 ID，可多个")
    p.add_argument("--workers", type=int, default=48)
    p.add_argument("--force", action="store_true", help="已存在也重新解剧本")
    p.add_argument("--no-voice", action="store_true")
    p.add_argument("--no-image", action="store_true")
    p.add_argument("--no-map", action="store_true")
    p.add_argument("--no-wiki", action="store_true", help="不用 WIKI 人名")
    p.set_defaults(fn=cmd_update)

    p = sub.add_parser("extract", help="只重新解剧本")
    p.add_argument("char", nargs="+")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_extract)

    p = sub.add_parser("download-all", help="全量下载清单里的资源包（跳过已存在）")
    p.add_argument("--kind", help="只下指定前缀，逗号分隔，如 bundle_voice_adv_,bundle_bgimage")
    p.add_argument("--workers", type=int, default=64)
    p.add_argument("--rounds", type=int, default=4, help="失败重试轮数")
    p.add_argument("--limit", type=int, help="只下前 N 个（测速用）")
    p.add_argument("--dry-run", action="store_true", help="只统计不下载")
    p.set_defaults(fn=cmd_download_all)

    p = sub.add_parser("adv", help="接入非角色剧情（主线/活动/其他）")
    p.add_argument("story", nargs="+", help="如 main_1 event_3 other_2")
    p.add_argument("--workers", type=int, default=48)
    p.add_argument("--force", action="store_true", help="已存在也重新解剧本")
    p.add_argument("--no-image", action="store_true")
    p.add_argument("--no-voice", action="store_true", help="跳过语音包")
    p.add_argument("--no-map", action="store_true")
    p.set_defaults(fn=cmd_adv)

    p = sub.add_parser("adv-all", help="一次接入全部非角色剧情（批量、幂等）")
    p.add_argument("--kind", help="只处理某些类型，逗号分隔：main,event,other,op")
    p.add_argument("--workers", type=int, default=64)
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-voice", action="store_true")
    p.add_argument("--no-map", action="store_true")
    p.set_defaults(fn=cmd_adv_all)

    p = sub.add_parser("adv-list", help="列出主线/活动/其他剧情的云端与本地数量")
    p.add_argument("--json", action="store_true", help="输出机器可读清单（给 GUI 用）")
    p.set_defaults(fn=cmd_adv_list)

    p = sub.add_parser("fetch", help="按 bundle 名下载资源包")
    p.add_argument("bundle", nargs="+")
    p.add_argument("--workers", type=int, default=48)
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("check", help="检查角色资源是否齐全")
    p.add_argument("char", nargs="+")
    p.set_defaults(fn=cmd_check)

    sub.add_parser("maps", help="重生成 imgmap/sfxmap/index").set_defaults(fn=cmd_maps)

    p = sub.add_parser("rename", help="重生成 names.js / charmeta.js")
    p.add_argument("--no-wiki", action="store_true")
    p.set_defaults(fn=cmd_rename)

    p = sub.add_parser("i18n-import", help="把人工译稿对齐注入译文仓库")
    p.add_argument("char")
    p.add_argument("file")
    p.add_argument("--lang", default="zh-Hans")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--show", type=int, default=8, help="打印多少条漂移明细")
    p.set_defaults(fn=cmd_i18n_import)

    p = sub.add_parser("i18n-migrate", help="把 char_*.js 里的遗留 Message_zh 迁出")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_i18n_migrate)

    p = sub.add_parser("i18n-restore", help="导入 inject_char_story 的备份 json")
    p.add_argument("char")
    p.add_argument("file")
    p.add_argument("--lang", default="zh-Hans")
    p.set_defaults(fn=cmd_i18n_restore)

    p = sub.add_parser("i18n-cov", help="译文覆盖率")
    p.add_argument("char", nargs="*")
    p.add_argument("--lang", default="zh-Hans")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_i18n_cov)

    p = sub.add_parser("i18n-prune", help="清理已不在原文里的失效译文")
    p.add_argument("char", nargs="+")
    p.add_argument("--lang", default="zh-Hans")
    p.set_defaults(fn=cmd_i18n_prune)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
