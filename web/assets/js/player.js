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
  timeIdx: null,         // 每句开头的估算时间点（Float64Array，见 buildTimeIndex）
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

/* 剧本里的 Time 字段混着三样东西：
 *   1. 真秒数 —— 0.25 / 0.5（绝大多数字幕与立绘指令）；
 *   2. 演出枚举 —— '右ワイプ' / '黒画面' / '中'（数值化没有意义）；
 *   3. 哨兵值 —— 100 / 255，游戏里含义是「等玩家点击」，**不是秒数**。
 * 以前一律 num(Time) 当秒数，遇到哨兵就要等 100~255 秒：表现是剧情卡在
 * 那一句不动、点暂停再播放又回到同一句继续等（seekTo 会重放这几条指令）。
 * 这里统一归一一次，别在各个指令里各自 num()。 */
const TIME_CAP = 5;            // 超过 5 秒的一定是哨兵/枚举，不可能是演出时长
/** Time → 毫秒。非数字走 defMs；哨兵走 sentinelMs（不传时同 defMs）。 */
function timeMs(v, defMs, sentinelMs) {
  const n = num(v, NaN);
  if (!isFinite(n) || n < 0) return defMs;
  if (n > TIME_CAP) return (sentinelMs == null ? defMs : sentinelMs);
  return n * 1000;
}

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

/* ================= 全屏 =================
 * 原生全屏只显示被请求的那个元素本身：全屏的是 #stageWrap，写在它外面的
 * #controls 整条都不可见 —— 所以以前全屏后一个按钮都没有，手机上只能靠系统
 * 返回键退出。现在的做法：
 *   · 进全屏时把 #controls 临时搬进 #stageWrap，绝对定位压在底部；
 *   · 右上角固定一个「✕ 退出全屏」，任何平台都能一键退出；
 *   · 鼠标 / 手指静止 2.6s 后两者一起淡出，动一下就回来。
 * 另外 iOS Safari 不支持 div 的 requestFullscreen，退化成 CSS 伪全屏。
 */
let _ctlHome = null;            // 控制条原来的父节点与后继兄弟
let _fsChromeTimer = null;

function _fsActive() {
  const el = document.getElementById('stageWrap');
  return !!(document.fullscreenElement || document.webkitFullscreenElement)
    || !!(el && el.classList.contains('fsp'));
}

function toggleFullscreen() {
  const el = document.getElementById('stageWrap');
  if (!el) return;
  // 已在原生全屏：退出
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    const exit = document.exitFullscreen || document.webkitExitFullscreen
      || document.msExitFullscreen;
    if (exit) exit.call(document);
    return;
  }
  // 已在伪全屏：退出
  if (el.classList.contains('fsp')) {
    el.classList.remove('fsp');
    onFsChange();
    return;
  }
  const req = el.requestFullscreen || el.webkitRequestFullscreen
    || el.msRequestFullscreen;
  if (req) {
    try {
      const p = req.call(el);
      // 被浏览器拒绝（没有用户手势等）时退化成伪全屏，别什么都不发生
      if (p && p.catch) p.catch(() => { el.classList.add('fsp'); onFsChange(); });
      return;
    } catch (e) { /* 落到下面的伪全屏 */ }
  }
  el.classList.add('fsp');
  onFsChange();
}

/* 显示控制条 + 退出按钮，并重置自动淡出计时 */
function showFsChrome() {
  const el = document.getElementById('stageWrap');
  if (!el || !el.classList.contains('fs')) return;
  const ctl = document.getElementById('controls');
  const ex = document.getElementById('fsExit');
  if (ctl) ctl.classList.add('show');
  if (ex) ex.classList.add('show');
  el.classList.remove('hidecur');
  if (_fsChromeTimer) clearTimeout(_fsChromeTimer);
  _fsChromeTimer = setTimeout(() => {
    const w = document.getElementById('stageWrap');
    if (!w || !w.classList.contains('fs')) return;
    const c = document.getElementById('controls');
    const e = document.getElementById('fsExit');
    if (c) c.classList.remove('show');
    if (e) e.classList.remove('show');
    // 控制条淡出了，气泡不能还悬在画面上（拖动中的除外，那是手指/鼠标正在上面）
    if (!seekDrag.on) hideSeekTip();
    w.classList.add('hidecur');
  }, 2600);
}

function _fsShowCursor() {
  showFsChrome();
}

/* 控制条在「main 里」与「#stageWrap 里」之间来回搬 */
function _fsMoveControls(on) {
  const wrap = document.getElementById('stageWrap');
  const ctl = document.getElementById('controls');
  if (!wrap || !ctl) return;
  /* 进度条气泡也必须跟着进全屏子树：原生全屏只渲染 #stageWrap 本身，
     留在 main 里的 fixed 元素（哪怕 z-index 再高）根本不参与绘制 ——
     那样全屏时拖进度条就"没有句号和位置提示"，和非全屏对不上。 */
  const tip = document.getElementById('seekTip');
  if (on) {
    if (!_ctlHome) _ctlHome = { parent: ctl.parentNode, next: ctl.nextSibling };
    if (ctl.parentNode !== wrap) wrap.appendChild(ctl);
    if (tip && tip.parentNode !== wrap) wrap.appendChild(tip);
    ctl.classList.add('in-fs');
  } else {
    ctl.classList.remove('in-fs');
    ctl.classList.remove('show');
    if (tip && _ctlHome && tip.parentNode !== _ctlHome.parent) {
      _ctlHome.parent.appendChild(tip);      // fixed 定位，放回父节点哪个位置都不影响布局
    }
    if (_ctlHome && ctl.parentNode !== _ctlHome.parent) {
      // 记住的后继兄弟可能已被移除，退回 appendChild
      if (_ctlHome.next && _ctlHome.next.parentNode === _ctlHome.parent) {
        _ctlHome.parent.insertBefore(ctl, _ctlHome.next);
      } else {
        _ctlHome.parent.appendChild(ctl);
      }
    }
    _ctlHome = null;
  }
}

/* 退出按钮：只在第一次进全屏时创建一次，之后复用 */
function _ensureFsExit() {
  const wrap = document.getElementById('stageWrap');
  if (!wrap) return;
  if (!document.getElementById('fsExit')) {
    const b = document.createElement('button');
    b.id = 'fsExit';
    b.type = 'button';
    b.textContent = '✕ 退出全屏';
    b.onclick = (e) => { e.stopPropagation(); toggleFullscreen(); };
    b.addEventListener('touchstart', (e) => e.stopPropagation(), { passive: true });
    wrap.appendChild(b);
  }
}

function onFsChange() {
  const el = document.getElementById('stageWrap');
  const fs = _fsActive();
  if (el) el.classList.toggle('fs', fs);
  /* 全屏时尽量把屏幕转成横屏（舞台本来就是 16:9 横版），退出时解锁回到
     系统默认的竖屏。iOS / 桌面拿不到锁权限，静默失败（见 common.js 的 ESC.orient）。 */
  if (window.ESC && ESC.orient) {
    if (fs) ESC.orient.lockLandscape(); else ESC.orient.unlock();
  }
  fitStage();
  const b = document.getElementById('btnFs');
  if (b) b.textContent = fs ? '⛶ 退出' : '⛶ 全屏';
  _fsMoveControls(fs);
  if (fs && el) {
    _ensureFsExit();
    el.addEventListener('mousemove', _fsShowCursor);
    el.addEventListener('touchstart', _fsShowCursor, { passive: true });
    showFsChrome();
  } else if (el) {
    el.removeEventListener('mousemove', _fsShowCursor);
    el.removeEventListener('touchstart', _fsShowCursor);
    el.classList.remove('hidecur');
    if (_fsChromeTimer) { clearTimeout(_fsChromeTimer); _fsChromeTimer = null; }
  }
}
document.addEventListener('fullscreenchange', onFsChange);
document.addEventListener('webkitfullscreenchange', onFsChange);
const _btnFs = document.getElementById('btnFs');
if (_btnFs) _btnFs.onclick = toggleFullscreen;

/* ================= 画面 ================= */
/* 背景/CG 层「台上现在是哪张图」的状态。
 *
 * 为什么需要记这个：点进度条、点台词、上一句/下一句都走 seekTo()，
 * 而 seekTo() 会从第 0 条开始重放画面指令。旧写法每次都 innerHTML=''
 * 再 new Image，于是舞台先空一帧（透出黑底）、新图再从 opacity:0 淡入
 * 0.45s——每跳一次就闪一下黑屏。
 * 现在同一张图直接复用（不拆不建不淡入），真换图就新旧叠放交叉淡入，
 * 任何时刻图层里都有图。
 * hit：本轮重建里有没有下过这类指令（没下过说明这个位置本来没图，需清空）。 */
const VIS = {
  bg: { key: null, el: null, hit: false },
  cg: { key: null, el: null, hit: false },
};

/** 往某个图层画一张图（背景 / CG 共用）：同图复用、换图交叉淡入 */
function setLayerImage(kind, sel, id, fade) {
  const L = $(sel), st = VIS[kind];
  const key = id || '';
  const list = key ? candsOf(kind === 'bg' ? 'back' : 'cg', key) : [];
  st.hit = true;
  if (!key || !list.length) {
    L.innerHTML = '';
    st.key = null; st.el = null;
    return Promise.resolve();
  }
  const instant = ST.fastForward;          // 跳转重放：不演淡入，直接就位
  if (st.key === key && st.el && st.el.parentNode === L) {
    // 同一张图：不清层、不重建、不淡入（filter 由随后的颜色乘算指令重新施加）
    st.el.style.filter = '';
    /* 镜头状态也要归零：ZoomInEventCG / StareEventCG 是往这张 img 上写的内联 transform，
       再次 ShowEventCg 同一张图时会复用同一个 <img>，不清就会带着上一幕的放大/偏移，
       看起来像"画面被挤到角落且回不来"。 */
    st.el.style.transform = '';
    st.el.style.transformOrigin = '';
    if (instant) L.querySelectorAll('img').forEach(o => { if (o !== st.el) o.remove(); });
    return instant ? Promise.resolve() : wait(fade * 1000);
  }
  const img = applyChain(new Image(), list);
  img.style.filter = '';
  img.style.opacity = instant ? 1 : 0;
  img.style.transition = instant ? 'none' : `opacity ${fade}s`;
  const olds = Array.from(L.children);
  L.appendChild(img);
  st.key = key; st.el = img;
  let done = false;
  const reveal = () => {
    if (done) return; done = true;
    if (!instant) requestAnimationFrame(() => { img.style.opacity = 1; });
    // 等新图真的能画出来再撤旧图：中间不能有「两张都没有」的瞬间
    const dropOld = () => olds.forEach(o => { if (o !== img) o.remove(); });
    if (instant) dropOld();
    else setTimeout(dropOld, fade * 1000 + 80);
  };
  if (img.complete && img.naturalWidth) reveal();
  else {
    img.addEventListener('load', reveal, { once: true });
    // 候选链走完仍加载不出来才算失败，避免中途误撤旧图
    img.addEventListener('error', () => { if (!(img.dataset.chain || '').length) reveal(); }, { once: true });
    setTimeout(reveal, 2500);              // 兜底：别让旧图永远压在台上
  }
  return instant ? Promise.resolve() : wait(fade * 1000);
}

function setBackground(id) { return setLayerImage('bg', '#bgLayer', id, 0.45); }

function setDoubleBackground(l, r) {
  const L = $('#bgLayer');
  const key = `D|${l || ''}|${r || ''}`;
  VIS.bg.hit = true;
  const instant = ST.fastForward;
  if (VIS.bg.key === key && L.childElementCount === 2) {
    return instant ? Promise.resolve() : wait(400);
  }
  VIS.bg.key = key; VIS.bg.el = null;
  const olds = Array.from(L.children);
  const made = [];
  [[l, 'left'], [r, 'right']].forEach(([id, side]) => {
    const list = id ? candsOf('back', id) : [];
    if (!list.length) return;
    const img = applyChain(new Image(), list);
    img.className = 'half';
    img.style[side] = '0';
    if (instant) img.style.opacity = 1;
    L.appendChild(img);
    made.push(img);
  });
  if (!made.length) {
    L.innerHTML = '';
    VIS.bg.key = null;
    return Promise.resolve();
  }
  let n = made.length, fired = false;
  const drop = () => {
    if (fired) return; fired = true;
    olds.forEach(o => { if (made.indexOf(o) < 0) o.remove(); });
  };
  const one = () => { if (--n <= 0) drop(); };
  made.forEach(im => {
    let used = false;
    const once = () => { if (used) return; used = true; one(); };
    if (im.complete && im.naturalWidth) { once(); return; }
    im.addEventListener('load', once, { once: true });
    im.addEventListener('error', () => { if (!(im.dataset.chain || '').length) once(); }, { once: true });
  });
  setTimeout(drop, 2500);
  return instant ? Promise.resolve() : wait(400);
}

function setCg(id, fade) {
  const f = (fade == null || !isFinite(fade)) ? 0.3 : Math.max(fade, 0);
  return setLayerImage('cg', '#cgLayer', id, f);
}

/** 当前台上的 CG：交叉淡入期间 #cgLayer 里可能同时有新/旧两张，取状态里记的那张 */
function cgImg() { return VIS.cg.el || $('#cgLayer').querySelector('img'); }

/* ================= CG 镜头（推近 / 复位） =================
 * 这套指令在脚本里是**成对**的，不是独立特效：
 *   · `ZoomInEventCG{Rate:1.01~1.6}` 推近，随后再发一条 `ZoomInEventCG{Rate:1}` 拉回原样
 *     （全站 524 条里 Rate=1 的 205 条基本都扮演"复位"）；
 *   · `StareEventCG` 慢推 1.12（6 秒），`EndStareEventCG` 拉回（829/825 条，成对）；
 *   · `FastZoomInEventCG` 是"砸进去"的冲击镜头：**全站 45 条都带 EventImage**，
 *     语义是"先切到这张 CG，再放大到 Rate"，后面常紧跟 `Shake` 震屏。
 *     （旧实现完全忽略 EventImage，这 45 处演出都少了一次切图。）
 * `PosX/PosY` 是**放大中心**（1280×720 舞台坐标），不是位移量。 */
