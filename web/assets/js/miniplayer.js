/* 跨页面的「原声集」迷你播放条
 * ----------------------------
 * 本站是多页应用（点点导航就整页重载），<audio> 会跟着文档一起销毁 ——
 * 想让「切到别的页面还在放」，最省事可靠的办法是：
 *
 *   ① 原声集页持续把「放的是哪首 + 播到第几秒 + 音量 + 倍速 + 循环模式」
 *      写进 localStorage['esc_ost_now']，队列写进 ['esc_ost_queue']；
 *   ② 别的页面读这两份状态，用**自己的** <audio> 接着播（从记录的位置起）。
 *
 * 这不是真正的"后台播放"（换页时会有一次性重新解码），但用户听感上只差一瞬间，
 * 而且不用 Service Worker / 弹窗 / iframe 那套重活。
 *
 * 形态：底部居中，**平时只有一条 6px 进度条**；鼠标移上去才展开完整控制台
 * （上一首/播放/下一首、循环、倍速、音量、拖动定位、时长、打开原声集、收起）。
 * 放右边会和「资源管理」页右下角的组件撞上，一直摊开一张卡片也占地方。
 * 触屏没有 hover，改成点一下细条展开。剧情页底部控制栏是全宽的，单独抬高。
 *
 * 浏览器自动播放策略：新文档里 `play()` 可能被拒。被拒时强制展开卡片并显示
 * 「点一下继续播放」，绝不弹红字。
 */
