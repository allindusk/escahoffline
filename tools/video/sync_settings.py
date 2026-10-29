"""把播放器设置 escah_set 同步到本地 player_settings.json。

优先级：① 连接运行中的浏览器(CDP) → ② 扫描本机浏览器磁盘存储(leveldb/sqlite)。
写完后 render_browser 会用 add_init_script 把这份本地配置注入截图用的页面，
保证「本地配置 == 浏览器保存的设置」。以后浏览器里改了设置，重跑本脚本即可拉回。
"""
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
VIDEO_DIR = Path(__file__).resolve().parent
OUT = VIDEO_DIR / 'player_settings.json'
CDP_PORTS = (9222, 9223, 9229, 9333, 9220)


def try_cdp():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    for port in CDP_PORTS:
        try:
            with sync_playwright() as p:
                b = p.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
                for ctx in b.contexts:
                    for pg in ctx.pages:
                        try:
                            if '8000' in pg.url:
                                val = pg.evaluate("() => localStorage.getItem('escah_set')")
                                if val:
                                    b.close()
                                    return json.loads(val), f'CDP:{port} {pg.url}'
                        except Exception:
                            pass
                b.close()
        except Exception:
            pass
    return None


def try_disk():
    roots = [Path(os.environ['APPDATA']), Path(os.environ['LOCALAPPDATA'])]
    best = {}
    for root in roots:
        for dp, _, fs in os.walk(root):
            if dp.count(os.sep) - str(root).count(os.sep) > 5:
                continue
            for fn in fs:
                if not fn.endswith(('.log', '.ldb', '.sqlite', '.sqlite3', '.db')):
                    continue
                p = Path(dp) / fn
                try:
                    raw = p.read_bytes()
                except OSError:
                    continue
                for m in re.finditer(rb'"alpha"', raw):
                    start = raw.rfind(b'{', max(0, m.start() - 500), m.start() + 8)
                    if start < 0:
                        continue
                    depth = 0
                    end = -1
                    for j in range(start, min(len(raw), start + 4000)):
                        if raw[j] == 0x7b:
                            depth += 1
                        elif raw[j] == 0x7d:
                            depth -= 1
                            if depth == 0:
                                end = j + 1
                                break
                    if end < 0:
                        continue
                    try:
                        obj = json.loads(raw[start:end].decode('latin-1'))
                    except Exception:
                        continue
                    if 'msgBg' in obj or 'bgmVol' in obj:
                        if m.start() > best.get('_pos', 0):
                            best = dict(obj, _pos=m.start(), _src=f'{p}')
    best.pop('_pos', None)
    return (best.get('_src') and best.pop('_src')), (best or None)


def main():
    src, obj = try_cdp() or (None, None)
    if obj is None:
        src, obj = try_disk()
    if not obj:
        print('未找到运行中的浏览器，也没扫到 escah_set。保留现有 player_settings.json（即你贴的那份）。')
        return
    # 仅保留播放器认识的键，避免脏数据
    keys = {'alpha', 'fs', 'typeRate', 'showWin', 'standH', 'bgmVol', 'seVol', 'voiceVol',
            'fit', 'instantText', 'pausePerChar', 'pauseBase', 'pauseAfterVoice', 'showPartial',
            'sortBy', 'rarSel', 'standY', 'standOX', 'msgBg', 'msgColor', 'msgFont',
            'msgLH', 'msgStroke', 'msgX', 'msgAlign', 'nameX', 'nameY', 'nameAlign'}
    obj = {k: v for k, v in obj.items() if k in keys}
    OUT.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'已写入 {OUT}  （来源: {src}）')
    print(json.dumps(obj, ensure_ascii=False))


if __name__ == '__main__':
    main()