function camOrigin(m) {
  return `${num(m.PosX, 640) / 1280 * 100}% ${num(m.PosY, 360) / 720 * 100}%`;
}
/** instant=true 用于跳转重放：不演过渡、不等待，直接落到最终状态 */
async function zoomCg(m, instant) {
  if (m.EventImage) await setCg(m.EventImage, 0);   // FastZoom：先换图（瞬时，原作就是砸进来）
  const img = cgImg();
  if (!img) return;
  img.style.transformOrigin = camOrigin(m);
  img.style.transition = instant ? 'none' : `transform ${num(m.Time, 1)}s ease`;
  img.style.transform = `scale(${num(m.Rate, 1.3)})`;
  if (num(m.Blur, 0) > 0) img.style.filter = `blur(${num(m.Blur, 0)}px)`;
  if (!instant) await wait(num(m.Time, 1) * 1000);
}
async function stareCg(m, instant) {
  const img = cgImg();
  if (!img) return;
  const back = m.Method === 'EndStareEventCG';
  img.style.transformOrigin = '50% 50%';            // 凝视不带焦点参数，永远以画面正中推
  img.style.transition = instant ? 'none' : (back ? 'transform .5s' : 'transform 6s ease-out');
  img.style.transform = `scale(${back ? 1 : 1.12})`;
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

/* 图片层到底有没有图可拿（侧栏横幅 / 头像 / 章节横幅都靠它决定要不要发请求）。
 *
 * 为什么必须问一句：media/ 下的东西**一个都不随程序分发**，全新环境里
 * 384 行列表 × 6 个候选 = 5000 多个 404，把启动窗口刷成一片红，用户以为坏了。
 *
 * 三态，顺序很重要：
 *   ① 还没问到服务端（首屏比 /api/state 早）→ 一律不发图，等 PACKSTATE.on 重画；
 *   ② 问到了且离线（纯静态托管、file://）→ 无从判断，照旧发（老行为）；
 *   ③ 在线 → 按"该层素材是否已下载"答。
 */
function imgLayer() {
  const PS = window.PACKSTATE;
  if (!PS || !PS.known || !PS.known()) return { banners: false, avatars: false };
  if (!PS.online) return { banners: true, avatars: true };
  return {
    banners: !!(PS.bannersReady && PS.bannersReady()),
    avatars: !!(PS.avatarsReady && PS.avatarsReady()),
  };
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
  const t = $('#msgText');
  t.classList.remove('partNotice');      // 段间提示的样式只属于提示那一行
  t.classList.toggle('narr', !who);
  return wait(winFadeMs(m));
}
function hideWindow(m) {
  $('#msgWindow').classList.remove('on');
  return wait(winFadeMs(m));
}
/** 对话框开关的淡入淡出时长：Time 常写成擦除方向（'右ワイプ'）或 100/255
 *  哨兵，这些都不是时长，统一按 0.2s 处理（原来会等 100 秒）。 */
function winFadeMs(m) { return Math.max(timeMs(m.Time, 200), 50); }

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
  loopSe.dataset.src = src;        // 记录下来源：跳转时同一段循环音效不要重启
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
    case 'StareEventCG': P(stareCg(m, false)); return;
    case 'EndStareEventCG': P(stareCg(m, false)); return;
    case 'ZoomInEventCG':
    case 'FastZoomInEventCG': P(zoomCg(m, false)); return;
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
      // 播放进度：每显示一句记一次（内存立即更新，"播放记录"与自动续播都靠它）
      noteProgress();
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
    /* Wait 的 Time=100（漫画/演出里 696 处）同样是「等玩家点击」哨兵：
       真等 100 秒就等于卡死，这里按 2 秒算（自动播放时的翻页节奏）。 */
    case 'Wait': return wait(timeMs(m.Time, 500, 2000));
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
      const img = cgImg();
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
  buildTimeIndex();
  renderLineList();
}

/* ---- 时间轴（只给进度条气泡用）----
 * 逐句演出**没有客观时长**：打字机速度、语音长短、玩家点快慢都在变，所以这里不统计
 * 墙钟时间，而是按台本量静态推算一条时间轴 —— 用来回答"这段大概多长、我现在大概在
 * 几分几秒"，跟视频进度条语义一致。基础停留 0.4s + 每字 0.07s + 带语音的句子多给 1.6s。 */
const SEC_BASE = 0.4, SEC_PER_CHAR = 0.07, SEC_VOICE = 1.6;
function buildTimeIndex() {
  const lines = ST.allLines || [];
  const acc = new Float64Array(lines.length + 1);   // acc[i] = 第 i+1 句开始时的累计秒
  let t = 0;
  for (let i = 0; i < lines.length; i++) {
    const midx = lines[i].midx;
    const prev = ST.methods[midx - 1];
    const voice = !!(prev && prev.Method === 'VOICE' && prev.Voice);
    const n = String(lines[i].text || '').replace(/\s+/g, '').length;
    t += SEC_BASE + n * SEC_PER_CHAR + (voice ? SEC_VOICE : 0);
    acc[i + 1] = t;
  }
  ST.timeIdx = acc;
}
/** 第 no 句（1 基）开始时的估算毫秒；no=0 → 0 */
function timeAtLine(no) {
  const a = ST.timeIdx;
  if (!a || a.length < 2) return 0;
  const i = Math.max(0, Math.min(a.length - 1, no | 0));
  return a[i] * 1000;
}
/** 本段估算总时长（毫秒） */
function partTimeMs() {
  const a = ST.timeIdx;
  return a && a.length ? a[a.length - 1] * 1000 : 0;
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
/* 镜头指令也必须重放：otherwise「上一句」回到的还是当前这一句的缩放状态，
   玩家想对比前后两句的画面就看不出差别（2026-11-08 用户反馈）。 */
const CAM_REPLAY = new Set(['ZoomInEventCG', 'FastZoomInEventCG',
  'StareEventCG', 'EndStareEventCG']);
const REPLAY = new Set([
  'ShowBackground', 'HideBackground', 'ShowDoubleBackground',
  'ShowDoubleBackgroundOffset', 'MulColorBackground',
  'ShowStand', 'ShowStandAndSit', 'HideStand', 'MoveStand', 'MulStand',
  'ShowEventCg', 'HideEventCg',
  'SetDrawModeSepia', 'SetDrawModeGrayscale', 'SetDrawModeDefault',
  ...CAM_REPLAY,
]);
/* 背景/CG 这两类只重放「最后一条」：中间的先 Show 再 Hide 再 Show 会互相抵消，
   逐条重放等于把同一张图拆了建好几次，每一次都可能有一帧没有图（闪黑）。 */
const BG_REPLAY = new Set(['ShowBackground', 'HideBackground',
  'ShowDoubleBackground', 'ShowDoubleBackgroundOffset']);
const CG_REPLAY = new Set(['ShowEventCg', 'HideEventCg']);

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
    /* 镜头：跳转重放要**瞬时**落到该句应有的状态（不演 6 秒推镜、不等待） */
    case 'ZoomInEventCG':
    case 'FastZoomInEventCG': zoomCg(m, true); break;
    case 'StareEventCG':
    case 'EndStareEventCG': stareCg(m, true); break;
    case 'SetDrawModeSepia': setDrawMode('sepia'); break;
    case 'SetDrawModeGrayscale': setDrawMode('gray'); break;
    case 'SetDrawModeDefault': setDrawMode(''); break;
  }
}

let seeking = false;

/** 跳转后补"音频状态"：[0, midx) 里的 PlayMusic / StopMusic / PlayLoopSound /
 *  StopLoopSound 都被"不碰音频"的跳转跳过了，不补就会出现——
 *  从播放记录回到上次位置、拖进度条、点右侧句子跳转 → **一点 BGM 都没有**
 *  （2026-11-03 用户反馈；尤其是从列表点进来时 openStory 已经 stopAll 把音乐关掉了）。
 *
 *  只补"状态"，不补一次性音效（PlaySound）：那是"当时那一下"的效果，
 *  隔了几十句再响一次反而怪。
 *  同一首还在放时 playMusic 会直接续上（内部按 src 去重），所以逐句跳不会重起音乐。 */
function restoreAudioAt(midx, keepPaused) {
  let music = null, loop = null;
  for (let i = 0; i < midx; i++) {
    const m = ST.methods[i];
    switch (m.Method) {
      case 'PlayMusic': music = m; break;
      case 'StopMusic': music = null; break;
      case 'PlayLoopSound': loop = m; break;
      case 'StopLoopSound': loop = null; break;
    }
  }
  // 音乐：目标位置"应该正在放的那首"；目标位置本来就没有音乐，就把上一段留下的收掉
  if (music && SFX[music.Music]) playMusic(music);
  else if (!music && bgmAudio) stopMusic({ FadeTime: 0.25 });
  // 循环音效（雨声/机械声）：同样按最后一次状态恢复，同一段就继续放着
  if (loop && SFX[loop.Sound]) {
    const src = SFX[loop.Sound];
    if (!(loopSe && loopSe.dataset.src === src && !loopSe.paused)) playLoopSe(loop);
  } else if (loopSe) stopLoopSe();
  // 暂停状态下拖进度条：状态照建、声音先按住，等用户点播放由 resumeAudio 续上
  if (keepPaused) {
    if (bgmAudio) { try { bgmAudio.pause(); } catch (e) { } }
    if (loopSe) { try { loopSe.pause(); } catch (e) { } }
  }
}

/* 跳到某条台词：重建画面状态后从该句继续播 */
async function seekTo(midx, opt) {
  if (seeking || !ST.methods.length) return;
  if (midx == null || midx < 0 || midx >= ST.methods.length) return;
  /* opt.keepPaused：跳过去但**不自动播**（暂停时点进度条用）。
     其它调用方（方向键、句子跳转）不传，行为不变。 */
  const resume = !(opt && opt.keepPaused);
  seeking = true;
  try {
    haltLoop();                    // 只杀循环与语音，保留 BGM
    resumeAudio();                 // 暂停时挂起的 BGM/循环音效续上
    ST.typeSkip = true;            // 掐断还在跑的打字机
    resetStage({ keepVisual: true });   // 背景/CG 先留在台上，别拆了再建（会闪黑）
    ST.fastForward = true;

    // 1) 同步重建画面状态（几百条指令也就几十毫秒）
    /* 背景/CG 只重放最终状态：中间的先 Show 再 Hide 再 Show 会互相抵消，
       逐条重放等于把同一张图拆了建好几次，每次都可能有一帧没图（闪黑）。
       MulColorBackground 是给当前背景上色的，只补在最后一条背景指令之后。 */
    let lastBg = null, lastCg = null, lastCam = null;
    const muls = [];
    for (let i = 0; i < midx; i++) {
      const m = ST.methods[i];
      if (!REPLAY.has(m.Method)) continue;
      const M = m.Method;
      if (BG_REPLAY.has(M)) { lastBg = { i, m }; continue; }
      if (CG_REPLAY.has(M)) { lastCg = { i, m }; continue; }
      if (CAM_REPLAY.has(M)) { lastCam = { i, m }; continue; }
      if (M === 'MulColorBackground') { muls.push({ i, m }); continue; }
      try { replayOne(m); } catch (e) { }
    }
    if (lastBg) { try { replayOne(lastBg.m); } catch (e) { } }
    for (const x of muls) {
      if (lastBg && x.i < lastBg.i) continue;   // 旧背景上的颜色乘算已被最终背景替代
      try { mulColorBg(x.m); } catch (e) { }
    }
    if (lastCg) { try { replayOne(lastCg.m); } catch (e) { } }
    /* 镜头最后应用：它写的是台上那张 <img> 的内联 transform，得等 CG 定下来才有效。
       镜头指令是绝对赋值（scale 覆盖，不是叠加），所以只取最后一条就是该句的最终画面；
       落在最后一条 CG 指令之前说明那张图早被换掉，忽略。 */
    if (lastCam && lastCam.i > (lastCg ? lastCg.i : -1)) {
      try { replayOne(lastCam.m); } catch (e) { }
    }
    ST.fastForward = false;
    // 目标位置本来就没有背景/CG：重放里一条都没下过才撤掉，否则留着复用
    if (!VIS.bg.hit) { $('#bgLayer').innerHTML = ''; VIS.bg.key = null; VIS.bg.el = null; }
    if (!VIS.cg.hit) { $('#cgLayer').innerHTML = ''; VIS.cg.key = null; VIS.cg.el = null; }

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

    // 3) 音频状态（BGM / 循环音效）：跳过的段落里的 PlayMusic 要补上，否则进来没声音
    restoreAudioAt(midx, !resume);

    // 4) 目标句的语音挂在 midx 前一条 VOICE 上，补播它
    ST.voiceP = null;
    const prev = ST.methods[midx - 1];
    if (prev && prev.Method === 'VOICE' && prev.Voice) {
      ST.voiceP = playVoice(prev.Voice);
    }

    ST.idx = midx;
    ST.curMsgIdx = midx;          // 立刻更新：连按方向键才不会被当成「还在原句」
    ST.running = resume;
    setPlayBtn(resume);
    if (ST.lineMap && ST.lineMap[midx] != null) focusLine(ST.lineMap[midx]);
    updateProgress();
    if (!resume) {
      // 暂停着点进度条：把那一句的文本贴上去，画面停在这儿等人点播放
      const m = ST.methods[midx];
      if (m && m.Method === 'ShowMessage') {
        $('#msgText').classList.remove('partNotice');
        $('#msgText').textContent = lineText(m);
      }
      seeking = false;
      return;
    }
    await wait(50);
    continueLoop();
  } finally {
    seeking = false;
  }
}

/* ================= 播放控制 ================= */
/** 播放某一段。
 *  opt.noResume：忽略播放进度、从这一段开头播（重播 / 下一段 / 自动续播下一段用）。
 *  默认行为是从**上次看到的那一句**接着播 —— 侧栏点进来就能马上定位。 */
async function playPart(partIdx, opt) {
  // 只换播放循环，不销毁 BGM：重播/切段后音乐还能续上（脚本里的 PlayMusic 会自然换曲）
  haltLoop();
  stopLoopSe();
  resumeAudio();
  await wait(0);
  ST.part = partIdx;
  // 段变了要更新侧栏选中态：合集包里同一个 key 有多条"话"，靠 data-part 区分
  if (typeof refreshSelection === 'function') refreshSelection();
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
  /* 接着上次看：只有"这一段确实留了进度、而且没看到最后一句"才跳，
     否则从开头播（已经看完的段再点进来应该重看，不是停在最后一句话上）。 */
  const rec = (opt && opt.noResume) ? null : (UD ? UD.prog(ST.key) : null);
  if (rec && rec.p === partIdx && rec.mi > 0 && rec.mi < ST.methods.length
      && (rec.n ? rec.l < rec.n - 1 : true)) {
    toast(`继续上次：第 ${partIdx + 1} 段 第 ${UD.lineNo(rec)}/${rec.n} 句`
      + `（${UD.progPct(rec)}%）· 点「⏮ 重播」从头看`);
    seekTo(rec.mi);
    return;
  }
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
  if (ST.running && ST.idx >= ST.methods.length) await partBoundary(gen);
}

/* ================= 段间衔接 =================
 * 一段播完后**画面保持不动**，只在台词框里贴一行提示：
 *   第 N 段播放完毕 · 3 秒后自动播放第 N+1 段
 * 3 秒是真实时间（不乘倍速）；按暂停 / 切段 / 点「下一句」都会立即打断。
 * 三种情况只提示、不自动续播：
 *   1. 角色剧情播完**倒数第二段**（最后一段不自动进，要玩家手动切）；
 *   2. 控制条的「自动」被关掉；
 *   3. 已经是最后一段。 */
const PART_GAP_MS = 3000;

/** 固定时长停留（不受倍速影响），可被 skipAll（暂停/跳转/快进）立刻打断。
 *  返回 true = 等满了；false = 被打断（调用方就此收手，别再往下播）。 */
function gapWait(ms) {
  return new Promise(res => {
    let done = false;
    const finish = ok => {
      if (done) return;
      done = true;
      clearTimeout(t);
      ST.skips = ST.skips.filter(f => f !== cancel);
      res(ok);
    };
    const cancel = () => finish(false);
    const t = setTimeout(() => finish(true), Math.max(0, ms));
    ST.skips.push(cancel);
  });
}

/** 把提示写进对话框：不动背景/立绘、不动进度、也不进右侧文本列表。
 *  玩家把对话框关掉了（控制条「对话框」按钮）就退化成顶部 toast，绝不强行弹框。 */
function showPartNotice(text) {
  const w = $('#msgWindow'), t = $('#msgText'), n = $('#msgName');
  if (!w || !t) return;
  if (!SET.showWin) { toast(text); return; }
  w.classList.add('on');
  if (n) { n.textContent = ''; n.classList.remove('on'); }
  t.classList.remove('narr');
  t.classList.add('partNotice');
  t.textContent = text;
}