(function () {
  'use strict';

  // shell.html 里播放器 document 常驻、换页根本不会中断，不需要续播迷你条
  if (window.parent !== window) return;
  const IN_SHELL = false;

  const KEY = 'esc_ost_now';
  const QUEUE_KEY = 'esc_ost_queue';
  const STALE_MS = 6 * 3600 * 1000;
  // 与主播放器 music.js 完全同一套，改那边这里也要同步
  const MODES = [['list', '🔁'], ['one', '🔂'], ['single', '⏹'],
                ['shuffle', '🔀'], ['seq', '➡']];
  const RATES = [1, 1.25, 1.5, 2, 0.75, 0.5];

  function read(k) {
    try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { return null; }
  }
  function write(k, d) {
    try { localStorage.setItem(k, JSON.stringify(d)); } catch (e) { /* 忽略 */ }
  }
  function fmt(sec) {
    sec = Math.max(0, Math.floor(+sec || 0));
    return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
  }

  let saved = read(KEY);
  if (!saved || !saved.s) return;
  // 本页就是原声集页：有完整播放器，不需要迷你条
  if (/music\.html$/.test(location.pathname)) return;
  // 用户这次会话里主动关掉过：别再跳出来烦他
  try {
    if (sessionStorage.getItem('esc_ost_mini_closed') === '1') return;
  } catch (e) { /* 忽略 */ }

  const stale = (Date.now() - (+saved.at || 0)) > STALE_MS;
  const touch = window.matchMedia && window.matchMedia('(hover: none)').matches;

  /* ---------------------------------------------------------------- 样式 */
  const css = `
#ostMini{position:fixed;z-index:3500;left:50%;bottom:52px;
  transform:translateX(-50%);width:min(280px,62vw);height:26px;
  color:#c7d0e0;font:14px/1.5 inherit;user-select:none;
  animation:mpIn .22s ease-out}
@keyframes mpIn{from{opacity:0}to{opacity:1}}

/* 平时只看见这一条。26px 高的透明容器是鼠标命中区，视觉上只有 6px。 */
#ostMini .mp-rail{position:absolute;left:0;right:0;top:50%;margin-top:-3px;
  height:6px;border-radius:4px;background:rgba(160,175,200,.22);
  box-shadow:0 2px 10px rgba(0,0,0,.5);cursor:pointer;
  transition:opacity .16s}
#ostMini .mp-rail i{display:block;height:100%;width:0;border-radius:4px;
  background:#e0b566;box-shadow:0 0 6px rgba(224,181,102,.55)}
#ostMini.mp-paused .mp-rail i{background:#8b93a6;box-shadow:none}

/* 完整控制台：从细条的位置向上"长"出来，默认完全隐藏 */
#ostMini .mp-card{position:absolute;left:50%;bottom:-4px;
  transform:translate(-50%,8px);width:min(362px,92vw);box-sizing:border-box;
  display:flex;flex-direction:column;gap:7px;padding:9px 11px;
  border-radius:12px;background:rgba(18,22,31,.97);border:1px solid #33445c;
  box-shadow:0 12px 34px rgba(0,0,0,.6);backdrop-filter:blur(6px);
  opacity:0;pointer-events:none;transition:opacity .16s,transform .16s}
#ostMini:hover .mp-card,
#ostMini.mp-open .mp-card,
#ostMini.mp-need .mp-card{opacity:1;pointer-events:auto;transform:translate(-50%,0)}
#ostMini:hover .mp-rail,
#ostMini.mp-open .mp-rail,
#ostMini.mp-need .mp-rail{opacity:0}

#ostMini .mp-top{display:flex;align-items:center;gap:9px;min-width:0}
#ostMini .mp-cover{width:38px;height:38px;flex:none;border-radius:7px;
  object-fit:cover;background:#0a0c11;border:1px solid #2a3040;cursor:pointer}
#ostMini .mp-main{flex:1;min-width:0;cursor:pointer}
#ostMini .mp-title{display:block;color:#e0b566;font-weight:600;line-height:1.35;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#ostMini .mp-sub{display:block;color:#6b7488;line-height:1.35;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#ostMini .mp-hint{color:#e0b566;font-size:14px;display:none}
#ostMini.mp-need .mp-hint{display:inline}

/* 展开态的精确进度条（可拖动），与外面的细条共用同一个 fill 计算 */
#ostMini .mp-seek{position:relative;height:8px;border-radius:5px;
  background:#2a3344;cursor:pointer;touch-action:none}
#ostMini .mp-seek i{display:block;height:100%;width:0;border-radius:5px;
  background:#e0b566}
#ostMini .mp-ctrl{display:flex;align-items:center;gap:5px;min-width:0}
#ostMini button{flex:none;height:26px;min-width:26px;padding:0 5px;border-radius:7px;
  cursor:pointer;border:1px solid #3a4a60;background:#1c2230;color:#c7d0e0;
  font-family:inherit;font-size:14px;line-height:1}
#ostMini button:hover{color:#e0b566;border-color:#4a3f78}
#ostMini button.on{color:#e0b566;border-color:#4a3f78}
#ostMini .mp-play{width:30px;min-width:30px;background:#e0b566;color:#141822;
  border-color:#e0b566;font-size:15px}
#ostMini .mp-rate{min-width:38px}
#ostMini .mp-mute{padding:0 4px}
#ostMini .mp-vol{flex:1;min-width:44px;width:60px;height:4px;margin:0 2px;
  accent-color:#e0b566;cursor:pointer}
#ostMini .mp-time{flex:none;font-size:14px;color:#6b7488;
  font-variant-numeric:tabular-nums;white-space:nowrap}
#ostMini .mp-x{position:absolute;top:-7px;right:-7px;width:20px;height:20px;
  min-width:20px;padding:0;font-size:14px;background:#2a3344;border-color:#3a4a60}

/* 触屏没有 hover：CSS 的 :hover 会"点过之后一直粘住"，改成显式 mp-open */
#ostMini.mp-touch:hover .mp-card{opacity:0;pointer-events:none;transform:translate(-50%,8px)}
#ostMini.mp-touch:hover .mp-rail{opacity:1}
#ostMini.mp-touch.mp-open .mp-card,
#ostMini.mp-touch.mp-need .mp-card{opacity:1;pointer-events:auto;transform:translate(-50%,0)}
#ostMini.mp-touch.mp-open .mp-rail,
#ostMini.mp-touch.mp-need .mp-rail{opacity:0}

/* 剧情播放页底部控制栏是全宽的，居中贴底会压住它自己的进度条 */
#ostMini.mp-story{bottom:108px}
@media (max-width:860px){
  #ostMini{bottom:88px;width:min(220px,60vw)}
  #ostMini.mp-story{bottom:150px}
  #ostMini .mp-card{width:min(330px,94vw);gap:6px;padding:8px 9px}
  #ostMini .mp-cover{width:32px;height:32px}
  #ostMini .mp-vol{width:40px}
}`;

  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  /* ---------------------------------------------------------------- DOM */
  const box = document.createElement('div');
  box.id = 'ostMini';
  if (touch) box.classList.add('mp-touch');
  if (/index\.html$/.test(location.pathname)) box.classList.add('mp-story');
  box.innerHTML =
    '<div class="mp-rail"><i></i></div>' +
    '<div class="mp-card">' +
    '<div class="mp-top">' +
    (saved.img ? '<img class="mp-cover" alt="">' : '<span class="mp-cover"></span>') +
    '<span class="mp-main"><span class="mp-title"></span>' +
    '<span class="mp-sub"><span class="mp-hint">点一下继续播放 · </span>' +
    '<span class="mp-subtxt"></span></span></span>' +
    '<button class="mp-openfull" title="打开原声集">⤢</button>' +
    '<button class="mp-x" title="收起（本页不再显示）">✕</button>' +
    '</div>' +
    '<div class="mp-seek"><i></i></div>' +
    '<div class="mp-ctrl">' +
    '<button class="mp-prev" title="上一首">⏮</button>' +
    '<button class="mp-play" title="播放 / 暂停">▶</button>' +
    '<button class="mp-next" title="下一首">⏭</button>' +
    '<button class="mp-mode" title="循环模式">🔁</button>' +
    '<button class="mp-rate" title="倍速">1.0×</button>' +
    '<button class="mp-mute" title="静音">🔊</button>' +
    '<input class="mp-vol" type="range" min="0" max="100" value="80" title="音量">' +
    '<span class="mp-time">0:00 / 0:00</span>' +
    '</div></div>';

  document.addEventListener('DOMContentLoaded', () => document.body.appendChild(box));
  if (document.readyState !== 'loading') document.body.appendChild(box);

  const $ = s => box.querySelector(s);
  const cover = $('.mp-cover');
  const rail = $('.mp-rail'), railFill = $('.mp-rail i');
  const seek = $('.mp-seek'), seekFill = $('.mp-seek i');
  const playBtn = $('.mp-play'), modeBtn = $('.mp-mode'), rateBtn = $('.mp-rate');
  const muteBtn = $('.mp-mute'), volInput = $('.mp-vol'), timeEl = $('.mp-time');

  /* ---------------------------------------------------------------- 音频 */
  const au = document.createElement('audio');
  au.preload = 'auto';
  au.src = saved.s;
  au.volume = saved.vol == null ? 0.8 : +saved.vol;
  au.muted = !!saved.muted;
  au.playbackRate = +saved.rate || 1;
  let mode = MODES.some(m => m[0] === saved.mode) ? saved.mode : 'list';
  let seeking = false;

  // 调试/测试用：迷你条自己的 <audio> 没挂进 DOM，外面用 querySelector 找不到
  window.__OST_MINI = { au: au, box: box, get saved() { return saved; }, get mode() { return mode; } };

  function paintCard() {
    if (cover && cover.tagName === 'IMG') {
      // 切到没有封面的曲目时别把 src 设成空串（浏览器会去请求当前页 → 破图）
      if (saved.img) {
        cover.src = saved.img;
        cover.style.visibility = '';
      } else {
        cover.removeAttribute('src');
        cover.style.visibility = 'hidden';
      }
    }
    $('.mp-title').textContent = saved.t || '原声集';
    $('.mp-subtxt').textContent = (saved.sub || '') +
      (saved.dur ? ' · ' + fmt(saved.dur) : '');
  }
  function paint() {
    const d = au.duration || +saved.dur || 0;
    const t = au.currentTime || 0;
    const p = (d ? (t / d) * 100 : 0).toFixed(1) + '%';
    railFill.style.width = p;
    if (!seeking) seekFill.style.width = p;
    playBtn.textContent = au.paused ? '▶' : '⏸';
    box.classList.toggle('mp-paused', au.paused);
    timeEl.textContent = fmt(t) + ' / ' + fmt(d);
  }
  const MODE_CN = { list: '列表循环', one: '单曲循环', shuffle: '随机播放', seq: '顺序播放' };
  function paintCtrls() {
    modeBtn.textContent = (MODES.find(m => m[0] === mode) || MODES[0])[1];
    modeBtn.classList.toggle('on', mode !== 'list');
    modeBtn.title = '循环模式：' + (MODE_CN[mode] || mode);
    rateBtn.textContent = (String(au.playbackRate).indexOf('.') < 0
      ? au.playbackRate.toFixed(1) : au.playbackRate) + '×';
    volInput.value = String(Math.round((au.muted ? 0 : au.volume) * 100));
    muteBtn.textContent = au.muted || au.volume === 0 ? '🔇' : (au.volume < 0.5 ? '🔉' : '🔊');
  }

  function persist(playing) {
    const d = au.duration || +saved.dur || 0;
    const st = {
      k: saved.k, s: saved.s, t: saved.t, sub: saved.sub, img: saved.img,
      no: saved.no, dur: d, pos: Math.min(au.currentTime || 0, d || 1e9),
      playing: playing == null ? !au.paused : !!playing,
      vol: au.volume, muted: au.muted, rate: au.playbackRate, mode: mode,
      at: Date.now(),
    };
    write(KEY, st);
  }

  au.addEventListener('timeupdate', paint);
  au.addEventListener('durationchange', paint);
  au.addEventListener('play', () => { paint(); persist(true); });
  au.addEventListener('pause', () => { paint(); persist(false); });
  au.addEventListener('ended', () => {
    paint();
    if (mode === 'one') { au.currentTime = 0; au.play().catch(() => {}); return; }
    if (mode === 'seq' || mode === 'single') { persist(false); return; }
    step(1, true);                     // 列表循环 / 随机：自动接下一首
  });
  // 每秒落一次盘，换页时能接到"刚刚那一秒"
  setInterval(() => { if (!au.paused) persist(true); }, 1000);
  window.addEventListener('pagehide', () => persist());

  const startAt = +saved.pos || 0;
  if (startAt > 0.5) {
    au.addEventListener('loadedmetadata', () => {
      if (isFinite(au.duration) && startAt < au.duration - 1) au.currentTime = startAt;
    }, { once: true });
  }

  /* ------------------------------------------------------------ 队列 / 切歌 */
  function step(dir, keepPlaying) {
    const q = read(QUEUE_KEY);
    const items = (q && q.items) || [];
    if (!items.length) {                       // 没在原声集页播放过就没有队列
      const t = $('.mp-title'), old = t.textContent;
      t.textContent = '没有播放队列：请先到原声集页播放一次';
      setTimeout(() => { t.textContent = old; }, 1800);
      return;
    }
    let i = items.findIndex(x => x.k === saved.k);
    if (i < 0) i = 0;
    let n = (mode === 'shuffle')
      ? Math.floor(Math.random() * items.length) : i + dir;
    if (n < 0) n = items.length - 1;
    if (n >= items.length) n = 0;
    useItem(items[n], keepPlaying == null ? !au.paused : keepPlaying);
  }

  function useItem(it, keepPlaying) {
    saved = Object.assign({}, saved, {
      k: it.k, s: it.s, t: it.t, sub: it.sub || '', img: it.img || '',
      no: it.no || 0, dur: it.d || 0, pos: 0,
    });
    au.src = it.s;
    au.load();
    paintCard(); paint(); persist(keepPlaying);
    if (keepPlaying) {
      au.play().then(() => box.classList.remove('mp-need'))
        .catch(() => { box.classList.add('mp-need'); paint(); });
    }
  }

  /* ---------------------------------------------------------------- 交互 */
  function toggle() {
    if (au.paused) {
      au.play().then(() => box.classList.remove('mp-need'))
        .catch(() => { box.classList.add('mp-need'); paint(); });
    } else {
      au.pause();
    }
  }
  playBtn.onclick = toggle;
  $('.mp-prev').onclick = () => step(-1);
  $('.mp-next').onclick = () => step(1);

  modeBtn.onclick = () => {
    const i = MODES.findIndex(m => m[0] === mode);
    mode = MODES[(i + 1) % MODES.length][0];
    paintCtrls(); persist();
  };
  rateBtn.onclick = () => {
    const i = RATES.findIndex(r => Math.abs(r - au.playbackRate) < 0.001);
    au.playbackRate = RATES[(i + 1) % RATES.length];
    paintCtrls(); persist();
  };
  muteBtn.onclick = () => {
    au.muted = !au.muted;
    if (!au.muted && au.volume === 0) au.volume = 0.8;
    paintCtrls(); persist();
  };
  volInput.oninput = () => {
    au.muted = false;
    au.volume = Math.min(1, Math.max(0, +volInput.value / 100));
    paintCtrls(); persist();
  };

  function openPage() {
    // 不带 ?play=N：原声集页会按 localStorage 里那份状态**接着上次的位置**放
    persist();
    location.href = 'music.html';
  }
  $('.mp-main').onclick = openPage;
  if (cover) cover.onclick = openPage;
  $('.mp-openfull').onclick = openPage;
  $('.mp-x').onclick = () => {
    au.pause();
    try { sessionStorage.setItem('esc_ost_mini_closed', '1'); } catch (e) { /* 忽略 */ }
    box.remove();
  };

  // 展开态进度条：按下→拖动→松开定位
  function seekFrac(ev) {
    const r = seek.getBoundingClientRect();
    return Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
  }
  seek.addEventListener('pointerdown', ev => {
    seeking = true;
    seek.setPointerCapture(ev.pointerId);
    seekFill.style.width = (seekFrac(ev) * 100).toFixed(1) + '%';
  });
  seek.addEventListener('pointermove', ev => {
    if (seeking) seekFill.style.width = (seekFrac(ev) * 100).toFixed(1) + '%';
  });
  seek.addEventListener('pointerup', ev => {
    if (!seeking) return;
    const p = seekFrac(ev);
    seeking = false;
    if (isFinite(au.duration) && au.duration > 0) {
      au.currentTime = p * au.duration; paint(); persist();
    }
  });

  // 默认细条：点哪儿跳哪儿。触屏上第一次点是"展开"，展开后再点才定位。
  rail.onclick = ev => {
    if (touch && !box.classList.contains('mp-open')) {
      box.classList.add('mp-open'); return;
    }
    const r = rail.getBoundingClientRect();
    const p = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
    if (isFinite(au.duration) && au.duration > 0) {
      au.currentTime = p * au.duration; paint(); persist();
    }
  };
  if (touch) {
    document.addEventListener('click', ev => {
      if (!box.contains(ev.target)) box.classList.remove('mp-open');
    });
  }

  /* ---------------------------------------------------------------- 起播 */
  paintCard(); paint(); paintCtrls();
  if (saved.playing && !stale) {
    // 先静音播一帧把"已开播"拿下来，再恢复音量 —— 有些浏览器对静音自动播放更宽松
    au.muted = true;
    au.play().then(() => {
      au.muted = !!saved.muted;
      paint(); paintCtrls();
    }).catch(() => {
      au.muted = !!saved.muted;
      box.classList.add('mp-need');   // 被拦：展开卡片 + 「点一下继续播放」
      paint(); paintCtrls();
    });
  }
})();
