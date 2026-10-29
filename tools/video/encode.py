"""音节合成：帧序列 + 音频时间轴 → mp4（PyAV，无需外部 ffmpeg.exe）。"""
from __future__ import annotations

import sys
from pathlib import Path

import av
import numpy as np
from av.audio.resampler import AudioResampler
from PIL import Image

from story import WEB, Timeline

SR = 44100
FPS = 25
W, H = 1920, 1080
DISSOLVE = 3          # 每句开头 3 帧（0.12s）与上一句交叉溶解


def decode_audio(path: Path) -> np.ndarray:
    """任意音频 → (2, N) float32 44.1k。"""
    try:
        with av.open(str(path)) as ct:
            if not ct.streams.audio:
                return np.zeros((2, 0), np.float32)
            rs = AudioResampler(format='fltp', layout='stereo', rate=SR)
            chunks = []
            for fr in ct.decode(audio=0):
                for rf in rs.resample(fr):
                    chunks.append(rf.to_ndarray())
            for rf in rs.resample(None):
                chunks.append(rf.to_ndarray())
    except Exception:
        return np.zeros((2, 0), np.float32)
    if not chunks:
        return np.zeros((2, 0), np.float32)
    a = np.concatenate(chunks, axis=1).astype(np.float32)
    return a.reshape(-1, a.shape[1])[:2]


def _add(dst: np.ndarray, src: np.ndarray, at: int, gain: float = 1.0):
    if src.size == 0 or gain <= 0:
        return
    n = dst.shape[1]
    if at >= n:
        return
    if at < 0:
        src = src[:, -at:]
        at = 0
    m = min(src.shape[1], n - at)
    if m <= 0:
        return
    dst[:, at:at + m] += src[:, :m] * gain


def _loop_to(src: np.ndarray, n: int) -> np.ndarray:
    if src.shape[1] == 0 or n <= 0:
        return np.zeros((2, max(n, 0)), np.float32)
    reps = int(np.ceil(n / src.shape[1]))
    return np.tile(src, (1, reps))[:, :n]


def build_audio(tl: Timeline) -> np.ndarray:
    st = tl.settings
    total = int(tl.total * SR) + SR // 2
    mix = np.zeros((2, total), np.float32)
    cache: dict[str, np.ndarray] = {}

    def load(src: str):
        if not src:
            return np.zeros((2, 0), np.float32)
        if src not in cache:
            cache[src] = decode_audio(WEB / src)
        return cache[src]

    # --- BGM：PlayMusic → 下一个 StopMusic（或结尾），循环播放 ---
    events = tl.audio
    for i, e in enumerate(events):
        if e['kind'] != 'music':
            continue
        start = int(e['t'] * SR)
        end = total
        for e2 in events[i + 1:]:
            # 下一个 StopMusic 或 下一个 PlayMusic 都结束当前 BGM 段，
            # 避免同一首/换曲被当成新层叠加（网页里重播会先停再播）。
            if e2['kind'] in ('stopmusic', 'music'):
                end = int(e2['t'] * SR)
                break
        src = load(e.get('src') or '')
        seg = _loop_to(src, end - start)
        if seg.shape[1] == 0:
            continue
        # 淡入 / 淡出（网页 fadeTo 用 FadeTime，0 表示瞬间）
        fade = max(e.get('fade') or 0, 0.6)
        nf = int(fade * SR)
        if nf > 0 and seg.shape[1] > 2 * nf:
            ramp = np.linspace(0, 1, nf, dtype=np.float32)
            seg[:, :nf] *= ramp
            seg[:, -nf:] *= ramp[::-1]
        _add(mix, seg, start, st['bgmVol'])

    # --- 循环音效 ---
    for i, e in enumerate(events):
        if e['kind'] != 'loopse':
            continue
        start = int(e['t'] * SR)
        end = total
        for e2 in events[i + 1:]:
            if e2['kind'] == 'stoploopse':
                end = int(e2['t'] * SR)
                break
        _add(mix, _loop_to(load(e.get('src') or ''), end - start), start, st['seVol'])

    # --- 单次音效 ---
    for e in events:
        if e['kind'] == 'se':
            _add(mix, load(e.get('src') or ''), int(e['t'] * SR), st['seVol'])

    # --- 语音 ---
    for l in tl.lines:
        if not l['voice']:
            continue
        v = WEB / 'media' / 'audio' / str(tl.char_id) / f'{l["voice"]}.m4a'
        if not v.exists():
            continue
        key = str(v)
        if key not in cache:
            cache[key] = decode_audio(v)
        _add(mix, cache[key], int(l['start'] * SR), st['voiceVol'])

    peak = float(np.abs(mix).max()) if mix.size else 0.0
    if peak > 0.99:
        mix *= 0.99 / peak
    return mix


