import sys
sys.stdout.reconfigure(encoding='utf-8')
import av
import numpy as np
from PIL import Image
from pathlib import Path
from story import Timeline
from encode import rgb_to_yuv420p

OUT = Path(__file__).parent / 'out'
FLAGS = {a for a in sys.argv[1:] if a.startswith('--')}
ARGS = [a for a in sys.argv[1:] if not a.startswith('--')]
CHROMIUM = '--chromium' in FLAGS


def to_srgb_from_709(rgb255):
    """模拟 Chromium 对 bt709 视频的色彩管理：709 EOTF → 线性 → sRGB OETF。"""
    v = np.clip(rgb255, 0, 255) / 255.0
    lin = np.where(v < 0.081, v / 4.5, np.power((v + 0.099) / 1.099, 1.0 / 0.45))
    s = np.where(lin <= 0.0031308, 12.92 * lin,
                 1.055 * np.power(np.maximum(lin, 1e-8), 1.0 / 2.4) - 0.055)
    return np.clip(s * 255.0, 0, 255)


if '--matrix' in FLAGS:
    L = rgb_to_yuv420p(np.zeros((4, 4, 3), np.uint8) + (255, 0, 0))
    print('纯红 Y =', int(L[0, 0]), '（709 full 期望 ≈54；601 为 ≈76）')
    sys.exit(0)

p = Path(ARGS[0]) if ARGS else OUT / 'haruka_p4_web.mp4'
if not p.is_absolute():
    p = OUT / p
_parts = [int(x) for x in ARGS[1].split(',')] if len(ARGS) > 1 else [4]
tl = Timeline(78, _parts)
FPS = 25
vin = 0
frame_of = {}
for l in tl.lines:
    frame_of[l['idx']] = vin
    vin += max(1, int(round(l['dur'] * FPS)))

idxs = [l['idx'] for l in tl.lines[:4]]
# 句首 3 帧是与上一句的交叉溶解，比对时要跳到句内稳定帧
DISSOLVE = 3
want = {frame_of[i] + (DISSOLVE if frame_of[i] > 0 else 0): i for i in idxs}
c = av.open(str(p))
vs = c.streams.video[0]
print('== VUI ==', {k: getattr(vs.codec_context, k, None) for k in
                    ('colorspace', 'color_primaries', 'color_trc', 'color_range')},
      '| 模拟Chromium=' + ('on' if CHROMIUM else 'off'))
got = {}
cnt = 0
for f in c.decode(vs):
    if cnt in want:
        got[cnt] = np.asarray(f.to_image().convert('RGB'), dtype=float)
    cnt += 1
    if len(got) == len(want):
        break

for fc, idx in want.items():
    if fc not in got:
        print(f'idx{idx:03d}: 未匹配')
        continue
    src = np.asarray(Image.open(OUT / 'frames_web' / f'{idx:04d}.png').convert('RGB'), dtype=float)
    dec = got[fc]
    if dec.shape != src.shape:
        dec = np.asarray(Image.fromarray(dec.astype('uint8')).resize(src.shape[1::-1][::-1]), dtype=float)
    if CHROMIUM:
        dec = to_srgb_from_709(dec)
    mae = np.abs(src - dec).mean()
    per = [round(float(np.abs(src[..., k] - dec[..., k]).mean()), 2) for k in range(3)]
    print(f'idx{idx:03d}  MAE={mae:.2f}  R/G/B={per}')
