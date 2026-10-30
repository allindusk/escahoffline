/* 演出引擎：按 Methods 指令序列实时复现游戏内剧情演出
 *
 * 时序模型（与 Unity 协程一致）：
 *   画面/文字/语音类指令「立即启动、不阻塞」，把动画 Promise 累积到 ST.pendingAll
 *   WaitMotion 才是同步点：等待本轮累积的全部动作完成
 *   Wait 是显式停顿（数据里带 Time）
 */
const INDEX = window.__INDEX || [];
const ADV = window.__ADV || [];            // 主线/活动/其他剧情清单（advindex.js）
const IMGMAP = window.__IMGMAP || { stand: {}, back: {}, cg: {} };
const SFX = window.__SFX || {};
const NAMES = window.__NAMES || {};
const CHARMETA = window.__CHARMETA || {};
/* 剧情数据分两类，结构完全同构（都有 parts[].methods），只有 key 不同：
     char_<cid>     角色个人剧情（data/char_<cid>.js，window.__CHAR）
     adv_<kind>_<no> 主线/活动/其他（data/adv_<kind>_<no>.js，window.__STORY） */
/* 剧本包在 CDN 上共 6 种：char(角色) / main / event / other / tutorial / op。
   游戏 UI 只有 4 个入口——tutorial 与 op 是流程性播放、不单独占菜单，
   归在「其他」里显示（见 ADV_MENU）。 */
const ADV_LABEL = { main: '主线剧情', event: '活动剧情', other: '其他剧情',
                    tutorial: '新手教程', op: '开场动画' };
const ADV_TAG = { main: '主', event: '活', other: '他', tutorial: '教', op: '开' };
/* 侧栏某个按钮对应的 kind 集合 */
const ADV_MENU = { char: ['char'], main: ['main'], event: ['event'],
                   other: ['other', 'tutorial', 'op'] };
const isCharKey = k => String(k || '').startsWith('char_');
const $ = s => document.querySelector(s);
/* 多语言层（web/i18n.js）。缺失时整套降级为「原文」，行为与加这层之前一致。 */
const NOOP_I18N = {
  msg: m => (m == null ? '' : (m.Message != null ? m.Message : '')),
  speaker: n => (n == null ? '' : n),
  t: (k, fb) => (fb != null ? fb : k),
  lang: () => 'ja', source: 'ja', label: () => '日本語',
  cycle: () => 'ja', applyDom: () => { }, loadStory: () => Promise.resolve(null),
  on: () => { }, bindChar: () => { }, ready: Promise.resolve(), supported: ['ja'],
};
const L10N = window.I18N || NOOP_I18N;

/* 对话框上方的发言人名字。ShowMessageWindow.Name 是日文口语称呼（トキサダ），
   译文放在全局表 _speakers.json 里，没译文就原样显示日文。 */
function spkName(m) {
  const n = m && m.Name;
  return n ? (L10N.speaker ? L10N.speaker(n) : n) : '';
}

const ST = {
  char: null, key: null, part: 0,
  methods: [], idx: 0,
  running: false, speed: 1, auto: true,
  pendingAll: [],
  stands: new Map(),
  voice: null, cache: new Map(),
  skips: [], typeSkip: false, voiceHas: false, voiceP: null,
  typeRate: 1,           // 打字速度倍率（越大越快）
  allLines: [], lineMap: {}, curLine: -1, linePage: 0,
  curName: '', fastForward: false, curMsgIdx: null,
  typing: false,         // 打字机是否正在输出（决定「下一句」先补全还是翻页）
  rush: false,           // 按住 Ctrl 的快进状态
  run: 0,                // 播放循环代号：暂停/跳转/重播时自增，用来作废旧循环
};
/* 告诉 i18n 层"当前是哪部剧情"，它据此去查译文表（char_<cid> / adv_<kind>_<no>） */
L10N.bindChar(() => S.key);

/* ================= 工具 ================= */
const _sjis = (() => { try { return new TextDecoder('shift_jis'); } catch (e) { return null; } })();
function decodeName(s) {
  if (!s || !_sjis) return s || '';
  return String(s).split('_').map(seg => {
    if (seg.length >= 4 && seg.length % 2 === 0 && /^[0-9A-F]+$/.test(seg)) {
      try {
        const b = new Uint8Array(seg.match(/../g).map(h => parseInt(h, 16)));
        const t = _sjis.decode(b);
        if (/^[\u3000-\u30ff\u4e00-\u9fff\uff00-\uffef]+$/.test(t)) return t;
      } catch (e) { }
    }
    return seg;
  }).join('·');
}
function num(v, d = 0) { const n = parseFloat(v); return isNaN(n) ? d : n; }

function wait(ms) {
  return new Promise(res => {
    const t = setTimeout(done, Math.max(0, ms / ST.speed));
    function done() { clearTimeout(t); ST.skips = ST.skips.filter(f => f !== done); res(); }
    ST.skips.push(done);
  });
}
function skipAll() { ST.skips.slice().forEach(f => f()); ST.skips = []; }

/* ================= 舞台缩放 ================= */
function fitStage() {
  const wrap = $('#stageWrap'), stage = $('#stage');
  if (!wrap || !stage) return;
  // 舞台与文本列表是 flex 并列，clientWidth 已是实际可用宽度
  const w = wrap.clientWidth, h = wrap.clientHeight;
  const base = Math.min(w / 1280, h / 720);
  let s;
  // 全屏：无视「1x 不放大」设定，直接铺满大屏（素材已超分，放大也清晰）
  if (wrap.classList.contains('fs')) s = base;
  else if (SET.fit === '1x') s = Math.min(base, 1);  // 不放大，720p 素材 1:1 最清晰
  else if (SET.fit === '15') s = Math.min(base, 1.5);
  else s = base * 0.995;                              // 适应窗口
  stage.style.transform = `scale(${s})`;
  const ind = $('#dbgFit');
  if (ind) {
    ind.textContent = `${s.toFixed(2)}x` +
      (wrap.classList.contains('fs') ? ' 全屏' :
        (s > 1.001 ? ' 放大·略糊' : (s >= 0.999 ? ' 原尺寸' : ' 缩小')));
  }
}
window.addEventListener('resize', fitStage);

/* ================= 全屏 ================= */
function toggleFullscreen() {
  const el = document.getElementById('stageWrap');
  if (!el) return;
  const fsEl = document.fullscreenElement || document.webkitFullscreenElement;
  if (!fsEl) {
    const req = el.requestFullscreen || el.webkitRequestFullscreen || el.msRequestFullscreen;
    if (req) req.call(el);
  } else {
    const exit = document.exitFullscreen || document.webkitExitFullscreen || document.msExitFullscreen;
    if (exit) exit.call(document);
  }
}
let _fsMouseTimer = null;
function _fsShowCursor() {
  const el = document.getElementById('stageWrap');
  if (el) el.classList.remove('hidecur');
  if (_fsMouseTimer) clearTimeout(_fsMouseTimer);
  _fsMouseTimer = setTimeout(() => {
    const e = document.getElementById('stageWrap');
    if (e) e.classList.add('hidecur');
  }, 2500);
}
function onFsChange() {
  const fs = !!(document.fullscreenElement || document.webkitFullscreenElement);
  const el = document.getElementById('stageWrap');
  if (el) el.classList.toggle('fs', fs);
  fitStage();
  const b = document.getElementById('btnFs');
  if (b) b.textContent = fs ? '⛶ 退出' : '⛶ 全屏';
  // 全屏时鼠标静止 2.5s 后隐藏，移动即显
  if (fs) {
    el.addEventListener('mousemove', _fsShowCursor);
    _fsShowCursor();
  } else if (el) {
    el.removeEventListener('mousemove', _fsShowCursor);
    el.classList.remove('hidecur');
    if (_fsMouseTimer) { clearTimeout(_fsMouseTimer); _fsMouseTimer = null; }
  }
}
document.addEventListener('fullscreenchange', onFsChange);
document.addEventListener('webkitfullscreenchange', onFsChange);
const _btnFs = document.getElementById('btnFs');
if (_btnFs) _btnFs.onclick = toggleFullscreen;

/* ================= 画面 ================= */
function setBackground(id) {
  const list = id ? candsOf('back', id) : [];
  const L = $('#bgLayer');
  L.innerHTML = '';
  if (!list.length) return Promise.resolve();
  const img = applyChain(new Image(), list);
  img.style.opacity = 0;
  img.style.transition = 'opacity .45s';
  L.appendChild(img);
  requestAnimationFrame(() => { img.style.opacity = 1; });
  return wait(450);
}

function setDoubleBackground(l, r) {
  const L = $('#bgLayer');
  L.innerHTML = '';
  [[l, 'left'], [r, 'right']].forEach(([id, side]) => {
    const list = id ? candsOf('back', id) : [];
    if (!list.length) return;
    const img = applyChain(new Image(), list);
    img.className = 'half';
    img.style[side] = '0';
    L.appendChild(img);
  });
  return wait(400);
}

function setCg(id, fade) {
  const L = $('#cgLayer');
  L.innerHTML = '';
  if (!id) return Promise.resolve();
  const list = candsOf('cg', id);
  if (!list.length) return Promise.resolve();
  const img = applyChain(new Image(), list);
  img.style.opacity = 0;
  img.style.transition = `opacity ${fade}s`;
  L.appendChild(img);
  requestAnimationFrame(() => { img.style.opacity = 1; });
  return wait(fade * 1000);
}

/* ================= 立绘 ================= */
const POS_X = { Left: '23%', Center: '50%', Right: '77%', All: '50%' };
const POS_OFF = { Left: -70, Right: 70, Down: 40, None: 0 };
// 立绘缩放：原作里中间站位最大、两侧略小，避免三张满高图互相压盖
/** 立绘缩放：单人居中满高，同框越多缩得越小（按实际在场数量动态计算） */
function updateStandScale() {
  const n = ST.stands.size;
  const sc = n <= 1 ? 1 : (n === 2 ? 0.86 : 0.76);
  ST.stands.forEach(el => {
    const im = el.querySelector('img');
    if (im) im.style.setProperty('--sc', sc);
  });
}
const Z_VAL = { Back: 1, Middle: 3, Front: 5 };

/* 立绘按「站位」管理：每个 Position 只保留一个立绘，
 * 换形态（GroupId 变化）时直接替换图片，避免同位置叠加。
 */
function standKey(m) { return m.Position || 'Center'; }

/* 图片资源版本号：合成图更新过，加参数强制浏览器取新文件 */
const IMG_VER = '7';
function imgSrc(path) {
  if (!path) return path;
  return path + (path.includes('?') ? '&' : '?') + 'v=' + IMG_VER;
}

/* 图片候选链：本地超分覆盖层 → 本地转码覆盖层 → 出厂映射。
   三层都缺才是真缺图。任何一层缺失只是画质降级，不会黑图。
   （覆盖层由 server/media.py 生成：data/imgmap.x2.js / data/imgmap.base.js） */
const IMGX2 = window.__IMGX2 || {};
const IMGBASE = window.__IMGBASE || {};

function candsOf(kind, key) {
  const out = [];
  const push = p => { if (p && out.indexOf(p) < 0) out.push(p); };
  if (SET.useX2 !== false) push((IMGX2[kind] || {})[key]);
  push((IMGBASE[kind] || {})[key]);
  push((IMGMAP[kind] || {})[key]);
  return out.map(imgSrc);
}

/** 给 <img> 挂上候选链：加载失败自动换下一个，全失败就隐藏自身 */
function applyChain(img, list) {
  if (!img) return img;
  if (!list || !list.length) { img.style.visibility = 'hidden'; return img; }
  // 同一个 <img> 会被复用（如灯箱）。上一轮候选链全失败会留下 visibility:hidden，
  // 这里必须复位，否则第二轮即使图能加载也永远看不见。
  img.style.visibility = '';
  img.dataset.chain = list.slice(1).join('|');
  img.onerror = function () {
    const rest = (this.dataset.chain || '').split('|').filter(Boolean);
    if (!rest.length) { this.onerror = null; this.style.visibility = 'hidden'; return; }
    this.dataset.chain = rest.slice(1).join('|');
    this.src = rest[0];
  };
  img.src = list[0];
  return img;
}