_SRGB2_709_LUT = None


def _srgb_to_709_lut() -> 'np.ndarray':
    """sRGB(0-255) → BT.709 OETF 编码值(0-255) 的 256 项查找表。

    截图是 sRGB；而视频按 BT.709 的传递函数编码/解码。若把 sRGB 值直接当 709 值，
    播放器会用 709 EOTF 还原、再按 sRGB 显示，中间调被抬亮（约 +12/255）、整片发灰。
    先把 sRGB 值经 sRGB EOTF → 线性 → 709 OETF 转成 709 值，播放端反向还原即与网页一致。
    """
    global _SRGB2_709_LUT
    if _SRGB2_709_LUT is None:
        x = np.arange(256, dtype=np.float32) / 255.0
        lin = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
        v = np.where(lin < 0.018, 4.5 * lin,
                     1.099 * np.power(np.maximum(lin, 1e-8), 0.45) - 0.099)
        _SRGB2_709_LUT = np.clip(v * 255.0, 0, 255).astype(np.float32)
    return _SRGB2_709_LUT


def rgb_to_yuv420p(rgb: 'np.ndarray', full: bool = True) -> 'np.ndarray':
    """RGB(uint8 HxWx3) → YUV420P(BT.709) 的连续内存数组，PyAV from_ndarray 可直接用。

    关键：yuv420p 在内存中总行数是 H*3/2（Y 占 H 行，U、V 各占 H/4 行，
    按「连续字节」重新排成 W 列宽），不是 H + H/2 + H/2 行。用一维拼接后 reshape 才正确；
    用 vstack([Y,U,V]) 会得到 2H 行 → 平面错位（画面全绿）。
    PyAV 18 的 reformat() 不接受 colorspace 参数，默认 RGB→YUV 用 BT.601 会偏黄，
    故这里手动做 BT.709 转换。

    full=True 用 full range（0-255，JPEG range），保留暗部/亮部全部细节，最接近浏览器 sRGB；
    full=False 用 limited range（16-235），兼容性最好但会夹掉两头细节。
    """
    H, W = rgb.shape[:2]
    # 注：用 float32 而非 int32 —— 实测 int32 更慢（4 字节/元素，内存带宽翻倍），
    # 1080p 上 float32 快约 2 倍。
    # 实测（Chromium headless 播放对照）：不要做 sRGB→709 transfer 转换。
    # Chromium 播放 mp4 时按 YUV→RGB 直接还原，不做 transfer；若加上反而整体偏暗（MAE 1.5→10.4）。
    r = rgb[..., 0].astype(np.float32)
    g = rgb[..., 1].astype(np.float32)
    b = rgb[..., 2].astype(np.float32)
    yv = 0.2126 * r + 0.7152 * g + 0.0722 * b
    uv = -0.1146 * r - 0.3854 * g + 0.5 * b
    vv = 0.5 * r - 0.4542 * g - 0.0458 * b
    if full:
        y8 = np.clip(yv, 0, 255).round().astype(np.uint8)
        u = np.clip(128.0 + uv, 0, 255)
        v = np.clip(128.0 + vv, 0, 255)
    else:
        y8 = np.clip(16.0 + (219.0 / 255.0) * yv, 16, 235).round().astype(np.uint8)
        u = np.clip(128.0 + (224.0 / 255.0) * uv, 16, 240)
        v = np.clip(128.0 + (224.0 / 255.0) * vv, 16, 240)
    ua = u.reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3)).round().astype(np.uint8)
    va = v.reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3)).round().astype(np.uint8)
    yuv = np.concatenate([y8.ravel(), ua.ravel(), va.ravel()])
    return yuv.reshape(H * 3 // 2, W)


def encode(tl: Timeline, frames_dir: Path, out_path: Path, label: str = '', limit: int | None = None):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if limit and limit < len(tl.lines):
        import copy
        sub = copy.copy(tl)
        sub.lines = tl.lines[:limit]
        sub.total = tl.lines[limit - 1]['end']
        sub.audio = [e for e in tl.audio if e['t'] < sub.total]
        tl = sub
    audio = build_audio(tl)
    total_samples = audio.shape[1]

    container = av.open(str(out_path), 'w')
    vs = container.add_stream('libx264', rate=FPS)
    vs.width, vs.height = W, H
    vs.pix_fmt = 'yuv420p'
    # 色彩：截图是 sRGB（≈BT.709 矩阵）。PyAV 的 reformat 不支持 colorspace 参数，
    # 故手动做 RGB→YUV709(limited) 转换；并写入 VUI 元数据，避免播放器按 601 解码偏黄。
    try:
        vs.colorspace = 1      # BT.709
        vs.color_primaries = 1  # BT.709
        vs.color_trc = 1        # BT.709
        vs.color_range = 2      # JPEG (full range) —— 与浏览器 sRGB 显示最接近
    except Exception:
        pass
    vs.options = {'crf': '18', 'preset': 'veryfast', 'tune': 'stillimage'}
    aus = container.add_stream('aac', rate=SR)
    aus.bit_rate = 192000
    aus.layout = 'stereo'

    # 预加载帧
    imgs: list[Image.Image] = []
    for l in tl.lines:
        p = frames_dir / f'{l["idx"]:04d}.png'
        if not p.exists():
            raise FileNotFoundError(f'缺帧 {p}')
        imgs.append(Image.open(p).convert('RGB').resize((W, H), Image.LANCZOS))

    def audio_chunk(i0: int, n: int):
        blk = audio[:, i0:i0 + n]
        if blk.shape[1] < n:
            blk = np.pad(blk, ((0, 0), (0, n - blk.shape[1])))
        fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(blk, dtype=np.float32),
                                        format='fltp', layout='stereo')
        fr.sample_rate = SR
        fr.pts = i0
        for pkt in aus.encode(fr):
            container.mux(pkt)

    vin = 0
    apos = 0
    CHUNK = SR // 2          # 每 0.5s 交一块音频
    prev = None
    for li, l in enumerate(tl.lines):
        img = imgs[li]
        nfr = max(1, int(round(l['dur'] * FPS)))
        yuv_base = None  # 同一句的静止画面只做一次 RGB→YUV 转换，重复帧直接复用
        for k in range(nfr):
            if prev is not None and k < DISSOLVE and li > 0:
                a = (k + 1) / (DISSOLVE + 1)
                arr = rgb_to_yuv420p(np.asarray(Image.blend(prev, img, a).convert('RGB'), dtype=np.uint8))
            else:
                if yuv_base is None:
                    yuv_base = rgb_to_yuv420p(np.asarray(img.convert('RGB'), dtype=np.uint8))
                arr = yuv_base
            vf = av.VideoFrame.from_ndarray(arr, format='yuv420p')
            vf.pts = vin
            vin += 1
            for pkt in vs.encode(vf):
                container.mux(pkt)
            # 音频按时间推进，保持交错
            target = int(vin / FPS * SR)
            while apos + CHUNK <= min(target, total_samples):
                audio_chunk(apos, CHUNK)
                apos += CHUNK
        prev = img

    # 收尾：剩余音频
    while apos < total_samples:
        n = min(CHUNK, total_samples - apos)
        audio_chunk(apos, n)
        apos += n
    for pkt in vs.encode(None):
        container.mux(pkt)
    for pkt in aus.encode(None):
        container.mux(pkt)
    container.close()
    dur = vin / FPS
    print(f'{label}: {out_path.name}  {W}x{H} {FPS}fps  {dur:.1f}s  '
          f'{out_path.stat().st_size / 1048576:.1f}MB')
    return out_path


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    cid = int(sys.argv[1]) if len(sys.argv) > 1 else 78
    arg = sys.argv[2] if len(sys.argv) > 2 else '1'
    parts = [int(x) for x in arg.split(',') if x]
    which = sys.argv[3] if len(sys.argv) > 3 else 'web'
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else None
    tl = Timeline(cid, parts)
    name = 'haruka' if cid == 78 else f'char{cid}'
    seg = f'p{parts[0]}' if parts[0] == parts[-1] else f'p{parts[0]}-{parts[-1]}'
    out_name = f'{name}_{seg}_{which}.mp4'
    if limit:
        out_name = out_name.replace('.mp4', f'_test{limit}.mp4')
    encode(tl, Path(__file__).parent / 'out' / f'frames_{which}',
           Path(__file__).parent / 'out' / out_name, which, limit=limit)