/** 段尾该显示的提示文案（自动续播时带剩余秒数）。切换语言时也用它重画。 */
function partNoticeText(secs) {
  const total = (S.data && S.data.parts) ? S.data.parts.length : 1;
  const cur = ST.part;                 // 刚播完的段（0 基）
  const n = cur + 1;                   // 界面口径（1 基）
  if (n >= total) {
    return L10N.t('part.endLast', `第 ${n} 段播放完毕 · 已是最后一段`, { n });
  }
  const nextNo = n + 1;
  // 角色剧情：最后一段不自动进，停在倒数第二段播完处
  if (isCharKey(S.key) && cur === total - 2) {
    return L10N.t('part.endHold',
      `第 ${n} 段播放完毕 · 已暂停，要播放第 ${nextNo} 段请手动切换`, { n, m: nextNo });
  }
  if (!ST.auto) {
    return L10N.t('part.endNoAuto',
      `第 ${n} 段播放完毕 · 「自动」已关闭，要播放第 ${nextNo} 段请手动切换`,
      { n, m: nextNo });
  }
  const s = secs == null ? Math.round(PART_GAP_MS / 1000) : secs;
  return L10N.t('part.endAuto',
    `第 ${n} 段播放完毕 · ${s} 秒后自动播放第 ${nextNo} 段`, { n, s, m: nextNo });
}

/** 一段播完的收尾：提示 + 决定续播还是停下。gen 用来确认这期间没被暂停/切段。 */
async function partBoundary(gen) {
  const total = (S.data && S.data.parts) ? S.data.parts.length : 1;
  const cur = ST.part;
  const willPlay = cur + 1 < total && ST.auto
    && !(isCharKey(S.key) && cur === total - 2);
  if (!willPlay) {
    showPartNotice(partNoticeText());
    ST.running = false; setPlayBtn(false);
    return;
  }
  const next = cur + 1;
  for (let s = Math.round(PART_GAP_MS / 1000); s >= 1; s--) {
    showPartNotice(partNoticeText(s));
    const ok = await gapWait(1000);
    if (gen !== ST.run || !ST.running) return;      // 被暂停/切段接管
    // 按住 K 快进时不等：直接把下一段接上，别把快进卡在段界上
    if (!ok) { if (ST.rush) playPart(next, { noResume: true }); return; }
  }
  // noResume：自动播下一段就是"接着往下看"，别又跳回下一段自己的旧进度
  playPart(next, { noResume: true });
}

/* opt.keepVisual：跳转重建时保留背景/CG（见 seekTo），避免黑屏闪烁 */
function resetStage(opt) {
  const keepVisual = !!(opt && opt.keepVisual);
  if (keepVisual) {
    $('#fxLayer').innerHTML = '';
    VIS.bg.hit = false; VIS.cg.hit = false;
    // 颜色乘算 / 缩放等一次性效果由重放重新施加，先复位成原始画面
    ['#bgLayer', '#cgLayer'].forEach(s => {
      $(s).querySelectorAll('img').forEach(i => {
        i.style.transition = 'none';
        i.style.filter = '';
        i.style.transform = '';
        i.style.opacity = 1;
      });
    });
    $('#standLayer').innerHTML = '';
  } else {
    ['#bgLayer', '#cgLayer', '#fxLayer', '#standLayer'].forEach(s => { $(s).innerHTML = ''; });
    VIS.bg.key = null; VIS.bg.el = null;
    VIS.cg.key = null; VIS.cg.el = null;
  }
  $('#flashLayer').style.opacity = 0;
  $('#placeLayer').style.opacity = 0;
  $('#msgWindow').classList.remove('on');
  $('#msgText').style.fontSize = '';
  /* 段间提示（「第 N 段播放完毕…」）不是台词：切段时连文字一起清掉，
     否则下一段 ShowMessageWindow 把框打开的一瞬间会闪一下上一段的提示。 */
  const mtEl = $('#msgText');
  if (mtEl.classList.contains('partNotice')) { mtEl.textContent = ''; mtEl.classList.remove('partNotice'); }
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
/* ================= 进度：条长 + 句号 =================
 * 条长用**指令下标**比例（一条立绘/背景也算一格）——这是唯一连续的进度；
 * 文字用**台词序号**（第 K/T 句）——玩家真正能对上的是这个，两者天然不等。
 * 这里**不统计墙钟时间**：逐句演出没有客观时长（打字机速度、语音长短、玩家点快慢都在变），
 * "你看了多久"对玩家毫无用处。时间只在气泡里给一个按台本量估的参考（见 timeAtLine）。 */
/** 进度条拖动状态（updateProgress 读它决定要不要让出填充宽度，所以必须提前声明） */
const seekDrag = { on: false, ratio: 0 };

/** 毫秒 → m:ss（过 1 小时自动 h:mm:ss） */
function fmtClock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  const p2 = v => (v < 10 ? '0' + v : '' + v);
  return h ? `${h}:${p2(m)}:${p2(ss)}` : `${m}:${p2(ss)}`;
}

/** 当前台词是本段第几句（1 基）；还没播到台词时是 0 */
function curLineNo() {
  if (ST.curMsgIdx == null || !ST.lineMap) return 0;
  const gi = ST.lineMap[ST.curMsgIdx];
  return gi == null ? 0 : gi + 1;
}

/** 「第 N/M 段 · K/T 句」。每条指令都会调，所以文本没变就不写 DOM。 */
let _posText = '';
function updatePos() {
  const pinfo = $('#pinfo');
  if (!pinfo || !S.data) return;
  const segs = S.data.parts.length;
  const total = (ST.allLines || []).length;
  const txt = `第 ${ST.part + 1}/${segs} 段 · ${curLineNo()}/${total} 句`;
  if (txt !== _posText) {
    _posText = txt;
    const seg = $('#pseg'), line = $('#pline');
    if (seg && line) {                    // 两段分开写：窄窗口 CSS 只藏掉段号
      seg.textContent = `第 ${ST.part + 1}/${segs} 段 · `;
      line.textContent = `${curLineNo()}/${total} 句`;
    } else pinfo.textContent = txt;
  }
}

function updateProgress() {
  const p = ST.methods.length ? ST.idx / ST.methods.length * 100 : 0;
  const fill = $('#progress > i');
  // 拖动中由预览接管填充宽度，别让播放循环把它抢回来
  if (fill && !seekDrag.on) fill.style.width = p + '%';
  updatePos();
}
function updateInfo() {
  const p = S.data?.parts[ST.part];
  if (!p) return;
  // 段号与剧本内部文件名（adv_op_4 这种）塞进 title：窄窗口会藏掉段号，
  // 需要排查时悬停就能看到完整信息
  $('#pinfo').title = `第 ${ST.part + 1}/${S.data.parts.length} 段 · ${p.file}`;
  _posText = '';                 // 强制重写（段号可能没变，但句数变了）
  updatePos();
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
              msgBox: 'box',                // 文本框外观：box / soft / bare
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
  /* 文本框外观：box=有边框 / soft=无框+柔和底衬 / bare=无框+纯文字（只剩字幕）。
     用 class 而不是内联样式，CSS 那边全靠 #msgWindow.wb-* 覆盖，改动集中在一处。 */
  const wEl = $('#msgWindow');
  const wb = SET.msgBox || 'box';
  if (wEl) {
    wEl.classList.toggle('wb-soft', wb === 'soft');
    wEl.classList.toggle('wb-bare', wb === 'bare');
  }
  // 无框时背景色/不透明度不生效：控件变灰（面板上那行说明已经解释了原因），
  // 免得用户拖了没反应以为坏了
  const noBg = wb !== 'box';
  ['#setA', '#setMsgBg'].forEach(sel => {
    const el = $(sel);
    if (el) el.disabled = noBg;
  });
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
  /* 同步层：SET 里哪些键变了就给哪个打时间戳，推给 userdata/prefs.json。
     换设备/换浏览器登录同一台机器时，这些设置会跟着回来（用户 2026-11-03 要求）。
     注意用 window.UD：本函数可能在 UD 这个 const 声明之前就被调用（模块初始化阶段）。 */
  if (window.UD && window.UD.syncSet) window.UD.syncSet(SET);
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
  if ($('#setMsgBox')) $('#setMsgBox').value = SET.msgBox || 'box';
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
  // 稀有度只有 SSR/SR/R 三档：「其他」按钮已删（384 个角色现在都有稀有度），
  // 但 localStorage 里可能还留着 'other'，会筛出空列表 → 读盘时清掉。
  SET.rarSel = (SET.rarSel || '').split(',')
    .filter(x => x === 'SSR' || x === 'SR' || x === 'R').join(',');
  /* 偏好层优先：escah_prefs 里有的键覆盖上面从 escah_set 读到的值。
     为什么以偏好层为准：它每改一次都会跟着打时间戳并推给 userdata/prefs.json，
     而 escah_set 只是"本机最后一次实际应用"的镜像（也给视频导出脚本读）。
     escah_set 被清掉、或这台设备是新的，就靠这一句把设置恢复回来。
     服务端有更新的值（别的设备改过）时，hydrate 之后还会再覆盖一次。 */
  if (window.UD && window.UD.prefValue) {
    for (const k in DEFAULTS) {
      const v = window.UD.prefValue('set', k, undefined);
      if (v !== undefined) SET[k] = v;
    }
  }
  syncSettingControls();
  applySettings();
  /* 老用户第一次升级到「设置也同步」的版本：本机设置还没登记进偏好层，
     这里补一次（时间戳给 1 —— 别的设备真改过的时间戳是真实时间，永远优先）。 */
  if (window.UD && window.UD.syncSet) window.UD.syncSet(SET, true);
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

/* ================= 进度条：点击 / 拖动 / 悬停预览 ================= */
/* 像视频播放器那样：点哪跳哪。比例先换算成"第几条指令"，再**吸附到它前面最近的一句台词**
   —— 必须落在 ShowMessage 的下标上，因为 seekTo 是从头重放到那一条来重建画面的。 */
function pickLineAtRatio(ratio) {
  const list = lineIdxList();
  if (!list.length) return null;
  const r = Math.max(0, Math.min(1, ratio));
  const target = Math.round(r * (ST.methods.length - 1));
  let pick = list[0];
  for (const i of list) { if (i <= target) pick = i; else break; }
  return pick;
}
function seekByRatio(ratio) {
  if (!S.data || !ST.methods.length) return;
  const pick = pickLineAtRatio(ratio);
  if (pick == null) return;
  seekTo(pick, { keepPaused: !ST.running });
}

/** 气泡：第 K/T 句 + 台本估算的「位置时间 / 本段总时长」，跟着鼠标横坐标走（fixed，上方 6px） */
function showSeekTip(ratio, clientX) {
  const tip = $('#seekTip');
  const bar = $('#progress');
  if (!tip || !bar || !ST.methods.length) return;
  const midx = pickLineAtRatio(ratio);
  if (midx == null) return;
  const total = (ST.allLines || []).length;
  const no = (ST.lineMap && ST.lineMap[midx] != null) ? ST.lineMap[midx] + 1 : 0;
  const totalMs = partTimeMs();
  const timeTxt = totalMs
    ? `<em>约 ${fmtClock(timeAtLine(no))} / ${fmtClock(totalMs)}</em>` : '';
  tip.innerHTML = `<b>${no}/${total}</b> 句${timeTxt}`;
  tip.hidden = false;
  const r = bar.getBoundingClientRect();
  // 靠边时别让气泡跑出屏幕（半个气泡宽 ≈ 70px）
  const x = Math.min(window.innerWidth - 78, Math.max(78, clientX));
  tip.style.left = x + 'px';
  tip.style.top = (r.top - 6) + 'px';
}
function hideSeekTip() {
  const tip = $('#seekTip');
  if (tip) tip.hidden = true;
}

/** 拖动预览：填充变淡 + 跟手，松手才真跳 */
function paintSeekPreview(ratio) {
  const fill = $('#progress > i');
  if (!fill) return;
  fill.classList.add('pre');
  fill.style.width = (Math.max(0, Math.min(1, ratio)) * 100) + '%';
}
function clearSeekPreview() {
  const fill = $('#progress > i');
  if (fill) fill.classList.remove('pre');
}

/* 用 pointer 事件统一「点击」与「拖动」：
   **松手才 seekTo**。seekTo 要从前到后重放几百条指令重建画面，
   拖动过程中跟着跳等于一秒跳几十次，直接卡死；只有松手那一下值得付这个成本。 */
(() => {
  const bar = $('#progress');
  if (!bar) return;
  bar.title = L10N.t('ctrl.seek', '点击或拖动进度条，跳到想看的位置');
  const ratioAt = x => {
    const r = bar.getBoundingClientRect();
    return r.width ? Math.max(0, Math.min(1, (x - r.left) / r.width)) : 0;
  };
  bar.addEventListener('pointerdown', e => {
    if (!S.data || !ST.methods.length) return;
    seekDrag.on = true;
    seekDrag.ratio = ratioAt(e.clientX);
    bar.classList.add('drag');
    try { bar.setPointerCapture(e.pointerId); } catch (err) { }
    paintSeekPreview(seekDrag.ratio);
    showSeekTip(seekDrag.ratio, e.clientX);
    e.preventDefault();               // 别让浏览器把拖动当文本选择/滚动
  });
  bar.addEventListener('pointermove', e => {
    const r = ratioAt(e.clientX);
    if (seekDrag.on) { seekDrag.ratio = r; paintSeekPreview(r); }
    showSeekTip(r, e.clientX);
  });
  const finish = () => {
    if (!seekDrag.on) return;
    seekDrag.on = false;
    bar.classList.remove('drag');
    hideSeekTip();
    clearSeekPreview();
    seekByRatio(seekDrag.ratio);       // seekTo 里会 updateProgress，填充宽度自然回到真实位置
  };
  bar.addEventListener('pointerup', finish);
  bar.addEventListener('pointercancel', () => {
    if (!seekDrag.on) return;
    seekDrag.on = false;
    bar.classList.remove('drag');
    hideSeekTip();
    clearSeekPreview();
    updateProgress();
  });
  bar.addEventListener('pointerleave', () => {
    if (!seekDrag.on) { hideSeekTip(); updateProgress(); }
  });
})();

/* ================= 快捷键帮助（最右边的 ? 按钮） ================= */
/* 原来这个按钮只有 title，**没有 onclick**，用户点了没反应。
   现在点一下弹一张小卡片，把播放器实际绑定的快捷键列清楚。 */
const KEY_HELP = [
  ['空格 / P', 'keys.play', '暂停 · 继续'],
  ['回车 / ↓ / →', 'keys.next', '下一句'],
  ['↑ / ←', 'keys.prev', '上一句'],
  ['按住 K', 'keys.rush', '快进'],
  ['点画面', 'keys.click', '暂停 · 继续'],
  ['S', 'keys.set', '显示设置'],
  ['F', 'keys.fs', '全屏'],
  ['L', 'keys.lang', '切换语言'],
  ['Esc', 'keys.esc', '关闭浮层'],
];

function closeKeyHelp() {
  const t = $('#keyTip');
  if (t) t.remove();
}

function toggleKeyHelp() {
  if ($('#keyTip')) { closeKeyHelp(); return; }
  const btn = $('#btnKeys');
  const tip = document.createElement('div');
  tip.id = 'keyTip';
  tip.innerHTML = `<div class="kt-h">${L10N.t('keys.title', '快捷键')}</div>`
    + KEY_HELP.map(([k, key, def]) =>
      `<div class="kt-r"><kbd>${esc(k)}</kbd><span>${esc(L10N.t(key, def))}</span></div>`).join('');
  document.body.appendChild(tip);
  const b = btn.getBoundingClientRect();
  tip.style.right = Math.max(8, Math.round(innerWidth - b.right)) + 'px';
  tip.style.bottom = Math.round(innerHeight - b.top + 8) + 'px';
}

if ($('#btnKeys')) {
  $('#btnKeys').onclick = e => { e.stopPropagation(); toggleKeyHelp(); };
  // 点别处 / 滚轮 / 窗口变化就收起，别让它一直挂着挡画面
  document.addEventListener('click', e => {
    if (!$('#keyTip')) return;
    if (e.target.closest && e.target.closest('#keyTip')) return;
    if (e.target.id === 'btnKeys') return;
    closeKeyHelp();
  });
  window.addEventListener('resize', closeKeyHelp);
}
if ($('#setClose')) {
  $('#setClose').onclick = () => { $('#settings').hidden = true; };
}

/* 关闭按钮是**面板外的浮窗**，位置随面板实时算：
 *   · 垂直居中于面板（不是视口 —— 面板高度随内容变）
 *   · 水平贴面板左侧 10px；窄屏放不下就夹在屏幕内
 * 面板显隐由很多地方改（按钮、Esc、代码），用 MutationObserver 统一同步，
 * 免得漏一处就出现"面板关了按钮还在"。 */
(function bindSetClose() {
  const st = $('#settings');
  const btn = $('#setClose');
  if (!st || !btn) return;

  function place() {
    const r = st.getBoundingClientRect();
    if (!r.height) return;
    btn.style.top = Math.round(r.top + r.height / 2) + 'px';
    const right = window.innerWidth - r.left + 10;
    btn.style.right = Math.max(6, Math.min(right, window.innerWidth - 40)) + 'px';
  }

  function sync() {
    btn.hidden = st.hidden;
    if (!st.hidden) {
      // 面板刚显示时高度可能还没算好，下一帧再定位一次
      place();
      requestAnimationFrame(place);
    }
  }

  new MutationObserver(sync).observe(st, {
    attributes: true, attributeFilter: ['hidden'],
  });
  window.addEventListener('resize', function () { if (!st.hidden) place(); });
  sync();
})();
/* 设置面板的滑块：自己接管拖动。
   有些内嵌浏览器（Electron / webview）里原生 range 只响应"点击跳到该点"，
   按住拖动时 thumb 不跟随 —— 这里按 pointer 坐标直接换算数值再派发 input，
   原生实现正常时两者结果一致，不会互相打架。 */
function bindRangeDrag(inp) {
  if (!inp || inp.dataset.dragBound) return;
  inp.dataset.dragBound = '1';
  inp.style.touchAction = 'none';
  const apply = clientX => {
    const r = inp.getBoundingClientRect();
    if (!r.width) return;
    const min = +inp.min || 0;
    const max = +inp.max || 100;
    const step = +inp.step || 1;
    let ratio = (clientX - r.left) / r.width;
    ratio = Math.min(1, Math.max(0, ratio));
    let v = min + ratio * (max - min);
    v = min + Math.round((v - min) / step) * step;
    v = Math.min(max, Math.max(min, v));
    if (+inp.value === v) return;
    inp.value = v;
    inp.dispatchEvent(new Event('input', { bubbles: true }));
  };
  inp.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    e.preventDefault();                 // 别让 label 抢焦点，也别触发文本选择
    try { inp.setPointerCapture(e.pointerId); } catch (_) { /* 忽略 */ }
    apply(e.clientX);
    const move = ev => apply(ev.clientX);
    const up = () => {
      inp.removeEventListener('pointermove', move);
      inp.removeEventListener('pointerup', up);
      inp.removeEventListener('pointercancel', up);
    };
    inp.addEventListener('pointermove', move);
    inp.addEventListener('pointerup', up);
    inp.addEventListener('pointercancel', up);
  });
}
document.querySelectorAll('#settings input[type=range]').forEach(bindRangeDrag);

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
$('#setMsgBox').onchange = e => { SET.msgBox = e.target.value; applySettings(); };
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
   播放状态一律由 S.key / S.data 描述。
   用户最后停在哪个分类也要记住（用户 2026-11-03 要求保存筛选条件：
   上次在「活动」里翻剧情，下次打开不该又跳回「角色」）。 */