function showStand(m, instant) {
  const id = m.StandImage || '';
  const srcs = candsOf('stand', id);
  const src = srcs[0] || '';
  const key = standKey(m);
  if (!src) return Promise.resolve();
  /* 只有「分层身体底图」(*_base.png) 才是残缺立绘（脸部是透明洞），默认隐藏。
     img/stand/*.webp 现在是 compose_stand_all.py 用「底图+表情差分」合成的
     完整立绘（落点由像素级匹配反推，与游戏一致），可以正常显示。 */
  const isPartial = /_base\.png$/i.test(src);
  if (isPartial && !SET.showPartial) return Promise.resolve();
  // 同位置已有立绘：若换了角色（GroupId 不同）先移除旧的
  let el = ST.stands.get(key);
  if (el && (el.dataset.gid || '') !== (m.GroupId || '')) {
    el.remove();
    ST.stands.delete(key);
    el = null;
  }
  // 其他位置若残留同一 GroupId 的立绘（角色移动到别的站位），也清掉
  for (const [k, other] of Array.from(ST.stands)) {
    if (k !== key && m.GroupId && other.dataset.gid === m.GroupId) {
      other.remove();
      ST.stands.delete(k);
    }
  }
  if (!el) {
    el = document.createElement('div');
    el.className = 'stand';
    el.innerHTML = '<img alt="">';
    $('#standLayer').appendChild(el);
    ST.stands.set(key, el);
  }
  el.dataset.gid = m.GroupId || '';
  el.dataset.pos = key;
  const img = el.querySelector('img');
  if (img.getAttribute('src') !== src) applyChain(img, srcs);
  updateStandScale();
  el.style.zIndex = Z_VAL[m.Z] || 3;
  el.style.opacity = 0;
  const off = POS_OFF[m.Motion] ?? 0;
  const dx = (m.Motion === 'Left' || m.Motion === 'Right') ? -off : 0;
  const dy = m.Motion === 'Down' ? -(off || 40) : 0;
  el.style.left = `calc(${POS_X[m.Position] || '50%'} + var(--standOX, 0px))`;
  if (instant) {
    // 跳转重建状态：直接落到最终位置，不走动画
    el.style.transition = 'none';
    el.style.opacity = 1;
    el.style.transform = 'translateX(-50%) translate(0,0)';
    return Promise.resolve();
  }
  el.style.transition = 'left .45s ease, opacity .3s ease, transform .35s ease';
  el.style.transform = `translateX(-50%) translate(${dx}px, ${dy}px)`;
  const t = Math.max(num(m.Time, 0.25), 0.05) * 1000;
  requestAnimationFrame(() => {
    el.style.opacity = 1;
    el.style.transform = 'translateX(-50%) translate(0,0)';
  });
  return wait(t);
}

function hideStand(m) {
  // 数据里 92% 的 HideStand 写的是 Position:"All"（表示清空全场），
  // 不能当成站位名——否则 get('All') 恒空，一条都删不掉，立绘只增不减。
  const key = (m.Position && m.Position !== 'All') ? m.Position : null;
  const targets = key ? [key] : Array.from(ST.stands.keys());
  const t = Math.max(num(m.Time, 0.25), 0.05) * 1000;
  for (const k of targets) {
    const el = ST.stands.get(k);
    if (!el) continue;
    ST.stands.delete(k);          // 先出队，避免与随后的 ShowStand 抢同一站位
    el.style.opacity = 0;
    el.style.transform = `translateX(-50%) translateY(${m.Motion === 'Down' ? 30 : 0}px)`;
    setTimeout(() => el.remove(), t + 40);
  }
  updateStandScale();
  return wait(t);
}

/** 按 Position 定位当前操作的立绘（找不到则退回最后一个） */
function currentStand(m) {
  return ST.stands.get(m.Position || 'Center') ||
    Array.from(ST.stands.values()).pop() || null;
}

function motionStand(m) {
  const el = currentStand(m);
  if (!el) return Promise.resolve();
  const map = {
    'ポップ': 'm-pop', '移動': 'm-move', '横揺れ': 'm-sway',
    'おじぎ': 'm-bow', '微動': 'm-tremble', '振動': 'm-tremble',
    '右突き': 'm-thrust', '左突き': 'm-thrust-l', '左ポップ': 'm-pop',
  };
  const cls = map[m.Motion] || 'm-tremble';
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
  return wait(520).then(() => el.classList.remove(cls));
}

function moveStand(m) {
  const from = m.PositionFrom || 'Center';
  const to = m.PositionTo || 'Center';
  const el = ST.stands.get(from) || currentStand(m);
  if (!el) return Promise.resolve();
  const t = Math.max(num(m.Time, 0.5), 0.1);
  el.style.transition = `left ${t}s ease`;
  el.style.left = `calc(${POS_X[to] || '50%'} + var(--standOX, 0px))`;
  el.dataset.pos = to;
  const im = el.querySelector('img');
  updateStandScale();
  ST.stands.delete(from);
  ST.stands.set(to, el);
  return wait(t * 1000);
}

function shakeStand(m) {
  const el = currentStand(m);
  if (!el) return;
  el.classList.remove('m-shake'); void el.offsetWidth;
  el.classList.add('m-shake');
  setTimeout(() => el.classList.remove('m-shake'), 400);
}

function mulStand(m) {
  const el = currentStand(m);
  if (!el) return;
  const clamp = v => Math.min(255, Math.max(0, v));
  const r = clamp(num(m.r, 0)), g = clamp(num(m.g, 0)), b = clamp(num(m.b, 0));
  const img = el.querySelector('img');
  const shadow = 'drop-shadow(0 6px 26px rgba(0,0,0,.75))';
  /* MulStand 的 r/g/b 是 0~255：0,0,0（384 次）和 255,255,255（411 次）
     都表示「不调色」——前者是清空、后者是恢复，效果等价。 */
  if ((r === 0 && g === 0 && b === 0) ||
      (r > 250 && g > 250 && b > 250)) {
    img.style.filter = shadow;
    return;
  }
  const lum = (r + g + b) / 3 / 255;
  const hue = (r - b) / 255 * 60;
  img.style.filter = `brightness(${Math.max(0.15, lum)}) hue-rotate(${hue}deg) ` + shadow;
}

