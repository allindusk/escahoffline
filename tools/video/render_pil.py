"""方案 B：纯 Python（Pillow）复刻播放器画面逐句渲染 → 帧。

尽量还原 web/player.css 的排版与角色对话框、立绘定位、背景/CG 规则。
输出 _video/out/frames_pil/<idx>.png（1920x1080）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from story import WEB, POS_X, Timeline

W, H = 1920, 1080
S = 1.5                 # stage(1280x720) → 输出(1920x1080)
FRAMES = Path(__file__).parent / 'out' / 'frames_pil'

FONTS = {
    'sans': (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\msyhbd.ttc'),
    'serif': (r'C:\Windows\Fonts\simsun.ttc', r'C:\Windows\Fonts\simsun.ttc'),
    'yu': (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\msyhbd.ttc'),
}


def _font_path(bold: bool):
    fam = FONT_PATHS
    p = fam[1] if bold else fam[0]
    if p and Path(p).exists():
        return p
    return 'arial.ttf'


FONT_PATHS = (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\msyhbd.ttc')

_cache_font = {}


def font(px: int, bold: bool):
    key = (px, bold)
    if key in _cache_font:
        return _cache_font[key]
    try:
        f = ImageFont.truetype(_font_path(bold), px)
    except Exception:
        f = ImageFont.load_default()
    _cache_font[key] = f
    return f


def hex2rgb(h: str):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def parse_color(c):
    c = str(c)
    if c.startswith('#'):
        return hex2rgb(c)
    return tuple(int(p) for p in c.split(','))


def cover(img: Image.Image):
    iw, ih = img.size
    r = max(W / iw, H / ih)
    img = img.resize((int(iw * r + 0.5), int(ih * r + 0.5)), Image.LANCZOS)
    return img.crop(((img.width - W) // 2, (img.height - H) // 2,
                     (img.width + W) // 2, (img.height + H) // 2))


def contain(img: Image.Image, canvas: Image.Image):
    iw, ih = img.size
    r = min(W / iw, H / ih)
    img = img.resize((int(iw * r + 0.5), int(ih * r + 0.5)), Image.LANCZOS)
    canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))


def make_shadow(alpha: Image.Image, blur: float, strength: float):
    sh = Image.new('L', alpha.size, 0)
    sh.paste(alpha, (0, 0))
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    return sh.point(lambda v: int(v * strength))


class Painter:
    def __init__(self, tl: Timeline):
        self.tl = tl
        self.st = tl.settings
        self.fnt_path, self.fnt_bold = self._pick_fonts()

    def _pick_fonts(self):
        fam = self.st.get('msgFont', 'sans')
        reg, bold = FONTS.get(fam, FONTS['sans'])
        if not Path(reg).exists():
            reg, bold = 'arial.ttf', 'arial.ttf'
        return reg, bold

    # ---- 立绘 ----
    _stand_cache: dict = {}

    def stand_img(self, img_id: str):
        rel = self.tl.imgmap['stand'].get(img_id)
        if not rel:
            return None
        c = getattr(self, '_raw_cache', None)
        if c is None:
            c = self._raw_cache = {}
        if img_id in c:
            return c[img_id]
        im = Image.open(WEB / rel).convert('RGBA')
        c[img_id] = im
        return im

    def draw_stand(self, canvas: Image.Image, img_id, scale, pos, ox):
        st_img = self.stand_img(img_id)
        if st_img is None:
            return
        iw, ih = st_img.size
        max_h = min(720 * self.st['standH'] / 100, 720) * S
        max_w = 1280 * S
        r = min(1.0, max_h / ih, max_w / iw)
        nw, nh = max(1, round(iw * r * scale)), max(1, round(ih * r * scale))
        cache_key = (img_id, nw, nh)
        small = self._stand_cache.get(cache_key)
        if small is None:
            small = st_img.resize((nw, nh), Image.LANCZOS)
            self._stand_cache[cache_key] = small
        alpha = small.split()[-1]
        # 投影
        sh = Image.new('RGBA', canvas.size, (0, 0, 0, 0))
        shadow = make_shadow(alpha, 26 * S / 2, 0.75)
        sh.paste(Image.new('RGBA', shadow.size, (0, 0, 0, 0)), (0, 0), shadow)
        cx = POS_X.get(pos, 0.5) * W + ox * S
        by = H - self.st['standY'] * S
        left = int(cx - nw / 2)
        top = int(by - nh)
        canvas.alpha_composite(sh, (left, int(top + 6 * S)))
        canvas.alpha_composite(small, (left, top))

    # ---- 文本排版 ----
    def wrap(self, text: str, fnt, max_w: int):
        lines = []
        for para in text.split('\n'):
            if not para:
                lines.append('')
                continue
            cur, cur_w = '', 0
            for ch in para:
                w = fnt.getlength(ch)
                if cur and cur_w + w > max_w:
                    lines.append(cur)
                    cur, cur_w = ch, w
                else:
                    cur += ch
                    cur_w += w
            lines.append(cur)
        return lines

    def draw_window(self, canvas: Image.Image, name: str, text: str):
        d = ImageDraw.Draw(canvas, 'RGBA')
        fs = round(self.st['fs'] * S)
        lh = fs * self.st['msgLH']
        pad_x = 26 * S
        pad_y = 20 * S
        rad = 10 * S
        x0, x1 = 52 * S, W - 52 * S
        y1 = H - 26 * S
        fnt = font(fs, False)
        wrapped = self.wrap(text, fnt, (x1 - x0 - 2 * pad_x))
        text_h = max(96 * S, len(wrapped) * lh)
        box_h = max(150 * S, pad_y + text_h + 18 * S)
        y0 = y1 - box_h
        bg = parse_color(self.st['msgBg'])
        alpha = int(255 * self.st['alpha'] / 100)
        border = (224, 181, 102, 107)
        d.rounded_rectangle([x0, y0, x1, y1], radius=rad,
                            fill=(bg[0], bg[1], bg[2], alpha), outline=border, width=round(S))

        # 名字标签
        if name:
            nf = font(round(15 * S), True)
            nb = d.textbbox((0, 0), name, font=nf)
            tw, th = nb[2] - nb[0], nb[3] - nb[1]
            nw_, nh_ = int(tw + 32 * S), int(th + 4 * S)
            nx = int(x0 + 22 * S + self.st['nameX'] * S)
            ny = int(y0 - 15 * S + self.st['nameY'] * S)
            grad = Image.new('RGB', (nw_, nh_), (240, 208, 144))
            gl = np.asarray(grad).astype(float)
            gl[:, :] = gl[:, :] * np.linspace(1.0, 0.83, nw_)[None, :, None]
            gimg = Image.fromarray(gl.astype('uint8')).convert('RGBA')
            mask = Image.new('L', (nw_, nh_), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, nw_ - 1, nh_ - 1],
                                                  radius=int(5 * S), fill=255)
            label = Image.new('RGBA', (nw_, nh_), (0, 0, 0, 0))
            label.paste(gimg, (0, 0), mask)
            canvas.alpha_composite(label, (nx, ny))
            d.text((int(nx + 16 * S), int(ny + 2 * S - nb[1])), name, font=nf,
                   fill=(26, 18, 6), anchor='lt')

        # 正文（四向黑描边近似）
        sw = max(1, round(self.st['msgStroke'] * S))
        msg = parse_color(self.st['msgColor'])
        tx = x0 + pad_x + self.st['msgX'] * S
        ty = y0 + pad_y
        for ln in wrapped:
            d.text((tx, ty), ln, font=fnt, fill=msg, stroke_width=sw,
                   stroke_fill=(0, 0, 0), anchor='lt')
            ty += lh

    def render(self, line: dict) -> Image.Image:
        canvas = Image.new('RGBA', (W, H), (5, 6, 10, 255))
        st = line['state']
        # 背景
        if st['bg2']:
            for i, bid in enumerate([st['bg2'][0], st['bg2'][1]]):
                img = self.tl.imgmap['back'].get(bid)
                if img:
                    im = cover(Image.open(WEB / img).convert('RGBA'))
                    half = im.crop((0, 0, W // 2, H)) if i == 0 else im.crop((W // 2, 0, W, H))
                    canvas.paste(half, (0 if i == 0 else W // 2, 0))
        elif st['bg']:
            b = self.tl.imgmap['back'].get(st['bg'])
            if b:
                canvas = cover(Image.open(WEB / b).convert('RGBA'))
        # 立绘（按 z 升序）
        for s in sorted(line['state']['stands'], key=lambda v: v['z']):
            n = len(line['state']['stands'])
            sc = 1.0 if n <= 1 else (0.86 if n == 2 else 0.76)
            self.draw_stand(canvas, s['img'], sc, s['pos'], self.st['standOX'])
        # CG
        if st['cg']:
            c = self.tl.imgmap['cg'].get(st['cg'])
            if c:
                contain(Image.open(WEB / c).convert('RGBA'), canvas)
        # 对话框
        if self.st['showWin']:
            self.draw_window(canvas, line['name'], line['text'])
        # 滤镜
        if st['draw'] == 'gray':
            canvas = canvas.convert('L').convert('RGB')
        elif st['draw'] == 'sepia':
            a = np.array(canvas).astype(float)
            r, g, b = a[..., 0], a[..., 1], a[..., 2]
            nr = np.clip(0.393 * r + 0.769 * g + 0.189 * b, 0, 255)
            ng = np.clip(0.349 * r + 0.686 * g + 0.168 * b, 0, 255)
            nb = np.clip(0.272 * r + 0.534 * g + 0.131 * b, 0, 255)
            canvas = Image.fromarray(np.dstack([nr, ng, nb]).astype('uint8'))
        return canvas.convert('RGB')

    def save_frames(self, limit=0):
        FRAMES.mkdir(parents=True, exist_ok=True)
        lines = self.tl.lines[:limit] if limit else self.tl.lines
        for l in lines:
            self.render(l).save(FRAMES / f'{l["idx"]:04d}.png')
        return len(lines)


def selfcheck(path: Path):
    """像素级自检：确认对话框有文字、画面非全黑（无需肉眼看图）。"""
    img = np.array(Image.open(path).convert('RGB')).astype(int)
    nonblack = (img.sum(2) > 30).mean()
    # 对话框区域（底部）
    y0 = int(H - 220 * S)
    win = img[y0:H, int(40 * S):int(W - 40 * S)]
    msgc = hex2rgb(DEFAULTS['msgColor'])
    d = np.abs(win - np.array(msgc)).sum(2)
    lit = (d < 70).sum()
    # 中部立绘是否彩色：方差
    mid = img[H // 2 - 200:H // 2 + 200, W // 2 - 300:W // 2 + 300]
    colorful = mid.std()
    print(f'{path.name}: nonblack={nonblack:.2%} 文字像素={lit} 中部色彩σ={colorful:.1f}')
    return nonblack > 0.8 and lit > 200


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    from story import DEFAULTS
    cid = int(sys.argv[1]) if len(sys.argv) > 1 else 78
    pidx = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    lim = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    tl = Timeline(cid, pidx - 1)
    p = Painter(tl)
    n = p.save_frames(lim)
    print(f'saved {n} frames -> {FRAMES}')
    if lim:
        for i in range(min(lim, n)):
            selfcheck(FRAMES / f'{i:04d}.png')
