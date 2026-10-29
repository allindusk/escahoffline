"""导出剧情视频（mp4）：复用 ``tools/video/`` 里已验证的「逐句截帧 + PyAV 编码」链路。

为什么不沿用浏览器里的 MediaRecorder 实时录制
--------------------------------------------
那条路只能出 webm，而且抓的是"实时播放"的画面：掉帧、切标签页、音轨混合都可能出问题，
还无法把多段精确拼成一条。这套脚本是：
    剧情数据 → 时间轴（每句一帧 + 音频事件）→ headless Edge 逐句定格截图
    → PyAV 按时间轴编码 mp4（自带 ffmpeg 库，不需要外部 exe）
好处是画面稳定、音频精确对轨、任意连续段合成一条；代价是**离线渲染比实时慢**。

能导出什么
----------
只支持**角色剧情**（``char_<id>``）的 1–6 段。主线/活动的剧本是另一套命名与分段，
``tools/video/story.py`` 的时间轴还没接它们 —— 这里如实报错，不假装支持。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import paths, proc

VIDEO_DIR = os.path.join(paths.ROOT, "tools", "video")
FRAME_ROOT = os.path.join(paths.EXPORT, "frames")

_STEP = re.compile(r"^\s*\[(\d+)\]")


class VideoExportError(RuntimeError):
    """给用户看的失败原因（缺依赖、没数据、渲染失败…）"""


def _ensure_importable() -> None:
    if VIDEO_DIR not in sys.path:
        sys.path.insert(0, VIDEO_DIR)


def missing_deps() -> list[str]:
    """导出视频需要的三方库。"""
    out = []
    for mod, note in (("playwright", "驱动 headless Edge 截帧"),
                      ("av", "编码 mp4（内含 ffmpeg）"),
                      ("numpy", "音频重采样")):
        try:
            __import__(mod)
        except Exception:                                      # noqa: BLE001
            out.append(f"{mod}（{note}）")
    return out


def seg_label(parts: list[int]) -> str:
    return f"p{parts[0]}" if parts[0] == parts[-1] else f"p{parts[0]}-{parts[-1]}"


def _unique(path: str) -> str:
    """同名文件不覆盖，自动追加 _2/_3（重导同一段时很常见）。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{stem}_{i}{ext}"):
        i += 1
    return f"{stem}_{i}{ext}"


def probe(key: str, parts: list[int]) -> dict:
    """先算一下要渲染多少句、剧情多长（给前端显示预估，避免点下去一脸茫然）。"""
    _ensure_importable()
    cid = _cid_of(key)
    from story import Timeline                                # noqa: PLC0415
    tl = Timeline(cid, parts)
    return {"unit": key, "parts": parts, "lines": len(tl.lines),
            "seconds": round(tl.total, 1)}


def _cid_of(key: str) -> int:
    if not key or not key.startswith("char_"):
        raise VideoExportError(
            "视频导出目前只支持「角色剧情」（char_<id>）。\n"
            "主线/活动的分段时间轴还没接入，先用浏览器录屏或等后续版本。")
    try:
        return int(key[5:])
    except ValueError as e:
        raise VideoExportError(f"角色标识不对：{key}") from e


def export(job, key: str, parts: list[int], limit: int = 0,
           port: int = 8000, keep_frames: bool = False) -> dict:
    """渲染 + 编码一条视频（多段会合成同一条）。返回文件信息。

    ``limit`` 只用于验证链路（渲染前 N 句），正常导出不要传。
    """
    miss = missing_deps()
    if miss:
        raise VideoExportError("缺少依赖：" + "、".join(miss)
                              + "。可在控制台「剧情安装」里点「自动安装依赖」，"
                                "或 pip install playwright av numpy")
    _ensure_importable()
    cid = _cid_of(key)
    parts = sorted({int(p) for p in parts})
    if not parts:
        raise VideoExportError("没有选择段落")

    from story import Timeline                                # noqa: PLC0415
    tl = Timeline(cid, parts)
    total = len(tl.lines)
    if limit:
        total = min(total, limit)
    if not total:
        raise VideoExportError(f"{key} 的第 {parts} 段没有可渲染的台词")
    job.set_total(total, "帧")          # 导出进度是"帧"，不是字节
    job.log(f"{key}：{len(parts)} 段 / {total} 句 / 约 {tl.total / 60:.1f} 分钟剧情")

    seg = seg_label(parts)
    fdir = os.path.join(FRAME_ROOT, f"{key}_{seg}")
    os.makedirs(fdir, exist_ok=True)
    for name in os.listdir(fdir):                              # 清掉上次残留
        try:
            os.remove(os.path.join(fdir, name))
        except OSError:
            pass

    t0 = time.time()
    # ---------------------------------------------------------------- ① 截帧
    cmd = [sys.executable, os.path.join(VIDEO_DIR, "render_browser.py"),
           str(cid), ",".join(str(p) for p in parts), str(limit), fdir]
    env = dict(os.environ, PYTHONUTF8="1", ESC_PORT=str(port))
    job.log("① 逐句截帧（headless 浏览器，这一步最慢）…")
    # 走 proc.popen：不弹黑窗，且服务退出时会被统一清理（不留孤儿进程）
    child = proc.popen(cmd, cwd=VIDEO_DIR, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True,
                       encoding="utf-8", errors="replace", env=env)
    done = 0
    tail = []
    for line in child.stdout or ():
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        tail = tail[-8:]
        if _STEP.match(line):
            done += 1
            job.progress(done=min(done, total),
                         message=f"截帧 {done}/{total}"
                                 f"（{(time.time() - t0):.0f}s）")
        elif line.startswith(("char=", "part ", "launched", "done ")):
            job.log("   " + line[:150])
    rc = child.wait()
    proc.reap(child)
    frames = len([n for n in os.listdir(fdir) if n.endswith(".png")])
    if rc != 0 or not frames:
        raise VideoExportError(
            "截帧失败（退出码 %s，%d 帧）。常见原因：\n"
            "· 没装 playwright 或找不到 Edge 浏览器\n"
            "· 端口 %s 上不是本程序（截图会拿到空白页）\n"
            "最后几行日志：\n%s"
            % (rc, frames, port, "\n".join(tail[-5:])))
    job.log(f"   截帧完成：{frames} 帧，用时 {time.time() - t0:.0f}s")

    # ---------------------------------------------------------------- ② 编码
    from encode import encode as encode_mp4                    # noqa: PLC0415
    from . import names as nameutil
    # 文件名用角色名（``神騎プラチネル_p2-4.mp4``），不要 char_1 这种机器 ID
    stem = nameutil.file_stem(cid, "_" + seg)
    out = _unique(os.path.join(paths.EXPORT, f"{stem}.mp4"))
    job.progress(done=total, message="编码 mp4（含音频对轨）…")
    job.log("② 编码 mp4（PyAV，音频按时间轴对轨）…")
    t1 = time.time()
    encode_mp4(tl, Path(fdir), Path(out), label=stem, limit=limit or None)
    size = os.path.getsize(out)

    if not keep_frames:
        for name in os.listdir(fdir):
            try:
                os.remove(os.path.join(fdir, name))
            except OSError:
                pass
        try:
            os.rmdir(fdir)
        except OSError:
            pass

    job.log(f"   完成：{os.path.basename(out)}（{size / 1048576:.1f} MB，"
            f"编码 {time.time() - t1:.0f}s）")
    return {"file": os.path.basename(out), "bytes": size, "frames": frames,
            "seconds": round(time.time() - t0, 1), "path": out}