/* ================= 特效 ================= */
function screenFlash(m) {
  const L = $('#flashLayer');
  const col = `rgb(${num(m.ColorR, 255)},${num(m.ColorG, 255)},${num(m.ColorB, 255)})`;
  const it = num(m.InTime, 0.15), ot = num(m.OutTime, 0.25);
  L.style.background = col;
  L.style.transition = `opacity ${it}s`;
  L.style.opacity = 1;
  return wait(it * 1000).then(() => {
    L.style.transition = `opacity ${ot}s`;
    L.style.opacity = 0;
    return wait(ot * 1000);
  });
}
function flashIn(m) {
  const L = $('#flashLayer');
  L.style.background = `rgb(${num(m.ColorR, 255)},${num(m.ColorG, 255)},${num(m.ColorB, 255)})`;
  L.style.transition = `opacity ${num(m.Time, .2)}s`;
  L.style.opacity = 1;
}
function flashOut(m) {
  const L = $('#flashLayer');
  L.style.transition = `opacity ${num(m.Time, .3)}s`;
  L.style.opacity = 0;
}
function shakeScreen(m) {
  const st = $('#stage');
  const px = num(m.ShakePixel, 8);
  const dur = Math.max(num(m.Time, 0.3), 0.1) * 1000;
  const t0 = performance.now();
  function frame(t) {
    const p = (t - t0) / dur;
    if (p >= 1) { st.style.marginLeft = 0; st.style.marginTop = 0; return; }
    const d = px * (1 - p);
    st.style.marginLeft = `${(Math.random() - .5) * d * 2}px`;
    st.style.marginTop = `${(Math.random() - .5) * d * 2}px`;
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  return wait(dur);
}
function mulColorBg(m) {
  /* r/g/b 是 0~255 的乘法系数：255 = 原色，100 = 变暗到 39%，0 = 全黑
     （之前误用 /100，导致 255 变成 2.55 倍亮度 → 背景过曝）
     数据里还有 280/300/500 这类超范围值，统一 clamp 到 255。 */
  const clamp = v => Math.min(255, Math.max(0, v));
  const r = clamp(num(m.r, 255)) / 255;
  const g = clamp(num(m.g, 255)) / 255;
  const b = clamp(num(m.b, 255)) / 255;
  const lum = (r + g + b) / 3;          // 整体亮度
  const hue = (r - b) * 90;             // 色偏（0 时无偏色）
  const sat = Math.min(2.2, Math.max(0, 1 + Math.abs(r - g) + Math.abs(g - b)));
  $('#bgLayer').querySelectorAll('img').forEach(i => {
    i.style.transition = `filter ${num(m.Time, .3)}s`;
    if (r > 0.999 && g > 0.999 && b > 0.999) {
      i.style.filter = 'none';          // 255,255,255 = 恢复原色
      return;
    }
    if (lum < 0.02) {                   // 0,0,0 = 全黑
      i.style.filter = 'brightness(0)';
      return;
    }
    i.style.filter = `brightness(${lum}) saturate(${sat}) hue-rotate(${hue}deg)`;
  });
}
function setDrawMode(mode) {
  const st = $('#stage');
  st.classList.remove('f-sepia', 'f-gray');
  if (mode === 'sepia') st.classList.add('f-sepia');
  if (mode === 'gray') st.classList.add('f-gray');
}

const SEFFECT_ICON = {
  '汗': '💧', '焦り': '💦', '怒り': '💢', '恥': '💗', '音符': '♪',
  'ビックリ': '！', 'ビックリはてな': '！？', 'はてな': '？',
  '点々': '…', '困り': '💧', '落ち込み': '⤵',
};
function showStandEffect(m) {
  // 无 Name 的是「清除特效」指令，别建一个空 div
  if (!m.Name) return;
  const d = document.createElement('div');
  d.className = 'seffect';
  d.textContent = SEFFECT_ICON[m.Name] || m.Name;
  const stage = $('#stage');
  const sr = stage.getBoundingClientRect();
  // 舞台内部坐标系固定 1280 宽（CSS 用 transform 缩放），高度按比例推
  const iw = 1280;
  const ih = sr.width ? iw * (sr.height / sr.width) : 720;
  const sx = sr.width ? iw / sr.width : 1;
  const el = currentStand(m);
  const r = el ? el.getBoundingClientRect() : null;
  let left, top;
  if (r && r.width > 0 && r.height > 0) {
    left = (r.left - sr.left + r.width * 0.66) * sx;
    top = (r.top - sr.top + r.height * 0.12) * sx;
  } else {
    // 立绘还没解码完时 getBoundingClientRect 全是 0，照算会跑到舞台外，
    // 被 #stage 的 overflow:hidden 裁掉（表现就是"符号没出现"）。退回默认站位。
    left = iw * 0.62;
    top = ih * 0.18;
  }
  const pad = 24;
  d.style.left = `${Math.min(Math.max(left, pad), iw - pad)}px`;
  d.style.top = `${Math.min(Math.max(top, pad), ih - pad)}px`;
  $('#fxLayer').appendChild(d);
  setTimeout(() => d.remove(), 2200);
}
function showScreenEffect(m) {
  const d = document.createElement('div');
  d.className = 'screff';
  const name = m.Name || '';
  if (/集中線|線/.test(name)) {
    d.style.background = 'repeating-conic-gradient(from 0deg at 50% 50%,' +
      'rgba(255,255,255,.85) 0deg 1.2deg, transparent 1.2deg 5deg)';
    d.style.maskImage = 'radial-gradient(circle, transparent 26%, #000 62%)';
    d.style.webkitMaskImage = 'radial-gradient(circle, transparent 26%, #000 62%)';
  } else if (/光|キラ/.test(name)) {
    d.style.background = 'radial-gradient(circle at 50% 45%,rgba(255,255,230,.9),transparent 55%)';
  } else if (/爆発|斬撃|打撃/.test(name)) {
    d.style.background = 'radial-gradient(circle at 50% 50%,rgba(255,220,150,.95),rgba(255,90,40,.5) 40%,transparent 68%)';
  } else {
    d.style.background = 'radial-gradient(circle at 50% 50%,rgba(200,220,255,.45),transparent 62%)';
  }
  d.style.opacity = 0;
  d.style.transition = 'opacity .12s';
  $('#fxLayer').appendChild(d);
  requestAnimationFrame(() => d.style.opacity = 1);
  setTimeout(() => { d.style.opacity = 0; setTimeout(() => d.remove(), 300); }, 900);
}

/* ================= 文字窗口 ================= */
/* Unity/TMP 富文本：<sprite=0> 之类是「行内图标」（SpriteAsset 图集里的第 N 张，
   原作里显示成心形/汗滴之类的装饰）。网页端没有对应图集，直接剥掉，
   否则会把标签原文当成剧情字显示出来。 */
const RICH_TAG = new RegExp('</?(?:sprite|color|size|material|font|b|i|u|s|ruby|quad|' +
  'link|mark|style|align|cspace|mspace|indent|pos|space|alpha|voffset|' +
  'lowercase|uppercase|smallcaps|nobr|page)(?:=[^<>]*)?>', 'gi');
function cleanText(s) { return String(s ?? '').replace(RICH_TAG, ''); }
/* 一句台词的最终输出：先查当前语言译文，查不到就用日文原文（i18n 的核心策略）。
   field 将来若要显示别名字段（如 Message_en）可以复用同一个出口。 */
function lineText(m, field) { return cleanText(L10N.msg(m, field)); }

function showWindow(m) {
  const w = $('#msgWindow'), n = $('#msgName');
  if (!SET.showWin) return Promise.resolve();
  w.classList.add('on');
  const who = spkName(m);
  if (who) { n.textContent = who; n.classList.add('on'); }
  else n.classList.remove('on');
  $('#msgText').classList.toggle('narr', !who);
  return wait(Math.max(num(m.Time, .2), .05) * 1000);
}
function hideWindow(m) {
  $('#msgWindow').classList.remove('on');
  return wait(Math.max(num(m.Time, .2), .05) * 1000);
}

function typeText(text) {
  const el = $('#msgText');
  const gen = ST.run;                 // 归属哪一次播放循环
  el.textContent = '';
  ST.typeSkip = false;
  ST.typing = true;
  return new Promise(res => {
    let i = 0;
    const finish = () => {
      ST.typing = false;
      if (gen === ST.run) el.textContent = text;   // 已被跳转取代就别覆盖新台词
      res();
    };
    const step = () => {
      if (gen !== ST.run) { ST.typing = false; res(); return; }
      if (ST.typeSkip || i >= text.length) { finish(); return; }
      const ch = text[i++];
      el.textContent += ch;
      const base = /[、。！？…]/.test(ch) ? 85 : (/[\u3000\s]/.test(ch) ? 40 : 22);
      setTimeout(step, base / (ST.speed * ST.typeRate));
    };
    step();
  });
}

/* ================= 音频 ================= */
let bgmAudio = null, loopSe = null;

function fadeTo(audio, target, ms, cb) {
  const steps = 24, dur = Math.max(ms, 40) / steps;
  const start = audio.volume, delta = (target - start) / steps;
  let i = 0;
  const t = setInterval(() => {
    i++;
    audio.volume = Math.max(0, Math.min(1, start + delta * i));
    if (i >= steps) { clearInterval(t); cb && cb(); }
  }, dur);
}

function playMusic(m) {
  const src = SFX[m.Music];
  if (!src) return;
  // 同一首还在（可能只是被暂停）：直接续上，避免重播/跳转时重复起播、音量叠加
  if (bgmAudio && bgmAudio.dataset.src === src) {
    if (bgmAudio.paused) {
      bgmAudio.volume = SET.bgmVol;
      bgmAudio.play().catch(() => { });
    }
    return;
  }
  const fade = num(m.FadeTime, 0.6);
  const old = bgmAudio;
  bgmAudio = new Audio(src);
  bgmAudio.dataset.src = src;      // 记录下来源，供去重与恢复判断
  bgmAudio.loop = true;
  bgmAudio.volume = 0;
  bgmAudio.play().catch(() => { });
  fadeTo(bgmAudio, SET.bgmVol, fade * 1000);
  if (old) fadeTo(old, 0, fade * 1000, () => { try { old.pause(); } catch (e) { } });
}

function stopMusic(m) {
  if (!bgmAudio) return;
  const a = bgmAudio;
  bgmAudio = null;
  fadeTo(a, 0, num(m.FadeTime, 0.6) * 1000, () => { try { a.pause(); } catch (e) { } });
}

function playSe(m) {
  const src = SFX[m.Sound];
  if (!src) return;
  const a = new Audio(src);
  a.volume = SET.seVol;
  a.play().catch(() => { });
}

function playLoopSe(m) {
  const src = SFX[m.Sound];
  if (!src) return;
  if (loopSe) { try { loopSe.pause(); } catch (e) { } }
  loopSe = new Audio(src);
  loopSe.loop = true;
  loopSe.volume = SET.seVol;
  loopSe.play().catch(() => { });
}

function stopLoopSe() {
  if (loopSe) { try { loopSe.pause(); } catch (e) { } loopSe = null; }
}

function playVoice(id) {
  if (!id) return Promise.resolve();
  // 非角色剧情：本地没有语音文件时直接跳过，不发 audio 请求
  //（主线/活动目前拿不到语音资源，见 README；将来补上语音后 voiceRes > 0 自动生效）
  if (ST.char == null && !S.voiceRes) return Promise.resolve();
  if (ST.voice) { try { ST.voice.pause(); } catch (e) { } }
  // 角色剧情的语音目录按角色 ID 分（audio/69/…）；非角色剧情用剧情 key。
  const dir = ST.char != null ? ST.char : ST.key;
  const a = new Audio(`media/audio/${dir}/${id}.m4a`);
  a.playbackRate = Math.min(Math.max(ST.speed, 1), 3);
  a.volume = SET.voiceVol;
  ST.voice = a;
  return new Promise(res => {
    let done = false;
    const fin = () => { if (done) return; done = true; res(); };
    a.onended = fin;
    a.onerror = fin;
    setTimeout(fin, 40000);          // 硬兜底，防卡死
    /* duration 兜底：等元数据就绪后再设，且留足余量。
       注意部分 MP4 的 moov 在文件末尾，play() 刚 resolve 时 duration 还读不到，
       过早用错误值会算出偏短的等待时间（表现为"话没说完就跳下一句"）。 */
    let armed = false;
    const arm = () => {
      if (armed) return;
      armed = true;
      const d = a.duration;
      if (d && isFinite(d) && d > 0.2) {
        setTimeout(fin, d * 1000 / (a.playbackRate || 1) + 2000);
      }
    };
    a.onloadedmetadata = arm;
    a.play().then(arm).catch(() => { arm(); setTimeout(fin, 1500 / ST.speed); });
  });
}

/* ================= 指令分派 =================
 * 注意：画面/文字/语音指令只「启动」并把 Promise 塞进 pendingAll，
 * 真正等待只发生在 WaitMotion / Wait 上。
 */
const P = p => { if (p && typeof p.then === 'function') ST.pendingAll.push(p); return p; };

function runOne(m, midx, isSeek) {
  const M = m.Method;
  switch (M) {
    /* --- 画面：启动即返回 --- */
    case 'ShowBackground': P(setBackground(m.BackImage)); return;
    case 'HideBackground': P(setBackground('')); return;
    case 'ShowDoubleBackground':
    case 'ShowDoubleBackgroundOffset':
      P(setDoubleBackground(m.LBackImage, m.RBackImage)); return;
    case 'MulColorBackground': mulColorBg(m); return;
    case 'ShowStand': P(showStand(m)); return;
    case 'ShowStandAndSit': P(showStand(m)); return;
    case 'HideStand': P(hideStand(m)); return;
    case 'MotionStand': P(motionStand(m)); return;
    case 'MoveStand': P(moveStand(m)); return;
    case 'ShakeStand': shakeStand(m); return;
    case 'MulStand': mulStand(m); return;
    case 'ShowEventCg': P(setCg(m.EventImage, num(m.Time, .5))); return;
    case 'HideEventCg': P(setCg('', num(m.Time, .3))); return;
    case 'StareEventCG': {
      const img = $('#cgLayer').querySelector('img');
      if (img) { img.style.transition = 'transform 6s ease-out'; img.style.transform = 'scale(1.12)'; }
      return;
    }
    case 'EndStareEventCG': {
      const img = $('#cgLayer').querySelector('img');
      if (img) { img.style.transition = 'transform .5s'; img.style.transform = 'scale(1)'; }
      return;
    }
    case 'ZoomInEventCG':
    case 'FastZoomInEventCG': {
      const img = $('#cgLayer').querySelector('img');
      if (img) {
        img.style.transition = `transform ${num(m.Time, 1)}s ease`;
        img.style.transform = `scale(${num(m.Rate, 1.3)}) translate(${num(m.PosX, 0)}px,${num(m.PosY, 0)}px)`;
        if (num(m.Blur, 0) > 0) img.style.filter = `blur(${num(m.Blur, 0)}px)`;
        P(wait(num(m.Time, 1) * 1000));
      }
      return;
    }
    /* --- 文字 --- */
    case 'ShowMessageWindow':
      // VoiceMark: ○ 有配音 / ● 无配音（主角）
      ST.voiceHas = (m.VoiceMark === '○');
      ST.curName = spkName(m);
      P(showWindow(m));
      return;
    case 'HideMessageWindow': P(hideWindow(m)); return;
    case 'ShowMessage': {
      const text = lineText(m);
      const voiceP = ST.voiceP;          // 仅当本句确实有 VOICE 指令时非空
      const voiceAudio = ST.voice;       // 本句对应的音频对象
      ST.voiceP = null;
      const hasVoice = ST.voiceHas && !!voiceP;
      if (midx != null) ST.curMsgIdx = midx;   // 供“暂停后继续”定位
      if (!isSeek && midx != null && ST.lineMap) {
        const gi = ST.lineMap[midx];
        if (gi != null) focusLine(gi);
      }
      // 快进（按住 Ctrl）：直接落字，不播语音、不停顿
      if (ST.rush) {
        $('#msgText').textContent = text;
        ST.typing = false;
        return;
      }
      const gen = ST.run;
      P((async () => {
        if (hasVoice) {
          /* 有配音：等待时长完全由语音长度决定
           * 打字机与语音并行，语音播完立即补齐文字，再停「语音后停顿」 */
          const typeP = SET.instantText
            ? ($('#msgText').textContent = text, Promise.resolve())
            : typeText(text);
          await voiceP;
          if (gen !== ST.run) return;      // 已被跳转/暂停取代

          /* 双保险：Promise 可能因 duration 误判提前 resolve，
             以音频对象的真实播放状态为准，确保这一句真的播完 */
          if (voiceAudio && !voiceAudio.paused && !voiceAudio.ended
              && voiceAudio.currentTime > 0) {
            await new Promise(res => {
              let done = false;
              const fin2 = () => { if (!done) { done = true; res(); } };
              voiceAudio.addEventListener('ended', fin2, { once: true });
              voiceAudio.addEventListener('pause', fin2, { once: true });
              setTimeout(fin2, 30000);
            });
          }
          ST.typeSkip = true;          // 强制补全剩余文字
          await typeP;
          if (gen !== ST.run) return;
          await wait(SET.pauseAfterVoice);
        } else {
          /* 无配音：打字机 + 按字符数给阅读时间 */
          if (SET.instantText) {
            $('#msgText').textContent = text;
            await wait(0);
          } else {
            await typeText(text);
          }
          if (gen !== ST.run) return;
          await wait(SET.pauseBase + text.length * SET.pausePerChar);
        }
      })());
      return;
    }
    /* --- 语音 --- */
    case 'VOICE':
      if (ST.fastForward || ST.rush) return;   // 跳转重放 / 快进时不播语音
      if (m.Voice) {
        ST.voiceHas = true;
        ST.voiceP = playVoice(m.Voice);   // 只记录，等待交给 ShowMessage
      }
      return;
    /* --- 同步点 --- */
    case 'WaitMotion': {
      const all = ST.pendingAll.slice();
      ST.pendingAll.length = 0;
      return Promise.all(all).then(() => undefined);
    }
    case 'Wait': return wait(num(m.Time, .5) * 1000);
    /* --- 音频 --- */
    case 'PlayMusic':
      if (!ST.fastForward) playMusic(m); return;
    case 'StopMusic': stopMusic(m); return;
    case 'PlaySound':
      if (!ST.fastForward) playSe(m); return;
    case 'PlayLoopSound':
      if (!ST.fastForward) playLoopSe(m); return;
    case 'StopLoopSound': stopLoopSe(); return;
    /* --- 其它特效 --- */
    case 'Shake': P(shakeScreen(m)); return;
    case 'ScreenFlash': P(screenFlash(m)); return;
    case 'ScreenFlashIn': flashIn(m); return;
    case 'ScreenFlashOut': flashOut(m); return;
    case 'SetDrawModeSepia': setDrawMode('sepia'); return;
    case 'SetDrawModeGrayscale': setDrawMode('gray'); return;
    case 'SetDrawModeDefault': setDrawMode(''); return;
    case 'ShowPlace': P(showPlace(m)); return;
    case 'ShowStandEffect': showStandEffect(m); return;
    case 'ShowScreenEffect':
    case 'ShowScreenEffectBack': showScreenEffect(m); return;
    case 'ToUpper': $('#msgText').style.fontSize = 'calc(var(--msgFS,21px) * 1.24)'; return;
    case 'ToLower': $('#msgText').style.fontSize = ''; return;
    case 'ZoomOutEventCG': {
      const img = $('#cgLayer').querySelector('img');
      if (img) {
        img.style.transition = `transform ${num(m.Time, .5)}s ease`;
        img.style.transform = 'scale(1)';
        img.style.filter = '';
      }
      return;
    }
    case 'SwapStand': {
      const a = m.Position0 || 'Left', b = m.Position1 || 'Center';
      const ea = ST.stands.get(a), eb = ST.stands.get(b);
      const place = (el, p) => {
        el.style.left = `calc(${POS_X[p] || '50%'} + var(--standOX, 0px))`;
        el.dataset.pos = p;
      };
      if (ea) ST.stands.delete(a);
      if (eb) ST.stands.delete(b);
      if (ea) { place(ea, b); ST.stands.set(b, ea); }
      if (eb) { place(eb, a); ST.stands.set(a, eb); }
      return;
    }
    case 'ClearTalkCharacter': case 'TalkCharacter': return;
    default: return;
  }
}

function showPlace(m) {
  const L = $('#placeLayer');
  L.querySelector('span').textContent = m.PlaceName || '';
  L.style.opacity = 1;
  return wait(1800).then(() => { L.style.opacity = 0; return wait(400); });
}

/* ================= 本段文本列表（全量 + 分页 + 跟随播放） ================= */
const esc = s => String(s || '').replace(/[&<>]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const PAGE_SIZE = 40;

function buildLineIndex() {
  const out = [];
  let name = '';
  ST.methods.forEach((m, i) => {
    if (m.Method === 'ShowMessageWindow') name = spkName(m);
    else if (m.Method === 'ShowMessage') {
      out.push({ midx: i, who: name, text: lineText(m) });
    }
  });
  ST.allLines = out;
  ST.lineMap = {};
  out.forEach((l, i) => { ST.lineMap[l.midx] = i; });
  ST.linePage = 0;
  ST.curLine = -1;
  renderLineList();
}

function renderLineList() {
  const box = $('#hList');
  if (!box) return;
  const total = ST.allLines.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (ST.linePage >= pages) ST.linePage = pages - 1;
  if (ST.linePage < 0) ST.linePage = 0;
  const start = ST.linePage * PAGE_SIZE;
  const items = ST.allLines.slice(start, start + PAGE_SIZE);
  box.innerHTML = '';
  items.forEach((l, k) => {
    const gi = start + k;
    const d = document.createElement('div');
    d.className = 'hItem' + (l.who ? '' : ' narr') + (gi === ST.curLine ? ' on' : '');
    d.dataset.gi = gi;
    d.innerHTML = `<span class="hw">${esc(l.who) || '旁白'}</span>` +
      `<span class="ht">${esc(l.text)}</span>` +
      `<span class="hcpy2" title="只复制台词">💬</span>` +
      `<span class="hcpy" title="复制这句（含定位信息）">⧉</span>`;
    d.onclick = () => seekTo(l.midx);
    d.querySelector('.hcpy2').onclick = ev => {
      ev.stopPropagation();
      copyText(l.text, '已复制台词');
    };
    d.querySelector('.hcpy').onclick = ev => {
      ev.stopPropagation();
      const head = `${storyTitle(S.key)} / 第${ST.part + 1}段 第${gi + 1}句`;
      const voice = ST.methods[l.midx - 1] && ST.methods[l.midx - 1].Method === 'VOICE'
        ? ST.methods[l.midx - 1].Voice : '';
      copyText(`${head}\n${l.who || '旁白'}：${l.text}` + (voice ? `\n[voice: ${voice}]` : ''),
        '已复制这句');
    };
    box.appendChild(d);
  });
  const pager = $('#hPager');
  pager.innerHTML = '';
  if (pages > 1) {
    pager.innerHTML =
      `<button data-p="-1" ${ST.linePage === 0 ? 'disabled' : ''}>◀</button>` +
      `<span>${ST.linePage + 1} / ${pages}</span>` +
      `<button data-p="1" ${ST.linePage >= pages - 1 ? 'disabled' : ''}>▶</button>`;
    pager.querySelectorAll('button').forEach(b => {
      b.onclick = () => { ST.linePage += +b.dataset.p; renderLineList(); };
    });
  }
  const cnt = $('#hCount');
  if (cnt) cnt.textContent = `${total} 句`;
}

function focusLine(gi) {
  if (gi == null || gi < 0 || gi >= ST.allLines.length) return;
  ST.curLine = gi;
  const page = Math.floor(gi / PAGE_SIZE);
  if (page !== ST.linePage) {
    ST.linePage = page;
    renderLineList();
  }
  const box = $('#hList');
  box.querySelectorAll('.hItem').forEach(e =>
    e.classList.toggle('on', +e.dataset.gi === gi));
  const el = box.querySelector(`.hItem[data-gi="${gi}"]`);
  if (el) el.scrollIntoView({ block: 'nearest' });
}

/* ---- 跳转：只重放「画面状态」类指令，不碰文字与音频 ---- */
const REPLAY = new Set([
  'ShowBackground', 'HideBackground', 'ShowDoubleBackground',
  'ShowDoubleBackgroundOffset', 'MulColorBackground',
  'ShowStand', 'ShowStandAndSit', 'HideStand', 'MoveStand', 'MulStand',
  'ShowEventCg', 'HideEventCg',
  'SetDrawModeSepia', 'SetDrawModeGrayscale', 'SetDrawModeDefault',
]);

function hideStandInstant(m) {
  const key = (m.Position && m.Position !== 'All') ? m.Position : null;
  const targets = key ? [key] : Array.from(ST.stands.keys());
  for (const k of targets) {
    const el = ST.stands.get(k);
    if (el) { el.remove(); ST.stands.delete(k); }
  }
  updateStandScale();
}

/** 同步重建单条画面状态（不等待任何动画） */
function replayOne(m) {
  switch (m.Method) {
    case 'ShowBackground': setBackground(m.BackImage); break;
    case 'HideBackground': setBackground(''); break;
    case 'ShowDoubleBackground':
    case 'ShowDoubleBackgroundOffset':
      setDoubleBackground(m.LBackImage, m.RBackImage); break;
    case 'MulColorBackground': mulColorBg(m); break;
    case 'ShowStand':
    case 'ShowStandAndSit': showStand(m, true); break;
    case 'HideStand': hideStandInstant(m); break;
    case 'MoveStand': {
      const from = m.PositionFrom || 'Center';
      const to = m.PositionTo || 'Center';
      const el = ST.stands.get(from);
      if (el) {
        el.style.left = `calc(${POS_X[to] || '50%'} + var(--standOX, 0px))`;
        el.dataset.pos = to;
        ST.stands.delete(from);
        ST.stands.set(to, el);
      }
      break;
    }
    case 'MulStand': mulStand(m); break;
    case 'ShowEventCg': setCg(m.EventImage, 0.01); break;
    case 'HideEventCg': setCg(''); break;
    case 'SetDrawModeSepia': setDrawMode('sepia'); break;
    case 'SetDrawModeGrayscale': setDrawMode('gray'); break;
    case 'SetDrawModeDefault': setDrawMode(''); break;
  }
}

let seeking = false;

/* 跳到某条台词：重建画面状态后从该句继续播 */
async function seekTo(midx) {
  if (seeking || !ST.methods.length) return;
  if (midx == null || midx < 0 || midx >= ST.methods.length) return;
  seeking = true;
  try {
    haltLoop();                    // 只杀循环与语音，保留 BGM
    resumeAudio();                 // 暂停时挂起的 BGM/循环音效续上
    ST.typeSkip = true;            // 掐断还在跑的打字机
    resetStage();
    ST.fastForward = true;

    // 1) 同步重建画面状态（几百条指令也就几十毫秒）
    for (let i = 0; i < midx; i++) {
      const m = ST.methods[i];
      if (REPLAY.has(m.Method)) {
        try { replayOne(m); } catch (e) { }
      }
    }
    ST.fastForward = false;

    // 2) 恢复说话人与配音标记
    let lastWindow = null;
    for (let i = 0; i < midx; i++) {
      if (ST.methods[i].Method === 'ShowMessageWindow') lastWindow = ST.methods[i];
    }
    const whoJump = lastWindow ? spkName(lastWindow) : '';
    ST.curName = whoJump;
    ST.voiceHas = !!(lastWindow && lastWindow.VoiceMark === '○');
    if (lastWindow) {
      const w = $('#msgWindow'), n = $('#msgName');
      w.classList.add('on');
      if (whoJump) { n.textContent = whoJump; n.classList.add('on'); }
      else n.classList.remove('on');
      $('#msgText').classList.toggle('narr', !whoJump);
    }

    // 3) 目标句的语音挂在 midx 前一条 VOICE 上，补播它
    ST.voiceP = null;
    const prev = ST.methods[midx - 1];
    if (prev && prev.Method === 'VOICE' && prev.Voice) {
      ST.voiceP = playVoice(prev.Voice);
    }

    ST.idx = midx;
    ST.curMsgIdx = midx;          // 立刻更新：连按方向键才不会被当成「还在原句」
    ST.running = true;
    setPlayBtn(true);
    if (ST.lineMap && ST.lineMap[midx] != null) focusLine(ST.lineMap[midx]);
    updateProgress();
    await wait(50);
    continueLoop();
  } finally {
    seeking = false;
  }
}

/* ================= 播放控制 ================= */
async function playPart(partIdx) {
  // 只换播放循环，不销毁 BGM：重播/切段后音乐还能续上（脚本里的 PlayMusic 会自然换曲）
  haltLoop();
  stopLoopSe();
  resumeAudio();
  await wait(0);
  ST.part = partIdx;
  ST.methods = S.data.parts[partIdx].methods;
  ST.idx = 0;
  ST.running = true;
  ST.curMsgIdx = null;
  resetStage();
  buildLineIndex();
  ST.curName = '';
  renderTabs();
  updateInfo();
  setPlayBtn(true);
  continueLoop();
}

async function continueLoop() {
  const gen = ST.run;          // 记下本次循环的代号
  while (ST.running && gen === ST.run && ST.idx < ST.methods.length) {
    const m = ST.methods[ST.idx];
    const cur = ST.idx;
    ST.idx++;
    updateProgress();
    try { await runOne(m, cur); }
    catch (e) { console.warn('指令失败', m.Method, e); }
  }
  // 已经被暂停/跳转/重播取代：直接退出，别和新循环抢指令、也别改按钮状态
  if (gen !== ST.run) return;
  if (ST.running && ST.idx >= ST.methods.length) {
    if (ST.auto && ST.part + 1 < S.data.parts.length) {
      await wait(700);
      if (ST.running) playPart(ST.part + 1);
    } else {
      ST.running = false; setPlayBtn(false);
    }
  }
}

function resetStage() {
  ['#bgLayer', '#cgLayer', '#fxLayer', '#standLayer'].forEach(s => { $(s).innerHTML = ''; });
  $('#flashLayer').style.opacity = 0;
  $('#placeLayer').style.opacity = 0;
  $('#msgWindow').classList.remove('on');
  $('#msgText').style.fontSize = '';
  setDrawMode('');
  ST.stands.clear();
  ST.pendingAll.length = 0;
  ST.voiceHas = false;
  ST.voiceP = null;
  if (ST.voice) { try { ST.voice.pause(); } catch (e) { } }
}

function setPlayBtn(on) {
  $('#btnPlay').textContent = on ? L10N.t('ctrl.pause', '⏸ 暂停')
    : L10N.t('ctrl.play', '▶ 播放');
}
function stopAll() {
  ST.running = false;
  ST.run++;                       // 作废正在跑的播放循环
  skipAll();
  ST.pendingAll.length = 0;
  if (ST.voice) { try { ST.voice.pause(); } catch (e) { } }
  if (bgmAudio) { try { bgmAudio.pause(); } catch (e) { } bgmAudio = null; }
  if (loopSe) { try { loopSe.pause(); } catch (e) { } loopSe = null; }
}

/** 只中止当前播放循环、掐断语音与等待，不动 BGM/SE（句间跳转用） */
function haltLoop() {
  ST.running = false;
  ST.run++;
  skipAll();
  ST.pendingAll.length = 0;
  if (ST.voice) { try { ST.voice.pause(); } catch (e) { } }
}

/** 暂停：停循环/语音/等待，BGM 与循环音效暂停但**保留引用**（后面能续上） */
function pausePlayback() {
  haltLoop();
  if (bgmAudio && !bgmAudio.paused) { try { bgmAudio.pause(); } catch (e) { } }
  if (loopSe && !loopSe.paused) { try { loopSe.pause(); } catch (e) { } }
}

/** 恢复播放：把暂停时挂起的 BGM / 循环音效续上 */
function resumeAudio() {
  if (bgmAudio && bgmAudio.paused) { try { bgmAudio.play().catch(() => { }); } catch (e) { } }
  if (loopSe && loopSe.paused) { try { loopSe.play().catch(() => { }); } catch (e) { } }
}
function updateProgress() {
  const p = ST.methods.length ? ST.idx / ST.methods.length * 100 : 0;
  $('#progress > i').style.width = p + '%';
}
function updateInfo() {
  const p = S.data?.parts[ST.part];
  if (!p) return;
  const n = ST.methods.filter(x => x.Method === 'ShowMessage').length;
  $('#pinfo').textContent = `${p.file} · 第 ${ST.part + 1}/${S.data.parts.length} 段 · ${n} 句`;
}

/* ================= 句子级跳转 / 暂停 / 快进 ================= */
/* 快捷键与鼠标点击共用这几个入口 */
function lineIdxList() {
  return (ST.allLines || []).map(l => l.midx);
}

/** dir=1 下一句，dir=-1 上一句（按 ShowMessage 的位置跳，画面状态由 seekTo 重建） */
function stepLine(dir) {
  if (!ST.methods.length || !S.data) return;
  const list = lineIdxList();
  if (!list.length) return;
  const cur = (ST.curMsgIdx != null) ? ST.curMsgIdx : Math.max(0, ST.idx - 1);
  if (dir > 0) {
    const nx = list.find(i => i > cur);
    if (nx == null) {                     // 本段已到末尾：顺延到下一段
      const n = (ST.part || 0) + 1;
      if (n < S.data.parts.length) playPart(n);
      return;
    }
    seekTo(nx);
  } else {
    const back = list.filter(i => i < cur);
    seekTo(back.length ? back[back.length - 1] : list[0]);
  }
}

function nextLine() {
  // 打字机还没吐完：先补全这一句（galgame 习惯），再按一次才翻页
  if (ST.typing) { ST.typeSkip = true; skipAll(); return; }
  stepLine(1);
}

function prevLine() {
  if (ST.typing) { ST.typeSkip = true; skipAll(); }
  stepLine(-1);
}

function togglePlay() {
  if (!S.data) return;
  $('#btnPlay').click();
}

let rushTimer = null;
/** 按住 K 快进：跳过所有等待、掐断打字机与语音，逐句极速推进 */
function setRush(on) {
  on = !!on;
  if (on === ST.rush) return;
  ST.rush = on;
  if (on) {
    if (!ST.running) togglePlay();          // 暂停状态下按 Ctrl 先续播
    ST.typeSkip = true;
    if (ST.voice) { try { ST.voice.pause(); } catch (e) { } }
    rushTimer = setInterval(() => {
      ST.typeSkip = true;
      if (ST.voice) { try { ST.voice.pause(); } catch (e) { } }
      skipAll();
    }, 16);
  } else if (rushTimer) {
    clearInterval(rushTimer);
    rushTimer = null;
  }
}

/* ================= 设置 ================= */
const DEFAULTS = { alpha: 90, fs: 21, typeRate: 1, showWin: true, standH: 100,
              bgmVol: 0.5, seVol: 0.5, voiceVol: 0.5, fit: '1x',
              instantText: false, pausePerChar: 90, pauseBase: 400,
              pauseAfterVoice: 250, showPartial: false,
              // 默认按「实装日期 新→旧」：列表里最新的角色在最上面，
              // 比按 ID 排更符合"我想看看最近有什么新内容"的使用习惯
              sortBy: 'date-desc', rarSel: '', useX2: true,
              standY: 0, standOX: 0, msgBg: '10,14,22', msgColor: '#f2f5fb',
              msgFont: 'sans', msgLH: 1.62, msgStroke: 1.2,
              msgH: 150, msgY: 26,          // 对话框高度 / 垂直位置（都是 px）
              msgX: 0, msgAlign: 'left',
              nameX: 0, nameY: 0, nameAlign: 'left' };
/* 工作副本：所有改动作用在 SET 上，恢复默认时 Object.assign(SET, DEFAULTS) 即可 */
const SET = { ...DEFAULTS };

const FONT_MAP = {
  sans: '"Microsoft YaHei","Hiragino Sans","Noto Sans JP",sans-serif',
  serif: '"Yu Mincho","Hiragino Mincho ProN","Noto Serif JP",serif',
  yu: '"Yu Gothic","Meiryo","Microsoft YaHei",sans-serif',
};
function applySettings() {
  const st = $('#stage');
  st.style.setProperty('--msgA', (SET.alpha / 100).toFixed(2));
  st.style.setProperty('--msgFS', SET.fs + 'px');
  st.style.setProperty('--msgBg', SET.msgBg || '10,14,22');
  st.style.setProperty('--msgColor', SET.msgColor || '#f2f5fb');
  st.style.setProperty('--msgLH', SET.msgLH ?? 1.62);
  // 对话框高度与垂直位置（用户嫌挡住立绘时可以往上/往下挪）
  st.style.setProperty('--msgH', (SET.msgH ?? 150) + 'px');
  st.style.setProperty('--msgY', (SET.msgY ?? 26) + 'px');
  st.style.setProperty('--msgStroke', (SET.msgStroke ?? 1.2) + 'px');
  st.style.setProperty('--msgFont', FONT_MAP[SET.msgFont] || FONT_MAP.sans);
  st.style.setProperty('--msgX', (SET.msgX || 0) + 'px');
  st.style.setProperty('--msgAlign', SET.msgAlign || 'left');
  st.style.setProperty('--nameX', (SET.nameX || 0) + 'px');
  st.style.setProperty('--nameY', (SET.nameY || 0) + 'px');
  const nmEl = $('#msgName');
  if (nmEl) {
    nmEl.classList.remove('align-left', 'align-center', 'align-right');
    nmEl.classList.add('align-' + (SET.nameAlign || 'left'));
  }
  // 立绘高度上限锁死 720（舞台高），超出会被裁切导致"缺头"
  st.style.setProperty('--standH', Math.min(720 * SET.standH / 100, 720) + 'px');
  st.style.setProperty('--standY', (SET.standY || 0) + 'px');
  st.style.setProperty('--standOX', (SET.standOX || 0) + 'px');
  ST.typeRate = SET.typeRate;
  fitStage();
  $('#valA').textContent = SET.alpha + '%';
  $('#valFS').textContent = SET.fs + 'px';
  $('#valType').textContent = Math.round(ST.typeRate * 100) + '%';
  $('#valStandY').textContent = (SET.standY || 0) + 'px';
  $('#valStandX').textContent = (SET.standOX || 0) + 'px';
  $('#valLH').textContent = (SET.msgLH ?? 1.62).toFixed(2);
  if ($('#valMsgH')) $('#valMsgH').textContent = (SET.msgH ?? 150) + 'px';
  if ($('#valMsgY')) $('#valMsgY').textContent = Math.round(SET.msgY ?? 26) + 'px';
  $('#valStroke').textContent = (SET.msgStroke ?? 1.2) + 'px';
  $('#valMsgX').textContent = (SET.msgX || 0) + 'px';
  $('#valNameX').textContent = (SET.nameX || 0) + 'px';
  $('#valNameY').textContent = (SET.nameY || 0) + 'px';
  updateWinBtn();
  $('#valBgm').textContent = Math.round(SET.bgmVol * 100) + '%';
  $('#valSe').textContent = Math.round(SET.seVol * 100) + '%';
  $('#valVoice').textContent = Math.round(SET.voiceVol * 100) + '%';
  $('#valPpc').textContent = SET.pausePerChar + 'ms';
  $('#valPb').textContent = SET.pauseBase + 'ms';
  $('#valPav').textContent = SET.pauseAfterVoice + 'ms';
  if (bgmAudio) bgmAudio.volume = Math.min(1, SET.bgmVol);
  if (loopSe) loopSe.volume = Math.min(1, SET.seVol);
  if (ST.voice) ST.voice.volume = Math.min(1, SET.voiceVol);
  localStorage.setItem('escah_set', JSON.stringify(SET));
}
/* 把所有控件的值设为 SET 当前值（loadSettings 与恢复默认共用） */
function syncSettingControls() {
  $('#setA').value = SET.alpha;
  $('#setFS').value = SET.fs;
  $('#setType').value = Math.round(SET.typeRate * 100);
  $('#setNameX').value = SET.nameX ?? 0;
  $('#setNameY').value = SET.nameY ?? 0;
  $('#setNameAlign').value = SET.nameAlign || 'left';
  $('#setStand').value = SET.standH;
  $('#setBgm').value = Math.round((SET.bgmVol ?? .7) * 100);
  $('#setSe').value = Math.round((SET.seVol ?? .9) * 100);
  $('#setVoice').value = Math.round((SET.voiceVol ?? 1) * 100);
  $('#setFit').value = SET.fit || '1x';
  $('#setTextMode').value = SET.instantText ? 'instant' : 'type';
  $('#setPpc').value = SET.pausePerChar ?? 90;
  $('#setPb').value = SET.pauseBase ?? 400;
  $('#setPav').value = SET.pauseAfterVoice ?? 250;
  $('#setPartial').checked = !!SET.showPartial;
  const ux = $('#setUseX2');
  if (ux) ux.checked = SET.useX2 !== false;
  $('#setStandY').value = SET.standY ?? 0;
  $('#setStandX').value = SET.standOX ?? 0;
  $('#setMsgBg').value = SET.msgBg || '10,14,22';
  $('#setMsgColor').value = SET.msgColor || '#f2f5fb';
  $('#setMsgFont').value = SET.msgFont || 'sans';
  $('#setLH').value = Math.round((SET.msgLH ?? 1.62) * 100);
  if ($('#setMsgH')) $('#setMsgH').value = SET.msgH ?? 150;
  if ($('#setMsgY')) $('#setMsgY').value = Math.round(SET.msgY ?? 26);
  $('#setStroke').value = Math.round((SET.msgStroke ?? 1.2) * 10);
  $('#setMsgX').value = SET.msgX ?? 0;
  $('#setAlign').value = SET.msgAlign || 'left';
  $('#sortBy').value = SET.sortBy || 'id';
  const rarArr = (SET.rarSel || '').split(',');
  document.querySelectorAll('.rar-row button').forEach(b =>
    b.classList.toggle('on', rarArr.includes(b.dataset.r)));
}

function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem('escah_set') || '{}');
    Object.assign(SET, s);
  } catch (e) { }
  if (!SET.fit) SET.fit = '1x';   // 旧配置升级：默认改为不放大
  // 音量默认值从 70/90/100 降到 50：老用户 localStorage 里存着旧默认值的，
  // 一次性迁移过来（只在那三个值正好等于旧默认时改，避免覆盖用户自己调过的）
  // msgY 短暂存过百分比（默认 3.6）：只有小于 10 的才当百分比残留换算成 px，
  // 避免把正常的 26px 误判成 26%。
  if (typeof SET.msgY === 'number' && SET.msgY > 0 && SET.msgY < 10) {
    SET.msgY = Math.round(SET.msgY / 100 * 720);
  }
  if (SET.bgmVol === 0.7 && SET.seVol === 0.9 && SET.voiceVol === 1) {
    SET.bgmVol = 0.5;
    SET.seVol = 0.5;
    SET.voiceVol = 0.5;
  }
  if (SET.standY === -16) SET.standY = 0;   // 旧版默认值（写反了）重置
  syncSettingControls();
  applySettings();
}

