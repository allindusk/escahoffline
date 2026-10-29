"""方案 A：headless Edge 驱动真实播放器，逐句定格 → 截 #stage。

- 用 add_init_script 把本地 player_settings.json 注入浏览器 localStorage，截图即采用你的设置。
- 支持多段合并：parts="1,2,3" 时各段首尾相接，帧按全局 idx 连续命名。
输出 _video/out/frames_web/<idx>.png（1920x1080，即 1280x720 舞台的 1.5 倍）
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import os

from story import VIDEO_DIR, Timeline

OUT = VIDEO_DIR / 'out'
FRAMES = OUT / 'frames_web'
LOG = OUT / 'render_web.log'
DSF = 1.5
#: 由调用方（server/videxport.py）通过环境变量传实际端口，避免和用户的服务撞端口
PORT = int(os.environ.get('ESC_PORT') or 8000)
WAIT_MS = 80            # 截图前的额外缓冲；paint 由 rAF×2 保证，这里只留很小余量

STAGE_FIX = """() => {
  const st = document.querySelector('#stage');
  document.body.appendChild(st);
  st.style.position = 'fixed';
  st.style.left = '0px'; st.style.top = '0px';
  st.style.transform = 'none';
  st.style.zIndex = '999999';
  Array.from(document.body.children).forEach(e => { if (e !== st) e.style.display = 'none'; });
  document.documentElement.style.overflow = 'hidden';
  document.body.style.margin = '0';
  document.body.style.background = '#000';
  const r = st.getBoundingClientRect();
  return [r.width, r.height];
}"""

RENDER = """(midx) => {
  haltLoop();
  resetStage();
  ST.fastForward = true;
  let prevMsg = -1;
  for (let i = midx - 1; i >= 0; i--) { if (ST.methods[i].Method === 'ShowMessage') { prevMsg = i; break; } }
  const fxFrom = prevMsg + 1;
  let winVisible = false, curName = '', toUpper = false, cgT = '', cgF = '';
  for (let i = 0; i < midx; i++) {
    const m = ST.methods[i], M = m.Method;
    if (REPLAY.has(M)) { try { replayOne(m); } catch (e) { } continue; }
    if (M === 'ShowMessageWindow') { winVisible = true; curName = m.Name || ''; }
    else if (M === 'HideMessageWindow') { winVisible = false; }
    else if (M === 'StareEventCG') { cgT = 'scale(1.12)'; }
    else if (M === 'EndStareEventCG') { cgT = 'scale(1)'; }
    else if (M === 'ZoomInEventCG' || M === 'FastZoomInEventCG') {
      cgT = 'scale(' + ((+m.Rate) || 1.3) + ') translate(' + ((+m.PosX) || 0) + 'px,' + ((+m.PosY) || 0) + 'px)';
      cgF = ((+m.Blur) > 0) ? ('blur(' + m.Blur + 'px)') : '';
    }
    else if (M === 'ZoomOutEventCG') { cgT = 'scale(1)'; cgF = ''; }
    else if (M === 'ToLower') { toUpper = false; }
    else if (M === 'SwapStand') {
      const a = m.Position0 || 'Left', b = m.Position1 || 'Center';
      const ea = ST.stands.get(a), eb = ST.stands.get(b);
      const put = (el, p) => { el.style.left = 'calc(' + (POS_X[p] || '50%') + ' + var(--standOX, 0px))'; el.dataset.pos = p; };
      if (ea) ST.stands.delete(a);
      if (eb) ST.stands.delete(b);
      if (ea) { put(ea, b); ST.stands.set(b, ea); }
      if (eb) { put(eb, a); ST.stands.set(a, eb); }
    }
  }
  // 块内瞬态演出（会随时间消失，只保留本句块内的）
  for (let i = fxFrom; i < midx; i++) {
    const m = ST.methods[i], M = m.Method;
    if (M === 'ShowStandEffect') { try { showStandEffect(m); } catch (e) { } }
    else if (M === 'ShowScreenEffect' || M === 'ShowScreenEffectBack') { try { showScreenEffect(m); } catch (e) { } }
    else if (M === 'Shake') {
      const st = document.querySelector('#stage');
      const px = (+m.ShakePixel) || 8;
      st.style.marginLeft = (px * 0.5) + 'px';
      st.style.marginTop = (-px * 0.35) + 'px';
    } else if (M === 'ScreenFlashIn' || M === 'ScreenFlash') {
      const L = document.querySelector('#flashLayer');
      L.style.background = 'rgb(' + ((+m.ColorR) || 255) + ',' + ((+m.ColorG) || 255) + ',' + ((+m.ColorB) || 255) + ')';
      L.style.transition = 'none';
      L.style.opacity = '1';
    } else if (M === 'ScreenFlashOut') {
      const L = document.querySelector('#flashLayer');
      L.style.transition = 'none';
      L.style.opacity = '0';
    } else if (M === 'ShowPlace') {
      const L = document.querySelector('#placeLayer');
      const sp = L.querySelector('span');
      if (sp) sp.textContent = m.PlaceName || '';
      L.style.transition = 'none';
      L.style.opacity = '1';
    } else if (M === 'ToUpper') { toUpper = true; }
  }
  ST.fastForward = false;
  const cg = document.querySelector('#cgLayer img');
  if (cg && cgT) {
    cg.style.transition = 'none';
    cg.style.transform = cgT;
    cg.style.filter = cgF || '';
  }
  const w = document.querySelector('#msgWindow'), n = document.querySelector('#msgName'), t = document.querySelector('#msgText');
  if (winVisible) {
    w.classList.add('on');
    if (curName) { n.textContent = curName; n.classList.add('on'); }
    else n.classList.remove('on');
    t.textContent = lineText(ST.methods[midx]);
    t.classList.toggle('narr', !curName);
    t.style.fontSize = toUpper ? 'calc(var(--msgFS,21px) * 1.24)' : '';
  } else { w.classList.remove('on'); }
  document.querySelectorAll('#fxLayer .seffect, #fxLayer .screff').forEach(e => {
    e.style.animation = 'none';
    e.style.transition = 'none';
    e.style.opacity = '1';
    e.style.transform = 'none';
  });
  return [document.querySelectorAll('#bgLayer img,#cgLayer img,#standLayer img').length,
          document.querySelectorAll('#fxLayer .seffect, #fxLayer .screff').length,
          winVisible ? 1 : 0,
          document.querySelector('#flashLayer').style.opacity,
          document.querySelector('#placeLayer').style.opacity];
}"""

WAIT_IMGS = """() => {
  const ims = Array.from(document.querySelectorAll('#bgLayer img,#cgLayer img,#standLayer img'));
  return ims.every(im => im.complete && im.naturalWidth > 0);
}"""


#: 依次尝试的浏览器。用户的机器上不一定有 Edge（Linux/macOS 上几乎必然没有），
#: 只有 Chrome 也很常见；最后再退回 playwright 自带的 chromium。
#: 顺序讲究：Edge/Chrome 是系统已装的（秒开、不额外占几百 MB），放前面。
CHANNELS = ('msedge', 'chrome', None)


def _launch(p):
    """→ (browser, 说明)。全部失败时抛一条人能看懂的错。"""
    errors = []
    for ch in CHANNELS:
        kw = {'args': ['--autoplay-policy=no-user-gesture-required', '--mute-audio']}
        if ch:
            kw['channel'] = ch
        try:
            return p.chromium.launch(**kw), (ch or 'playwright 自带 chromium')
        except Exception as e:                                 # noqa: BLE001
            errors.append(f'{ch or "chromium"}: {str(e).splitlines()[0][:110]}')
    raise RuntimeError(
        "找不到可用的浏览器。视频导出需要一个基于 Chromium 的浏览器来截帧：\n"
        "  1) 装 Microsoft Edge 或 Google Chrome（推荐，装完即可用）；\n"
        "  2) 或者执行 `python -m playwright install chromium` 让 playwright 自带一份"
        "（约 150 MB）。\n"
        "已尝试：\n  " + "\n  ".join(errors))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    OUT.mkdir(parents=True, exist_ok=True)
    argdir = sys.argv[4] if len(sys.argv) > 4 else 'frames_web'
    _p = argdir if isinstance(argdir, str) else str(argdir)
    fdir = _p if os.path.isabs(_p) else str(OUT / _p)
    fdir = Path(fdir)
    fdir.mkdir(parents=True, exist_ok=True)
    logf = open(LOG, 'w', encoding='utf-8')

    def log(*a):
        s = ' '.join(str(x) for x in a)
        print(s, flush=True)
        logf.write(s + '\n')
        logf.flush()

    cid = int(sys.argv[1]) if len(sys.argv) > 1 else 78
    arg = sys.argv[2] if len(sys.argv) > 2 else '1'
    parts = [int(x) for x in arg.split(',') if x]
    limit = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else 0
    tl = Timeline(cid, parts)
    log(f'char={cid} parts={parts} lines={len(tl.lines)} total={tl.total}s')

    inj = "localStorage.setItem('escah_set', JSON.stringify(%s));" % json.dumps(tl.settings, ensure_ascii=False)

    from playwright.sync_api import sync_playwright
    t0 = time.time()
    with sync_playwright() as p:
        b, _ch = _launch(p)
        log('launched', b.version, 'via', _ch)
        ctx = b.new_context(viewport={'width': 1400, 'height': 900}, device_scale_factor=DSF)
        ctx.add_init_script(inj)
        pg = ctx.new_page()
        pg.set_default_timeout(15000)

        done = 0
        # 图片等待超时的连续次数：一旦某页有永久缺失的图（资源不全），
        # 每帧等满 8 秒会让 160 帧变成二十多分钟。连续超 3 次后降到 0.6 秒。
        miss_streak = 0
        for pi in parts:
            if limit and done >= limit:
                break
            url = f'http://localhost:{PORT}/index.html?char={cid}&part={pi}&autoplay=1&lang=zh-Hans'
            pg.goto(url, wait_until='domcontentloaded')
            pg.wait_for_function("() => typeof ST === 'object' && ST.methods && ST.methods.length > 0")
            pg.wait_for_timeout(700)
            log(f'part {pi}: stage', pg.evaluate(STAGE_FIX))
            pg.evaluate('() => { ST.running = false; ST.run++; if (ST.voice) { try { ST.voice.pause(); } catch (e) { } } }')
            pg.wait_for_timeout(150)
            part_lines = [l for l in tl.lines if l['part'] == pi]
            for l in part_lines:
                if limit and done >= limit:
                    break
                fx = pg.evaluate(RENDER, l['midx'])
                try:
                    pg.wait_for_function(
                        WAIT_IMGS, timeout=3000 if miss_streak < 3 else 600)
                except Exception:
                    miss_streak += 1
                    if miss_streak <= 3:
                        log(f'  [{l["idx"]}] 图片等待超时（继续；'
                            f'连续 {miss_streak} 次后会自动放宽等待）')
                # 等两帧 rAF 保证新画面已 paint（替代固定 260ms），再留极小缓冲
                pg.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
                if WAIT_MS:
                    pg.wait_for_timeout(WAIT_MS)
                dst = fdir / f'{l["idx"]:04d}.png'
                pg.screenshot(path=str(dst), clip={'x': 0, 'y': 0, 'width': 1280, 'height': 720})
                done += 1
                log(f'  [{l["idx"]}] {dst.stat().st_size // 1024}KB fx={fx} {l["text"][:20]!r}')
        b.close()
    log(f'done {len(tl.lines)} frames in {time.time() - t0:.1f}s')


if __name__ == '__main__':
    main()
