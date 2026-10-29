"""把 char_<id>.js 的指令序列解析成「每句一台词 + 画面状态快照 + 音频事件」的时间轴。

支持多段合并：传入 parts=[1,2,3]，各段状态机独立重置，时间轴首尾相接。
两个渲染器（浏览器截图 / Pillow 自绘）共用这份时间轴。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

VIDEO_DIR = Path(__file__).resolve().parent
# 兼容两种位置：原来在 <根>/_video/，现在在 <根>/tools/video/。
# 一律以"含 web/ 的那一层"为项目根，免得挪目录就把路径弄错。
ROOT = VIDEO_DIR.parent
if not (ROOT / 'web').is_dir() and (ROOT.parent / 'web').is_dir():
    ROOT = ROOT.parent
WEB = ROOT / 'web'

RICH_TAG = re.compile(
    r'</?(?:sprite|color|size|material|font|b|i|u|s|ruby|quad|link|mark|style|align|'
    r'cspace|mspace|indent|pos|space|alpha|voffset|lowercase|uppercase|smallcaps|nobr|page)'
    r'(?:=[^<>]*)?>', re.I)

DEFAULTS = {
    'alpha': 90, 'fs': 21, 'typeRate': 1, 'showWin': True, 'standH': 100,
    'bgmVol': 0.7, 'seVol': 0.9, 'voiceVol': 1, 'fit': '1x',
    'instantText': False, 'pausePerChar': 90, 'pauseBase': 400,
    'pauseAfterVoice': 250, 'showPartial': False,
    'standY': 0, 'standOX': 0, 'msgBg': '10,14,22', 'msgColor': '#f2f5fb',
    'msgFont': 'sans', 'msgLH': 1.62, 'msgStroke': 1.2,
    'msgX': 0, 'msgAlign': 'left',
    'nameX': 0, 'nameY': 0, 'nameAlign': 'left',
}

POS_X = {'Left': 0.23, 'Center': 0.50, 'Right': 0.77, 'All': 0.50}
Z_VAL = {'Back': 1, 'Middle': 3, 'Front': 5}


def load_settings() -> dict:
    s = dict(DEFAULTS)
    f = VIDEO_DIR / 'player_settings.json'
    if f.exists():
        s.update(json.loads(f.read_text('utf-8')))
    return s


def load_js_json(path: Path):
    txt = path.read_text('utf-8')
    return json.loads(txt[txt.index('=') + 1:].strip().rstrip(';'))


def clean_text(s: str) -> str:
    return RICH_TAG.sub('', '' if s is None else str(s))


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


class Timeline:
    def __init__(self, char_id: int, parts, lang: str = 'zh-Hans'):
        if isinstance(parts, int):
            parts = [parts]
        self.char_id = char_id
        self.parts = parts
        self.lang = lang
        self.settings = load_settings()
        char = load_js_json(WEB / 'data' / f'char_{char_id}.js')
        self.all_parts = char['parts']
        self.imgmap = load_js_json(WEB / 'data' / 'imgmap.js')
        self.sfxmap = load_js_json(WEB / 'data' / 'sfxmap.js')
        self.zh = {}
        zf = WEB / 'assets' / 'i18n' / lang / f'char_{char_id}.json'
        if zf.exists():
            self.zh = json.loads(zf.read_text('utf-8')).get('messages', {})
        self.lines: list[dict] = []
        self.audio: list[dict] = []
        self.total = 0.0
        for pi in parts:
            self._build_part(pi - 1)

    def text_of(self, m) -> str:
        ja = m.get('Message') or ''
        return clean_text(self.zh.get(ja, ja))

    def _build_part(self, pi: int):
        methods = self.all_parts[pi]['methods']
        st = {'bg': None, 'bg2': None, 'cg': None, 'stands': {}, 'draw': ''}
        cur_name = ''
        voice = None
        t = self.total
        part_audio = []

        for i, m in enumerate(methods):
            M = m.get('Method')
            if M == 'ShowBackground':
                st['bg'], st['bg2'] = m.get('BackImage'), None
            elif M == 'HideBackground':
                st['bg'] = None
            elif M in ('ShowDoubleBackground', 'ShowDoubleBackgroundOffset'):
                st['bg'], st['bg2'] = m.get('LBackImage'), m.get('RBackImage')
            elif M in ('ShowStand', 'ShowStandAndSit'):
                pos = m.get('Position') or 'Center'
                gid = m.get('GroupId') or ''
                cur = st['stands'].get(pos)
                if cur and cur['gid'] != gid:
                    st['stands'].pop(pos, None)
                for k in [k for k, v in st['stands'].items() if k != pos and gid and v['gid'] == gid]:
                    st['stands'].pop(k, None)
                st['stands'][pos] = {
                    'img': m.get('StandImage') or '', 'gid': gid, 'pos': pos,
                    'z': Z_VAL.get(m.get('Z'), 3), 'motion': m.get('Motion') or 'None',
                }
            elif M == 'HideStand':
                pos = m.get('Position')
                if not pos or pos == 'All':
                    st['stands'].clear()
                else:
                    st['stands'].pop(pos, None)
            elif M == 'MoveStand':
                f, to = m.get('PositionFrom') or 'Center', m.get('PositionTo') or 'Center'
                if f in st['stands']:
                    v = st['stands'].pop(f)
                    v['pos'] = to
                    st['stands'][to] = v
            elif M == 'ShowEventCg':
                st['cg'] = m.get('EventImage')
            elif M == 'HideEventCg':
                st['cg'] = None
            elif M == 'SetDrawModeSepia':
                st['draw'] = 'sepia'
            elif M == 'SetDrawModeGrayscale':
                st['draw'] = 'gray'
            elif M == 'SetDrawModeDefault':
                st['draw'] = ''
            elif M == 'ShowMessageWindow':
                cur_name = m.get('Name') or ''
            elif M == 'VOICE':
                voice = m.get('Voice') or None
            elif M == 'PlayMusic':
                part_audio.append({'kind': 'music', 't': t, 'key': m.get('Music'),
                                   'fade': _num(m.get('FadeTime'), .6), 'src': self.sfxmap.get(m.get('Music'))})
            elif M == 'StopMusic':
                part_audio.append({'kind': 'stopmusic', 't': t, 'fade': _num(m.get('FadeTime'), .6)})
            elif M == 'PlaySound':
                part_audio.append({'kind': 'se', 't': t, 'src': self.sfxmap.get(m.get('Sound')), 'vol': 1.0})
            elif M == 'PlayLoopSound':
                part_audio.append({'kind': 'loopse', 't': t, 'src': self.sfxmap.get(m.get('Sound'))})
            elif M == 'StopLoopSound':
                part_audio.append({'kind': 'stoploopse', 't': t})
            elif M == 'ShowMessage':
                txt = self.text_of(m)
                vdur = self.voice_duration(voice) if voice else 0.0
                if vdur > 0:
                    dur = vdur + self.settings['pauseAfterVoice'] / 1000
                else:
                    dur = self.settings['pauseBase'] / 1000 + len(txt) * self.settings['pausePerChar'] / 1000
                self.lines.append({
                    'idx': len(self.lines), 'part': pi + 1, 'midx': i,
                    'name': cur_name, 'text': txt, 'voice': voice,
                    'voice_dur': vdur, 'dur': round(dur, 3),
                    'start': round(t, 3), 'end': round(t + dur, 3),
                    'state': {'bg': st['bg'], 'bg2': st['bg2'], 'cg': st['cg'], 'draw': st['draw'],
                              'stands': [dict(v) for v in st['stands'].values()]},
                })
                t += dur
                voice = None

        for e in part_audio:
            e['t'] = round(e['t'], 3)
            self.audio.append(e)
        self.total = round(t, 3)

    _vdur_cache: dict = {}

    def voice_duration(self, vid: str) -> float:
        if not vid:
            return 0.0
        c = self._vdur_cache
        if vid in c:
            return c[vid]
        import av
        p = WEB / 'media' / 'audio' / str(self.char_id) / f'{vid}.m4a'
        d = 0.0
        if p.exists():
            try:
                with av.open(str(p)) as ct:
                    a = ct.streams.audio[0]
                    d = float(ct.duration or 0) / 1_000_000 or float(a.duration * a.time_base)
            except Exception:
                d = 0.0
        c[vid] = d
        return d

    def summary(self):
        zh_like = sum(1 for l in self.lines if l['text'] and not re.search(r'[\u3040-\u30ff]', l['text']))
        return {
            'char': self.char_id, 'parts': self.parts,
            'lines': len(self.lines),
            'voice_lines': sum(1 for l in self.lines if l['voice']),
            'total_sec': self.total, 'total_hms': f'{int(self.total // 60)}:{int(self.total % 60):02d}',
            'zh_like': zh_like, 'audio_events': len(self.audio),
        }

    def dump(self, path: Path):
        path.write_text(json.dumps({
            'meta': self.summary(), 'settings': self.settings,
            'audio': self.audio, 'lines': self.lines,
        }, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    cid = int(sys.argv[1]) if len(sys.argv) > 1 else 78
    arg = sys.argv[2] if len(sys.argv) > 2 else '1'
    parts = [int(x) for x in arg.split(',') if x]
    tl = Timeline(cid, parts)
    print(json.dumps(tl.summary(), ensure_ascii=False, indent=1))
    print('per-part lines:', {p: sum(1 for l in tl.lines if l['part'] == p) for p in parts})