/* 恢复默认：清空浏览器保存的设置，回到出厂值 */
function resetSettings() {
  Object.assign(SET, structuredClone(DEFAULTS));
  localStorage.removeItem('escah_set');
  syncSettingControls();
  applySettings();
  toast('已恢复默认设置');
}
$('#btnSet').onclick = () => { const s = $('#settings'); s.hidden = !s.hidden; };
if ($('#setClose')) {
  $('#setClose').onclick = () => { $('#settings').hidden = true; };
}
$('#btnReset').onclick = resetSettings;
$('#setA').oninput = e => { SET.alpha = +e.target.value; applySettings(); };
$('#setFS').oninput = e => { SET.fs = +e.target.value; applySettings(); };
$('#setType').oninput = e => { SET.typeRate = +e.target.value / 100; applySettings(); };
/* 显示/隐藏对话框：放在控制栏（#btnWin），不再进设置面板 */
function updateWinBtn() {
  const b = $('#btnWin');
  if (b) b.classList.toggle('off', !SET.showWin);
}
$('#btnWin').onclick = () => {
  SET.showWin = !SET.showWin;
  applySettings();
  const w = $('#msgWindow');
  if (SET.showWin) w.classList.add('on');   // 立即显：把当前框的 .on 加回
  else w.classList.remove('on');            // 立即隐
  updateWinBtn();
};
$('#setNameX').oninput = e => { SET.nameX = +e.target.value; applySettings(); };
$('#setNameY').oninput = e => { SET.nameY = +e.target.value; applySettings(); };
$('#setNameAlign').onchange = e => { SET.nameAlign = e.target.value; applySettings(); };
$('#setStand').oninput = e => { SET.standH = +e.target.value; applySettings(); };
$('#setPartial').onchange = e => { SET.showPartial = e.target.checked; applySettings(); };
if ($('#setUseX2')) {
  $('#setUseX2').onchange = e => {
    SET.useX2 = e.target.checked;
    applySettings();
    // 已渲染的图不会自动换，重画当前画面即可
    if (ST.methods.length) seekTo(ST.curMsgIdx ?? 0);
    toast(SET.useX2 ? '将优先使用超分图' : '只用原图（更省内存）');
  };
}
$('#setStandY').oninput = e => { SET.standY = +e.target.value; applySettings(); };
$('#setStandX').oninput = e => { SET.standOX = +e.target.value; applySettings(); };
$('#setMsgBg').onchange = e => { SET.msgBg = e.target.value; applySettings(); };
$('#setMsgColor').onchange = e => { SET.msgColor = e.target.value; applySettings(); };
$('#setMsgFont').onchange = e => { SET.msgFont = e.target.value; applySettings(); };
$('#setLH').oninput = e => { SET.msgLH = +e.target.value / 100; applySettings(); };
$('#setMsgH').oninput = e => { SET.msgH = +e.target.value; applySettings(); };
$('#setMsgY').oninput = e => { SET.msgY = +e.target.value; applySettings(); };
$('#setStroke').oninput = e => { SET.msgStroke = +e.target.value / 10; applySettings(); };
$('#setMsgX').oninput = e => { SET.msgX = +e.target.value; applySettings(); };
$('#setAlign').onchange = e => { SET.msgAlign = e.target.value; applySettings(); };
$('#setBgm').oninput = e => { SET.bgmVol = +e.target.value / 100; applySettings(); };
$('#setSe').oninput = e => { SET.seVol = +e.target.value / 100; applySettings(); };
$('#setVoice').oninput = e => { SET.voiceVol = +e.target.value / 100; applySettings(); };
$('#setFit').onchange = e => { SET.fit = e.target.value; applySettings(); };
$('#setTextMode').onchange = e => { SET.instantText = e.target.value === 'instant'; applySettings(); };
$('#setPpc').oninput = e => { SET.pausePerChar = +e.target.value; applySettings(); };
$('#setPb').oninput = e => { SET.pauseBase = +e.target.value; applySettings(); };
$('#setPav').oninput = e => { SET.pauseAfterVoice = +e.target.value; applySettings(); };