const SRC_KEY = 'escah_src';
const SRC_ALL = ['char', 'main', 'event', 'other'];
let SRC = (function () {
  // 偏好层优先（可能来自别的设备 / userdata/prefs.json），老键只作为迁移来源
  const v = (window.UD && window.UD.prefValue) ? window.UD.prefValue('ui', 'src', null) : null;
  if (v && SRC_ALL.indexOf(v) >= 0) return v;
  try {
    const s = localStorage.getItem(SRC_KEY);
    if (s && SRC_ALL.indexOf(s) >= 0) return s;
  } catch (e) { }
  return 'char';
})();
function saveSrc() {
  if (window.UD && window.UD.syncPref) window.UD.syncPref('ui', 'src', SRC);
  try { localStorage.setItem(SRC_KEY, SRC); } catch (e) { }
}
const S = { key: null, kind: 'char', char: null, data: null, voiceRes: 0 };

/** 按 key 取 advindex 条目：**优先整包条目**（没有 part 的），
    因为序章会以「同一 key + part」的形式额外挂几条别名。 */
function advByKey(key) {
  return ADV.find(a => a.key === key && !Number.isInteger(a.part))
    || ADV.find(a => a.key === key);
}

/** 剧情标题（用于列表与复制定位信息）。
    侧栏每一行左边已经有「主/活/其」标签，所以标题里**不再重复**分类名：
    序章 → 「第2部 序章」；主线章节 → 「第2部 第1章」；活动/其他 → 「第47话」。
    实现搬到 common.js 的 ESC.storyTitle —— 控制台 ②③⑤ 的列表、导出列表
    都用同一份，否则同一部剧情会在两处显示成两个名字。 */
function storyTitle(key) {
  return ESC.storyTitle(key);
}

