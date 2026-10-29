"""成品自检：抽帧 + 音频能量分布，确认画面/声音都真的在。"""
import sys
from pathlib import Path

import av
import numpy as np

sys.stdout.reconfigure(encoding='utf-8')


def main():
    p = Path(sys.argv[1])
    if not p.is_absolute():
        p = Path(__file__).parent / p
    tag = p.stem
    out = Path(__file__).parent / 'out'
    shots = [float(x) for x in (sys.argv[2].split(',') if len(sys.argv) > 2 else ['3', '22', '45'])]

    with av.open(str(p)) as ct:
        vs = ct.streams.video[0]
        dur = float(ct.duration or 0) / 1e6
        print(f'{p.name}: {vs.width}x{vs.height} {vs.average_rate} dur={dur:.2f}s '
              f'streams={[s.type for s in ct.streams]} size={p.stat().st_size/1048576:.1f}MB')
        for t in shots:
            ct.seek(int(t / vs.time_base), stream=vs)
            for fr in ct.decode(vs):
                img = fr.to_image()
                f = out / f'check_{tag}_{int(t)}s.png'
                img.save(f)
                print('  frame', t, '->', f.name)
                break

    with av.open(str(p)) as ct:
        rs = av.AudioResampler(format='flt', layout='mono', rate=8000)
        buf = []
        for fr in ct.decode(audio=0):
            for rf in rs.resample(fr):
                buf.append(rf.to_ndarray().reshape(-1))
        for rf in rs.resample(None):
            buf.append(rf.to_ndarray().reshape(-1))
        a = np.concatenate(buf) if buf else np.zeros(1, np.float32)
    print(f'  audio {a.shape[0]/8000:.1f}s peak={np.abs(a).max():.3f}')
    step = int(8000 * 5)
    rms = [float(np.sqrt(np.mean(a[i:i + step] ** 2))) for i in range(0, len(a), step)]
    print('  每5s RMS:', ' '.join(f'{r:.3f}' for r in rms))


if __name__ == '__main__':
    main()