/* ================= 剧情来源（侧栏列表） ================= */
/* SRC 只决定「左侧列表显示哪一类剧情」，与正在播放的内容无关；
   播放状态一律由 S.key / S.data 描述。 */
let SRC = 'char';
const S = { key: null, kind: 'char', char: null, data: null, voiceRes: 0 };

/** 剧情标题（用于列表与复制定位信息） */
function storyTitle(key) {
  if (isCharKey(key)) {
    const id = +String(key).slice(5);
    return NAMES[id] || ('角色 ' + id);
  }
  const adv = ADV.find(a => a.key === key);
  const m = /^adv_(\w+?)(?:_(\d+))?$/.exec(key || '');
  if (!m) return String(key || '');
  // tutorial / op 是单个整包，没有「第X话」
  const title = m[2] ? `${ADV_LABEL[m[1]] || m[1]} 第${m[2]}话`
    : (ADV_LABEL[m[1]] || m[1]);
  return title + (adv ? '' : '（未接入）');
}

function toast(msg) {
  const el = $('#toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('on');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('on'), 1600);
}

function copyText(text, msg) {
  const ok = () => toast(msg);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(ok).catch(fallback);
  } else fallback();
  function fallback() {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); ok(); } catch (e) { }
    ta.remove();
  }
}