function toast(msg) {
  const el = $('#toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('on');
  clearTimeout(el._t);
  /* 停留时间随字数走：固定 1.6 秒只够读「已复制」这种短句，
     报"哪几部剧情没下载"那种长句根本看不清就消失了。 */
  const ms = Math.min(6000, 1600 + String(msg).length * 120);
  el._t = setTimeout(() => el.classList.remove('on'), ms);
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

/* ================= 收藏夹 / 播放进度 =================
 * 数据层在 assets/js/userdata.js（CG 页共用同一份 localStorage），这里只负责
 * 把它接进左侧列表：每行一颗 ★ + 一行"看到多少"，外加「播放记录」视图。
 *
 * 收藏粒度 = 列表里的一行：
 *   角色 / 主线 / 活动 → 整部剧情（char_12 / adv_event_42）
 *   「其他」的一话     → 包 + 段（adv_other_7#2），因为那才是用户点的那一项
 */
const UD = window.UD || null;
/* 「播放记录」视图开关：点 rar-row 第四格切换，列表换成"最近看过什么" */
let HIST_ON = false;

/** 一行的收藏 id（part 传 null 表示整部剧情） */
function favIdStory(key, part) {
  return (part == null) ? String(key) : String(key) + '#' + part;
}
const dlFilterVal = () => ($('#dlFilter') || {}).value || '';
const favOnly = () => dlFilterVal() === 'fav';

/** 收藏星（拼进 innerHTML；点击在 bindFav 里绑） */
function favHTML(id) {
  if (!UD) return '';
  const on = UD.isFav('story', id);
  return `<span class="fav${on ? ' on' : ''}" title="${on ? '取消收藏' : '加入收藏'}">${on ? '★' : '☆'}</span>`;
}

/** 播放进度提示：「第2/5段 45%」；其他视图按"话"取，段对不上就不显示 */
function progHTML(key, part, totalParts) {
  const rec = UD ? UD.prog(key) : null;
  if (!rec) return '';
  if (part != null && rec.p !== part) return '';
  const seg = (totalParts && totalParts > 1) ? `第${rec.p + 1}/${totalParts}段 `
    : (rec.p > 0 ? `第${rec.p + 1}段 ` : '');
  // data-tp 记住总段数：播放中要就地刷新这行进度，格式不能变
  return `<span class="pg" data-tp="${totalParts || 0}"`
    + ` title="上次看到：第 ${rec.p + 1} 段，第 ${UD.lineNo(rec)} 句 / 共 ${rec.n} 句">`
    + `${seg}${UD.progPct(rec)}%</span>`;
}

/** 正在播的这一行，进度文字就地跟着走。
 *  不重画整个列表（384 行太重、还会闪），只改那一行里已经存在的 .pg。
 *  列表里本来没有进度提示的行（第一次看）不在这里插入 —— 下次刷新自然就有了。 */
function syncRowProg() {
  if (!UD || !ST.key) return;
  const box = $('#charList');
  if (!box) return;
  const rec = UD.prog(ST.key);
  if (!rec) return;
  box.querySelectorAll(`.char-btn[data-key="${ST.key}"]`).forEach(row => {
    const part = row.dataset.part == null ? null : +row.dataset.part;
    if (part != null && part !== ST.part) return;
    const pg = row.querySelector('.sub .pg');
    if (!pg) return;
    const tp = +pg.dataset.tp || 0;
    const seg = (tp > 1) ? `第${rec.p + 1}/${tp}段 ` : (rec.p > 0 ? `第${rec.p + 1}段 ` : '');
    pg.textContent = `${seg}${UD.progPct(rec)}%`;
    pg.title = `上次看到：第 ${rec.p + 1} 段，第 ${UD.lineNo(rec)} 句 / 共 ${rec.n} 句`;
  });
}

/** 绑收藏点击；onChange 用于"已收藏筛选开着时点掉就重画列表" */
function bindFav(root, id, onChange) {
  const el = root.querySelector('.fav');
  if (!el || !UD) return;
  el.onclick = ev => {
    ev.stopPropagation();          // 别顺手把这一行点开了
    const on = UD.toggleFav('story', id);
    el.classList.toggle('on', on);
    el.textContent = on ? '★' : '☆';
    el.title = on ? '取消收藏' : '加入收藏';
    toast(on ? '已加入收藏' : '已取消收藏');
    if (onChange) onChange();
  };
}

/** 收藏 / 已下载筛选变化后要不要立刻重画（收藏筛选开着时，星点掉这行就该走） */
function refilterIfFav() {
  if (favOnly()) renderSidebar(($('#search') || {}).value || '');
}

/** 当前剧情每收到一句台词就记一次进度（内存立即更新，落盘由 UD 合并） */
let _progTitleKey = null, _progTitle = '';
function noteProgress() {
  if (!UD || !ST.key || ST.curMsgIdx == null || !ST.lineMap) return;
  const line = ST.lineMap[ST.curMsgIdx];
  if (line == null) return;
  if (_progTitleKey !== ST.key) {
    _progTitleKey = ST.key;
    _progTitle = storyTitle(ST.key);
  }
  UD.setProg(ST.key, ST.part, ST.curMsgIdx, line, ST.allLines.length, _progTitle);
  syncRowProg();
}

/* ================= 云端有、本地没装的剧情 =================
 * index.js / advindex.js 都是随程序分发的静态索引，不随资源清单更新。
 * 所以官方新上线的角色/章节在控制台能装（那边从清单推导），但播放器侧栏里
 * 根本看不到 —— 用户会以为"没有这个角色 / 没有这个活动"。这里补一次：
 * 拉 /api/cdn/units，把"云端有、静态索引里没有"的单元并进列表并标未下载。
 *
 * 主线/活动/其他同样处理（2026-11-04 用户要求）：它们**没有 Wiki 可查**，
 * 名字只能靠 ESC.storyTitle 从编号推算（第N部 第X章 / 活动剧情 第N话），
 * 段数/句数要装了才知道，所以先显示"未下载"，点了引导去资源管理。
 */
let CLOUD_ONLY = [];        // 云端有、本地没装的**角色**（INDEX 里没有）
let CLOUD_ADV = [];         // 同上，但主线/活动/其他（ADV 里没有）
let _cloudLoaded = false;

function allChars() {
  return CLOUD_ONLY.length ? INDEX.concat(CLOUD_ONLY) : INDEX;
}

function allAdv() {
  return CLOUD_ADV.length ? ADV.concat(CLOUD_ADV) : ADV;
}

/** 云端单元 key → 侧栏用的"伪 advindex 条目"（结构对齐 advindex.js） */
function cloudAdvRow(key) {
  const p = /^adv_(\w+?)(?:_(\d+))?$/.exec(String(key)) || [];
  const kind = p[1] || '';
  const no = p[2] ? +p[2] : null;
  return {
    key: key, kind: kind, no: no,
    title: storyTitle(key),           // 推算：第N部 第X章 / 活动剧情 第N话
    // 活动/其他没有官方顺序（bundle 编号 ≠ 发表顺序）→ 排到本类末尾
    order: (kind === 'main' && no !== null) ? no : 1000 + (no || 0),
    // 横幅按约定路径拼（与解包产物同名）；没下过就 404 → onerror 自己收起来
    cover: (no !== null && kind !== 'tutorial' && kind !== 'op')
      ? `media/img/story/${kind}_${no}.webp` : '',
    parts: 0, lines: 0, first: '', voices: 0, voiceRes: 0, cloud: true,
  };
}

/** 「云端有、本地没装」的提示文案。
 *  只说「发现 N 部新剧情（未下载）」用户还得自己去列表里翻是哪几部（2026-11-06 用户
 *  要求报清楚**哪一类、标题叫什么**），所以这里把名字直接列出来；超过 4 条用
 *  「等 N 个」收口 —— 标题本身可能很长，全列出来 toast 会撑成一堵墙。
 *  标题一律走 ESC.storyTitle（"活动剧情 第42话" 这种自带分类名）。 */
function cloudTip(countText, chars, advs) {
  const clean = s => String(s || '').replace('（未接入）', '');
  const names = [];
  advs.forEach(a => names.push(clean(a.title) || clean(storyTitle(a.key))));
  chars.forEach(c => names.push(clean(storyTitle('char_' + c.id))));
  const MAX = 4;
  const list = names.slice(0, MAX).map(n => '「' + n + '」').join('、')
    + (names.length > MAX ? ' 等 ' + names.length + ' 个' : '');
  return '发现 ' + countText + '还没下载：' + list
    + '。到「资源管理」装上就会出现在这里';
}

async function loadCloudChars() {
  if (_cloudLoaded || !window.PACKSTATE) return;
  _cloudLoaded = true;
  try {
    const r = await fetch('api/cdn/units', { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    if (d.needManifest) return;              // 清单还没下，等用户装完再说
    /* 「索引里没有」≠「没下载」：本地状态才是唯一依据。
       2026-11-06 就是这样翻车的 —— 漫画活动（0 句台词的 ShowEventCg 连环画）被旧
       规则挡在 advindex 之外，剧本文件其实好好装着，资源管理显示「已全部安装」，
       而这里只看索引差异，于是弹「发现 5 部新剧情（未下载）」。两边说法打架。
       现在：只有 state=missing（清单有、本地没文件）才算未下载；已安装却没进索引
       的不再并进来（索引侧的根因已修，见 stages.sync_advindex 的空包判定）。 */
    const st = d.state || {};
    const isMissing = k => !st[k] || st[k] === 'missing';
    const have = new Set((INDEX || []).map(c => +c.id));
    const extra = [];
    ((d.units || {}).char || []).forEach(function (k) {
      const id = +String(k).slice(5);
      if (id && !have.has(id) && isMissing(String(k)))
        extra.push({ id: id, parts: 0, lines: 0, cloud: true });
    });
    const haveAdv = new Set((ADV || []).map(a => a.key));
    const extraAdv = [];
    ((d.units || {}).adv || []).forEach(function (k) {
      if (haveAdv.has(String(k))) return;
      if (!isMissing(String(k))) return;         // 装了、只是索引没登记 → 不谎报未下载
      extraAdv.push(cloudAdvRow(String(k)));
    });
    const msgs = [];
    if (extra.length) {
      CLOUD_ONLY = extra;
      msgs.push(extra.length + ' 个角色');
    }
    if (extraAdv.length) {
      CLOUD_ADV = extraAdv;
      msgs.push(extraAdv.length + ' 部剧情');
    }
    if (msgs.length) {
      renderSidebar((($('#search') || {}).value) || '');
      updateCnt();                  // 分类按钮上的总数跟着加上这些
      toast(cloudTip(msgs.join('、'), extra, extraAdv));
    }
  } catch (e) { /* 纯静态打开时没有服务端，忽略 */ }
}

/** 只更新"当前选中"高亮，**不重建列表**。
    点列表项播放时调用它：重建会导致 scrollTop 归零（用户看到的就是"点一下就滚回顶部"）。 */
function refreshSelection() {
  const box = $('#charList');
  if (!box) return;
  box.querySelectorAll('.char-btn').forEach(el => {
    const pt = el.dataset.part;
    const okPart = (pt === undefined) || (+pt === ST.part);
    el.classList.toggle('on', el.dataset.key === S.key && okPart);
  });
}

/* 侧栏重渲染的**统一滚动保护**（beginScroll / endScroll 一对）。
   为什么要有这个东西：`box.innerHTML = ''` 会把 scrollTop 归零，而侧栏有三条渲染路径
   （角色 / 主线活动 / 「其他」分组）。以前每条各写一遍保护，新加一条就忘一次 ——
   于是"点一下就跳回顶部"反复出现：2026-10-26 修过「点播放跳顶」（给角色和主线加了保护），
   2026-11-01 新写的「其他」分组视图又直接写了 `box.scrollTop = 0`，2026-11-02 用户再次反馈。
   现在只留这一个入口：key 变了（搜索词/分类切换）才回顶部，否则原地不动；
   传 anchorSel 就按"被点的那个元素"做锚点，展开/收起后它仍在视野里的同一位置。 */
function beginScroll(box, key, anchorSel) {
  const same = (beginScroll._k === key);
  beginScroll._k = key;
  const sc = { top: same ? box.scrollTop : 0, gap: null };
  if (same && anchorSel) {
    const el = box.querySelector(anchorSel);
    if (el) sc.gap = el.getBoundingClientRect().top - box.getBoundingClientRect().top;
  }
  return sc;
}

function endScroll(box, sc, anchorSel) {
  if (sc.gap !== null && anchorSel) {
    const el = box.querySelector(anchorSel);
    if (el) {
      const now = el.getBoundingClientRect().top - box.getBoundingClientRect().top;
      box.scrollTop += (now - sc.gap);
      return;
    }
  }
  box.scrollTop = sc.top;
}

/** 筛选条跟着"当前看什么"显隐。
 *  以前整条 #filters 只在「角色」里显示，于是主线/活动/其他的列表**根本没有筛选**，
 *  "只看已收藏 / 播放记录"在那些分类里等于不存在。现在：
 *    · 排序 + SSR/SR/R 只对角色有意义 → 非角色/播放记录视图里藏起来；
 *    · 资源与收藏筛选对所有分类都有效 → 一直显示（收藏就是四类都要用的）。 */
function syncFilterBar() {
  const flt = $('#filters');
  if (!flt) return;
  flt.style.display = '';
  const charView = (SRC === 'char') && !HIST_ON;
  const sortEl = $('#sortBy');
  if (sortEl) sortEl.style.display = charView ? '' : 'none';
  const dlEl = $('#dlFilter');
  if (dlEl) dlEl.style.display = HIST_ON ? 'none' : '';
  document.querySelectorAll('.rar-row button[data-r]').forEach(b => {
    b.style.display = charView ? '' : 'none';
  });
}

/** 「播放记录」视图：跨分类、按最后观看时间倒序。
 *  这是"上次看到哪"的最短路径 —— 不用先想起来它在角色还是活动里。 */
function renderHistList(box, filter = '') {
  const sc = beginScroll(box, 'hist|' + filter);
  box.innerHTML = '';
  const f = (filter || '').trim().toLowerCase();
  let list = UD ? UD.recent() : [];
  if (f) {
    list = list.filter(r => {
      const t = (r.t || storyTitle(r.key) || '').toLowerCase();
      return t.includes(f) || String(r.key).toLowerCase().includes(f);
    });
  }

  const hit = $('#hit');
  if (hit) {
    hit.textContent = list.length
      ? `播放记录 ${list.length} 条 · 点一条接着看`
      : '还没有播放记录：看过的剧情会自动记在这里';
    hit.className = list.length ? 'on' : '';
  }

  if (!list.length) {
    const d = document.createElement('div');
    d.className = 'advEmpty';
    d.textContent = '还没有播放记录。随便点一部剧情开始看，这里就会记住你看到第几句，'
      + '下次点「播放记录」就能直接接着看。';
    box.appendChild(d);
    endScroll(box, sc);
    return;
  }

  // 顶部一条：条数 + 清空（误积累太多时可以一次性清掉）
  const bar = document.createElement('div');
  bar.className = 'histBar';
  bar.innerHTML = `<span>共 ${list.length} 条</span>`;
  const clr = document.createElement('button');
  clr.textContent = '清空记录';
  clr.title = '删除全部播放记录（收藏不受影响）';
  clr.onclick = () => {
    if (!confirm('清空全部播放记录？（收藏不会被动）')) return;
    UD.clearAllProg();
    renderHistList(box, filter);
  };
  bar.appendChild(clr);
  box.appendChild(bar);

  list.forEach(r => {
    const key = r.key;
    const title = r.t || storyTitle(key);
    const kind = isCharKey(key) ? 'char' : String(key).split('_')[1];
    const d = document.createElement('div');
    d.className = 'char-btn hrow' + (key === S.key ? ' on' : '');
    d.dataset.key = key;
    d.title = `${title}\n上次看到：第 ${r.p + 1} 段，第 ${UD.lineNo(r)} 句 / 共 ${r.n} 句`
      + `\n${new Date(r.ts).toLocaleString()}`;
    d.innerHTML = `<span class="info"><span class="nm">`
      + (kind === 'char' ? '' : `<span class="srcTag" data-k="${kind}">${ADV_TAG[kind] || '剧'}</span>`)
      + `${esc(title)}</span>`
      + `<span class="sub"><span>第 ${r.p + 1} 段</span>`
      + `<span class="pg">看到 ${UD.progPct(r)}%</span>`
      + `<span>${UD.lineNo(r)}/${r.n} 句</span>`
      + `<span>${UD.ago(r.ts)}</span></span></span>`
      + `<span class="del" title="从记录里删掉这一条（不影响收藏）">✕</span>`;
    d.onclick = () => openStory(key);
    d.querySelector('.del').onclick = ev => {
      ev.stopPropagation();
      UD.clearProg(key);
      renderHistList(box, filter);
    };
    box.appendChild(d);
  });
  endScroll(box, sc);
}

function renderSidebar(filter = '') {
  const box = $('#charList');
  if (!box) return;
  syncFilterBar();
  // 播放记录是**独立视图**（跨分类、按时间倒序），不参与来源/稀有度筛选
  if (HIST_ON) { renderHistList(box, filter); return; }
  // 非角色列表各自由 renderAdvList / renderOtherGroups 负责清空与还原滚动，
  // 这里**不能**先清空，否则它们读到的 scrollTop 已经是 0，等于强制回顶部
  if (SRC !== 'char') { renderAdvList(filter); return; }
  const sc = beginScroll(box, 'char|' + filter);
  box.innerHTML = '';
  const f = filter.trim().toLowerCase();
  const sel = SET.rarSel ? SET.rarSel.split(',') : [];
  // 「已下载 / 未下载 / 已收藏」筛选：和安装列表、CG 页同一套状态口径
  const dlf = dlFilterVal();
  const isHave = (cid) => {
    const st = window.PACKSTATE ? PACKSTATE.status('char_' + cid) : 'unknown';
    return st === 'ready' || st === 'soft-missing' || st === 'partial';
  };

  let list = allChars().filter(c => {
    if (dlf === 'have' && !isHave(c.id)) return false;
    if (dlf === 'miss' && isHave(c.id)) return false;
    if (dlf === 'fav' && !(UD && UD.isFav('story', 'char_' + c.id))) return false;
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
    const active = !!f || sel.length > 0 || dlf === 'fav';
    hit.textContent = !active ? ''
      : (dlf === 'fav'
        ? (list.length ? `已收藏 ${list.length} 个角色` : '还没有收藏过角色剧情（点行尾的 ☆ 就能收藏）')
        : (list.length
          ? L10N.t('search.hit', '命中 {n} 个角色（可滚动）').replace('{n}', list.length)
          : L10N.t('search.none', '没有匹配的角色')));
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
    /* 图片要不要请求，必须"能确定磁盘上有图"才发 —— 横幅和头像都不随程序分发，
       全新环境里 384 行一次会打出 5000 多个 404，把启动窗口刷成一片红。
       两道关，缺一不可：
         ① 整层素材下过没有（这一层太粗：384/385 = 99.7% 仍算"齐"，
            官方刚上新的那个角色照样会被发请求）；
         ② 本机清单里到底有没有这张图（data/mediaindex.js，见 ESC.media）——
            没有就画首字色块占位，一个请求都不发。 */
    const L = imgLayer();
    const inst = st === 'ready' || st === 'soft-missing';
    const avatarsOK = inst || L.avatars;
    const bannersOK = inst || L.banners;
    const bnrSrc = bannersOK ? bannerOfChar(c.id) : '';
    const avSrcs = avatarsOK ? avatarCands(c.id) : [];
    const imgChain = (bnrSrc ? [imgSrc(bnrSrc)] : []).concat(avSrcs);
    const haveImg = imgChain.length > 0;
    // 侧栏一项 = 横幅（占一行）+ 标题/信息（占一行）
    d.className += ' bnr';
    d.innerHTML =
      `<span class="bnr">` + (haveImg ? '<img alt="" loading="lazy">'
        : `<span class="ava-ph">${esc(String(nm).slice(0, 1))}</span>`) +
      `</span>` +
      `<span class="info"><span class="nm">${esc(nm)}</span>` +
      `<span class="sub"><span class="rar" data-r="${rar}">${m.r || '—'}</span>` +
      `<span>${c.lines ? c.lines + ' 句' : '未安装'}</span>` +
      progHTML('char_' + c.id, null, c.parts) +
      (st === 'missing' ? '<span class="badge nodl">未下载</span>' :
        st === 'soft-missing' ? '<span class="badge soft">缺素材</span>' : '') +
      `</span>` +
      `<span class="cpy" title="复制角色名">⧉</span>` +
      // 收藏星放在最右边：每行位置一致，鼠标只需要上下移动
      favHTML('char_' + c.id) + `</span>`;
    // 先试横幅，失败自动退回头像（缺横幅的角色照样有图）
    if (haveImg) applyChain(d.querySelector('img'), imgChain);
    bindTip(d, () => {
      const parts = c.parts || 0;
      const tr = (typeof transStatsOf === 'function') ? '' : '';
      const rec = UD && UD.prog('char_' + c.id);
      // 浮窗里的缩略图同样只在"确定有图"时才请求（鼠标扫过侧栏就会触发）
      return (bnrSrc
        ? `<img src="${esc(imgSrc(bnrSrc))}" alt="" `
          + `onerror="this.style.display='none'">`
        : '')
        + `<div style="font-weight:600;margin-bottom:4px">${esc(nm)}</div>`
        + `<dl><dt>ID</dt><dd>${c.id}</dd>`
        + `<dt>稀有度</dt><dd>${esc(m.r || '未收录')}${m.d ? ' · 实装 ' + esc(m.d) : ''}</dd>`
        + `<dt>剧情</dt><dd>${parts} 段 · ${c.lines || 0} 句</dd>`
        + `<dt>状态</dt><dd>${st === 'missing' ? '资源未下载' : st === 'soft-missing' ? '缺公共素材' : '可播放'}</dd>`
        + (rec ? `<dt>上次看到</dt><dd>第 ${rec.p + 1} 段 · 第 ${UD.lineNo(rec)}/${rec.n} 句`
          + `（${UD.progPct(rec)}%，${UD.ago(rec.ts)}）</dd>` : '')
        + `</dl>${tr}`;
    });
    bindFav(d, 'char_' + c.id, refilterIfFav);
    d.onclick = () => openChar(c.id);
    d.querySelector('.cpy').onclick = ev => {
      ev.stopPropagation();
      copyText(nm, `已复制：${nm}`);
    };
    box.appendChild(d);
  });
  endScroll(box, sc);
}

/** 侧栏用的「封面横幅」路径。
    角色剧情：media/img/story/char_<cid>.webp（游戏「角色剧情」入口那张图）
    主线/活动：advindex 里的 cover（章节标题横幅）
    其他：没有横幅 → 返回空串，列表退回纯文字行 */
function bannerOfChar(id) {
  // 本机清单说这张横幅不存在（官方刚上新的角色、素材还没装）→ 返回空串，
  // 调用方据此画首字色块而不发请求。以前这里无条件拼路径，于是纯净包一启动
  // 就对 char_416 刷 404（2026-11-06 反馈）。
  if (ESC.media.has('story', 'char_' + id) === false) return '';
  return `media/img/story/char_${id}.webp`;
}
function bannerOfAdv(a) {
  return (a && a.cover) || '';
}

/** media/img/story/<stem>.<ext> 这张横幅在本机磁盘上到底有没有。
 *
 *  advindex 里的 cover 是**按清单推算**的路径（sync_advindex 按约定拼），
 *  官方刚上的新章节、或用户还没补公共素材时，路径算得出来、文件却没有 ——
 *  直接挂 src 就是一次必然失败的请求。清单不可用（null）时一律当"有"，
 *  退回老行为，不会因为缺少 mediaindex.js 就把横幅全关了。
 */
function storyImgOk(path) {
  const m = /^media\/img\/story\/([^./]+)\./.exec(path || '');
  return !m || ESC.media.has('story', m[1]) !== false;
}

/** 悬停浮窗：整个侧栏共用一个，鼠标移开就隐藏（不抢点击，pointer-events:none） */
let tipEl = null;
function tipShow(el, html) {
  if (!tipEl) {
    tipEl = document.createElement('div');
    tipEl.id = 'bnrTip';
    document.body.appendChild(tipEl);
  }
  tipEl.innerHTML = html;
  tipEl.classList.add('show');
  const r = el.getBoundingClientRect();
  const w = tipEl.offsetWidth, h = tipEl.offsetHeight;
  let x = r.right + 12, y = r.top;
  if (x + w > innerWidth - 10) x = Math.max(10, r.left - w - 12);
  if (y + h > innerHeight - 10) y = Math.max(10, innerHeight - h - 10);
  tipEl.style.left = x + 'px';
  tipEl.style.top = y + 'px';
}
function tipHide() {
  if (tipEl) tipEl.classList.remove('show');
}
/* 触屏设备没有真正的"鼠标移开"：点列表项会合成一个 mouseenter，但之后
   不会再有 mouseleave，浮窗就一直挂着挡住画面（手机上点剧情后再点播放，
   浮窗还压在上面）。所以只在**确实能悬停**的设备上绑这个浮窗。 */
const CAN_HOVER = !!(window.matchMedia
  && window.matchMedia('(hover: hover) and (pointer: fine)').matches);

function bindTip(el, htmlFn) {
  if (!CAN_HOVER) return;
  el.addEventListener('mouseenter', () => {
    const h = htmlFn();
    if (h) tipShow(el, h);
  });
  el.addEventListener('mouseleave', tipHide);
}
/* 兜底：触摸任何位置都收起浮窗（触屏笔记本这类混合设备上仍可能残留） */
document.addEventListener('touchstart', tipHide, { passive: true });

/** 头像候选链。
 *
 * 映射（imgmap.avatar / x2 / base）里已经有**真实路径**时直接用，不用猜；
 * 一条都没有才按命名兜底试扩展名 —— 而且必须先问本机清单（ESC.media）：
 * 清单说"没有这个角色头像"就一个候选都不给，调用方画首字色块。
 * 以前不看清单盲试 .png/.webp/.jpg，官方每上一个新角色就白送 3 个 404。
 */
function avatarCands(id) {
  // 本机清单说没有这张头像（官方刚上新、素材还没装）→ 一个候选都不给，
  // 调用方画首字色块。**以清单为准而不是以映射为准**：imgmap.js 是出厂包里
  // 那份，可能比本机磁盘"新"（换过机器、清理过空间）—— 那时映射里写着有、
  // 磁盘上其实没有，请求出去就是 404。
  if (ESC.media.has('avatar', id) === false) return [];
  const out = [];
  const push = p => { if (p && out.indexOf(p) < 0) out.push(p); };
  if (SET.useX2 !== false) push((IMGX2.avatar || {})[id]);
  push((IMGBASE.avatar || {})[id]);
  push((IMGMAP.avatar || {})[id]);
  // 头像导出的是 **.png**（原图格式），不是 .webp —— 以前只试 .webp，
  // 于是全新环境里 378 个头像全部 404、侧栏头像整片空白。
  // 导出的是 **.png**，所以硬编码兜底时先试 png 再试 webp：
  // 顺序反了会让每个角色都先吃一个 404 才回退。
  if (!out.length) {
    push(`media/img/avatar/${id}.png`);
    push(`media/img/avatar/${id}.webp`);
    push(`media/img/avatar/${id}.jpg`);
  }
  // **不再硬拼 media/img_x2/avatar/**：超分层是本机可选产物，只有 imgmap.x2.js
  // 登记过的才算数（上面 IMGX2.avatar 那一条已经覆盖）。硬拼路径等于给每个角色
  // 白送两个 404 —— 没跑过超分的人永远没有这个文件。
  return out.map(imgSrc);
}
/** 非角色剧情列表（主线 / 活动 / 其他） */
/* 侧栏分批渲染：全部剧情接进来后「其他」有 85 项、活动 94 项，
   一次性建 DOM 会明显卡顿，所以每次先画 BATCH 项，滚到底再追加。 */
const ADV_BATCH = 45;
let advAll = [], advCursor = 0;

/** 侧栏「其他」：16 个游戏入口分组 + 投票说明 + 未归类（新剧情）→ 展开成话。
    数据来自 window.__OTHER_EPS：[{no,name,eps:[{k,i,t,n,c}]}]，
    一条话 = 包 key + 第 i 段（不拆文件，播放时跳段）。
    t 现在是游戏里的话名（来襲/退場 そのN、周年、愚人节…），不再是首句截断。
    no<=0 的两组（0 投票说明 / -1 未归类）没有游戏横幅，靠 hasBnr 自动跳过。 */
const OTHER_OPEN = new Set();      // 展开的入口号（内存态，刷新后默认全收起）

/* 这一话有没有语音。
   优先用 othereps.js 里写好的**段级**条数（e.v，构建期从包数据里数出来）；
   老清单没有这个字段时退回 advindex 的整包语音数（voices 剧本引用数 /
   voiceRes 本地文件数）。「其他」里绝大多数包官方本来就没配语音轨，
   列表上必须写出来 —— 不然用户点进去一句都没声音，只会以为播放器坏了。 */
function epHasVoice(e) {
  if (Number.isInteger(e.v)) return e.v > 0;
  const a = advByKey(e.k);
  return !!(a && ((a.voices || 0) > 0 || (a.voiceRes || 0) > 0));
}

function renderOtherGroups(box, filter = '', opt = {}) {
  const f = (filter || '').trim().toLowerCase();
  const groups = window.__OTHER_EPS || [];

  /* 「其他」是分组视图，不走增量追加。清掉上一次活动/主线留下的 advAll/advCursor，
     否则切换瞬间若有 scroll 事件触发 appendAdvChunk，会把别类剧情追加进来
     （用户反馈：活动 → 其他 偶发两种列表并存）。 */
  advAll = [];
  advCursor = 0;

  /* 滚动位置：同一过滤条件下重渲染（展开/收起某一组）必须留在原位。
     以前这里直接 `box.scrollTop = 0`，于是"点分组就跳回顶部"——现在走统一保护，
     并以**被点的那个分组头**为锚点：展开/收起后它仍在视野里的同一位置。 */
  const anchor = opt.anchor || null;
  const anchorSel = anchor ? `.og-head[data-key="${anchor}"]` : null;
  const sc = beginScroll(box, 'other|' + f, anchorSel);

  box.innerHTML = '';

  const favMode = favOnly();       // 「只看已收藏」：只留收藏过的话，并自动展开
  let any = false;                 // 是否画出了任何分组/话（空结果要给一句提示）

  groups.forEach(g => {
    let eps = favMode
      ? g.eps.filter(e => UD && UD.isFav('story', favIdStory(e.k, e.i)))
      : g.eps;
    if (f) {
      eps = eps.filter(e => (e.t || '').toLowerCase().includes(f) ||
        (e.c || '').toLowerCase().includes(f) || g.name.toLowerCase().includes(f));
    }
    if (!eps.length) return;
    // 搜索 / 只看已收藏时全展开：结果是筛过的，收着等于让用户多点一次
    const open = OTHER_OPEN.has(g.no) || !!f || favMode;

    const head = document.createElement('div');
    head.className = 'og-head' + (open ? ' open' : '');
    head.dataset.key = 'etc_' + (g.no ?? '?');
    // 横幅里已经写着 RAID BOSS / 広域戦，标题里就不重复了，省出宽度给后半段
    const shortName = g.name.replace(/^RAID BOSS\s*/, '').replace(/^広域戦\s*/, '広域 ');
    // 入口横幅也是解包产物：没下过公共素材就别请求（否则「其他」一展开就一片 404）。
    // 只用正数入口号：0=投票说明、-1=未归类（新剧情），这两个没有游戏横幅。
    // 再按本机清单核一遍这张具体在不在 —— 整层"下过"不代表每个入口都导出了。
    const hasBnr = g.no > 0 && imgLayer().banners
      && ESC.media.has('story', 'etc_' + g.no) !== false;
    // 两行：第一行横幅（280×81 画框，跟主线/活动行同一套），第二行名字 + 话数 + 箭头
    head.innerHTML =
      (hasBnr ? `<span class="bnr"><img alt="" loading="lazy"`
        + ` src="${esc(imgSrc(`media/img/story/etc_${g.no}.webp`))}"`
        + ` onerror="this.closest('.bnr').style.display='none'"></span>` : '')
      + `<span class="row"><span class="nm">${esc(shortName)}</span>`
      + `<span class="cnt">${eps.length}</span>`
      + `<span class="arw">${open ? '▾' : '▸'}</span></span>`;
    head.onclick = () => {
      if (OTHER_OPEN.has(g.no)) OTHER_OPEN.delete(g.no); else OTHER_OPEN.add(g.no);
      renderOtherGroups(box, filter, { anchor: head.dataset.key });
    };
    box.appendChild(head);
    any = true;

    if (!open) return;
    eps.forEach(e => {
      const d = document.createElement('div');
      const on = (S.key === e.k) && (ST.part === e.i);
      const hv = epHasVoice(e);
      // .nov = 本话没有语音：挂徽标，并让 CSS 允许这一行的「句数 / 无语音」折行
      d.className = 'char-btn ep' + (on ? ' on' : '') + (hv ? '' : ' nov');
      d.dataset.key = e.k;
      d.dataset.part = e.i;
      // 「其他」的一话才是用户点的那一项，所以收藏/进度都按"包 + 段"算
      const fid = favIdStory(e.k, e.i);
      // 一行：话名左对齐（超长省略）＋ 句数 ＋ 无语音 ＋ 复制；塞不下时 CSS 让
      // 「句数 / 无语音」整体折到第二行（侧栏只有 300px，硬挤会把「その1」吃掉）
      d.innerHTML = `<span class="info"><span class="nm">${esc(e.t)}</span>`
        + `<span class="sub">${e.m ? `<span>#${e.i + 1}</span>` : ''}`
        + progHTML(e.k, e.i)
        + `<span>${e.n}句</span>`
        // 和资源管理页的安装列表同一个说法：官方没配语音轨的话就直接写「无语音」
        + (hv ? '' : `<span class="badge soft" title="这一话官方没有配音">`
          + L10N.t('adv.noVoice', '无语音') + `</span>`)
        + `</span>`
        + `<span class="cpy" title="复制">⧉</span>`
        + favHTML(fid) + `</span>`;
      bindFav(d, fid, refilterIfFav);
      d.onclick = () => openStory(e.k, { part: e.i });
      d.querySelector('.cpy').onclick = ev => {
        ev.stopPropagation();
        copyText(`${g.name} #${e.i + 1}`, '已复制');
      };
      bindTip(d, () => {
        const rec = UD && UD.prog(e.k);
        return `<div style="font-weight:600;margin-bottom:4px">${esc(g.name)}</div>`
          + `<dl><dt>段</dt><dd>#${e.i + 1}</dd><dt>句数</dt><dd>${e.n}</dd>`
          + `<dt>语音</dt><dd>${hv ? '有语音' : '无语音（官方没配）'}</dd>`
          + `<dt>首句</dt><dd>${esc(e.t)}</dd>`
          + `<dt>主要人物</dt><dd>${esc(e.c || '—')}</dd>`
          + ((rec && rec.p === e.i)
            ? `<dt>上次看到</dt><dd>第 ${UD.lineNo(rec)}/${rec.n} 句`
              + `（${UD.progPct(rec)}%，${UD.ago(rec.ts)}）</dd>` : '')
          + `</dl>`;
      });
      box.appendChild(d);
    });
  });

  // 教程 / 开场这类没有入口分组的，直接平铺在最后（以前靠 renderAdvList 平铺，
  // 改成分组视图后如果不补，它们就从列表里消失了）
  const extra = allAdv().filter(a => a.kind === 'tutorial' || a.kind === 'op')
    .filter(a => !favMode || (UD && UD.isFav('story', a.key)))
    .filter(a => !f || (a.title || storyTitle(a.key)).toLowerCase().includes(f));
  if (extra.length) {
    // 这一组也是可折叠的（以前写死 open 又没有 onclick，点了没反应）。
    // 用伪入口号 'tuto' 存展开态，跟其它分组一样默认收起。
    const openT = OTHER_OPEN.has('tuto') || !!f || favMode;
    const head = document.createElement('div');
    head.className = 'og-head' + (openT ? ' open' : '');
    head.dataset.key = 'tuto';
    head.innerHTML = `<span class="row"><span class="nm">${L10N.t('other.tuto', '教程 / 开场')}</span>`
      + `<span class="cnt">${extra.length}</span>`
      + `<span class="arw">${openT ? '▾' : '▸'}</span></span>`;
    head.onclick = () => {
      if (OTHER_OPEN.has('tuto')) OTHER_OPEN.delete('tuto'); else OTHER_OPEN.add('tuto');
      renderOtherGroups(box, filter, { anchor: 'tuto' });
    };
    box.appendChild(head);
    any = true;
    if (openT) extra.forEach(a => box.appendChild(advItem(a)));
  }

  /* 没被任何入口组认领的「其他」剧情：两种都要列，否则它们等于不存在 ——
       · 云端新上、还没装（CLOUD_ADV，只有清单里有）；
       · 装了、但 `othereps.js` 是构建期产物（tools/build_other_episodes.py），
         新包还没归类 → 装完反而会从分组视图里"消失"（2026-11-04 用户反馈）。
     名字只能按编号推算（这一类没有 Wiki 可查），点行会引导去资源管理下载。 */
  const covered = new Set();
  groups.forEach(g => (g.eps || []).forEach(e => covered.add(e.k)));
  const loose = allAdv().filter(a => a.kind === 'other' && !covered.has(a.key))
    .filter(a => !favMode || (UD && UD.isFav('story', a.key)))
    .filter(a => !f || (a.title || storyTitle(a.key)).toLowerCase().includes(f));
  if (loose.length) {
    const openC = OTHER_OPEN.has('cloud') || !!f || favMode;
    const head = document.createElement('div');
    head.className = 'og-head' + (openC ? ' open' : '');
    head.dataset.key = 'cloud';
    head.innerHTML = `<span class="row">`
      + `<span class="nm">${L10N.t('other.cloud', '未归类 / 新上线')}</span>`
      + `<span class="cnt">${loose.length}</span>`
      + `<span class="arw">${openC ? '▾' : '▸'}</span></span>`;
    head.onclick = () => {
      if (OTHER_OPEN.has('cloud')) OTHER_OPEN.delete('cloud'); else OTHER_OPEN.add('cloud');
      renderOtherGroups(box, filter, { anchor: 'cloud' });
    };
    box.appendChild(head);
    any = true;
    if (openC) loose.forEach(a => box.appendChild(advItem(a)));
  }

  /* 一条都没画出来：必须给一句话，否则用户看到的是"空白的黑列表"，
     会以为清单没加载（"只看已收藏"刚开始时必然是这个状态）。 */
  if (!any) {
    const d = document.createElement('div');
    d.className = 'advEmpty';
    d.textContent = favMode
      ? '「其他」里还没有收藏过的剧情：展开下面任意一组，点行尾的 ☆ 就能收藏。'
      : '没有匹配的剧情';
    box.appendChild(d);
  }

  endScroll(box, sc, anchorSel);

  const total = groups.reduce((a, g) => a + g.eps.length, 0) + extra.length;
  const hit = $('#hit');
  if (hit) {
    // 收藏筛选下也要有计数：否则"只显示 2 话"看起来像数据丢了
    const n = favMode
      ? groups.reduce((a, g) => a + g.eps.filter(e => UD && UD.isFav('story', favIdStory(e.k, e.i))).length, 0)
        + extra.length
      : total;
    hit.textContent = favMode ? `已收藏 ${n} 话`
      : (f ? `命中 ${n} 话` : '');
    hit.className = (favMode || f) ? 'on' : '';
  }
}

function renderAdvList(filter = '') {
  const box = $('#charList');
  if (!box) return;
  // 「其他」走入口分组视图（16 个入口 → 展开成话）
  if (SRC === 'other') {
    if (window.__OTHER_EPS && window.__OTHER_EPS.length) {
      renderOtherGroups(box, filter);
    } else if (!window.__OTHER_EPS_TRIED) {
      // 清单可能没随更新包下来（1.0.3 增量更新漏过它）。按需拉一次，
      // 缺失就静默降级到「暂不可用」，不再有 <script> 404 刷屏，也不会死循环重试。
      window.__OTHER_EPS_TRIED = true;
      box.innerHTML = '<div class="advEmpty">其他剧情清单加载中…</div>';
      // 缓存要关：这个文件会被**运行时改写**（装上一部新的「其他」剧情后，
      // 后端 stages.sync_othereps 会把它归进对应入口并补条目）。固定 ?v= 会让
      // 浏览器一直读旧副本，用户就会觉得"装完还是没归类"。
      fetch('data/othereps.js?v=1118a', { cache: 'no-store' })
        .then(r => r.ok ? r.text() : null)
        .then(t => { if (t) { try { (0, eval)(t); } catch (e) { } } })
        .catch(() => { })
        .finally(() => renderSidebar());
    } else {
      box.innerHTML = '<div class="advEmpty">其他剧情清单暂不可用</div>';
    }
    return;
  }
  const f = filter.trim().toLowerCase();
  const kinds = ADV_MENU[SRC] || [SRC];
  const favMode = favOnly();
  advAll = allAdv().filter(a => kinds.includes(a.kind))
    .filter(a => !favMode || (UD && UD.isFav('story', a.key)))
    .filter(a => !f || (a.title || storyTitle(a.key)).toLowerCase().includes(f)
      || String(a.first || '').toLowerCase().includes(f) || a.key.includes(f));
  /* 未下载资源的排到后面，和角色列表同口径 */
  const stOf2 = key => (window.PACKSTATE ? PACKSTATE.status(key) : 'unknown');
  advAll = advAll.slice().sort((a, b) =>
    (stOf2(a.key) === 'missing' ? 1 : 0) - (stOf2(b.key) === 'missing' ? 1 : 0));

  const hit = $('#hit');
  if (hit) {
    hit.textContent = favMode
      ? `已收藏 ${advAll.length} 部剧情`
      : (f ? L10N.t('search.hitAdv', '命中 {n} 部剧情').replace('{n}', advAll.length) : '');
    hit.className = (favMode || f) ? (advAll.length ? 'on' : 'none') : '';
  }

  // 过滤条件没变时保留滚动位置（否则切语言/刷新下载状态都会跳回顶部）
  const sc = beginScroll(box, 'adv|' + SRC + '|' + filter);
  box.innerHTML = '';
  advCursor = 0;
  if (!advAll.length) {
    const d = document.createElement('div');
    d.className = 'advEmpty';
    d.textContent = favMode ? '这一类还没有收藏过剧情（点列表行尾的 ☆ 就能收藏）'
      : (kinds.length && ADV.some(a => kinds.includes(a.kind))
        ? L10N.t('adv.nomatch', '没有匹配的剧情')
        : L10N.t('adv.none', '这一类剧情还没有接入，用 pipeline.cli adv-all 接入'));
    box.appendChild(d);
    return;
  }
  appendAdvChunk();
  endScroll(box, sc);
}

/** 追加下一批（滚动到底部时由 #charList 的 scroll 事件调用） */
function appendAdvChunk() {
  const box = $('#charList');
  if (!box || advCursor >= advAll.length) return;
  // 安全护栏：「其他」走分组视图（renderOtherGroups 一次渲染完，也不重置
  // advAll/advCursor），若这里被触发会把上一次活动/主线残留数据追加进去，
  // 造成两种列表并存。角色分类同样不增量。
  if (SRC === 'char' || SRC === 'other') return;
  const frag = document.createDocumentFragment();
  advAll.slice(advCursor, advCursor + ADV_BATCH).forEach(a => frag.appendChild(advItem(a)));
  advCursor = Math.min(advCursor + ADV_BATCH, advAll.length);
  box.appendChild(frag);
}

function advItem(a) {
  /* 三种情况要分开：有语音文件 / 剧本里有语音但这台机器没下音频包 / 官方就没配。
     以前只看 voiceRes，后两种一律写「无语音」，用户会以为剧本身就是哑的。 */
  const vnote = a.voiceRes ? `${a.voiceRes} 语音`
    : (a.voices ? '语音未下载' : '无语音');
  const st = window.PACKSTATE ? PACKSTATE.status(a.key) : 'unknown';
  // 同一部剧情的多个条目（如 adv_op 里的三部序章）共用 key，靠 part 区分：
  // 标题优先用条目自带的 title，起始段用 a.part。
  const title = a.title || storyTitle(a.key);
  const startPart = Number.isInteger(a.part) ? a.part : null;
  const d = document.createElement('div');
  d.className = 'char-btn adv bnr' + (a.key === S.key ? ' on' : '')
    + (st === 'missing' ? ' nodl' : '');
  d.dataset.key = a.key;                 // refreshSelection() 靠它判断选中
  d.dataset.id = a.key;
  if (startPart !== null) d.dataset.part = startPart;
  /* 章节横幅同样是解包产物（bundle_image_story_*），没下过就别请求；
     下过了也要再按本机清单核一遍这张具体在不在（新章节刚上时最容易缺）。 */
  const L = imgLayer();
  const cover = (L.banners || st === 'ready' || st === 'soft-missing')
    ? (storyImgOk(bannerOfAdv(a)) ? bannerOfAdv(a) : '') : '';
  /* 云端新上、还没接入目录的条目（a.cloud）只有两种状态：
     · missing      → 官方发了包、本地没装；
     · 装了但 0 段 0 句 → 官方先放了**空占位包**（正文还没上），
       这种条目 sync_advindex 会主动剔掉，所以只能从清单里看到。 */
  const emptyPack = !!(a.cloud && st !== 'missing' && !a.parts);
  d.title = `${title}\n${a.parts} 段 · ${a.lines} 句 · ${vnote}`
    + (a.first ? `\n${a.first}` : '')
    + (st === 'missing' ? '\n资源未下载：点「资源管理」安装后即可播放' : '')
    + (emptyPack ? '\n官方还没放出正文（空占位包）' : '');
  // 两行一项：横幅（没横幅就只留标题，别塞一张假图）
  d.innerHTML =
    (cover
      ? `<span class="bnr"><img alt="" loading="lazy" src="${esc(imgSrc(cover))}"`
        + ` onerror="this.parentNode.style.display='none'"></span>`
      : '') +
    `<span class="info"><span class="nm">`
    // 主线/活动的分类已经写在标题里（第N部 第X章 / 活动标题），不再放「主 / 活」角标
    + (a.kind === 'main' || a.kind === 'event' ? ''
      : `<span class="srcTag" data-k="${a.kind}">${ADV_TAG[a.kind] || '剧'}</span>`)
    + `${esc(title)}</span>` +
    `<span class="sub"><span>${a.parts} 段</span><span>${a.lines} 句</span>` +
    `<span>${vnote}</span>` +
    progHTML(a.key, null, a.parts) +
    (st === 'missing' ? '<span class="badge nodl">未下载</span>' : '') +
    (emptyPack ? '<span class="badge soft">空包</span>' : '') +
    `</span>` +
    `<span class="cpy" title="复制剧情名">⧉</span>` +
    favHTML(a.key) + `</span>`;
  bindTip(d, () => {
    const rec = UD && UD.prog(a.key);
    return (cover ? `<img src="${esc(imgSrc(cover))}" alt="" `
      + `onerror="this.style.display='none'">` : '')
      + `<div style="font-weight:600;margin-bottom:4px">${esc(title)}</div>`
      + `<dl><dt>剧情</dt><dd>${a.parts} 段 · ${a.lines} 句</dd>`
      + `<dt>语音</dt><dd>${vnote}</dd>`
      + (a.first ? `<dt>首句</dt><dd>${esc(a.first)}</dd>` : '')
      + `<dt>状态</dt><dd>${st === 'missing' ? '资源未下载' : st === 'soft-missing' ? '缺公共素材' : '可播放'}</dd>`
      + (rec ? `<dt>上次看到</dt><dd>第 ${rec.p + 1} 段 · 第 ${UD.lineNo(rec)}/${rec.n} 句`
        + `（${UD.progPct(rec)}%，${UD.ago(rec.ts)}）</dd>` : '')
      + `</dl>`;
  });
  // 主线/活动按"整部剧情"收藏（一部的所有段算一个）
  bindFav(d, a.key, refilterIfFav);
  d.onclick = () => openStory(a.key, startPart === null ? undefined : { part: startPart });
  d.querySelector('.cpy').onclick = ev => {
    ev.stopPropagation();
    copyText(title, `已复制：${title}`);
  };
  return d;
}

/* 侧栏滚到底部时继续追加（只对主线/活动增量列表生效；
   「其他」是分组视图一次渲染完，不增量，否则会误追加上一次活动残留） */
$('#charList').addEventListener('scroll', () => {
  const box = $('#charList');
  // 播放记录视图里 advAll 还是上一次主线/活动留下的，追加会把两类数据混在一起
  if (HIST_ON || SRC === 'char' || SRC === 'other') return;
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
    saveSrc();                     // 记住最后停在哪个分类（下次打开还是这里）
    setHistView(false);            // 点分类就是"离开播放记录、看这一类"
    document.querySelectorAll('#srcRow button').forEach(x =>
      x.classList.toggle('on', x === b));
    $('#search').value = '';
    updateCnt();
    renderSidebar();
  };
});

$('#sortBy').onchange = e => {
  SET.sortBy = e.target.value;
  applySettings();                 // 排序方式跟着设置一起存
  renderSidebar($('#search').value);
};
if ($('#dlFilter')) {
  $('#dlFilter').onchange = () => {
    const v = $('#dlFilter').value;
    if (window.UD && window.UD.syncPref) window.UD.syncPref('ui', 'dlf', v);
    try { localStorage.setItem('escah_dlf', v); } catch (e) { }
    renderSidebar($('#search').value);
  };
  try {
    const v = (window.UD && window.UD.prefValue ? window.UD.prefValue('ui', 'dlf', '')
      : localStorage.getItem('escah_dlf')) || '';
    // 旧版本存过的值只在选项还存在时恢复（避免选到已被删掉的值）
    if (v && $('#dlFilter').querySelector(`option[value="${v}"]`)) $('#dlFilter').value = v;
  } catch (e) { }
}
document.querySelectorAll('.rar-row button[data-r]').forEach(b => {
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

/** 「播放记录」视图开关（rar-row 第四格） */
const btnHist = document.querySelector('.rar-row button[data-f="hist"]');
function setHistView(on) {
  on = !!on;
  if (HIST_ON === on) return;
  HIST_ON = on;
  if (btnHist) btnHist.classList.toggle('on', on);
}
if (btnHist) {
  btnHist.onclick = () => {
    setHistView(!HIST_ON);
    // 换视图时清掉搜索词：否则"记录是空的"其实是被上次的搜索词滤没了
    $('#search').value = '';
    renderSidebar();
  };
}

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
    : ((advByKey(key) || {}).voiceRes || 0);
  ST.char = S.char;
  ST.key = key;
  sessionStorage.setItem('story', key);
  // 只切换高亮，不重建列表（重建会把 scrollTop 归零 → 点一下滚回顶部）
  refreshSelection();
  try { S.data = await loadStoryData(key); }
  catch (e) { alert(e.message); return; }
  // 先把这部剧情的译文拉下来（失败也只是没译文），再开工，避免第一句先闪日文
  await L10N.loadStory(key);
  $('#empty')?.remove();
  // ?part=1 表示第 1 段（界面口径），内部索引再减 1
  // opt.noPlay：只加载数据不开始播（脚本化加载时用，避免先白播一段）
  if (opt && opt.noPlay) return;
  /* 从哪一段开始，优先级：
   *   1) 侧栏点了具体某一话（opt.part，0 基）—— 用户点的是哪段就播哪段；
   *   2) URL 里的 ?part=（1 基，分享链接 / 测试钩子）；
   *   3) 上次看到的那一段（播放进度）—— 就是"接着看"；
   *   4) 第 1 段。 */
  const qp = +(new URLSearchParams(location.search).get('part') || 0);
  const rec = UD ? UD.prog(key) : null;
  // 上次这一段已经看完了 → 当"从头再看"，别停在最后一段的第一句
  const recDone = !!(rec && rec.n && rec.l >= rec.n - 1);
  const want = (opt && Number.isInteger(opt.part)) ? opt.part + 1
    : (qp || (rec && !recDone ? rec.p + 1 : 1));
  playPart(Math.min(Math.max(want - 1, 0), S.data.parts.length - 1));
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
  const strip = $('#tabStrip') || $('#tabs');
  // 多段时让左右箭头常驻：它们现在是「上一段 / 下一段」，不是标签条滚动条
  const tabsEl = $('#tabs');
  if (tabsEl) tabsEl.classList.toggle('multi', (S.data.parts || []).length > 1);
  strip.querySelectorAll('.tab').forEach(e => e.remove());
  S.data.parts.forEach((p, i) => {
    const d = document.createElement('div');
    d.className = 'tab' + (i === ST.part ? ' on' : '');
    d.textContent = L10N.t('meta.part', `第 ${i + 1} 段`, { n: i + 1 });
    d.onclick = () => playPart(i);
    strip.appendChild(d);
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
  fitTabs(true);
}

/** 段标签条自适应：装得下就一排放平；装不下就横向滚动 + 在最右边亮出 ◀ ▶ 翻页。
    **绝不换行把标题栏撑高**（用户 2026-11-02 明确要求）。
    scrollToActive=true 时把当前段滚进视野（切段/跳段后调用）。 */
function fitTabs(scrollToActive) {
  const t = $('#tabs'), s = $('#tabStrip');
  if (!t || !s) return;
  const over = s.scrollWidth > s.clientWidth + 2;
  t.classList.toggle('paged', over);
  const prev = $('#tabPrev'), next = $('#tabNext');
  // 箭头现在是「上一段 / 下一段」：可点状态由当前段位置决定，跟滚动位置无关
  const n = (S.data && S.data.parts) ? S.data.parts.length : 0;
  if (prev) prev.disabled = n < 2 || ST.part <= 0;
  if (next) next.disabled = n < 2 || ST.part >= n - 1;
  // 当前段可能被挤到可视区外（切段/翻页后），自动带进视野
  if (scrollToActive) {
    const on = s.querySelector('.tab.on');
    if (on) {
      const r = on.getBoundingClientRect(), sr = s.getBoundingClientRect();
      if (r.left < sr.left) s.scrollLeft -= (sr.left - r.left) + 8;
      else if (r.right > sr.right) s.scrollLeft += (r.right - sr.right) + 8;
    }
  }
}

/* 翻页按钮：只在 #tabs.paged（装不下）时由 CSS 显示 */
(() => {
  const s = $('#tabStrip');
  const prev = $('#tabPrev'), next = $('#tabNext');
  if (!s || !prev || !next) return;
  /* 原来是「把标签条横向滚一下」，画面完全不变，用户点了会以为按钮坏了
     （用户 2026-11-02 反馈）。改成真正的「上一段 / 下一段」：直接切段播放，
     标签条再自动滚到当前段。 */
  const step = d => {
    if (!S.data || !S.data.parts) return;
    const i = ST.part + d;
    if (i < 0 || i >= S.data.parts.length) return;
    playPart(i);
    fitTabs(true);        // playPart 内部已重建标签，这里把当前段滚进视野
  };
  prev.onclick = () => step(-1);
  next.onclick = () => step(1);
  s.addEventListener('scroll', () => fitTabs(), { passive: true });
  window.addEventListener('resize', () => fitTabs());
})();

/** 当前剧情的横幅图路径。
    主线/活动/其他的章节标题在游戏里本身就是图（topic_*，导出成 main_N/event_N），
    角色剧情没有文字横幅，用游戏「角色剧情」入口那张角色图（episode_character_<cid>，
    由 cdn/stages.export_char_banners() 落到 img/story/char_<cid>.webp）。 */
function bannerOf(key) {
  const p = isCharKey(key)
    ? 'media/img/story/char_' + String(key).slice(5) + '.webp'
    : ((advByKey(key) || {}).cover || '');
  return storyImgOk(p) ? p : '';
}

/** 标题区：横幅 + 剧名。只显示当前这一张，切换剧情时才换 src。 */
function updateBanner() {
  // 横幅不再放播放器标题区（用户 2026-11-01 明确）：标题横幅交给左侧列表，
  // 这里只保留一行纯文字标题，空间还给画面。
  const head = $('#storyHead');
  if (head) head.remove();
  return;
  /* eslint-disable no-unreachable */
  const t = $('#tabs');
  const src = bannerOf(S.key);
  if (!src) {
    if (head) head.hidden = true;
    return;
  }
  if (!head) {
    head = document.createElement('div');
    head.id = 'storyHead';
    head.innerHTML = '<img id="storyBanner" alt="" loading="lazy">' +
      '<span id="storyName"></span>';
    t.insertBefore(head, t.firstChild);      // 摆在左侧：横幅就是标题
    $('#storyBanner').onerror = () => { head.hidden = true; };
  }
  head.hidden = false;
  const b = $('#storyBanner');
  if (b.getAttribute('src') !== src) b.src = src;
  b.title = storyTitle(S.key);
  $('#storyName').textContent = storyTitle(S.key);
}

/** 总量改为各剧情分类按钮的悬停提示（不再写在标题区）。
 *  每个分类的计数是静态的，所以一次性给四个按钮都算好 */
function updateCnt() {
  // 角色数要算上 loadCloudChars() 并进来的「云端有、本地没装」的角色：
  // INDEX 是随程序分发的静态索引，不含新实装角色 —— 用 INDEX.length 会出现
  // 列表里已经有 char_416 这一行、悬停却还写着「384 个角色」（2026-11-04 用户反馈）。
  document.querySelectorAll('#srcRow button').forEach(b => {
    const src = b.dataset.src;
    if (src === 'char') {
      const n = allChars().length;
      b.title = L10N.t('cnt.char', `${n} 个角色`, { n: n });
    } else {
      const kinds = ADV_MENU[src] || [src];
      const n = allAdv().filter(a => kinds.includes(a.kind)).length;
      b.title = L10N.t('cnt.adv', `${n} 部剧情`, { n });
    }
  });
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
  updateSidebarToggleText();              // 左侧栏开合按钮文案同样跟随语言
  if (ST.curMsgIdx != null && ST.methods[ST.curMsgIdx]) {
    // 正在播：整句重放（连带语音）；暂停中：直接把画面上的文字换掉
    if (ST.running) seekTo(ST.curMsgIdx);
    else if ($('#msgText').classList.contains('partNotice')) {
      showPartNotice(partNoticeText());     // 停在段界上：提示本身按新语言重画
    } else $('#msgText').textContent = lineText(ST.methods[ST.curMsgIdx]);
  }
}

async function switchLang(explicit) {
  explicit ? L10N.setLang(explicit) : L10N.cycle();
  // 语言也算"设置"：记进偏好层，换设备也是同一种语言
  if (window.UD && window.UD.syncPref) window.UD.syncPref('ui', 'lang', L10N.lang());
  toast(L10N.t('toast.lang', '显示语言：' + L10N.lang(), { name: L10N.label() }));
  if (S.key) await L10N.loadStory(S.key);
  refreshForLang();
}
if ($('#btnLang')) $('#btnLang').onclick = () => switchLang();

/* ================= 局域网访问（二维码浮层） =================
 * 只有播放器页有 #btnLan（header.js 仅给 index.html 注入），其它页无此按钮。
 * 点按钮 → 拉 /api/lan_info 拿本机 IP:端口 → 拼出当前页的局域网 URL
 * → /api/lan_qr 生成二维码 → 浮层展示二维码与文字地址。点浮层外关闭。 */
if (document.getElementById('btnLan')) {
  document.getElementById('btnLan').onclick = (e) => {
    e.stopPropagation();          // 别让 document 的关闭监听立刻把它关掉
    toggleLanPanel();
  };
}
document.addEventListener('click', () => closeLanPanel());

let _lanPanel = null;
function closeLanPanel() {
  if (_lanPanel) { _lanPanel.style.display = 'none'; _lanPanel.dataset.open = '0'; }
}
function buildLanPanel() {
  const d = document.createElement('div');
  d.id = 'lanPanel';
  d.dataset.open = '0';
  d.innerHTML =
    '<div class="lan-title">局域网二维码</div>' +
    // 别写"用电脑扫码"：电脑上没有扫码这回事，另一台电脑是**输地址**。
    '<div class="lan-desc">播放器已在局域网内共享。手机连同一个 WiFi，' +
    '扫下面的二维码就能打开；另一台电脑把下面的地址输进浏览器就行。</div>' +
    '<img class="lan-qr" alt="局域网访问二维码">' +
    '<div class="lan-url"></div>' +
    '<div class="lan-hint">若手机扫不出：确认手机与电脑连同一 WiFi，' +
    '并放行 Windows 防火墙该端口的入站连接。</div>';
  d.addEventListener('click', (e) => e.stopPropagation());
  document.body.appendChild(d);
  return d;
}
function toggleLanPanel() {
  const btn = document.getElementById('btnLan');
  if (!btn) return;
  if (!_lanPanel) _lanPanel = buildLanPanel();
  const p = _lanPanel;
  if (p.dataset.open === '1') { closeLanPanel(); return; }
  p.style.display = 'block';
  p.dataset.open = '1';
  p.querySelector('.lan-url').textContent = '获取中…';
  p.querySelector('.lan-qr').removeAttribute('src');
  // 定位到按钮正下方，右边缘对齐按钮右边缘
  const r = btn.getBoundingClientRect();
  const w = p.offsetWidth || 260;
  let left = r.right - w;
  if (left < 8) left = 8;
  p.style.left = left + 'px';
  p.style.top = (r.bottom + 8) + 'px';
  fetch('/api/lan_info').then((res) => res.json()).then((info) => {
    const ip = info.ip || location.hostname;
    const port = info.port || location.port;
    const url = 'http://' + ip + ':' + port + location.pathname;
    p.querySelector('.lan-url').textContent = url;
    const qr = p.querySelector('.lan-qr');
    qr.style.display = '';
    qr.onerror = () => {
      // 服务端缺二维码库时别留一个裂图，直接把地址留着让用户手输
      qr.style.display = 'none';
      const u = p.querySelector('.lan-url');
      if (u) u.textContent = url + '（二维码生成失败，请手动输入上面的地址）';
    };
    qr.src = '/api/lan_qr?text=' + encodeURIComponent(url);
  }).catch(() => {
    p.querySelector('.lan-url').textContent =
      '获取局域网地址失败（请确认服务以 --host 0.0.0.0 启动）';
  });
}

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
// 重播 = 从这一段开头播（noResume：不能又跳回"上次看到的那一句"）
$('#btnRestart').onclick = () => { playPart(ST.part, { noResume: true }); };
$('#btnNext').onclick = () => {
  const n = ((ST.part || 0) + 1) % (S.data?.parts.length || 1);
  playPart(n, { noResume: true });
};
// 点击舞台 = 暂停/播放（与 P 键一致）
let _lastStageTap = 0;
$('#stage').onclick = () => {
  const wrap = document.getElementById('stageWrap');
  const inFs = !!(wrap && wrap.classList.contains('fs'));
  /* 触屏 + 全屏：单击只呼出控制条，双击才暂停/继续。
     否则每次想看一眼按钮都会顺手把播放暂停掉（用户 2026-11-02 反馈）。
     桌面仍保持单击暂停 —— 鼠标用户已经习惯那样。 */
  if (inFs && !CAN_HOVER) {
    const now = Date.now();
    if (now - _lastStageTap < 300) {      // 300ms 内第二次点击 = 双击
      _lastStageTap = 0;
      togglePlay();
      showFsChrome();
      return;
    }
    _lastStageTap = now;
    showFsChrome();                        // 单击：只把控制条叫出来
    return;
  }
  togglePlay();
};
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
/* 左侧栏收起/展开：与右侧文本列表同一套交互 */
function updateSidebarToggleText() {
  const st = $('#sToggle');
  if (!st) return;
  const opened = !$('#sidebar').classList.contains('hide');
  st.textContent = opened ? L10N.t('sidebar.collapse', '收起 ◂')
    : L10N.t('sidebar.expand', '展开 ▸');
}
$('#sToggle').onclick = () => {
  const s = $('#sidebar');
  s.classList.toggle('hide');
  const opened = !s.classList.contains('hide');
  document.body.classList.toggle('sideOpen', opened);
  $('#sTab').classList.toggle('show', !opened);
  updateSidebarToggleText();
  fitStage();
  localStorage.setItem('escah_sidebar', opened ? '1' : '0');
};
$('#sTab').onclick = () => $('#sToggle').click();
$('#speed').onchange = e => { ST.speed = parseFloat(e.target.value) || 1; };
$('#auto').onchange = e => { ST.auto = e.target.checked; };
document.addEventListener('keydown', e => {
  // Esc 关设置面板：关闭按钮的提示里写着「（Esc）」，但这里以前压根没有
  // Escape 分支 —— 提示是假的。放在输入框过滤**之前**，焦点在滑杆上也能关。
  if (e.code === 'Escape' || e.key === 'Escape') {
    const kt = $('#keyTip');
    if (kt) { e.preventDefault(); kt.remove(); return; }
    const st = $('#settings');
    if (st && !st.hidden) {
      e.preventDefault();
      st.hidden = true;
      return;
    }
  }

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
    case 'BracketLeft': $('#sToggle').click(); break;           // [ = 收起/展开左侧栏
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
/* ?msgbox=box|soft|bare 供自动化测试：启动即切到该文本框外观
   （与 ?seek / ?settings / ?langswitch 同一套约定，正常访问不带这个参数就完全没影响）
   注意只切 DOM class，**不碰 SET**：否则会被 escah_set 记住、还会经 UD 同步到
   userdata/prefs.json，把用户真机上的设置一起改掉（踩过一次）。
   调用点在 hydrate 之后 —— applySettings() 会按 SET.msgBox 重设 class，抢在前面会被清掉。 */
function applyTestMsgBox() {
  const mb = new URLSearchParams(location.search).get('msgbox');
  if (mb !== 'box' && mb !== 'soft' && mb !== 'bare') return;
  const w = $('#msgWindow');
  if (!w) return;
  w.classList.toggle('wb-soft', mb === 'soft');
  w.classList.toggle('wb-bare', mb === 'bare');
}
updateLangBtn();                      // 按钮先显示当前语言（不等 ui.json）
L10N.ready.then(() => {               // ui.json 到位后再刷一遍静态文案
  L10N.applyDom();
  updateLangBtn();
  updateSidebarToggleText();
  setPlayBtn(ST.running);
  if (S.data) renderTabs();
});
/* 恢复上次的分类（SRC 在声明处已经从 localStorage 读回来了）：
   按钮高亮是写死在 HTML 里的「角色」，这里同步一次，否则停在「活动」但按钮亮着「角色」 */
document.querySelectorAll('#srcRow button').forEach(x =>
  x.classList.toggle('on', x.dataset.src === SRC));
/* 语言也走偏好层：浏览器里的 escah_lang 被清掉（换浏览器 / 清缓存 / 新设备）时，
   用偏好层记的语言把界面切回来。L10N 自己只认 localStorage，这里补一刀。 */
(function applyPrefLang() {
  const lang = (window.UD && window.UD.prefValue) ? window.UD.prefValue('ui', 'lang', null) : null;
  if (!lang || !L10N || !L10N.setLang || L10N.lang() === lang) return;
  L10N.setLang(lang);
  updateLangBtn();
})();

renderSidebar();
/** 把偏好层里"别的设备改过的"值搬到界面：设置面板 + 分类 + 筛选 + 语言。
 *  只在启动 hydrate 拿到远端更新时调用一次；应用完立刻用合并后的值重建 diff 基准，
 *  不会把远端值当成"用户刚改的"再推回去。 */
function applyRemotePrefsToUI() {
  if (!UD) return;
  // 设置面板：只认 DEFAULTS 里有的键（防止旧版本残留的垃圾键混进来）
  let any = false;
  for (const k in DEFAULTS) {
    const v = UD.prefValue('set', k, undefined);
    if (v !== undefined && SET[k] !== v) { SET[k] = v; any = true; }
  }
  if (any) { syncSettingControls(); applySettings(); }
  // 列表分类 / 资源筛选
  const src = UD.prefValue('ui', 'src', null);
  if (src && SRC_ALL.indexOf(src) >= 0) SRC = src;
  document.querySelectorAll('#srcRow button').forEach(x =>
    x.classList.toggle('on', x.dataset.src === SRC));
  const dlf = UD.prefValue('ui', 'dlf', null);
  if (dlf != null && $('#dlFilter') && $('#dlFilter').querySelector(`option[value="${dlf}"]`)) {
    $('#dlFilter').value = dlf;
  }
  if (typeof updateCnt === 'function') updateCnt();
  renderSidebar(($('#search') || {}).value || '');
  // 语言：静默切换（不弹 toast —— 用户没点任何东西）
  const lang = UD.prefValue('ui', 'lang', null);
  if (lang && L10N && L10N.setLang && L10N.lang() !== lang) {
    L10N.setLang(lang);
    updateLangBtn();
    if (S.key) L10N.loadStory(S.key).then(() => refreshForLang());
  }
}

/* 收藏 / 播放进度 / 设置：浏览器 localStorage 是主，userdata/*.json 是备份。
   启动时拉一次合并进来（换浏览器、清过缓存、手机扫码进来都能看到自己的收藏与设置），
   有变化就重画一次列表、重新套一遍设置。打不开服务端时 UD 内部直接放弃。 */
if (UD && UD.hydrate) {
  UD.hydrate().then(r => {
    if (r && r.prefs) applyRemotePrefsToUI();
    if (r && r.marks) renderSidebar(($('#search') || {}).value || '');
    applyTestMsgBox();               // 必须排在 applyRemotePrefsToUI 之后（它会重套设置）
  });
} else {
  applyTestMsgBox();
}
// 恢复历史面板展开状态
(() => {
  const open = localStorage.getItem('escah_hist') !== '0';
  document.body.classList.toggle('histOpen', open);
  $('#history').classList.toggle('hide', !open);
  $('#hTab').classList.toggle('show', !open);
})();
// 恢复左侧栏展开状态（?collapse=side|hist|both 测试钩子可强制收起，与 ?autoplay 同套约定）
(() => {
  const qc = new URLSearchParams(location.search).get('collapse');
  const forceHide = qc === 'side' || qc === 'both';
  const open = !forceHide && localStorage.getItem('escah_sidebar') !== '0';
  document.body.classList.toggle('sideOpen', open);
  $('#sidebar').classList.toggle('hide', !open);
  $('#sTab').classList.toggle('show', !open);
  updateSidebarToggleText();
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
  replayPart() { return S.data ? playPart(ST.part, { noResume: true }) : Promise.resolve(); },
  /** 播指定段。注意它会一直跑到该段播完。
      自动化脚本用得到"从段首开始"，所以这里不食用播放进度（noResume）。 */
  playPart(i) { return S.data ? playPart(i, { noResume: true }) : Promise.resolve(); },
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