/** 稀有度归一化：SSR/SR/R，其余归为 other */
function rarityOf(cid) {
  const r = (CHARMETA[cid] || {}).r || '';
  return (r === 'SSR' || r === 'SR' || r === 'R') ? r : 'other';
}

/* ================= 云端有、本地没装的角色 =================
 * index.js 是随程序分发的静态索引，不随资源清单更新。
 * 所以官方新上线的角色在控制台能装（那边从清单推导），但播放器侧栏里
 * 根本看不到 —— 用户会以为"没有这个角色"。这里补一次：
 * 拉 /api/cdn/units，把"云端有、index.js 里没有"的角色并进列表并标未下载。
 */
let CLOUD_ONLY = [];
let _cloudLoaded = false;

function allChars() {
  return CLOUD_ONLY.length ? INDEX.concat(CLOUD_ONLY) : INDEX;
}

async function loadCloudChars() {
  if (_cloudLoaded || !window.PACKSTATE) return;
  _cloudLoaded = true;
  try {
    const r = await fetch('api/cdn/units', { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    if (d.needManifest) return;              // 清单还没下，等用户装完再说
    const have = new Set((INDEX || []).map(c => +c.id));
    const extra = [];
    ((d.units || {}).char || []).forEach(function (k) {
      const id = +String(k).slice(5);
      if (id && !have.has(id)) extra.push({ id: id, parts: 0, lines: 0, cloud: true });
    });
    if (extra.length) {
      CLOUD_ONLY = extra;
      renderSidebar((($('#search') || {}).value) || '');
      toast('发现 ' + extra.length + ' 个新角色（未下载）：装完就会出现在这里');
    }
  } catch (e) { /* 纯静态打开时没有服务端，忽略 */ }
}

function renderSidebar(filter = '') {
  const box = $('#charList');
  if (!box) return;
  box.innerHTML = '';
  const flt = $('#filters');
  if (flt) flt.style.display = SRC === 'char' ? '' : 'none';   // 排序/稀有度只对角色有意义
  if (SRC !== 'char') { renderAdvList(filter); return; }
  const f = filter.trim().toLowerCase();
  const sel = SET.rarSel ? SET.rarSel.split(',') : [];
  // 「已下载 / 未下载」筛选：和安装列表、CG 页同一套状态口径
  const dlf = ($('#dlFilter') || {}).value || '';
  const isHave = (cid) => {
    const st = window.PACKSTATE ? PACKSTATE.status('char_' + cid) : 'unknown';
    return st === 'ready' || st === 'soft-missing' || st === 'partial';
  };

  let list = allChars().filter(c => {
    if (dlf === 'have' && !isHave(c.id)) return false;
    if (dlf === 'miss' && isHave(c.id)) return false;
    if (sel.length && !sel.includes(rarityOf(c.id))) return false;
    if (!f) return true;
    const m = CHARMETA[c.id] || {};
    return String(c.id).includes(f) ||
      (NAMES[c.id] || '').toLowerCase().includes(f) ||
      (m.zh || '').toLowerCase().includes(f);
  });

  // 排序走共用函数（common.js），和 CG 页、安装列表同一套规则
  list = ESC.sortItems(list, SET.sortBy || 'id', c => ({
    id: String(c.id).padStart(6, '0'),
    name: NAMES[c.id] || '',
    date: (CHARMETA[c.id] || {}).d || '',
  }));

  /* 资源没下载的排到列表底部（稳定排序，各自内部顺序不变）。
     只有本地服务端在线时才有 status，否则一律当"未知"不折腾顺序。 */
  const stOf = key => (window.PACKSTATE ? PACKSTATE.status(key) : 'unknown');
  const rankOf = key => (stOf(key) === 'missing' ? 1 : 0);
  list = list.slice().sort((a, b) => rankOf('char_' + a.id) - rankOf('char_' + b.id));

  /* 命中计数：列表自身可滚动，结果多于可视区时用户会以为"搜不到" */
  const hit = $('#hit');
  if (hit) {
    const active = !!f || sel.length > 0;
    hit.textContent = !active ? ''
      : (list.length
        ? L10N.t('search.hit', '命中 {n} 个角色（可滚动）').replace('{n}', list.length)
        : L10N.t('search.none', '没有匹配的角色'));
    hit.className = !active ? '' : (list.length ? 'on' : 'none');
  }

  list.forEach(c => {
    const nm = NAMES[c.id] || ('角色 ' + c.id);
    const m = CHARMETA[c.id] || {};
    const rar = rarityOf(c.id);
    const st = stOf('char_' + c.id);
    const d = document.createElement('div');
    d.className = 'char-btn' + (S.key === 'char_' + c.id ? ' on' : '')
      + (st === 'missing' ? ' nodl' : '');
    // 带上标识：自动化测试和调试时不用靠 title 去猜是哪一行
    d.dataset.id = c.id;
    d.dataset.key = 'char_' + c.id;
    d.title = `${nm}${m.zh ? ' / ' + m.zh : ''}\n` +
      `ID ${c.id} · ${m.r || '未收录'}${m.d ? ' · ' + m.d : ''}` +
      (st === 'missing' ? '\n资源未下载：点「资源管理」安装后即可播放' : '') +
      (st === 'soft-missing' ? '\n公共素材未下载：可播放但可能缺背景/音乐' : '');
    // 头像要不要请求，看**头像素材本身**是否已下载，而不是"该角色剧情是否安装"。
    // 以前只用剧情状态判定 → 只装 2 个角色却下了全部 378 张头像时，
    // 另外 382 个角色全变成首字色块，用户会以为头像没加载出来。
    // 头像没下载时才退回色块（避免 384×N 个 404 刷屏）。
    const haveImg = (st === 'ready' || st === 'soft-missing' || st === 'partial')
      || (window.PACKSTATE && PACKSTATE.avatarsReady && PACKSTATE.avatarsReady());
    d.innerHTML =
      (haveImg
        ? `<img alt="" loading="lazy">`
        : `<span class="ava-ph">${esc((nm || '?').trim().slice(0, 1))}</span>`) +
      `<span class="info"><span class="nm">${esc(nm)}</span>` +
      `<span class="sub"><span class="rar" data-r="${rar}">${m.r || '—'}</span>` +
      `<span>${c.lines ? c.lines + ' 句' : '未安装'}</span>` +
      (st === 'missing' ? '<span class="badge nodl">未下载</span>' :
        st === 'soft-missing' ? '<span class="badge soft">缺素材</span>' : '') +
      `</span></span>` +
      `<span class="cpy" title="复制角色名">⧉</span>`;
    if (haveImg) applyChain(d.querySelector('img'), avatarCands(c.id));
    d.onclick = () => openChar(c.id);
    d.querySelector('.cpy').onclick = ev => {
      ev.stopPropagation();
      copyText(nm, `已复制：${nm}`);
    };
    box.appendChild(d);
  });
}

/** 头像候选链（imgmap 没有 avatar 段，直接按命名拼） */
function avatarCands(id) {
  const out = [];
  const push = p => { if (p && out.indexOf(p) < 0) out.push(p); };
  if (SET.useX2 !== false) push((IMGX2.avatar || {})[id]);
  push((IMGBASE.avatar || {})[id]);
  push((IMGMAP.avatar || {})[id]);
  // 头像导出的是 **.png**（原图格式），不是 .webp —— 以前只试 .webp，
  // 于是全新环境里 378 个头像全部 404、侧栏头像整片空白。
  // 导出的是 **.png**，所以硬编码兜底时先试 png 再试 webp：
  // 顺序反了会让每个角色都先吃一个 404 才回退。
  push(`media/img/avatar/${id}.png`);
  push(`media/img/avatar/${id}.webp`);
  push(`media/img/avatar/${id}.jpg`);
  if (SET.useX2 !== false) {
    push(`media/img_x2/avatar/${id}.webp`);
    push(`media/img_x2/avatar/${id}.png`);
  }
  return out.map(imgSrc);
}
/** 非角色剧情列表（主线 / 活动 / 其他） */
/* 侧栏分批渲染：全部剧情接进来后「其他」有 85 项、活动 94 项，
   一次性建 DOM 会明显卡顿，所以每次先画 BATCH 项，滚到底再追加。 */
const ADV_BATCH = 45;
let advAll = [], advCursor = 0;

function renderAdvList(filter = '') {
  const box = $('#charList');
  if (!box) return;
  const f = filter.trim().toLowerCase();
  const kinds = ADV_MENU[SRC] || [SRC];
  advAll = ADV.filter(a => kinds.includes(a.kind))
    .filter(a => !f || storyTitle(a.key).toLowerCase().includes(f)
      || String(a.first || '').toLowerCase().includes(f) || a.key.includes(f));
  /* 未下载资源的排到后面，和角色列表同口径 */
  const stOf2 = key => (window.PACKSTATE ? PACKSTATE.status(key) : 'unknown');
  advAll = advAll.slice().sort((a, b) =>
    (stOf2(a.key) === 'missing' ? 1 : 0) - (stOf2(b.key) === 'missing' ? 1 : 0));

  const hit = $('#hit');
  if (hit) {
    hit.textContent = f
      ? L10N.t('search.hitAdv', '命中 {n} 部剧情').replace('{n}', advAll.length)
      : '';
    hit.className = f ? (advAll.length ? 'on' : 'none') : '';
  }

  box.innerHTML = '';
  box.scrollTop = 0;
  advCursor = 0;
  if (!advAll.length) {
    const d = document.createElement('div');
    d.className = 'advEmpty';
    d.textContent = kinds.length && ADV.some(a => kinds.includes(a.kind))
      ? L10N.t('adv.nomatch', '没有匹配的剧情')
      : L10N.t('adv.none', '这一类剧情还没有接入，用 pipeline.cli adv-all 接入');
    box.appendChild(d);
    return;
  }
  appendAdvChunk();
}

/** 追加下一批（滚动到底部时由 #charList 的 scroll 事件调用） */
function appendAdvChunk() {
  const box = $('#charList');
  if (!box || advCursor >= advAll.length) return;
  const frag = document.createDocumentFragment();
  advAll.slice(advCursor, advCursor + ADV_BATCH).forEach(a => frag.appendChild(advItem(a)));
  advCursor = Math.min(advCursor + ADV_BATCH, advAll.length);
  box.appendChild(frag);
}

function advItem(a) {
  const vnote = a.voiceRes ? `${a.voiceRes} 语音` : '无语音';
  const st = window.PACKSTATE ? PACKSTATE.status(a.key) : 'unknown';
  const d = document.createElement('div');
  d.className = 'char-btn adv' + (a.key === S.key ? ' on' : '')
    + (st === 'missing' ? ' nodl' : '');
  d.title = `${storyTitle(a.key)}\n${a.parts} 段 · ${a.lines} 句 · ${vnote}`
    + (a.first ? `\n${a.first}` : '')
    + (st === 'missing' ? '\n资源未下载：点「资源管理」安装后即可播放' : '');
  d.innerHTML =
    `<span class="srcTag" data-k="${a.kind}">${ADV_TAG[a.kind] || '剧'}</span>` +
    `<span class="info"><span class="nm">${esc(storyTitle(a.key))}</span>` +
    `<span class="sub"><span>${a.parts} 段</span><span>${a.lines} 句</span>` +
    `<span>${vnote}</span>` +
    (st === 'missing' ? '<span class="badge nodl">未下载</span>' : '') +
    `</span>` +
    (a.first ? `<span class="sub first">${esc(a.first)}</span>` : '') +
    `</span>` +
    `<span class="cpy" title="复制剧情名">⧉</span>`;
  d.onclick = () => openStory(a.key);
  d.querySelector('.cpy').onclick = ev => {
    ev.stopPropagation();
    copyText(storyTitle(a.key), `已复制：${storyTitle(a.key)}`);
  };
  return d;
}

/* 侧栏滚到底部时继续追加（只对剧情列表生效） */
$('#charList').addEventListener('scroll', () => {
  const box = $('#charList');
  if (SRC === 'char') return;
  if (box.scrollTop + box.clientHeight >= box.scrollHeight - 160) appendAdvChunk();
});

/* 搜索防抖：384 个角色 / 227 部剧情，逐字符重渲染会明显掉帧 */
let _searchTimer = 0;
$('#search').oninput = e => {
  clearTimeout(_searchTimer);
  const v = e.target.value;
  _searchTimer = setTimeout(() => renderSidebar(v), 120);
};

/* 来源切换：角色 / 主线 / 活动 / 其他 */
document.querySelectorAll('#srcRow button').forEach(b => {
  b.onclick = () => {
    SRC = b.dataset.src;
    document.querySelectorAll('#srcRow button').forEach(x =>
      x.classList.toggle('on', x === b));
    $('#search').value = '';
    updateCnt();
    renderSidebar();
  };
});

$('#sortBy').onchange = e => {
  SET.sortBy = e.target.value;
  applySettings();
  renderSidebar($('#search').value);
};
if ($('#dlFilter')) {
  $('#dlFilter').onchange = () => {
    try { localStorage.setItem('escah_dlf', $('#dlFilter').value); } catch (e) { }
    renderSidebar($('#search').value);
  };
  try {
    const v = localStorage.getItem('escah_dlf') || '';
    $('#dlFilter').value = v;
  } catch (e) { }
}
document.querySelectorAll('.rar-row button').forEach(b => {
  b.onclick = () => {
    const r = b.dataset.r;
    let arr = SET.rarSel ? SET.rarSel.split(',') : [];
    arr = arr.includes(r) ? arr.filter(x => x !== r) : arr.concat(r);
    SET.rarSel = arr.join(',');
    b.classList.toggle('on', arr.includes(r));
    applySettings();
    renderSidebar($('#search').value);
  };
});

/** 剧情数据缓存上限：227 部全接进来后不能无限堆内存（每部 0.1–0.5 MB） */
const STORY_CACHE_MAX = 6;

function cacheStory(key, d) {
  ST.cache.delete(key);          // 重新插入到队尾，近似 LRU
  ST.cache.set(key, d);
  while (ST.cache.size > STORY_CACHE_MAX) {
    ST.cache.delete(ST.cache.keys().next().value);
  }
}

/** 按剧情 key 加载 data/<key>.js（char_ 读 __CHAR，adv_ 读 __STORY） */
function loadStoryData(key) {
  if (ST.cache.has(key)) return Promise.resolve(ST.cache.get(key));
  return new Promise((res, rej) => {
    document.querySelectorAll('script[data-story]').forEach(e => e.remove());
    const s = document.createElement('script');
    s.dataset.story = key;
    s.src = `data/${key}.js`;
    s.onload = () => {
      const d = isCharKey(key) ? window.__CHAR : window.__STORY;
      if (!d) { rej(new Error('data/' + key + '.js 没有产出数据')); return; }
      cacheStory(key, d);
      res(d);
    };
    s.onerror = () => rej(new Error('无法加载 data/' + key + '.js'));
    document.head.appendChild(s);
  });
}

/** 打开任意剧情：key = char_<cid> 或 adv_<kind>_<no> */
async function openStory(key, opt) {
  /* 资源没下载就别让用户对着黑屏发呆：直接引导到资源管理页。
     纯静态打开（没有本地服务端）时 status 是 unknown，这里不拦。 */
  const pst = window.PACKSTATE ? PACKSTATE.status(key) : 'unknown';
  if (pst === 'missing') {
    const miss = PACKSTATE.missingDepots(key);
    const msg = `「${storyTitle(key)}」的资源还没有下载。\n\n` +
      `缺少：${miss.hard.join('、') || '剧情数据'}\n\n` +
      `现在打开「资源管理」页面去下载吗？`;
    if (confirm(msg)) {
      location.href = 'setup.html?unit=' + encodeURIComponent(key);
    }
    return;
  }
  if (pst === 'soft-missing') {
    toast('公共素材未下载：可以播放，但可能缺背景/音乐（资源管理里可补齐）');
  }
  stopAll();
  S.key = key;
  S.char = isCharKey(key) ? +String(key).slice(5) : null;
  S.kind = isCharKey(key) ? 'char' : String(key).split('_')[1];
  S.voiceRes = isCharKey(key) ? 1
    : ((ADV.find(a => a.key === key) || {}).voiceRes || 0);
  ST.char = S.char;
  ST.key = key;
  sessionStorage.setItem('story', key);
  renderSidebar($('#search').value);
  try { S.data = await loadStoryData(key); }
  catch (e) { alert(e.message); return; }
  // 先把这部剧情的译文拉下来（失败也只是没译文），再开工，避免第一句先闪日文
  await L10N.loadStory(key);
  $('#empty')?.remove();
  // ?part=1 表示第 1 段（界面口径），内部索引再减 1
  // opt.noPlay：只加载数据不开始播（脚本化加载时用，避免先白播一段）
  if (opt && opt.noPlay) return;
  const qp = +(new URLSearchParams(location.search).get('part') || 1);
  playPart(Math.min(Math.max(qp - 1, 0), S.data.parts.length - 1));
}

function openChar(id) { return openStory('char_' + id); }

/** 当前角色在当前语言下的译文覆盖情况（只算去重后的原文句数） */
function transStats() {
  const ja = new Set(), hit = new Set();
  (S.data?.parts || []).forEach(p => (p.methods || []).forEach(m => {
    if (m.Method !== 'ShowMessage') return;
    const src = m.Message || '';
    ja.add(src);
    if (L10N.msg(m) !== src) hit.add(src);
  }));
  return { done: hit.size, total: ja.size };
}

function renderTabs() {
  const t = $('#tabs');
  t.querySelectorAll('.tab').forEach(e => e.remove());
  S.data.parts.forEach((p, i) => {
    const d = document.createElement('div');
    d.className = 'tab' + (i === ST.part ? ' on' : '');
    d.textContent = L10N.t('meta.part', `第 ${i + 1} 段`, { n: i + 1 });
    d.onclick = () => playPart(i);
    t.appendChild(d);
  });
  const total = S.data.parts.reduce((a, p) =>
    a + p.methods.filter(m => m.Method === 'ShowMessage').length, 0);
  const voices = S.data.parts.reduce((a, p) => a + (p.voices || []).length, 0);
  let meta = L10N.t('meta.stats',
    `${S.data.parts.length} 段 · ${total} 句 · ${voices} 语音`,
    { p: S.data.parts.length, l: total, v: voices });
  // 源语言不需要显示覆盖率，只给非源语言显示（含"还没有译文"的情况）
  if (L10N.lang() !== L10N.source) {
    const s = transStats();
    const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
    meta += ' · ' + L10N.t('meta.translated', `已译 ${s.done}/${s.total}（${pct}%）`,
      { n: s.done, t: s.total, p: pct });
  }
  $('#meta').textContent = meta;
  updateCnt();
  updateBanner();
}

/** 剧情章节的标题横幅（游戏里章节标题本身就是图，只能这样拿到）
    只显示当前这一张，切换剧情时才换 src，不会给列表增加图片请求。 */
function updateBanner() {
  const t = $('#tabs');
  if (!t) return;
  const adv = ADV.find(a => a.key === S.key);
  let b = $('#storyBanner');
  if (!adv || !adv.cover) {
    if (b) b.hidden = true;
    return;
  }
  if (!b) {
    b = document.createElement('img');
    b.id = 'storyBanner';
    b.loading = 'lazy';
    b.alt = '';
    t.appendChild(b);
  }
  b.hidden = false;
  if (b.getAttribute('src') !== adv.cover) b.src = adv.cover;
  b.title = storyTitle(S.key);
}

/** 顶部标题旁的总量：随侧栏来源切换 */
function updateCnt() {
  const el = $('#hdrSub');
  if (!el) return;
  if (SRC === 'char') {
    el.textContent = L10N.t('cnt.char', `${INDEX.length} 个角色`, { n: INDEX.length });
  } else {
    const kinds = ADV_MENU[SRC] || [SRC];
    const n = ADV.filter(a => kinds.includes(a.kind)).length;
    el.textContent = L10N.t('cnt.adv', `${n} 部剧情`, { n });
  }
}

/* ================= 语言切换（日文 ↔ 中文） =================
 * 切源时整套 UI 与全部台词都要重算，所以集中在这一个入口里：
 *   换语言 -> 重拉该角色译文 -> 重建台词索引 -> 复写当前句 -> 刷新统计
 * 没有译文的角色不受影响：L10N.msg() 会自动回退到日文原文。
 */
function updateLangBtn() {
  const b = $('#btnLang');
  if (!b) return;
  b.textContent = L10N.label();
  b.classList.toggle('has-zh', L10N.lang() !== L10N.source);
}

function refreshForLang() {
  updateLangBtn();
  if (!S.data) return;
  const keep = ST.curLine;              // 保住文本列表的高亮位置
  buildLineIndex();
  if (keep >= 0) focusLine(Math.min(keep, ST.allLines.length - 1));
  renderTabs();
  const ht = $('#hToggle');               // 文本列表的开合按钮文案也要跟着语言走
  if (ht) {
    ht.textContent = $('#history').classList.contains('hide')
      ? L10N.t('history.expand', '展开 ◂')
      : L10N.t('history.collapse', '收起 ▸');
  }
  if (ST.curMsgIdx != null && ST.methods[ST.curMsgIdx]) {
    // 正在播：整句重放（连带语音）；暂停中：直接把画面上的文字换掉
    if (ST.running) seekTo(ST.curMsgIdx);
    else $('#msgText').textContent = lineText(ST.methods[ST.curMsgIdx]);
  }
}

async function switchLang(explicit) {
  explicit ? L10N.setLang(explicit) : L10N.cycle();
  toast(L10N.t('toast.lang', '显示语言：' + L10N.lang(), { name: L10N.label() }));
  if (S.key) await L10N.loadStory(S.key);
  refreshForLang();
}
if ($('#btnLang')) $('#btnLang').onclick = () => switchLang();

/* ================= 事件 ================= */
$('#btnPlay').onclick = () => {
  if (ST.running) {
    pausePlayback();               // 暂停不销毁 BGM/循环音效
    setPlayBtn(false);
    return;
  }
  resumeAudio();
  // 继续：若已播到某句，从该句重建画面后接着播；否则从指令指针继续
  if (ST.curMsgIdx != null && ST.curMsgIdx < ST.methods.length) {
    seekTo(ST.curMsgIdx);
  } else if (ST.idx > 0 && ST.idx < ST.methods.length) {
    ST.running = true;
    setPlayBtn(true);
    continueLoop();
  } else {
    playPart(ST.part);
  }
};
$('#btnRestart').onclick = () => { playPart(ST.part); };
$('#btnNext').onclick = () => {
  const n = ((ST.part || 0) + 1) % (S.data?.parts.length || 1);
  playPart(n);
};
// 点击舞台 = 暂停/播放（与 P 键一致）
$('#stage').onclick = () => togglePlay();
$('#hToggle').onclick = () => {
  const h = $('#history');
  h.classList.toggle('hide');
  const opened = !h.classList.contains('hide');
  document.body.classList.toggle('histOpen', opened);
  $('#hTab').classList.toggle('show', !opened);
  $('#hToggle').textContent = opened ? L10N.t('history.collapse', '收起 ▸')
    : L10N.t('history.expand', '展开 ◂');
  fitStage();
  localStorage.setItem('escah_hist', opened ? '1' : '0');
};
$('#hTab').onclick = () => $('#hToggle').click();
$('#speed').onchange = e => { ST.speed = parseFloat(e.target.value) || 1; };
$('#auto').onchange = e => { ST.auto = e.target.checked; };
document.addEventListener('keydown', e => {
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

  // 已解绑 Ctrl：带 Ctrl/Alt/Meta 的组合键不触发单键快捷键
  if (e.ctrlKey || e.altKey || e.metaKey) return;

  switch (e.code) {
    case 'Space':
      e.preventDefault(); togglePlay(); break;      // 空格 = 暂停/播放
    case 'Enter': case 'NumpadEnter':
      e.preventDefault(); nextLine(); break;       // 回车 = 下一句
    case 'ArrowDown': case 'ArrowRight':
      e.preventDefault(); nextLine(); break;
    case 'ArrowUp': case 'ArrowLeft':
      e.preventDefault(); prevLine(); break;
    case 'KeyP':
      e.preventDefault(); togglePlay(); break;
    case 'KeyL': e.preventDefault(); switchLang(); break;   // L = 切换语言
    case 'KeyS': $('#btnSet').click(); break;
    case 'KeyF': e.preventDefault(); toggleFullscreen(); break; // F = 全屏
    case 'KeyK': setRush(true); break;              // K = 快进（按住）
    case 'KeyD': {
      const d = $('#dbg');
      d.style.display = d.style.display === 'none' ? 'block' : 'none';
      break;
    }
  }
});
document.addEventListener('keyup', e => {
  if (e.code === 'KeyK') setRush(false);
});
window.addEventListener('blur', () => setRush(false));

/* ================= 调试 ================= */
const ERRLOG = [];
window.addEventListener('error', e => ERRLOG.push(e.message + ' @' + (e.lineno || '?')));
window.addEventListener('unhandledrejection', e => ERRLOG.push('REJ:' + (e.reason?.message || e.reason)));
setInterval(() => {
  const el = $('#dbg');
  if (!el) return;
  const si = $('#standLayer').querySelector('img');
  const sInfo = si
    ? ` | img cli=${si.clientWidth}x${si.clientHeight} nat=${si.naturalWidth}x${si.naturalHeight} top=${Math.round(si.getBoundingClientRect().top - $('#stage').getBoundingClientRect().top)}`
    : '';
  el.textContent =
    `idx=${ST.idx}/${ST.methods.length} run=${ST.running ? 1 : 0} ` +
    `bg=${$('#bgLayer').querySelectorAll('img').length} stand=${ST.stands.size} ` +
    `cg=${$('#cgLayer').querySelectorAll('img').length} pend=${ST.pendingAll.length} ` +
    `vh=${ST.voiceHas ? 1 : 0} key=${ST.key} part=${ST.part} ` +
    `win=${$('#msgWindow').classList.contains('on') ? 1 : 0} ` +
    `bgm=${bgmAudio ? (bgmAudio.paused ? 'p' : 'o') : '-'} sfx=${Object.keys(SFX).length}` +
    sInfo +
    (ERRLOG.length ? `\nERR: ${ERRLOG.slice(-2).join(' | ')}` : '');
}, 250);
(() => {
  const d = $('#dbg');
  if (d && !location.search.includes('debug')) d.style.display = 'none';
  if (location.search.includes('settings')) $('#btnSet').click();
  const seekN = +(new URLSearchParams(location.search).get('seek') || -1);
  if (seekN >= 0) setTimeout(() => seekTo(seekN), 1800);
  // ?langswitch=zh-Hans 供自动化测试：启动后跑一遍「运行中切换语言」的完整链路
  const ls = new URLSearchParams(location.search).get('langswitch');
  if (ls) setTimeout(() => switchLang(ls === '1' ? undefined : ls), 2500);
})();

/* ================= 启动 ================= */
// ?seek=N 供自动化测试：启动后直接跳到第 N 条指令
window.__testSeek = n => seekTo(n);
INDEX.sort((a, b) => a.id - b.id);   // 按数字 ID 排序（文件名排序会得到 1,10,100…）
loadSettings();
updateLangBtn();                      // 按钮先显示当前语言（不等 ui.json）
L10N.ready.then(() => {               // ui.json 到位后再刷一遍静态文案
  L10N.applyDom();
  updateLangBtn();
  setPlayBtn(ST.running);
  if (S.data) renderTabs();
});
renderSidebar();
// 恢复历史面板展开状态
(() => {
  const open = localStorage.getItem('escah_hist') !== '0';
  document.body.classList.toggle('histOpen', open);
  $('#history').classList.toggle('hide', !open);
  $('#hTab').classList.toggle('show', !open);
})();
fitStage();
updateCnt();
(() => {
  const qs = new URLSearchParams(location.search);
  const auto = qs.has('autoplay');
  // ?adv=main_1 优先（测试钩子 + 可分享链接），其次 ?char=<id>，最后恢复上次观看
  const qa = qs.get('adv');
  if (qa) {
    const key = /^adv_/.test(qa) ? qa : 'adv_' + qa;
    const parsed = /^adv_(main|event|other|tutorial|op)(?:_(\d+))?$/.exec(key);
    if (parsed) {
      // tutorial / op 在侧栏归到「其他」菜单下
      SRC = (parsed[1] === 'tutorial' || parsed[1] === 'op') ? 'other' : parsed[1];
      document.querySelectorAll('#srcRow button').forEach(x =>
        x.classList.toggle('on', x.dataset.src === SRC));
      updateCnt();
      renderSidebar();
      if (auto) openStory(key);
    }
    return;
  }
  const q = +(qs.get('char') || 0);
  const saved = sessionStorage.getItem('story');
  const h = isCharKey(saved) ? +String(saved).slice(5) : null;
  const id = q && INDEX.some(c => c.id === q) ? q
    : (h && INDEX.some(c => c.id === h) ? h : null);
  // 默认不自动加载剧情：保留 #empty 空状态提示，等用户点列表角色再加载
  if (id && auto) openChar(id);
})();

/* ================= 对外接口（译文 / 新手引导用） ================= */
/* 这些模块不想碰播放器内部状态，所以只暴露最小的控制面。 */
window.PLAYER_API = {
  setAuto(v) { ST.auto = !!v; const el = $('#auto'); if (el) el.checked = ST.auto; },
  replayPart() { return S.data ? playPart(ST.part) : Promise.resolve(); },
  /** 播指定段。注意它会一直跑到该段播完 */
  playPart(i) { return S.data ? playPart(i) : Promise.resolve(); },
  partDone() { return !ST.running && ST.methods.length > 0 && ST.idx >= ST.methods.length; },
  key: () => ST.key,
  part: () => ST.part,
  title: k => storyTitle(k || ST.key),
  charId: () => S.char,
  toast: m => toast(m),
  chapters: () => (S.data && S.data.parts ? S.data.parts.length : 0),
};

/* 补上"云端有、本地没装"的新角色（清单随官方更新，index.js 是静态的） */
if (window.PACKSTATE) {
  setTimeout(loadCloudChars, 600);
  PACKSTATE.on(() => { _cloudLoaded = false; setTimeout(loadCloudChars, 300); });
}

/* 资源状态变化（下载/安装完回来）→ 重画侧栏的"未下载"标记与排序 */
if (window.PACKSTATE) {
  PACKSTATE.on(() => {
    renderSidebar(($('#search') || {}).value || '');
  });
}
