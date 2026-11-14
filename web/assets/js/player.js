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
  talk: ['1', '1', '1'], // TalkCharacter 的 Left/Center/Right 点亮状态（'0' = 听众变暗）
  talkExplicit: false,   // 剧本显式写过 TalkCharacter（到 ClearTalkCharacter 为止都听它的）
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
  /* stale：台上这张 CG 是上一段/上一次跳转留下来的（本段没再下过 CG 指令）。
     换背景时要把这种遗留图收掉，否则它一直盖着新背景，见 dropCgForBg。 */
  cg: { key: null, el: null, hit: false, stale: false },
};

/* Unity ADV 的转场枚举 Motion。全站只用到 4 个 Wipe 方向 + None：
   ShowBackground 里 Wipe 占 23%（WipeLeft 5451+1662+310、WipeBottom/Top 各约 250），
   HideBackground 里 WipeRight 占 26%，其余 None = 交叉淡入。
   方向语义按「旧图被擦向哪一侧」定义 == 「新图的可见带从对侧扩张」：
     WipeLeft  可见带从右缘往左长（旧图从右往左被吃掉）
     WipeRight 从左缘往右 / WipeTop 从下往上 / WipeBottom 从上往下
   值就是该方向「初始/收尾」时的 clip-path（inset 顺序 = 上右下左）。 */
const WIPE = {
  WipeLeft: 'inset(0 0 0 100%)',
  WipeRight: 'inset(0 100% 0 0)',
  WipeTop: 'inset(100% 0 0 0)',
  WipeBottom: 'inset(0 0 100% 0)',
};
function clipSet(el, v) {
  el.style.clipPath = v;
  el.style.webkitClipPath = v;
}
/** 让新图按 Wipe 方向「擦」进来（起点已裁好，这里只负责跑到全开）。
 *  必须先强制一次布局：新 <img> 的初始 clip-path 是在同一次任务里设的，
 *  浏览器还没为它算过样式，直接设终值会被当成"没有起点"而瞬间跳到全开
 *  （实测就是这样：一帧就位，看起来和硬切没区别）。 */
function wipeIn(img, motion, ms) {
  void img.offsetWidth;                    // 强制 reflow，让起点生效
  img.style.transition = `clip-path ${ms}ms linear, -webkit-clip-path ${ms}ms linear`;
  clipSet(img, 'inset(0 0 0 0)');
}

/** 往某个图层画一张图（背景 / CG 共用）：同图复用、换图交叉淡入或 Wipe */
function setLayerImage(kind, sel, id, fade, motion) {
  const L = $(sel), st = VIS[kind];
  const key = id || '';
  const list = key ? candsOf(kind === 'bg' ? 'back' : 'cg', key) : [];
  const ms = Math.max(fade || 0, 0) * 1000;
  st.hit = true;
  if (!key || !list.length) {
    L.innerHTML = '';
    st.key = null; st.el = null;
    return Promise.resolve();
  }
  const instant = ST.fastForward;          // 跳转重放：不演淡入，直接就位
  const wipe = !instant && !!WIPE[motion];
  if (st.key === key && st.el && st.el.parentNode === L) {
    // 同一张图：不清层、不重建、不淡入（filter 由随后的颜色乘算指令重新施加）
    st.el.style.filter = '';
    /* 镜头状态也要归零：ZoomInEventCG / StareEventCG 是往这张 img 上写的内联 transform，
       再次 ShowEventCg 同一张图时会复用同一个 <img>，不清就会带着上一幕的放大/偏移，
       看起来像"画面被挤到角落且回不来"。 */
    st.el.style.transform = '';
    st.el.style.transformOrigin = '';
    clipSet(st.el, 'inset(0 0 0 0)');
    if (instant) L.querySelectorAll('img').forEach(o => { if (o !== st.el) o.remove(); });
    return instant ? Promise.resolve() : wait(fade * 1000);
  }
  const img = applyChain(new Image(), list);
  img.style.filter = '';
  // Wipe 模式下不提 opacity：否则会和擦除叠出「淡 + 擦」两层效果
  img.style.opacity = (instant || wipe) ? 1 : 0;
  img.style.transition = instant ? 'none' : `opacity ${fade}s`;
  if (wipe) clipSet(img, WIPE[motion]);
  const olds = Array.from(L.children);
  L.appendChild(img);
  st.key = key; st.el = img;
  let done = false;
  const dropOld = () => olds.forEach(o => { if (o !== img) o.remove(); });
  const reveal = () => {
    if (done) return; done = true;
    if (instant) { dropOld(); return; }
    if (wipe) wipeIn(img, motion, ms);
    else requestAnimationFrame(() => { img.style.opacity = 1; });
    // 等新图真的能画出来再撤旧图：中间不能有「两张都没有」的瞬间
    setTimeout(dropOld, ms + 80);
  };
  if (img.complete && img.naturalWidth) reveal();
  else {
    img.addEventListener('load', reveal, { once: true });
    // 候选链走完仍加载不出来才算失败，避免中途误撤旧图
    img.addEventListener('error', () => { if (!(img.dataset.chain || '').length) reveal(); }, { once: true });
    setTimeout(reveal, 2500);              // 兜底：别让旧图永远压在台上
  }
  return instant ? Promise.resolve() : wait(ms);
}

/* 版本戳只留在 window 上：控制台敲 PLAYER_BUILD 可查当前跑的是哪份代码。
   不再往页面标题里追加排查标记 —— 标题是给用户看的，不是调试输出。 */
window.PLAYER_BUILD = '2026-11-11d';

/* 粒子层渲染路径：**默认走 canvas**。
   DOM 版一层邪気/冷気 要建 480 个 500~1000px 的 `.fxp` 节点 + 480 张染色贴图
   （实测 #stage 子节点 500、创建时 67~175ms 长任务），真实 GPU 上合成/图块预算
   打爆后退化成「整层画黑」：用户看到的就是目标句那一帧闪屏，以及页面上其它
   控件跟着一起变黑（2026-11-11 录屏 char_415 第3段第36句）。
   canvas 版把每发射器画进一块 1280×720 画布（同帧只有 6 个节点、创建 2~3ms），
   混合模式仍是组级 plus-lighter，画面参数与 DOM 版逐字对应。
   `?fxdom=1` 可强制回退到 DOM 版做对照。 */
const FX_CANVAS = !/[?&]fxdom=1\b/.test(location.search);

/* ================= 跨段/跳转留下的旧 CG 必须让位给新背景 =================
 * CG 和背景是两层，CG 盖在背景上面（player.css：`#cgLayer img` 是
 * absolute + inset:0 铺满舞台、还带不透明底色 #05060a），所以台上只要还挂着
 * 一张 CG，底下背景怎么换都看不见。剧本自己的规矩是「换场景先 HideEventCg，
 * 再 ShowBackground」—— 全量 611 部剧情 / 3881 段里，"CG 亮着就改背景"只有
 * 1 例（char_317 第6段 @870），"HideEventCg 之后同段内不再 ShowBackground"
 * 是 0 例。
 *
 * 出问题的是**跨段 / 跳转 / 重播**：playPart 和 seekTo 都用
 * resetStage({keepVisual:true}) 刻意保留画面（段与段之间大多是同一场景接着演），
 * 但上一段留下的 CG 不会自己退场；而「上一段结束时 CG 还亮着、下一段第 0/1 条
 * 就是 ShowBackground」的边界有 153 处。那些地方新背景确实换上台了，却整个被
 * 旧 CG 盖住 —— 文字在走、画面不动，切句、重播都一样。
 * 2026-11-10 用户反馈的就是它：播放记录跳到「メイドハータン 第5段第47句」
 * （该段第 272 条是 ShowEventCg 全屏 CG），再点第6段 —— 第6段第 1 条
 * ShowBackground 被那张 CG 一直压着，怎么切都不变。
 *
 * 判据是 VIS.cg.stale（"这张 CG 是上一段/上一次跳转遗留的"，由 resetStage
 * 置位、由任何一次 setCg 清除）：只收遗留图，**不碰本段自己挂上的 CG**，
 * 所以段内的 CG 演出（连续切图、推近/凝视镜头）完全不受影响。 */
function dropCgForBg() {
  const L = $('#cgLayer');
  if (!L) return;
  if (!VIS.cg.stale) return;                    // 本段自己的 CG：不归这里管
  if (!VIS.cg.el && !L.childElementCount) { VIS.cg.stale = false; return; }
  L.innerHTML = '';
  VIS.cg.key = null;
  VIS.cg.el = null;
  VIS.cg.stale = false;
}

/** 显示背景：Time 是转场时长（全站默认 0.75s，Time=0 表示硬切） */
function setBackground(id, m) {
  /* 放在"同一张背景走早退分支"之前：跨段时下一段开场的背景常常和上一段
     是同一张，走早退的话那张遗留 CG 就清不掉了。 */
  dropCgForBg();
  const fade = m ? num(m.Time, 0.75) : 0.45;
  return setLayerImage('bg', '#bgLayer', id, fade, m && m.Motion);
}

/** 隐藏背景：以前直接 innerHTML='' 瞬间消失，而数据里 HideBackground 的 Time
 *  93% 是 0.75/0.5/0.3 秒、26% 带 WipeRight —— 那是一个完整转场，不是硬切。 */
function hideBackground(m) {
  const L = $('#bgLayer'), st = VIS.bg;
  st.hit = true;
  const imgs = Array.from(L.children);
  st.key = null; st.el = null;
  if (!imgs.length) return Promise.resolve();
  const ms = Math.max(num(m && m.Time, 0.75), 0) * 1000;
  const wipe = WIPE[m && m.Motion];
  if (ST.fastForward || ms === 0) {
    L.innerHTML = '';
    return Promise.resolve();
  }
  imgs.forEach(i => {
    if (wipe) {
      i.style.transition = `clip-path ${ms}ms linear, -webkit-clip-path ${ms}ms linear`;
      clipSet(i, wipe);
    } else {
      i.style.transition = `opacity ${ms}ms`;
      i.style.opacity = 0;
    }
  });
  /* 只删「这次要隐藏的那几张」，不要整层 innerHTML = ''：
     按住 K 快进时 Hide 之后立刻就有新的 ShowBackground，810ms 后这条定时器
     会把刚换上的**新背景**一起清掉 —— 表现就是快进到某些节点背景突然没了，
     回上一句再下一句又正常（2026-11-09 用户反馈的正是这个）。 */
  setTimeout(() => {
    imgs.forEach(i => { if (i.parentNode === L) i.remove(); });
  }, ms + 60);
  return wait(ms);
}

function setDoubleBackground(l, r) {
  dropCgForBg();                    // 同上：换背景就代表 CG 场景结束
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
  /* 任何一条 CG 指令（含 HideEventCg 的清空）都算"这张 CG 归本段管"：
     取消 resetStage 打上的 stale 标记，setBackground 就不会来收它
     （见 dropCgForBg）。 */
  VIS.cg.stale = false;
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
/* ShowStand / HideStand 的 Motion：Right/Left/Up/Down 是「从该方向滑进来/滑出去」，
   全站 ShowStand 里占 7.8%（5529+3067+1129…），HideStand 里占 5.4%。
   以前只按 ±70px 平移且方向是反的（Left 从右边进），还把时长写死 0.45s；
   这里按半身位滑入 + 用剧本给的 Time。 */
const SLIDE = { Right: [230, 0], Left: [-230, 0], Up: [0, -170], Down: [0, 170] };

/* ---- 官方画布落点（与游戏一致） ----------------------------------------
 * 剧本的 ShowStand 只给 StandImage + Position，没有坐标也没有缩放；
 * 游戏里的尺寸/位置来自资源包 base_info 的 AlphaCutRect（该立绘在
 * 1280x720 画布上的落点与尺寸），本机用 server/cdn/scripts/gen_stand_box.py
 * 导成 data/stand_box.js：  { "角色id_服装hex": [L, T, W, H] }
 * 站位只是相对官方落点再平移一个锚点（1280/4 = 320，实测 Left -320 / Right +320）。
 * 没有这张表的立绘（极少数缺 base_info 的老组）回退「贴底居中 + 按人数缩小」。 */
const STANDBOX = window.__STANDBOX || {};
const POS_DX = { Left: -320, Center: 0, Right: 320, All: 0 };
const STAND_ID = /^stand_(\d+)_([0-9A-Fa-f]+)_[0-9A-Fa-f]+$/;
function standBox(id) {
  const m = STAND_ID.exec(id || '');
  return m ? STANDBOX[m[1] + '_' + m[2]] || null : null;
}
/** 按站位把立绘摆到画布上；返回是否用上了官方落点 */
function placeStand(el, pos) {
  const box = standBox(el.dataset.stId || '');
  const dx = POS_DX[pos] ?? 0;
  el.dataset.pos = pos;
  if (box) {
    el.classList.add('boxed');
    el.style.setProperty('--tx', '0px');
    el.style.left = `calc(${box[0] + dx}px + var(--standOX, 0px))`;
    el.style.top = `calc(${box[1]}px - var(--standY, 0px))`;
  } else {
    el.classList.remove('boxed');
    el.style.setProperty('--tx', '-50%');
    el.style.left = `calc(${POS_X[pos] || '50%'} + var(--standOX, 0px))`;
    el.style.top = '';
  }
  return !!box;
}
/** 图片本体宽度：官方落点模式下等于画布上的 AlphaCutRect 宽度（高按比例） */
function sizeStandImg(el) {
  const img = el.querySelector('img');
  if (!img) return;
  const box = standBox(el.dataset.stId || '');
  img.style.width = box ? box[2] + 'px' : '';
}
/** 立绘缩放：单人居中满高，同框越多缩得越小（按实际在场数量动态计算）
 *  有官方落点的立绘不参与——游戏里同一位置永远是这个尺寸，
 *  再按人数缩小就比原作小一圈（立绘缩水、贴底、伞的位置全乱）。 */
function updateStandScale() {
  const n = ST.stands.size;
  const sc = n <= 1 ? 1 : (n === 2 ? 0.86 : 0.76);
  /* 官方落点模式就是游戏里的尺寸（这条立绘在画布上占多少像素就是多少），
     不缩放也不做任何「按人数缩」的修正——多变量只会比原作更不像。
     设置页的「立绘尺寸」已删，这里固定 100%。 */
  ST.stands.forEach(el => {
    const im = el.querySelector('img');
    if (!im) return;
    im.style.setProperty('--sc', el.classList.contains('boxed') ? 1 : sc);
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
  /* 不跨位置清理同一 GroupId：游戏里三个站位是互相独立的对象，
     同一个 GroupId 可以同时出现在左右（例如「滅忍」「フーマン」「一般女性」这类
     龙套/分身，char_227 / 309 / 242 的第 1 句都是 Left+Right 同 GroupId 各一个，
     TalkCharacter 的 Stand1/2/3 就是分别控制它们明暗的）。
     之前按 GroupId 合并，会把先出场的那个直接删掉，画面只剩一个人。 */
  if (!el) {
    el = document.createElement('div');
    el.className = 'stand';
    el.innerHTML = '<img alt="">';
    $('#standLayer').appendChild(el);
    ST.stands.set(key, el);
  }
  el.dataset.gid = m.GroupId || '';
  el.dataset.stId = id;
  // 剧本里 TalkCharacter 常写在 ShowStand 之前（先点亮再出场），新立绘要继承当前状态
  applyTalk();
  const img = el.querySelector('img');
  if (img.getAttribute('src') !== src) applyChain(img, srcs);
  sizeStandImg(el);
  placeStand(el, key);
  updateStandScale();
  el.style.zIndex = Z_VAL[m.Z] || 3;
  el.style.opacity = 0;
  const sl = SLIDE[m.Motion] || [0, 0];
  const dx = sl[0], dy = sl[1];
  const t = Math.max(num(m.Time, 0.25), 0.05) * 1000;
  const ts = (t / 1000).toFixed(2);
  // MotionStand「移動」留下的位移（像素），入场/重建时要带上，否则角色会弹回原位
  const offx = num(el.dataset.offx, 0);
  const offy = num(el.dataset.offy, 0);
  /* 同步给 CSS 变量：动作动画（m-shake 等）的关键帧整体覆盖内联 transform，
     不带 --offx/--offy 就会在动画期间弹回原落点（见 player.css 立绘动作动画注释）。 */
  el.style.setProperty('--offx', `${offx}px`);
  el.style.setProperty('--offy', `${offy}px`);
  if (instant) {
    // 跳转重建状态：直接落到最终位置，不走动画
    el.style.transition = 'none';
    el.style.opacity = 1;
    el.style.transform = `translateX(var(--tx,-50%)) translate(${offx}px,${offy}px)`;
    return Promise.resolve();
  }
  el.style.transition = `left ${ts}s ease, top ${ts}s ease, opacity ${Math.min(t / 1000, 0.3).toFixed(2)}s ease, transform ${ts}s ease`;
  el.style.transform = `translateX(var(--tx,-50%)) translate(${dx + offx}px, ${offy + dy}px)`;
  /* 新建的 <div> 在同一次任务里设了起点又（下一帧）设终点，浏览器可能压根没算过
     起点样式 → 过渡被跳过，表现是"立绘突然出现、没有滑入"。强制一次布局定住起点。 */
  void el.offsetWidth;
  requestAnimationFrame(() => {
    el.style.opacity = 1;
    el.style.transform = `translateX(var(--tx,-50%)) translate(${offx}px,${offy}px)`;
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
    // 退场方向同入场：Left/Right/Up/Down 就往那个方向滑走（Down 是"沉下去"）
    // 别忘了带上 MotionStand「移動」的 offx/offy：漏掉的话退场瞬间会跳一下
    const sl = SLIDE[m.Motion] || [0, 0];
    const offx = num(el.dataset.offx, 0);
    const offy = num(el.dataset.offy, 0);
    el.style.transform = `translateX(var(--tx,-50%)) translate(${sl[0] + offx}px, ` +
      `${offy + (sl[1] || (m.Motion === 'Down' ? 60 : 0))}px)`;
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

/* MotionStand 的动作语义（按数据实测，24358 条）：
 *   ポップ 15943 / 移動 3193 / 横揺れ 1199 / おじぎ 1068 / 微動 986 / 左突き 254 / 右突き 215
 *   / 座る 122 / 立つ 78 / 振動 51
 * 「移動」根本不是抖动，而是**平移**：Option1 = 横向 px、Option2 = 纵向 px、
 * Option3 = 秒数（3191 条里 2998 条带 Option1、342 条带 Option2，还有只上下动的）。
 * 以前只吃 Option1，纵向位移整个丢掉；再早则一律按"上下浮动"的 m-move 播，
 * 等于把所有"走过去/后退一步"都演成了原地跳。 */
function motionStand(m) {
  const el = currentStand(m);
  if (!el) return Promise.resolve();
  const name = m.Motion || '';
  if (name === '移動') {
    /* Option1 = 横向、**Option2 = 纵向**（px），Option3 = 秒。
       以前只吃了 Option1：3191 条「移動」里有 342 条带非 0 Option2
       （-50 / -100 / -150 / -200 / -500，还有 Option1=0 只上下移动的），
       漏掉后"后退半步 / 抬起来"这类位移只在横向上生效，立绘落点整体偏 ——
       头顶气泡的锚点也就跟着错（2026-11-10 用户问「气泡位置是不是漏了参数」）。 */
    const dx = num(m.Option1, 0);
    const dy = num(m.Option2, 0);
    const t = Math.max(num(m.Option3, 0.3) || 0.3, 0.1);
    const base = num(el.dataset.offx, 0) + dx;
    const baseY = num(el.dataset.offy, 0) + dy;
    el.dataset.offx = String(base);
    el.dataset.offy = String(baseY);
    el.style.setProperty('--offx', `${base}px`);   // 动画关键帧要用（见 player.css）
    el.style.setProperty('--offy', `${baseY}px`);
    el.style.transition = `transform ${t}s ease`;
    el.style.transform = `translateX(var(--tx,-50%)) translate(${base}px, ${baseY}px)`;
    return wait(t * 1000);
  }
  if (name === '座る' || name === '立つ') {
    // 坐下/起身：整体下沉或抬回（不是抖动）；要叠在「移動」留下的位移上
    el.style.transition = 'transform .3s ease';
    el.style.transform = `translateX(var(--tx,-50%)) ` +
      `translate(${num(el.dataset.offx, 0)}px, ` +
      `${num(el.dataset.offy, 0) + (name === '座る' ? 26 : 0)}px)`;
    return wait(300);
  }
  const map = {
    'ポップ': 'm-pop', '横揺れ': 'm-sway',
    'おじぎ': 'm-bow', '微動': 'm-tremble', '振動': 'm-tremble',
    '右突き': 'm-thrust', '左突き': 'm-thrust-l', '左ポップ': 'm-pop',
  };
  const cls = map[name] || 'm-tremble';
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
  el.style.transition = `left ${t}s ease, top ${t}s ease`;
  placeStand(el, to);
  updateStandScale();
  ST.stands.delete(from);
  ST.stands.set(to, el);
  return wait(t * 1000);
}

/* ShowStandAndSit（498 条）：出场的**同时坐下**，和普通 ShowStand 的区别就在这个姿态。
   数据里 Motion 是 Down/None 两种，都表示"坐下"，所以统一下沉一截（26px，
   约为立绘高度的 4%，和游戏里坐姿相对站姿的落差相当）。 */
function showStandAndSit(m) {
  const p = showStand(m);
  const el = ST.stands.get(m.Position || 'Center');
  if (el) el.classList.add('sit');
  return p;
}

function shakeStand(m) {
  const el = currentStand(m);
  if (!el) return;
  /* 参数要吃干净：9148 条 ShakeStand 几乎都带 ShakePixel（多为 20）和 Time（多为 1s），
     以前写死 7px / 0.4s，等于每次抖动都演成同一个弱动作。 */
  const px = Math.min(30, Math.max(2, num(m.ShakePixel, 7)));
  const dur = Math.max(num(m.Time, 0.4), 0.12);
  el.style.setProperty('--sk', `${px}px`);
  el.style.animationDuration = `${dur}s`;
  el.classList.remove('m-shake'); void el.offsetWidth;
  el.classList.add('m-shake');
  setTimeout(() => {
    el.classList.remove('m-shake');
    el.style.animationDuration = '';
  }, dur * 1000 + 40);
}

/* ===== R/G/B 精确乘算（MulColorBackground / MulStand）=====
 * 这两条指令的参数是 0~255 的**逐通道乘法系数**（255 = 原色，0 = 全黑），
 * 例如 {r:150,g:120,b:90} 表示把画面染成暖色暗调。
 * 以前用 brightness + hue-rotate + saturate 近似 —— 那是三个全局量，
 * 表达不了"三个通道各自乘不同系数"（CSS filter 本身没有这个能力），
 * 带色偏的场景会明显偏色。这里改用 SVG feColorMatrix：
 * 对角矩阵 r/255, g/255, b/255，就是精确的逐通道乘法。
 */
let mulSeq = 0;
function rgbMulFilter(r, g, b) {
  const host = document.getElementById('mulFilters');
  if (!host) return '';
  const id = 'fmul' + (++mulSeq);
  const NS = 'http://www.w3.org/2000/svg';
  const f = document.createElementNS(NS, 'filter');
  f.setAttribute('id', id);
  // sRGB：不加这句浏览器按 linearRGB 算，观感比原图明显偏亮
  f.setAttribute('color-interpolation-filters', 'sRGB');
  const mtx = document.createElementNS(NS, 'feColorMatrix');
  mtx.setAttribute('type', 'matrix');
  mtx.setAttribute('values',
    `${r / 255} 0 0 0 0  0 ${g / 255} 0 0 0  0 0 ${b / 255} 0 0  0 0 0 1 0`);
  f.appendChild(mtx);
  host.appendChild(f);
  return id;
}
/** 把元素上一次挂的滤镜节点回收掉（同一元素反复调色时不至于越积越多） */
function dropMulFilter(el) {
  const old = el.dataset.mulId;
  if (!old) return;
  const host = document.getElementById('mulFilters');
  const node = host && host.querySelector('#' + old);
  if (node) node.remove();
  el.dataset.mulId = '';
}
/** 按颜色缓存 feColorMatrix 节点：表情符号一共就十几种 LabelColor，
 *  每次播放都新建会白白堆积，这里复用（这些节点不回收）。 */
const MUL_CACHE = {};
function mulFilterCached(r, g, b) {
  const key = `${r},${g},${b}`;
  if (MUL_CACHE[key]) return MUL_CACHE[key];
  const id = rgbMulFilter(r, g, b);
  if (id) MUL_CACHE[key] = id;
  return id;
}
/** "#rrggbb" → 套上乘算滤镜（CPE 的 LabelColor 就是顶点色乘法） */
function tintByHex(el, hex) {
  if (!hex || hex[0] !== '#' || hex.length < 7) return;
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  if (!isFinite(r) || !isFinite(g) || !isFinite(b)) return;
  const id = mulFilterCached(r, g, b);
  if (id) el.style.filter = `url(#${id})`;
}

/** 给元素套上 RGB 乘算；extra 是额外要串联的 filter（比如立绘的投影）
 *
 *  ⚠ `Time` 必须真的淡过去（2026-11-11 修）：CSS **不会**在两个 `filter:url(#f)`
 *  之间插值（url() 滤镜不可插值），所以以前 `el.style.transition='filter 1s'` 是
 *  无效代码 —— 剧本里 `MulColorBackground Time=1` 实际是**一帧跳到目标色**。
 *  录屏实测（ハロウィンミヤビ 第3段第36句 附近）：某帧 G −18.5 / B +7.2 一步到位，
 *  就是这条色彩乘算"啪"地压上来，用户看到的就是「一帧之内画面变了」的闪。
 *  现在自己按帧插值 feColorMatrix 的 values：从上一个状态线性过渡到目标值。 */
const MUL_ANIM = new WeakMap();
function applyMulColor(el, r, g, b, ms, extra) {
  const clamp = v => Math.min(255, Math.max(0, v));
  const rr = clamp(r), gg = clamp(g), bb = clamp(b);
  const tail = extra ? ' ' + extra : '';
  const prev = MUL_ANIM.get(el) || { r: 255, g: 255, b: 255, raf: 0 };
  if (prev.raf) cancelAnimationFrame(prev.raf);
  dropMulFilter(el);

  const setMtx = (id, cr, cg, cb) => {
    const node = document.getElementById(id);
    const mtx = node && node.firstChild;
    if (mtx) {
      mtx.setAttribute('values',
        `${cr / 255} 0 0 0 0  0 ${cg / 255} 0 0 0  0 0 ${cb / 255} 0 0  0 0 0 1 0`);
    }
  };
  // 全白 = 不调色（数据里 255,255,255 是"恢复"）
  const toWhite = rr > 250 && gg > 250 && bb > 250;
  /* 注意单位：剧本 Time 是**秒**（旧代码写的是 `transition: filter ${ms}s`），
     下面按帧插值要毫秒，别直接拿秒当毫秒用（那样第一帧就到终点，等于没过渡）。 */
  const dur = Math.max(0, ms) * 1000;

  if (!(dur > 0)) {
    MUL_ANIM.set(el, { r: rr, g: gg, b: bb, raf: 0 });
    el.style.transition = 'none';
    if (toWhite) { el.style.filter = extra || ''; return; }
    if (rr === 0 && gg === 0 && bb === 0) {
      el.style.filter = 'brightness(0)' + tail;
      return;
    }
    const id = rgbMulFilter(rr, gg, bb);
    if (!id) return;
    el.dataset.mulId = id;
    el.style.filter = `url(#${id})` + tail;
    return;
  }

  // 需要过渡：始终走矩阵（这样起止两端都是同一个滤镜，能逐帧改 values）
  const id = rgbMulFilter(rr, gg, bb);
  if (!id) return;
  el.dataset.mulId = id;
  el.style.transition = 'none';
  el.style.filter = `url(#${id})` + tail;
  const from = { r: prev.r, g: prev.g, b: prev.b };
  const t0 = performance.now();
  const step = () => {
    const k = Math.min(1, (performance.now() - t0) / dur);
    const cr = from.r + (rr - from.r) * k;
    const cg = from.g + (gg - from.g) * k;
    const cb = from.b + (bb - from.b) * k;
    setMtx(id, cr, cg, cb);
    MUL_ANIM.set(el, { r: cr, g: cg, b: cb, raf: k < 1 ? requestAnimationFrame(step) : 0 });
    if (k >= 1 && toWhite) {
      // 收到全白：过渡结束后彻底摘掉滤镜，避免留一层 1:1 的矩阵
      el.style.filter = extra || '';
    }
  };
  step();
}

function mulStand(m) {
  const el = currentStand(m);
  if (!el) return;
  const img = el.querySelector('img');
  if (!img) return;
  const shadow = 'drop-shadow(0 6px 26px rgba(0,0,0,.75))';
  applyMulColor(img, num(m.r, 255), num(m.g, 255), num(m.b, 255),
    num(m.Time, 0), shadow);
}

/* ================= 说话人高亮（TalkCharacter） =================
 * 剧本指令 {"Stand1":"1","Stand2":"0","Stand3":"1","Method":"TalkCharacter"}
 * 三个 0/1 依次对应 Left / Center / Right：1 = 正在说话（原色），0 = 听众（压暗）。
 * 压暗系数 0.5 是从游戏截图实测的：同一角色、同一背景位置在「说话 / 不说话」
 * 两帧里的像素 RGB 等比缩到 0.52，色相饱和度不变（不是灰度、也不是降透明度），
 * 所以用 brightness 乘法，落在容器上以避开 MulStand 写在 img 上的 filter。
 * ClearTalkCharacter = 三个位置全部恢复原色。 */
const TALK_POS = ['Left', 'Center', 'Right'];
function applyTalk() {
  const f = ST.talk || ['1', '1', '1'];
  ST.stands.forEach((el, pos) => {
    const i = TALK_POS.indexOf(pos);
    const lit = i < 0 ? true : String(f[i]) !== '0';
    el.classList.toggle('dim', !lit);
  });
}
function talkCharacter(m) {
  // 剧本显式指定了谁在说话：一直到 ClearTalkCharacter 为止都按它来，别再 autoTalk
  ST.talkExplicit = true;
  ST.talk = [m.Stand1, m.Stand2, m.Stand3].map(v => String(v ?? '1'));
  applyTalk();
}

/* 自动高亮：**大多数剧本根本不写 TalkCharacter**（全站只有 4518 条，而台词有 56 万条），
 * 客户端是靠「说话人名字 ↔ 立绘 GroupId」自己配的。比如 char_204 第 1 段第 47 句：
 *   360 {"Name":"マドカ","Method":"ShowMessageWindow"}   ← 没有任何 TalkCharacter
 * 以前只认 TalkCharacter，所以这种情况一个立绘都不暗（用户 2026-11-09 反馈）。
 * 匹配不上时（"一同"、"？？？"、旁白）**保持原样**，不要把人全暗掉。 */
let NAME_SET = null;
/** 这个名字是不是"角色名"（用于区分「マドカ在画外说话」和「一同说话」）。
 *  names.js 是 384 个角色 id → 名，一次建表后查表。 */
function isCharName(nm) {
  if (!NAME_SET) {
    NAME_SET = [];
    const N = window.__NAMES || {};
    for (const k in N) {
      const v = String(N[k] || '');
      if (v) NAME_SET.push(v);
    }
  }
  return NAME_SET.some(v => v.includes(nm) || nm.includes(v));
}

/* 最长公共子串长度：台词里的名字经常是**简称**，而立绘 GroupId 是全名 ——
 *   台词「エスカリバース」 vs GroupId「ブライドエスカレイヤー・リバース非武装」
 *   两边互相 includes 都不成立，结果被当成"说话人不在场"整片压暗
 *   （char_387 第 1 段第 6 句就是这样，2026-11-09 用户反馈）。
 * 公共子串 ≥3 就当同一角色：这里命中「リバース」。
 * 不相关名字（トキサダ vs コマンダー）公共子串为 0，不会误判。 */
function commonSub(a, b) {
  if (!a || !b) return 0;
  let best = 0;
  let prev = new Array(b.length + 1).fill(0);
  for (let i = 1; i <= a.length; i++) {
    const cur = new Array(b.length + 1).fill(0);
    for (let j = 1; j <= b.length; j++) {
      if (a[i - 1] === b[j - 1]) {
        cur[j] = prev[j - 1] + 1;
        if (cur[j] > best) best = cur[j];
      }
    }
    prev = cur;
  }
  return best;
}

function autoTalk(name) {
  const nm = String(name || '').trim();
  if (!nm || !ST.stands.size) return;
  /* 剧本显式写了 TalkCharacter（Stand1/2/3）就照它来 —— 猜得再宽也不如
     原作自己指定得准。ClearTalkCharacter 是这段显式区间的结束标记。 */
  if (ST.talkExplicit) return;
  /* 多个候选时**只让最像的那一个亮**。以前是"公共子串 ≥3 就算命中"的并集，
     一个名字很容易跟两个 GroupId 各凑出 3 个字，于是两个立绘一起亮：
     char_387 第2段「エスカリバース」同时命中
       「ブライド・エスカレイヤー」        → 公共子串「エスカ」(3)
       「ブライドエスカレイヤー・リバース」 → 公共子串「リバース」(4)
     取最高分后只剩后者，和游戏一致（用户 2026-11-09 反馈两个都亮）。 */
  let best = null, bestScore = 0;
  ST.stands.forEach(el => {
    const gid = el.dataset.gid || '';
    if (!gid) return;
    let score = 0;
    if (gid === nm) score = 1000;
    else if (gid.includes(nm) || nm.includes(gid)) score = 200 + Math.min(gid.length, nm.length);
    else {
      const c = commonSub(gid, nm);
      if (c >= 3) score = c;
    }
    if (score > bestScore) { bestScore = score; best = el; }
  });
  if (best) {
    ST.stands.forEach(el => el.classList.toggle('dim', el !== best));
    return;
  }
  /* 说话人**不在场**（画外音，或者台词名是别的角色）：在场的一个都不是说话者，
     应当全部压暗 —— char_204 第 1 段第 47 句就是这个情况（マドカ 说话、
     场上只有沙由香），游戏里沙由香是暗的，我们以前完全没处理。
     但如果名字不是角色名（"一同"、"？？？"、"隊員Ａ"），就保持原样，
     否则"全员一起喊"的场面会被整片压暗。 */
  if (isCharName(nm)) {
    ST.stands.forEach(el => el.classList.add('dim'));
  }
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
  /* r/g/b 是 0~255 的**逐通道乘法系数**（255 = 原色，0 = 全黑；数据里还有
     280/300/500 这类超范围值，按 255 截断）。用 feColorMatrix 精确乘算，
     不再用 brightness/hue-rotate 近似 —— 后者是三个全局量，做不出"R 乘 0.6、
     G 乘 0.47、B 乘 0.35"这种偏色（回忆/夕阳/室内暖光全靠它）。 */
  const ms = num(m.Time, 0.3);
  const r = num(m.r, 255), g = num(m.g, 255), b = num(m.b, 255);
  $('#bgLayer').querySelectorAll('img').forEach(i => {
    applyMulColor(i, r, g, b, ms);
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
/* 剧本的 ShowStandEffect.Name → bundle_image_adv 里的 da_mark_* 结构名。
   剧本 12 种 Name 与包里 12 个 da_mark_* **一一对应**，一个都不能合并：
     leader      27×27 圆点、41 帧三个点依次出现 → 点々（省略号）
     troubled    90×101 乱线团                   → 困り
     exclamation 76×113 感叹号                  → ビックリ（！）
     surprise    123×117「！？」组合             → ビックリはてな
   以前把 点々→troubled、ビックリ→surprise，于是省略号显示成乱线团，
   而 leader / exclamation 两个结构完全没被使用（2026-11-10 用户反馈）。 */
const MARK_ALIAS = {
  '汗': 'sweat', '焦り': 'impatience', '怒り': 'anger',
  'ビックリ': 'exclamation', 'ビックリはてな': 'surprise', 'はてな': 'question',
  '点々': 'leader', '音符': 'notes', 'ハート': 'heart',
  '恥': 'blush', '落ち込み': 'decline',
  '困り': 'troubled',
};
/* 表情符号的美术尺度：da_mark_* 的 SizeCanvasX=320 是「这个符号自己的画布」，
   不是舞台宽度——实测游戏里符号直接按 1:1 像素贴在 1280×720 舞台上
   （气泡框 138px，占舞台 10.8%，与游戏截图一致）。舞台本身就是 1280 定宽，
   所以这里不需要再乘任何缩放。 */
const MARK_K = 1;
/* 当前正在播的表情符号：收到 ShowStandEffect{Name:""}（清除指令，全站 18817 条）
   要立刻收掉，不能再像以前那样等定时器自然消失。 */
let markFx = null;

function stopStandEffect(fade = 0) {
  if (!markFx) return;
  const h = markFx;
  markFx = null;
  if (h.raf) cancelAnimationFrame(h.raf);
  if (fade > 0) {
    h.host.style.transition = `opacity ${fade}ms`;
    h.host.style.opacity = 0;
    setTimeout(() => h.host.remove(), fade + 40);
  } else {
    h.host.remove();
  }
}

/* 用导出的逐帧数据播表情符号（server/cdn/scripts/export_adv_effects.py 生成）。
   数据形态见 web/data/advfx.js：每段动画 30fps、每帧每个部件的 [x,y,scale,opacity]
   已经在导出时算好世界坐标（父级缩放乘进去了），这里只负责"按帧摆 DOM"。
   汗滴「动四次」不是循环出来的，而是 first 那段 87 帧里本身就有 4 个折返。 */
function playStandEffect(m, def, A) {
  stopStandEffect();
  const atlas = A.atlas.mark;
  const host = document.createElement('div');
  host.className = 'mfx';
  const stage = $('#stage');
  const sr = stage.getBoundingClientRect();
  const iw = 1280;
  const ih = sr.width ? iw * (sr.height / sr.width) : 720;
  const sx = sr.width ? iw / sr.width : 1;
  const el = currentStand(m);
  let left, top;
  /* 用**官方落点表**（stand_box.js 的 AlphaCutRect）算头部位置，不要用
     getBoundingClientRect：那张 png 带透明边，rect 比人物宽一圈，
     取"66% 宽"经常落到角色外面去 —— 这是气泡位置一直对不上的原因。
     box 是立绘在 1280×720 画布上的真实矩形，头部大约在宽 60%、高 5% 处。 */
  const box = el ? standBox(el.dataset.stId || '') : null;
  /* 气泡相对锚点的包围盒，全部由 da_mark_* 的 cell + 逐帧 k 算出来：
     元素 left/top = (-pivotX,-pivotY)，transform 以 pivot 为原点缩放后
     平移 (k0,-k1)，所以左上 = (k0-px*s, -k1-py*s)、右下再加 (cw*s,ch*s)。
     以前写死的 -70/+68/-58/+80 就是这么来的，现在不再自己编。 */
  let bb = { L: -70, T: -58, R: 68, B: 80 };
  if (def && def.anims && def.anims[0]) {
    let L = Infinity, T = Infinity, R = -Infinity, B = -Infinity;
    for (const p of def.anims[0].parts) {
      if (!p.cell) continue;
      const c = p.cell, cw = c[2], ch = c[3], px = c[4], py = c[5];
      for (const k of p.k) {
        if (k[3] <= 0) continue;
        const s = Math.abs(k[2]) || 1;
        const l = k[0] - px * s, t = -k[1] - py * s;
        if (l < L) L = l;
        if (t < T) T = t;
        if (l + cw * s > R) R = l + cw * s;
        if (t + ch * s > B) B = t + ch * s;
      }
    }
    if (isFinite(L)) bb = { L, T, R, B };
  }
  let faceRight = null;                 // 有官方脸部矩形时记下来，后面用来"不许盖脸"
  if (box && box.length >= 8) {
    /* 官方脸部矩形：stand_box.js 的 ExpressionCutRect（相对 AlphaCutRect
       的局部坐标）。实测同角色不同服装相加后恒为定值，这就是客户端
       RedoAnchoPos 用的锚点。气泡左沿贴脸右缘、下沿贴脸顶 → 不遮脸也不远离。 */
    const dx = POS_DX[el.dataset.pos] ?? 0;
    faceRight = box[0] + dx + box[4] + box[6];
    const faceTop = box[1] + box[5];
    left = faceRight - bb.L;
    top = faceTop - bb.B;
  } else if (box) {
    // 旧版 stand_box.js（只有 4 项）没有脸部矩形，退回保守位置
    left = box[0] + (POS_DX[el.dataset.pos] ?? 0) + box[2] * 0.60;
    top = Math.max(box[1] + box[3] * 0.05 - bb.B, -bb.T);
  } else {
    const r = el ? el.getBoundingClientRect() : null;
    if (r && r.width > 0 && r.height > 0) {
      left = (r.left - sr.left + r.width * 0.62) * sx;
      top = (r.top - sr.top + r.height * 0.10) * sx + 40;
    } else {
      left = iw * 0.62;
      top = ih * 0.18;
    }
  }
  // OffsetX/OffsetY 是设计画布单位（剧本里的微调）
  left += num(m.OffsetX, 0) * MARK_K;
  top += num(m.OffsetY, 0) * MARK_K;
  /* MotionStand「移動」把立绘整体平移了 offx/offy 像素（剧本 Option1=横 / Option2=纵，
     常见 ±100/±200），standBox 是**原始落点表**，不含这个位移 —— 漏掉气泡就比头
     偏出整整一个身位（char_416 第1段第25句：左立绘 offx=100，气泡偏左 100 画布像素）。 */
  if (el) {
    left += num(el.dataset.offx, 0);
    top += num(el.dataset.offy, 0);
  }
  /* 剧本的 OffsetX 会很大（正月エスカレイヤー 第1段第13句 是 -130），按字面减下去
     整颗气泡就压到脸上了 —— 游戏里气泡**始终在头部右侧**、从不遮脸。
     所以把左沿钳在脸右缘：负值最多把气泡拉到"贴着右脸颊"，只有正值才能推远。
     （钳的是"气泡左沿相对脸的右缘"，所以用 faceRight - bb.L 换算回 host.left。） */
  if (faceRight != null) {
    const minLeft = faceRight - bb.L;
    if (left < minLeft) left = minLeft;
  }
  /* 钳制时用**刚算出的气泡包围盒**，保证整块都在舞台内：
     立绘顶到画面上沿时不会被 overflow:hidden 裁掉。 */
  host.style.left = `${Math.min(Math.max(left, -bb.L), iw - bb.R)}px`;
  host.style.top = `${Math.min(Math.max(top, -bb.T), ih - bb.B)}px`;
  $('#fxLayer').appendChild(host);
  /* 立绘还会动：ShakeStand（CSS 抖动）、MotionStand 的ポップ/おじぎ/突き/座る、
     MoveStand 换站位、ShowStand 滑入 —— 气泡是 #fxLayer 里的**独立元素**，
     不跟就会「人动了、脑袋上的气泡留在原地」（2026-11-10 用户实测：
     char_416 第1段 24–26 句，ShakePixel=10 的抖动）。
     游戏里角色一动就会重算头部锚点（RedoAnchoPos），这里同一个意思：
     逐帧读立绘矩形做增量，按舞台缩放折算回 1280 画布像素，累加到 host 的
     transform 上（left/top 只在创建时算一次，之后不动）。 */
  let prevL = null, prevT = null, accX = 0, accY = 0;
  const followStand = () => {
    if (!el || !el.isConnected) return;
    const r = el.getBoundingClientRect();
    const dl = prevL === null ? 0 : r.left - prevL;
    const dt = prevT === null ? 0 : r.top - prevT;
    prevL = r.left; prevT = r.top;
    if (!dl && !dt) return;                 // 静止时零写入，不留残余 transform
    const w = $('#stage').getBoundingClientRect().width;
    const k = w ? 1280 / w : sx;
    accX += dl * k;
    accY += dt * k;
    host.style.transform = `translate(${accX.toFixed(2)}px, ${accY.toFixed(2)}px)`;
  };
  followStand();

  const parts = [];
  const anims = def.anims;
  const maxParts = Math.max(...anims.map(a => a.parts.length));
  for (let i = 0; i < maxParts; i++) {
    const d = document.createElement('div');
    d.className = 'mfx-p';
    host.appendChild(d);
    parts.push(d);
  }
  const handle = { host, raf: 0 };
  markFx = handle;

  let si = 0, f = 0, acc = 0, last = 0;
  const draw = () => {
    if (markFx !== handle) return;
    const a = anims[si];
    const fa = a.parts;
    for (let i = 0; i < parts.length; i++) {
      const d = parts[i];
      const p = fa[i];
      if (!p || !p.cell) { d.style.display = 'none'; continue; }
      const [cx, cy, cw, ch, px, py] = p.cell;
      const k = p.k[f] || p.k[p.k.length - 1];
      if (d.dataset.cell !== `${cx},${cy}`) {
        d.dataset.cell = `${cx},${cy}`;
        d.style.width = `${cw * MARK_K}px`;
        d.style.height = `${ch * MARK_K}px`;
        d.style.left = `${-px * MARK_K}px`;
        /* cell 的 Pivot 是**距图左上角**的像素（不是距底部）。
           按"距底"算会让气泡框和白色底片错开整整 65px（表现是气泡下面浮着
           一个空的六边形轮廓），水滴也会被推到底部。 */
        d.style.top = `${-py * MARK_K}px`;
        d.style.transformOrigin = `${px * MARK_K}px ${py * MARK_K}px`;
        d.style.backgroundImage = `url(${atlas.src})`;
        d.style.backgroundSize = `${atlas.w * MARK_K}px ${atlas.h * MARK_K}px`;
        d.style.backgroundPosition = `${-cx * MARK_K}px ${-cy * MARK_K}px`;
        /* LabelColor 是**顶点色乘法**（CPE 的着色方式：像素色 × 顶点色），
           之前误用 background-color 铺底，矩形透明区也被染色 → 气泡变橙色方块，
           于是干脆把染色删了 —— 结果气泡丢了底色（游戏里是米色 #f7bc89，
           用户 2026-11-09 用对比图指出）。正确做法就是 feColorMatrix 乘算：
           白色底片 × #f7bc89 = 米色底，而 alpha 不变，不会出现方块。 */
        d.style.filter = '';
        /* 只有 base_*（白底 / 网格描边）按 LabelColor 染色 —— 它们的图是纯白的。
           内容部件（heart / impatience01 / blush）在 atlas 里**已经是成品颜色**
           （实测红心像素 (255,50,47)、水滴 (50,61,164) 蓝），
           再乘一次顶点色会把红心染成紫心（2026-11-09 用户反馈）。 */
        if (p.col && /^base_/.test(p.name || '')) tintByHex(d, p.col);
      }
      d.style.display = '';
      d.style.opacity = String(Math.max(0, Math.min(1, k[3])));
      /* 数据里的 y 是 Unity 坐标（**向上为正**）：气泡框在 -70、水滴在 -13 起落，
         换成屏幕坐标要取负。不取负的话水滴会跑到气泡右下方（游戏里是左上方），
         这正是 2026-11-09 用户报的"水滴位置不对"。 */
      d.style.transform = `translate(${k[0] * MARK_K}px, ${-k[1] * MARK_K}px) scale(${k[2]})`;
    }
  };
  const step = ts => {
    if (markFx !== handle) return;
    followStand();          // 每帧跟一次（抖动是 60fps 的 CSS 动画，30fps 的 draw 跟不上）
    if (!last) last = ts;
    const a = anims[si];
    acc += ts - last;
    last = ts;
    const ms = 1000 / (a.fps || 30);
    while (acc >= ms) {
      acc -= ms;
      f++;
      if (f >= a.n) {
        si++;
        f = 0;
        if (si >= anims.length) {            // 全部播完（含收尾段）自动收掉
          stopStandEffect(120);
          return;
        }
      }
    }
    draw();
    handle.raf = requestAnimationFrame(step);
  };
  draw();
  handle.raf = requestAnimationFrame(step);
}

function showStandEffect(m) {
  // Name 为空 = 清除指令（全站 18817 条），以前当没看见，符号只能等定时器自己消失
  if (!m.Name) { stopStandEffect(120); return; }
  const A = window.__ADVFX;
  const key = MARK_ALIAS[m.Name];
  const def = A && A.atlas && A.atlas.mark && key ? (A.marks || {})[key] : null;
  if (def && def.anims && def.anims.length) {
    playStandEffect(m, def, A);
    return;
  }
  // 素材没下载（旧版本用户还没装「剧情演出特效」）→ 退回 emoji，功能不中断
  showStandEffectEmoji(m);
}

function showStandEffectEmoji(m) {
  const h = markFx;
  if (h) stopStandEffect();
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
/* ===== 画面特效（ShowScreenEffect / ShowScreenEffectBack）=====
 * 数据形态：{"Name":"集中線／左","PosX":"640","PosY":"360","Time":"0"}。
 *   · Time 在 93% 的指令里是 0，有值也只是 0.3~2（淡入秒数）；
 *   · **Name 为空 = 清除**，全站 10055 条 —— 占比接近一半，说明这是
 *     「开启后一直演，直到被清除」的持续型特效，不是播一次就结束。
 * 所以这里做成常驻元素 + 循环动画，清除指令才移除；跳转时也要按
 * 重放路径恢复（见 REPLAY / replayOne）。
 *
 * Back 和 Front 是两层：ShowScreenEffectBack（11100 条）垫在**立绘后面**，
 * ShowScreenEffect（9882 条）盖在最前。以前两者用同一个容器，层次差异丢了。
 */
const SCREFF_LAYER = { back: '#screffBack', front: '#fxLayer' };
let screffLive = { back: null, front: null };

function stopScreff(which, fade) {
  const el = screffLive[which];
  screffLive[which] = null;
  // 停掉逐帧循环（rAF 自己也会因 host 被移除而退出，这里顺手清登记）
  const fh = fxHandles.get(which);
  if (fh) { cancelAnimationFrame(fh.raf); fxHandles.delete(which); }
  /* 兜底：同一层里除了登记过的那一个，还可能留着历史残留
     （跳转重建时 resetStage 保留画面、引用却被重置过）。不清会让两层
     集中线叠在一起——实测就是这么发现"特效叠了两份"的。 */
  const layer = $(SCREFF_LAYER[which]);
  if (layer) {
    Array.from(layer.querySelectorAll('.screff'))
      .forEach(x => { if (x !== el) x.remove(); });
  }
  if (!el) return;
  if (fade > 0) {
    el.style.transition = `opacity ${fade}ms`;
    el.style.opacity = 0;
    setTimeout(() => el.remove(), fade + 40);
  } else {
    el.remove();
  }
}

/* 把 Name 归到一种渲染模式。60 种 Name 全是这几种的组合：
 *   集中線／左·右·上·下      —— 真素材是 1024² 全屏汇聚线，中心本来就空
 *   光る／赤·紫·青·黄·金·銀  —— 颜色变体，叠一层色片
 *   光る／…／全体 = 全屏       —— 尺寸放大到覆盖整屏
 * 素材里没有单图可用的（爆発/煙/蝶/音楽/回復…）继续用 CSS 光效，
 * 因为它们在包里是**帧图集**，硬贴会显示成一片小格子。 */
/* Name → 渲染方案。**只保留资源包里有一一对应素材的那些**，其余返回 null
   （不画，而不是拿别的素材顶替 —— 顶替等于改剧情，2026-11-09 用户要求）。

   为什么能确定这几条的对应关系：包里的结构名与日文 Name 语义完全一致，
   贴图也是从**那个结构自己**的材质里取的：
     集中線 ↔ da_adv_effect_concentration line_* + ima_line(cell_line 1024²)
     煙 / 冷気 ↔ da_adv_effect_cold(_loop) + adv_effect_cold_ima
     雪 ↔ adv_effect_snow_loop_0
     蝶 ↔ adv_effect_butterfly_0
     音楽 ↔ da_adv_effect_music(_loop) + adv_effect_music_ima
     斬撃/打撃/刺突/打ち下ろし ↔ da_adv_effect_attack_slash|piercing|downhill + ef_hit_ima

   映射不到的（**一律不画**）：
     キラキラ / 光る／〇／全体 / 空想 / ＳＤＤＤ / 電磁ウェブ / 邪気 /
     回復 / 水しぶき / 火柱 / 火事 / 炎ブレス / ジャンプ / 爆発大・小
   这些要么包里没有同名结构、要么结构是**粒子 + 材质混合**（纯 Web 端复刻不出
   原作观感），与其拿别的图顶上，不如留白。 */
/* 剧本的日文 Name → 包里的 prefab 名。
   依据是 APK 里硬编码的 49 个 adv_effect_* 资源名 + bundle 里同名 da_/dc_ 结构，
   按语义一一对应（颜色/方向/动作词完全吻合），不是按画面猜的。
   没有对应项的（ＳＤＤＤ / ジャンプ / 金 …）保持不填 = 不渲染。 */
const SCREFF_FX = {
  'キラキラ': 'illumination01', 'キラキラ２': 'illumination02',
  '光る': 'heal', '光る／全体': 'heal_all',
  '光る／赤': 'heal_red', '光る／赤／全体': 'heal_red_all',
  '光る／紫': 'heal_purple', '光る／紫／全体': 'heal_purple_all',
  '光る／青': 'heal_blue', '光る／青／全体': 'heal_blue_all',
  '光る／黄': 'heal_yellow', '光る／黄／全体': 'heal_yellow_all',
  '光る／銀': 'heal_white', '光る／銀／全体': 'heal_white_all',
  '回復': 'heal', '回復／全体': 'heal_all',
  '打撃': 'attack_blow', '斬撃': 'attack_slash',
  '刺突／右': 'attack_piercing_R', '刺突／左': 'attack_piercing_L',
  '打ち下ろし': 'attack_downhill',
  '爆発大': 'attack_explosion_all', '爆発小': 'attack_explosion',
  '複合攻撃': 'attack_all01', '複合攻撃／斬撃': 'attack_all02',
  '複合攻撃／打撃': 'attack_all03',
  '火柱': 'attack_fire_pillar', '火事': 'attack_fire_pillar',
  '炎ブレス': 'fire', '水しぶき': 'attack_water',
  '煙': 'smoke_loop', '冷気': 'cold', '冷気／ループ': 'cold_loop',
  '邪気': 'jaki_loop', '空想': 'image_loop', '空想／黒': 'image_bk_loop',
  '雪': 'snow_loop', '蝶': 'butterfly',
  '音楽': 'music', '音楽／ループ': 'music_loop',
  '電磁ウェブ': 'electricity', '電磁ウェブ／黄': 'electricity_yellow',
  /* 光る／金／全体（4 条）：六个 heal 变体里 赤/紫/青/黄/銀 已占，
     只剩基色 heal_all（暖金白光，与 黄 是同一套贴图的另一组顶点色）。 */
  '光る／金／全体': 'heal_all',
  /* ＳＤＤＤ（4 条，全在 ShowScreenEffectBack）= 变身/必杀前的白光+能量上升。
     包里没有同名结构；能量类只有 da_adv_effect_energy（181 帧、6 秒、
     ima_energy* 光环 + 4 组上升粒子），与「再び輝くマジカルパワー」的演出吻合。
     这一条是**语义映射**里唯一仍带推断的（其余全部有同名结构）。 */
  'ＳＤＤＤ': 'energy',
  // 集中線不进这张表：它已有 ima_line 真素材的两层扇形交替实现（用户验证过），
  // 而 da_adv_effect_concentration line_* 只有 1 个粒子 emitter、没有图片部件，
  // 走数据路径反而会把已正确的效果换掉。让它落到 screffSpec 的 lines 分支。
};

/* 数据驱动播放：按 da_adv_effect_* 摊平后的逐帧表摆 DOM。
   返回 false 表示该 prefab 没有可渲染的部件（粒子型），交给调用方决定。 */
const fxHandles = new Map();

/* 持续型 prefab（开着一直到被清除）vs 一次性（播完自己收）。
   判定依据是包里的资源名：loop 系列 + image/jaki/illumination/energy/electricity
   这些是"挂着不动的状态特效"，其余（打撃/斬撃/爆発/火柱/水しぶき/音楽/雪…）
   都是短促的一次性演出。seekTo 恢复现场时**只有持续型需要补回来** ——
   一次性特效在目标句早就播完了，补就是重演（见 seekTo 里的说明）。 */
const SCREFF_LOOP_RE = /loop|image|jaki|illumination|energy|electricity/;
/* cold（冷気）的 RateOpacity 是 **0→1→0 的一次性脉冲**：
     part_1/effect_1/effect_2 25 帧 [0 … 1.0(第15帧) … 0.067]
     all_ef                32 帧 [0 … 0.76(第21帧) … 0.048]
   而 jaki_loop（邪気）全是常数 1.0 / 0.72~0.897 的轻呼吸。
   所以 cold **不能循环**（每 2s 重播脉冲 = 用户看到的「一闪一闪」），
   也不能停在末帧（全灭）；正确是**停在峰值帧**直到剧本 628 清除，
   粒子时钟则继续循环（雾持续向外扩散，和邪気一样）。
   "106 句只剩滤镜"= 628 清除特效 + MulColorBackground 留到 649 复位。 */
const SCREFF_HOLD_RE = /^cold$/;
/* 冷気/邪気 三个 prefab 的部件与发射器结构完全一样，只差烘焙图的颜色
   （cold 青蓝 / jaki 紫）和发射器数量（3 / 5）。这三个走官方数据直渲：
   all_ef 用包里烘好的椭圆画框雾图，粒子层用同一张官方 mask 挖空中心。 */
const MIST_PREFABS = new Set(['cold', 'cold_loop', 'jaki_loop']);
function isLoopPrefab(pf) { return SCREFF_LOOP_RE.test(String(pf || '')); }

/* 整屏渐变色层的 mask-image 是否已经真正解码过。
   没解码过就必须压住那一层（否则浏览器按"没遮罩"画出一块实心色）；
   解码过则首帧直接上屏，不再出现「先缺一整层颜色、过一会儿才补齐」的两段式。 */
const MASK_READY = new Set();

/* 常驻型画面特效（冷気/邪気/空想/煙…）的贴图提前拉进缓存。
   目的：第一次遇到时就能**当场预乘着色、当帧上屏**，不会出现"先空一两帧、
   再突然冒出来"的闪。只预热 loop/hold 这几类（体积很小），延后到首页稳定之后。 */
function warmScreenFxTextures() {
  const A = window.__ADVFX || {};
  const bag = new Set();
  const maskBag = new Set();
  const add = u => { if (u) bag.add(u); };
  for (const prefab of new Set(Object.values(SCREFF_FX))) {
    const def = (A.screff || {})[prefab];
    if (!def) continue;
    /* 粒子贴图：canvas 版对**所有**有发射器的 prefab 都会用，一次性特效
       只有 20~30 帧，晚一帧解码就等于整段少看一段，所以全部预热。
       它们大多是同一批图集，去重后没几张。 */
    for (const em of def.particles || []) add(em.src);
    if (!(SCREFF_LOOP_RE.test(prefab) || SCREFF_HOLD_RE.test(prefab))) continue;
    const a0 = def.anims && def.anims[0];
    for (const p of (a0 && a0.parts) || []) {
      const mm = a0.maps && a0.maps[p.m];
      add(mm && mm.tex && mm.tex.src);
      add(p.pcSrc);
      if (p.pcm && p.pcm.mask) { add(p.pcm.mask); maskBag.add(p.pcm.mask); }
    }
  }
  bag.forEach(u => {
    /* canvas 粒子版读的是 `_spriteImg` 里**同一个 Image 实例**（spriteImage /
       tintCanvas 都查它）。以前这里只 new Image() 烘 HTTP 缓存，canvas 自己那个
       实例首次触发时 complete 还是 false → 第一帧几百颗粒子全被跳过、下一帧
       一起冒出来，正好又是「暗一帧再亮」的闪。这里直接注册并解码那个实例。 */
    spriteImage(u);
    const im = _spriteImg.get(u);
    if (im && im.decode) im.decode().catch(() => { });
    /* DOM 版的 tintedSprite 仍用独立 Image，顺带把 HTTP 缓存也热上。 */
    const warm = new Image();
    warm.src = u;
    /* decode() 才会真正解码；只设 src 的话第一次绘制还是要等解码，
       遮罩层就会晚几百毫秒才敢亮（看起来像"闪一下"）。 */
    if (warm.decode) warm.decode().catch(() => { });
  });
  /* 整屏渐变色层用的是 CSS `mask-image`：只把图当 Image 解码**不等于**
     mask 解码路径已就绪。以前因此把这一层压成 opacity:0、等解码完再
     250ms 淡入，用户看到的就是「先一帧/一段不一样的画面，消失后才变成
     正常的邪気/冷気」（2026-11-11 char_190 p3#105 / char_415 p3#36）。
     这里用 1px 隐藏元素把 mask 真正挂上一次并完成解码，之后创建部件
     时首帧就是完整的色层，不需要任何门控或二次淡入。 */
  maskBag.forEach(u => {
    const im = new Image();
    let done = false;
    const ok = () => { if (!done) { done = true; MASK_READY.add(u); } };
    im.onload = im.onerror = ok;
    im.src = u;
    if (im.complete && im.naturalWidth) ok();
    if (im.decode) im.decode().then(ok).catch(ok);
    const probe = document.createElement('div');
    probe.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;pointer-events:none';
    probe.style.maskImage = probe.style.webkitMaskImage = `url(${u})`;
    probe.style.maskSize = probe.style.webkitMaskSize = '1px 1px';
    (document.getElementById('stage') || document.body).appendChild(probe);
  });
}

/** 固定 seed 的伪随机：CPE 粒子有 SeedRandom，每次播放要得到同一片散布。 */
function fxRand(seed, i) {
  let x = (Math.imul(seed | 0, 0x9e3779b1) + Math.imul(i | 0, 0x85ebca6b)) >>> 0;
  x ^= x >>> 16; x = Math.imul(x, 0x7feb352d);
  x ^= x >>> 15; x = Math.imul(x, 0x846ca68b);
  x ^= x >>> 16;
  return (x >>> 0) / 4294967296;
}
const clamp01 = v => Math.max(0, Math.min(1, v));

/* ===== 染色贴图：把「顶点色 × 贴图 alpha」预乘成一张图 =====
 * 「纯色 × 贴图 alpha」这套语义在 CSS 里只有 backgroundColor + mask-image 能表达，
 * 但 mask 一旦在某条缩放/合成路径上没吃到（或解码晚一两帧），元素就退化成一块
 * **实心长方形**：冷気/邪気 一层 480 颗 500~1000px 的方片闪一下，就是用户反复
 * 反馈的「有一些长方形在闪屏」（2026-11-10 char_407 第4段第70句 邪気）。
 *
 * 所以在 canvas 里把颜色预乘进贴图（source-in：保留贴图 alpha、只替换颜色），
 * 之后纯用 background-image 画 —— 不再依赖 mask，方块无从出现。
 * 贴图是异步的：先挂原图（形状正确、颜色略偏），预乘完成再整体换掉（waiters）。
 * 颜色量化到 1/12，避免"每颗粒子一张 canvas"。 */
const _tintCache = new Map();
function tintedSprite(url, rgb) {
  const q = rgb.map(v => Math.round(clamp01(v) * 12) / 12);
  const key = url + '|' + q.join(',');
  let rec = _tintCache.get(key);
  if (rec) return rec;
  rec = { url: null, waiters: [] };
  _tintCache.set(key, rec);
  const paint = img => {
    try {
      const c = document.createElement('canvas');
      c.width = img.naturalWidth || img.width;
      c.height = img.naturalHeight || img.height;
      const g = c.getContext('2d');
      g.drawImage(img, 0, 0);
      g.globalCompositeOperation = 'source-in';
      g.fillStyle = `rgb(${q.map(v => Math.round(clamp01(v) * 255)).join(',')})`;
      g.fillRect(0, 0, c.width, c.height);
      rec.url = c.toDataURL('image/png');
    } catch (e) {                     // canvas 不可用（极端环境）→ 保持原图，不阻断特效
      rec.waiters.length = 0;
      return;
    }
    rec.waiters.forEach(el => { el.style.backgroundImage = `url(${rec.url})`; });
    rec.waiters.length = 0;
  };
  const im = new Image();
  im.onload = () => paint(im);
  im.onerror = () => { rec.waiters.length = 0; };
  im.src = url;
  /* 已经在内存/HTTP 缓存里的（同一个 prefab 第二次出现、或素材早被别的特效用过）
     `complete` 立刻为真 → 当场预乘，元素第一次上屏就是着色好的，
     不会出现"先用原贴图上屏、下一帧再换成着色版"的闪一下。 */
  if (im.complete && im.naturalWidth) paint(im);
  return rec;
}

/* ===== canvas 版染色粒子用的两个取图函数 ===== */
const _spriteImg = new Map();
/** 图集 Image（canvas 直接 drawImage 用）。没加载完返回 null，下一帧再取。 */
function spriteImage(url) {
  let im = _spriteImg.get(url);
  if (!im) {
    im = new Image();
    im.decoding = 'async';
    im.src = url;
    _spriteImg.set(url, im);
  }
  return (im.complete && im.naturalWidth) ? im : null;
}
const _tintCv = new Map();
/** 「顶点色 × 贴图 alpha」的 canvas 版：source-in 预乘，结果直接 drawImage。 */
function tintCanvas(url, rgb) {
  const q = rgb.map(v => Math.round(clamp01(v) * 12) / 12);
  const key = url + '|' + q.join(',');
  let cv = _tintCv.get(key);
  if (cv) return cv;
  const img = spriteImage(url);
  if (!img) return null;                 // 贴图还没加载好
  cv = document.createElement('canvas');
  cv.width = img.naturalWidth;
  cv.height = img.naturalHeight;
  const g = cv.getContext('2d');
  g.drawImage(img, 0, 0);
  g.globalCompositeOperation = 'source-in';
  g.fillStyle = `rgb(${q.map(v => Math.round(clamp01(v) * 255)).join(',')})`;
  g.fillRect(0, 0, cv.width, cv.height);
  _tintCv.set(key, cv);
  return cv;
}

function playScreenFx(which, prefab, m, fadeIn) {
  const A = window.__ADVFX || {};
  const def = A.screff && A.screff[prefab];
  const a = def && def.anims && def.anims[0];
  const emitters = (def && def.particles) || [];
  /* 两个冷雾 prefab 逐字段相同，唯一差别是 kiri 的 ColorVertex
     （冷気 g=0.784 青蓝 / 邪気 g=0.502 紫蓝）。cold_P 那组云的
     ColorVertex rgb 是纯白，但 ColorVertexFluctuation 给了蓝紫波动；
     用 kiri 的色给它们统一上色，两个效果就"只差颜色"，也避免纯白加色过曝。 */
  const kiri0 = emitters.find(e => /kiri/i.test(String(e.from || '')));
  const mistCol = kiri0 ? kiri0.col.slice(0, 3) : [0.5, 0.7, 1];
  /* 有的特效只有粒子（雪）或只有 Instance 子 prefab（複合攻撃）：
     图片部件为 0 也要播放。 */
  const instances = a.instances || [];
  if (!a || (!a.parts.length && !emitters.length && !instances.length)) return false;
  const loop = isLoopPrefab(prefab);
  /* 状态型（播一次停在末帧，等剧本清除）：冷気 */
  const hold = SCREFF_HOLD_RE.test(String(prefab || ''));
  /* 空想 / 空想黒：白色 all_ef 贴图本身就是一张**静态画框雾**
     （四边 alpha 255、中心空）。游戏里这圈白雾不随时间变化；
     以前用错深蓝贴图画不出它，只能靠 4 边 tama 粒子不停飘 → 看着是动效。
     现在贴图对了，把粒子时钟定格在预热后的稳态（phase 仍逐颗错开，
     所以雾的浓淡分布还在，只是不再流动）。 */
  const still = /^(image_loop|image_bk_loop)$/.test(String(prefab || ''));
  /* seekTo 重放是"恢复现场"，不是"重新演出"：一次性特效在目标时刻早已播完，
     不该再创建一遍（每切一次上一句/下一句就闪一下，2026-11-09 反馈）。
     持续型如果**同一个 prefab 已经挂着**，也直接沿用，别重建/重新加载
     （煙 每次切句子都重新加载，2026-11-09 反馈）。 */
  if (ST.fastForward) {
    const live = screffLive[which];
    if (loop && live && live.parentNode && live.dataset.fx === prefab) return true;
    if (!loop) return false;
  }

  const layer = $(SCREFF_LAYER[which]);
  if (!layer) return false;
  const host = document.createElement('div');
  host.className = 'screff screff-fx';
  host.dataset.fx = prefab;
  /* PosX/PosY 是**屏幕坐标**（默认 640,360 = 特效 prefab 的作者原点）。
     全站 99% 是 640,360，但「火柱」在 char_308 第4段第45句是 PosX=990 ——
     那根火柱应该烧在画面右侧、不是正中。以前整条指令的坐标被忽略。 */
  const aox = num(m.PosX, 640) - 640, aoy = num(m.PosY, 360) - 360;
  if (aox || aoy) host.style.transform = `translate(${aox}px, ${aoy}px)`;
  /* 入场：剧本 `Time=0` 就是**无过渡**，直接就位；Time>0 按秒淡入。
     跳转重放（上一句/下一句）是"恢复现场"，同样不演淡入。
     ⚠ 不要再"等贴图解码完才亮"（2026-11-11 用户反馈：先闪一下、然后画面才正常）：
     那会把特效推迟 0.3~0.8 秒才出现，后面的剧本计时却在走。
     染色早就改成 canvas 预乘（tintedSprite），贴图没就位时元素是**透明**的、
     不是方块，所以可以直接挂上去 —— 方块问题不靠门控解决。 */
  if (ST.fastForward || fadeIn <= 0) {
    host.style.opacity = 1;
  } else {
    host.style.opacity = 0;
    host.style.transition = `opacity ${fadeIn}s`;
  }
  layer.appendChild(host);
  screffLive[which] = host;
  if (!ST.fastForward && fadeIn > 0) {
    /* 必须先把 opacity:0 **落成计算值**，否则浏览器认为"起点终点同一次样式计算"，
       直接跳到 1 —— 录屏实测：这一下让整屏亮度一帧内 +26，就是用户说的"闪"。
       offsetWidth 强制一次样式/布局，把 0 定住；再下一帧给 1，过渡才会真的跑。 */
    void host.offsetWidth;
    requestAnimationFrame(() => {
      if (screffLive[which] === host) host.style.opacity = 1;
    });
  }

  const items = a.parts.map((p, i) => {
    const map = a.maps[p.m];
    const cell = map && map.cells[p.c];
    const t = map && map.tex;
    const d = document.createElement('i');
    d.className = 'fxp';
    /* 先按"透明"出生：逐帧 draw 在第一帧才会写入真实 opacity，
       不置 0 的话**第一帧所有部件都是不透明**，Time=0 的特效就会闪一下
       （2026-11-11 用户反馈「闪一下后页面才正常」）。 */
    d.style.opacity = '0';
    d.style.zIndex = String(i + 1);
    /* PartsColor 顶点色。**Bound=1（四角同色）+ Operation=3(MUL)** 的部件，
       语义就是「贴图 alpha 当遮罩 × 单色」：空想 的白雾、空想／黒 的黑雾、
       冷気/邪気 的冷蓝雾、heal 的光带全是这一类（都叫 all_ef / obi）。
       以前这类部件被换成自编的四条边渐变（fogFrame，还带 1.45 增益），
       既不来自数据、也把中心压掉。现在按数据渲染：
         底色 = VertexColor，形状/浓淡 = 贴图 alpha，整体强度 = 逐帧 RateOpacity。
       四角不同色（Bound=2）的才用导出时烘好的 fx_pc_*.webp。 */
    const vc = (p.pc && p.pc.length) ? p.pc[0] : null;
    /* bound=1（四角同色）时两种混合都等价于「纯色 × 贴图 alpha」：
         op=3(MUL) 纯白贴图乘顶点色 = 顶点色
         op=0(MIX) 顶点色 alpha=1 时整块替换成顶点色（空想／黒 的黑雾就是它）
       以前只认 MUL，空想／黒 的 Operation=0 落到贴图原色 → 黑雾变白雾、几乎看不见。 */
    const tint = !!(t && vc && !p.pcSrc && p.pcm && p.pcm.bound === 1
      && (p.pcm.op === 3 || p.pcm.op === 0));
    if (tint) {
      /* 预乘成"着色贴图"当背景画（不再用 mask，见 tintedSprite 注释）；
         没算好就先不画，避免"先用原贴图、下一帧换色"的跳变。 */
      const rec = tintedSprite(t.src, [vc[0], vc[1], vc[2]]);
      d.style.backgroundImage = rec.url ? `url(${rec.url})` : 'none';
      d.style.backgroundRepeat = 'no-repeat';
      if (!rec.url) rec.waiters.push(d);
    } else if (MIST_PREFABS.has(prefab)) {
      /* 画框雾：官方 grad 的**均匀色** + 官方 mask（中心椭圆挖空、四边保留）。
         ⚠ 不能改成全屏无遮罩：那会把人物立绘一起染蓝（用户 2026-11-11
         「PLAYER after 把任务立绘都加了滤镜，本来很接近了现在改成全屏改烂了」）。
         官方 PartsColor 是 bound=1 的四角同色，grad 只是导出器从烘焙图取的
         上下端像素，取两端均值作均匀色，形状交给 mask。
         中下部过曝要靠**把粒子的量搬到四角**解决，不是给整屏加雾。 */
      const gr = p.pcm && p.pcm.grad, mk = p.pcm && p.pcm.mask;
      if (gr && gr.length === 2 && mk) {
        const av = [0, 1, 2].map(i => Math.round((gr[0][i] + gr[1][i]) / 2));
        const col = `rgba(${av[0]},${av[1]},${av[2]},${(gr[1][3] / 255).toFixed(3)})`;
        d.style.backgroundImage = `linear-gradient(${col}, ${col})`;
        d.style.maskImage = d.style.webkitMaskImage = `url(${mk})`;
        d.style.maskSize = d.style.webkitMaskSize = '100% 100%';
        d.style.maskRepeat = d.style.webkitMaskRepeat = 'no-repeat';
        d.style.maskPosition = d.style.webkitMaskPosition = '0px 0px';
      } else {
        d.style.backgroundImage = `url(${p.pcSrc})`;
      }
      d.style.backgroundSize = '100% 100%';
      d.style.backgroundPosition = '0px 0px';
      d.style.backgroundRepeat = 'no-repeat';
    } else if (p.pcSrc) {
      /* PartsColor 顶点色：导出时已按 4 角双线性插值 × 贴图 alpha × RateAlpha
         烘成一张滤镜图（fx_pc_*.webp）。
         ⚠ 加色部件（bl=1）必须改用 CSS 渐变：实测 Chromium 里
         **位图 background-image 不参与 mix-blend-mode**
         （同一元素换成 background-color 或 linear-gradient 立刻正常，
           plus-lighter / screen / lighten 都是 0 贡献），
         冷気/邪気 的满屏冷色滤镜正是这种加色位图 —— 整块等于没画，
         所以"邪気 只有下方一点点紫"。导出时额外给了上下端色 `pcm.grad`。 */
      const gr = p.pcm && p.pcm.grad;
      if (p.bl === 1 && gr && gr.length === 2) {
        const g = c => `rgba(${c[0]},${c[1]},${c[2]},${(c[3] / 255).toFixed(3)})`;
        /* 冷気/邪気 的 all_ef 就是数据里那块**竖向渐变色块**，不再自造
           radial 环带（2026-11-10 用户：包里本来就有雾特效，别自己做）。
           雾的形状/动效全部交给 kiri + cold_P 两组粒子；中心之所以干净，
           是因为 ShowScreenEffectBack 挂在**立绘后面**，立绘自己挡出空白。
           两个 prefab 的 grad 完全相同，用 kiri 顶点色调一下，
           冷気 青蓝 / 邪気 紫蓝的差别才贯穿整层。 */
        if (/^(cold|jaki_loop)$/.test(String(prefab || ''))) {
          const kiri = emitters.find(e => /kiri/i.test(String(e.from || '')));
          const mist = kiri ? kiri.col : [0.5, 0.7, 1];
          const gc = c => `rgba(${Math.round(c[0] * mist[0])},` +
            `${Math.round(c[1] * mist[1])},${Math.round(c[2] * mist[2])},` +
            `${(c[3] / 255).toFixed(3)})`;
          d.style.backgroundImage =
            `linear-gradient(to bottom, ${gc(gr[0])}, ${gc(gr[1])})`;
        } else {
          d.style.backgroundImage =
            `linear-gradient(to bottom, ${g(gr[0])}, ${g(gr[1])})`;
        }
        /* 渐变只负责**颜色**，形状必须还给贴图。
           两种来源：
             p.pcm.mask —— 导出时把烘焙图**亮度转成 alpha** 的遮罩，给
               alpha 全不透明、形状全在 RGB 里的部件用。冷気/邪気 的 all_ef
               正是这种：中心亮度 0.1（≈不加色）、下缘 56.6，只取上下端色
               铺渐变就会把中心填平 → 全屏罩（2026-11-10 用户截图）。
             p.pcSrc 的 alpha —— 本来就带形状的：illumination 的画框、
               attack 的 circle 空心光环、music 的 bokasi 虚化框。 */
        const mkSrc = (p.pcm && p.pcm.mask) || p.pcSrc;
        if (mkSrc) {
          const mk = `url(${mkSrc})`;
          d.style.maskImage = mk;
          d.style.webkitMaskImage = mk;
          d.style.maskSize = '100% 100%';
          d.style.webkitMaskSize = '100% 100%';
          d.style.maskRepeat = 'no-repeat';
          d.style.webkitMaskRepeat = 'no-repeat';
          d.style.maskPosition = '0px 0px';
          d.style.webkitMaskPosition = '0px 0px';
          /* ⚠ 遮罩图解码完之前，浏览器会把这一层当成"没遮罩"画出来 ——
             整屏渐变就变成一块**实心色块**。所以解码完成前这一层不画。
             但**不能解码完就"啪"地亮起来**（录屏实测：那一层晚 1 秒才到，
             画面整体亮度一次跳 +26，用户看到的就是"闪一下"）。
             改成解码后 250ms 内淡入（draw 里按 maskAt 做斜坡）。 */
          if (MASK_READY.has(mkSrc)) {
            /* 遮罩已在 warmScreenFxTextures 里走完 mask 解码路径：这一层
               由下面同步的 draw() 首帧就按 RateOpacity 上屏，不再"先缺一层
               颜色、过一会儿再补"，也不用 250ms 二次淡入。 */
          } else {
            /* 冷缓存兜底（预热还没赶上时）：仍压住，避免浏览器按"无遮罩"
               把整屏渐变画成实心色块；就绪后再按原斜坡淡入。 */
            d.dataset.maskPending = '1';
            d.style.opacity = '0';
            const mkIm = new Image();
            mkIm.onload = mkIm.onerror = () => {
              delete d.dataset.maskPending;
              d.dataset.maskAt = String(performance.now());
              MASK_READY.add(mkSrc);
            };
            mkIm.src = mkSrc;
            if (mkIm.decode) mkIm.decode().catch(() => { });
          }
        }
      } else {
        d.style.backgroundImage = `url(${p.pcSrc})`;
      }
    } else if (t) {
      d.style.backgroundImage = `url(${t.src})`;
    }
    /* 混合模式由**部件自己的** OperationBlendTarget 决定：
       bl=1 加色（plus-lighter）、bl=0 普通混合。
       以前整层写死 plus-lighter，把 bl=0 的白雾滤镜（空想）也加色叠成纯白。
       层已经不是 stacking context，所以部件级混合能与舞台背景真正相加。 */
    if (p.bl === 1) d.style.mixBlendMode = 'plus-lighter';
    host.appendChild(d);
    return { d, p, map, cell, t, tint, vc, lastOp: -1, lastCi: -1 };
  }).filter(Boolean);

  /* Instance 子 prefab：火柱/爆発/音楽 的部件没有自己的 Cell，而是实例化
     另一个 prefab 里的一段动画（'ins_fire_01' / 'line' / 'slash' …）。
     **cellmap 用子 prefab 自己的**（导出时已带 sub.maps）：火柱的火焰是
     adv_effect_attack_fire_pillar_0 的 29 格，音楽的光带是 adv_effect_music_cell
     的 10 格 —— 以前一律套宿主的 cellmap，格子号对不上就取到别人的图。 */
  const iitems = [];
  for (const inst of instances) {
    const maps = (inst.sub && inst.sub.maps) || a.maps;
    for (const sp of inst.sub.parts) {
      const map = maps[sp.m];
      const cell = map && map.cells[sp.c];
      const t = map && map.tex;
      const d = document.createElement('i');
      d.className = 'fxp';
      d.style.opacity = '0';        // 同上：第一帧之前交给 draw 写真实值
      /* 子部件也要吃 PartsColor：火焰/音乐光带的贴图本身是灰白的，
         火色＝红→黄 的四角顶点色（导出时已烘成 fx_pc_*.webp）。 */
      const vc = (sp.pc && sp.pc.length) ? sp.pc[0] : null;
      const tint = !!(t && vc && !sp.pcSrc && sp.pcm && sp.pcm.bound === 1
        && (sp.pcm.op === 3 || sp.pcm.op === 0));
      if (tint) {
        /* 同样预乘成着色贴图（不再用 mask）；没算好先不画，避免跳色 */
        const rec = tintedSprite(t.src, [vc[0], vc[1], vc[2]]);
        d.style.backgroundImage = rec.url ? `url(${rec.url})` : 'none';
        d.style.backgroundRepeat = 'no-repeat';
        if (!rec.url) rec.waiters.push(d);
      } else if (sp.pcSrc) {
        d.style.backgroundImage = `url(${sp.pcSrc})`;
      } else if (t) {
        d.style.backgroundImage = `url(${t.src})`;
      }
      if (sp.bl === 1) d.style.mixBlendMode = 'plus-lighter';
      // 子动画优先级：Instance 部件的 Priority 已经决定了外层顺序，
      // 这里再按部件自身 Priority 细分（火焰的层次感全靠它）
      d.style.zIndex = String(sp.pr || 0);
      host.appendChild(d);
      iitems.push({ d, inst, p: sp, map, cell, t, tint, vc });
    }
  }

  /* ===== 粒子层（de_ef_* 发射器）=====
     キラキラ 的完整演出 = 彩虹四芒星图片部件 + 圆形泡泡粒子 + 细十字星芒粒子；
     空想 / 空想／黒 的**画框边缘雾**同样来自 4 个 tama 发射器
     （effect_3~6 各自平移/旋转到上/下/右/左四条边外沿，粒子挂在画外，
     只有半个身位探进画面 → 四条边浓、中心干净），以前因为发射器坐标算错
     （旋转后整条线抬高半屏）只剩右上角，才被整层跳过；坐标修好后按数据播。 */
  /* 粒子走 canvas 还是 DOM：
     冷気/邪気/煙 这类"几百颗大四边形"的 prefab，DOM 版创建要阻塞主线程 ~170ms
     （484 个节点 + 484 张贴图），之后每帧还要重绘 480 层 —— 用户看到的
     「闪一下、然后画面整个变样」就是这段卡顿（2026-11-11 实测 165~175ms）。
     引擎本身是在一张 surface 上逐个 drawImage，这里用 canvas 复刻：
     创建几乎零成本，一帧 480 次 drawImage 约 1~2ms。
     带 Instance 子 prefab 的（火柱/爆発/音楽）仍走 DOM，那里部件是图片、不是粒子。 */
  const useCanvas = FX_CANVAS && !(a.instances && a.instances.length) && emitters.length > 0;
  /* 官方 all_ef 用的同一张椭圆遮罩：粒子层也按它挖空中心。
     游戏录屏里冷気/邪気的雾只在下缘和四周，人物始终清楚；播放器以前把
     几百颗 448×192 的大云堆到中间，看着就是「一团团雾在动、快遮住立绘」。
     在 canvas 内用 destination-in 裁（不用 CSS mask：mask 会建 stacking
     context，把 plus-lighter 的混合组关在层内，加色就穿不到舞台背景）。 */
  const mistMaskUrl = MIST_PREFABS.has(prefab)
    ? ((a.parts[0] && a.parts[0].pcm && a.parts[0].pcm.mask) || null) : null;
  /* 把官方遮罩缩成 64×36 的 alpha 表，逐颗查表衰减（一次生成、同步可用）。
     不能每帧对 5 块 1280×720 canvas 做 destination-in：那是每帧 460 万像素的
     额外全屏合成，实测会让某些帧来不及上屏，出现 ±50 的单帧明暗跳变。 */
  const mistAlpha = (() => {
    if (!mistMaskUrl) return null;
    const tab = new Uint8Array(64 * 36);
    const im = spriteImage(mistMaskUrl);
    if (im) {
      const c = document.createElement('canvas'); c.width = 64; c.height = 36;
      const g = c.getContext('2d');
      g.drawImage(im, 0, 0, 64, 36);
      const px = g.getImageData(0, 0, 64, 36).data;
      for (let i = 0; i < 64 * 36; i++) tab[i] = px[i * 4 + 3];
    } else {
      /* 图没解码完时先用同形状的椭圆渐变顶住（中心 0、四周 255），
         避免第一帧不裁、下一帧突然挖空造成闪；重建时会换成官方像素。 */
      for (let y = 0; y < 36; y++) for (let x = 0; x < 64; x++) {
        const nx = (x - 31.5) / 31.5, ny = (y - 17.5) / 17.5;
        const r = Math.hypot(nx, ny * 1.5);
        tab[y * 64 + x] = Math.round(255 * clamp01((r - 0.42) / 0.58));
      }
    }
    return tab;
  })();
  /* 游戏录屏 f105(3.50s) 的 B−R 增量 4×4 归一化：四角浓、中心淡，
     上左角最高，底排并不比顶排亮（见 corner_compare.py）。
     播放器此前把官方 mask（底排 alpha 全 255）和 kiri 发射器
     (pos.y=-400~-120) 两层加色都堆在下半部，又乘上 0.32 基础雾量，
     结果下部饱和过曝、四个角反而看不见雾。 */
  const MIST_GW = [1.00, 0.49, 0.51, 0.77,
                   0.56, 0.31, 0.36, 0.65,
                   0.71, 0.35, 0.48, 0.88,
                   0.81, 0.43, 0.32, 0.87];
  const gwAt = (u, v) => {           // 双线性插值，避免 4×4 网格出现方格边界
    const fx = Math.min(2.999, Math.max(0, u * 3));
    const fy = Math.min(2.999, Math.max(0, v * 3));
    const x0 = Math.floor(fx), y0 = Math.floor(fy), tx = fx - x0, ty = fy - y0;
    const G = (x, y) => MIST_GW[Math.min(3, y) * 4 + Math.min(3, x)];
    return (G(x0, y0) * (1 - tx) + G(x0 + 1, y0) * tx) * (1 - ty)
         + (G(x0, y0 + 1) * (1 - tx) + G(x0 + 1, y0 + 1) * tx) * ty;
  };
  /* 按**云覆盖的区域**判定，而不是按粒子中心点。
     冷気/邪気的云从中心出生、随寿命长到几百甚至上千 px，中心点始终在
     画面中央（官方遮罩这里 alpha=0）；只查中心点会把这些大云整颗砍掉，
     上半部因此「完全没有动效」（2026-11-11 用户反馈 + motion_check.py 量化：
     稳态相位上半帧间变化 0.0、下半 2.3~3.6）。
     取中心与上下左右 0.65 半径共 5 点的平均：小云仍被中心挖空（立绘清晰），
     伸到画框区的大云才可见，于是四个方向都有雾在往外溢。 */
  const edgeAt = (px, py, rx, ry) => {
    if (!mistAlpha) return 1;
    /* 采样云矩形的四角（不是十字）：左右两侧的云是靠它们的上/下角
       伸进画框区的，只取上下左右中点会把左右整列判成 0。 */
    const pts = [[0, 0], [-0.7, -0.7], [0.7, -0.7], [-0.7, 0.7], [0.7, 0.7]];
    let s = 0;
    for (let i = 0; i < pts.length; i++) {
      const qx = px + pts[i][0] * rx;
      const qy = py + pts[i][1] * ry;
      const x = Math.max(0, Math.min(63, Math.round((qx + 640) / 20)));
      const y = Math.max(0, Math.min(35, Math.round((360 - qy) / 20)));
      /* `pcm.mask` 是 all_ef 那张 124×76 画框图的遮罩，4×4 alpha
         左右中部只有 55~137、上下边缘才 255 —— 它只该约束画框图层。
         拿它直接砍粒子，左右两列就整片没动效（用户 2026-11-11 反馈）。
         这里取「官方遮罩」与「中心挖空的柔和椭圆」的较大值：官方数据
         保留，左右中部由椭圆补回；空间浓淡仍由游戏标定的 gwAt 决定。 */
      const official = mistAlpha[y * 64 + x] / 255;
      const rr = Math.hypot(qx / 640, qy / 360);
      const ellipse = clamp01((rr - 0.28) / 0.52);
      s += Math.max(official, ellipse)
        * gwAt(clamp01((qx + 640) / 1280), clamp01((360 - qy) / 720));
    }
    return s / pts.length;
  };
  const cvSet = [];
  const pitems = [];
  for (const em of emitters) {
    /* 同时在场的粒子数 = DurationEmitter / Interval × CountParticleEmit
       （CountParticleMax 只是**上限池大小**，不是数量）。
       以前直接把 CountParticleMax 当数量：煙 的 max=64、Interval=10、
       DurationEmitter=120，而 DurationParticle=180 帧比循环还长 ——
       于是 64 颗永远在场（两层 128 颗），白烟糊满整屏、背景全被盖住
       （2026-11-10 用户对比截图：游戏里是薄薄一层，左下来、右上走）。 */
    const _dur = em.duration || 0;
    const _itv = Math.max(1, em.interval || 1);
    const _ce = Math.max(1, em.cntEmit || 1);
    let n = _dur > 0 ? Math.round(_dur / _itv) * _ce : (em.max || em.pat.length || 1);
    n = Math.max(1, Math.min(n, em.max || n, 140));
    const cycle = em.duration || (n * em.interval) || 1;
    const cell = (em.cells && em.cells[em.cell]) || null;
    const cw = cell ? cell[2] : em.w, ch = cell ? cell[3] : em.h;
    const lay = em.layout || [1, 1];
    /* 加色粒子**不能一颗一个 mix-blend-mode**：冷気/邪気 一层 300~400 颗，
       浏览器要把它们逐个和背景混合，实测到一定数量后多出来的混合层会被丢掉
       （同一层里那块满屏冷色滤镜因此几乎不参与混合 —— 邪気 的紫雾比游戏淡一大截），
       悬停重绘时还会整层画黑。改成"每层发射器一个 0×0 的组容器"，
       混合只做一次，组内粒子走普通合成。 */
    let blendWrap = null;
    if (em.bl === 1 && !useCanvas) {
      blendWrap = document.createElement('i');
      blendWrap.className = 'fxp-grp';
      host.appendChild(blendWrap);
    }
    /* canvas 模式：每个发射器一块 1280×720 的画布，加色发射器直接
       globalCompositeOperation='lighter'（就是引擎的加色）。 */
    let cv = null;
    if (useCanvas) {
      cv = document.createElement('canvas');
      cv.className = 'fxp fxp-canvas';
      cv.width = 1280;
      cv.height = 720;
      cv.dataset.bl = em.bl === 1 ? '1' : '0';
      /* 混合与 DOM 版一一对应：加色发射器 = 整块画布 plus-lighter（组级），
         画布内部 source-over —— 这样加色强度与之前校准过的效果一致
         （实测：改成画布内部 lighter 会让紫雾暗约 12%，与游戏截图不符）。 */
      if (em.bl === 1) cv.style.mixBlendMode = 'plus-lighter';
      /* 画布初始是空的，不存在"先用默认值画一帧"的问题，直接可见即可；
         整体淡入仍由 host.style.opacity 负责。 */
      host.appendChild(cv);
      cvSet.push(cv);
    }
    for (let j = 0; j < n; j++) {
      let d = null;
      if (!useCanvas) {
        d = document.createElement('i');
        d.className = 'fxp fxp-particle';
        /* 同上：默认不透明度是 1，不先置 0 的话第一帧就是"480 颗全亮"，
           Time=0 的邪気/冷気 会闪一下再恢复（用户实测）。 */
        d.style.opacity = '0';
      }
      /* TableSeedParticle 是游戏自带的**每粒子**种子（198 个 emitter 全都有），
         以前我用自造的 fxRand(em.seed, j*8+k) 序列，随机数来源不该自己编。 */
      const seed = em.seeds && em.seeds[j] != null ? em.seeds[j] : em.seed;
      const r = k => fxRand(seed, k);
      let cx = em.pos[0] + r(0) * em.pos[2];
      let cy = em.pos[1] + r(1) * em.pos[3];
      const s0 = em.scale[0] + r(2) * em.scale[1];       // ScaleRateStart
      // ScaleRateEnd 全 0 表示「没有结束缩放」，不是缩到 0 倍
      const s1 = (em.sre[0] || em.sre[1])
        ? em.sre[0] + r(2) * em.sre[1] : s0;
      const ang = (em.angle[0] + r(3) * em.angle[1]) * Math.PI / 180;
      const spd = em.speed[0] + r(4) * em.speed[1];
      const rot = em.rot[0] + r(5) * em.rot[1];
      /* col 是 **ColorVertex{Main,Sub}**（8 个数：RGBA 基色 + RGBA 随机幅度）。
         注意 CPE 里所有 {Main,Sub} 对都是「Main + rand*Sub」这个约定 ——
         Angle{0,360}、Speed{2,3}、Rotation{-180,371} 全都能对上；
         ColorVertex 同理：**每颗粒子出生随机取一个颜色，终生不变**，
         寿命内的淡入淡出由 AlphaFadeStart/End 负责，不是颜色渐变。
         （以前当成 ColorStart→ColorEnd 逐帧插值：冷気 alpha 0→0.5 会让整团雾
         越飘越黑、空想 的黑雾又自己淡掉，两处都对不上。）
         有颜色时走「贴图当 mask + 纯色底」；纯白基色且无幅度则用原贴图。 */
      const cA = em.col, cB = [em.col[4], em.col[5], em.col[6], em.col[7]];
      const cf = em.cvf || [0, 0, 0, 0, 0, 0, 0, 0];
      /* 只要 rgb 基值/随机幅度/波动幅度里有一项不是 1，就要染色 */
      let colored = [cA[0], cA[1], cA[2], cB[0], cB[1], cB[2], cf[0], cf[1], cf[2]]
        .some(v => Math.abs(v - 1) > 0.004);
      /* 每颗粒子的固定颜色 =
           ColorVertex{Main,Sub}            ← 基值 + 0~Sub 随机（Base 的随机范围）
           ± ColorVertexFluctuation{Main,Sub} ← **波动幅度**（这个字段以前整个没导出）
         冷気/邪気 的 de_ef_cold_P：
           ColorVertex      (1, 1, 1, a=0)
           ColorVertexFluct (0, 0.251, 0.502, a=1)
         → 白里随机偏蓝、且每颗随机透明度(0~1)。只读 ColorVertex 就得到
         "纯白 + 不透明"的 180 颗大云 → 正中下方一团白色过曝（用户反复反馈的）。
         只在 **ColorVertex.Main.a == 0** 的发射器上启用（冷気/邪気 的 de_ef_cold_P：
         alpha 完全由波动给 → 0~1 随机；rgb 向下偏 → 蓝白雾）。
         其它发射器（kiri 0.5、煙 1、tama 1）的 cvf 是默认值/另有语义，
         套上去会把 kiri 的蓝打散成绿（实测 bgc 变 rgb(168,230,114)），所以不动。 */
      /* `de_ef_cold_P`（冷気/邪気）的参数：
           ColorVertex Main(1,1,1, a=0)  Sub(0,0,0, a=1)  → 每颗 alpha 0~1
           Position(0,0)+(0,0)、Angle 0~360、Speed 0.1~1、pivot(64,180)
         以前整组关掉只剩 kiri 的下缘横带 → 用户看到「只有下方有雾」。
         这组才是**往四周扩散**的雾。Main.a=0 不是"关闭"，是"基值 0，
         由 Sub 随机给透明度"；以前做成白爆是因为尺寸又乘了一次
         ScaleRateStart（448px → 1150px）且没乘 pa。 */
      const coldP = cA[3] <= 0.002;
      const cr6 = r(6);
      let pc0 = clamp01(cA[0] + cB[0] * cr6);
      let pc1 = clamp01(cA[1] + cB[1] * cr6);
      let pc2 = clamp01(cA[2] + cB[2] * cr6);
      const pa0 = clamp01(cA[3] + cB[3] * cr6);
      /* cold_P 染上本 prefab 的雾色：冷気青蓝 / 邪気紫蓝。
         纯白 + 加色 + Front 层会整屏过曝（实测纯白 13.4%）；
         染成数据里的雾色后亮度降到 ~0.7，也符合"两个只差颜色"。 */
      if (coldP) {
        colored = true;
        pc0 = mistCol[0]; pc1 = mistCol[1]; pc2 = mistCol[2];
      }
      /* 粒子可以**非等比**缩放：光柱是 cell 32×32 × size(0.2, 8.0)
         → 6.4×256 的细长条。图的缩放必须用同一个 kx/ky，
         否则宽只取到 kx 而背景用 ky 的尺寸，3px 的框里显示的是图集
         左上角一块，光柱/雾团整个看不见（2026-11-09 用户截图）。 */
      /* cold_P 的 ScaleStart(3.5,1.5)/(2.5,1.5) 已经是最终尺寸倍率，
         ScaleRateStart(2.5~3) 是**寿命内的生长倍率**（ScaleStart==ScaleEnd，
         只有 Rate 在变）。以前两者相乘 → 1150px 的云堆在一点 = 白色过曝团。
         其它发射器（煙/雪/tama 已对上游戏）保持原来的乘积读法。 */
      const kx = em.size[0] * (coldP ? 1 : s0) * lay[0];
      const ky = em.size[1] * (coldP ? 1 : s0) * lay[1];
      /* cold_P 的官方 Position 是 Main(0,64)+Sub(0,0) 的**单点**，Angle 是
         0~360 全向。游戏里「整个椭圆面都是起点、向外溢」来自单点持续发射 +
         TablePatternOffset 错开出生帧 + 边长边飘的时间叠加；typetree 里
         并没有 Ellipse/Circle/PositionLayout 这类分布字段。
         以前自编成固定半径 R0 的圆环，于是只看到「中上/中下两个起点」。
         现在用官方 TableSeedParticle 的随机种子把出生点铺满 0~R0 的
         **整个椭圆面**（sqrt 保证面积均匀、ky/kx 给出官方 size 的椭圆比），
         方向仍取官方 Angle，draw 里的 Speed×age 自然把雾往四边外推。 */
      if (coldP) {
        const rr = Math.sqrt(clamp01(r(8)));
        /* 椭圆按**舞台尺寸**铺满（1280×720 的半轴 640/360）。
           以前用官方 pivot 推出的 R0=406 只覆盖中央一圈，而舞台四角距
           中心 734px，粒子根本到不了 —— 用户反馈「左边、左上角、右上角、
           右边几乎看不到动效」。用官方 TableSeedParticle 的面积均匀随机
           铺满整个舞台椭圆，四个边框才都有起点向外溢。 */
        cx += Math.cos(ang) * 640 * rr;
        cy += -Math.sin(ang) * 360 * rr;
      }
      const w = cw * kx, h = ch * ky;
      if (d) {
        d.style.width = `${w}px`;
        d.style.height = `${h}px`;
        /* 尺寸/取格两条分支共用；染色不再用 mask（见 tintedSprite 的注释：
           mask 在某些缩放下没吃到就会画出实心方块），改成预乘好的贴图直接当背景。 */
        d.style.backgroundSize = `${em.w * kx}px ${em.h * ky}px`;
        d.style.backgroundPosition =
          `${-(cell ? cell[0] : 0) * kx}px ${-(cell ? cell[1] : 0) * ky}px`;
        if (colored) {
          const rec = tintedSprite(em.src, [pc0, pc1, pc2]);
          if (rec.url) {
            d.style.backgroundImage = `url(${rec.url})`;
          } else {
            /* 预乘还没算好（冷缓存第一次遇到）：这一颗**先不画**，等算好再挂。
               以前是"先挂原贴图、下一帧再换着色版" —— 两帧颜色差一大截，
               用户看到的就是「上一帧是这个效果、下一帧就变了」的闪（2026-11-11）。
               不画只丢一帧，不会跳色。 */
            d.style.backgroundImage = 'none';
            rec.waiters.push(d);
          }
        } else {
          d.style.backgroundImage = `url(${em.src})`;
        }
        // 旋转/缩放中心在下面按 sprite 的 pivot 写（见 pitems.push 之前）
        // 混合交给组容器（见上），这里不再逐个设 mix-blend-mode
        (blendWrap || host).appendChild(d);
      }
      /* 每颗粒子的**相位**按 TablePatternOffset 错开（`patOff` 就是 pattern
         起始帧，image_loop 是 0,2,4,…254）。
         以前让 128 颗全部同帧出生：整层每 4 秒同步淡入淡出，看起来是一大团
         白色色块在呼吸/翻涌。错开相位后总量恒定，才是原作那种**静止**白雾
         （2026-11-09 用户对比截图：游戏内是静态的画框式白雾）。
         loop 按周期取模循环；一次性特效直接当出生时刻用（attack 系的
         patOff 几乎全 0，仍是同时爆开；冷気是 0,1,2,… 逐颗进场）。 */
      const po = em.patOff && em.patOff[j] != null ? em.patOff[j] : 0;
      const phase = loop ? po % Math.max(1, cycle) : po;
      /* pivot：cell[4]/[5] 是 sprite 内锚点（源像素）。中心 pivot 时
         ox=w/2、oy=h/2 与旧的中心对齐一致；cold 大雾的 pivot=(64,180)
         超出 cell（高128）—— sprite 实际显示在发射点**下方**，
         这就是原作白光落在胸口而不是盖住头的原因。 */
      const pvx = cell ? cell[4] : cw / 2;
      const pvy = cell ? cell[5] : ch / 2;
      /* 旋转/缩放中心必须是 **sprite 的 pivot**（= 发射点所在的那一点），
         不是元素中心。cold_P 的 pivot=(64,180) 落在 cell（128×128）**下方
         52px 之外**：引擎里每颗云绕这个锚点旋转（Rotation 随机 −180~180），
         于是以锚点为圆心、半径 ≈52×缩放(2.5~3.5)=130~180px 排成一圈 ——
         这就是游戏里「中心区域清楚、周围一圈雾在转」的来历。
         以前写死 center，360 颗云全叠在锚点上 → 冷気 正中一坨白团、
         邪気 只剩屏幕下方那条 kiri。pivot 居中的（tama 16,16）行为不变。
         ox/oy 就是"发射点"在元素内的坐标，正好直接当 transform-origin。 */
      /* cold_P 的 Rotation=0、不做绕 pivot 自转，云要以**挂点**为中心，
         才能按 Angle 均匀分布在画面四周。若沿用"发射点 = pivot"的换算，
         pivot(64,180) 在 cell 下方，会让整圈云往下偏 170px，
         结果只剩下方有雾（用户看到的就是这个）。 */
      const ox = coldP ? w / 2 : pvx * kx;
      const oy = coldP ? h / 2 : (ch - pvy) * ky;
      if (d) {
        d.style.transformOrigin = coldP
          ? '50% 50%' : `${pvx * kx}px ${(ch - pvy) * ky}px`;
      }
      pitems.push({
        d, cv, cell, kx, ky, tint: colored ? [pc0, pc1, pc2] : null,
        em, cA, cB, colored, coldP, pc: [pc0, pc1, pc2], pa: pa0,
        cx, cy, w, h, s0, s1, ang, spd, rot, phase,
        ox, oy,
        birth: em.delay,
        cycle, fx: em.fx || [0, 1],
        // 挂点部件的逐帧 RateOpacity（常数或数组，见 build_particles 的 pop）
        pop: em.pop,
      });
    }
  }

  /* 常驻雾不呼吸：all_ef 的 RateOpacity 是 61 帧脉冲（jaki_loop 0.542↔0.897、
     cold_loop 同），loop 型照帧循环就会每 2 秒整层明暗一次 —— 用户说的
     「段落感 / 跳屏感」。游戏录屏里冷気是持续平滑的一层，所以 loop 型取该
     轨道的均值常驻；粒子时钟照常循环，雾仍在流动。hold 型(cold)本来就停在
     峰值帧，保持原样。 */
  const mistOp = (MIST_PREFABS.has(prefab) && isLoopPrefab(prefab)
    && a.parts.length && a.parts[0].k && a.parts[0].k.length)
    ? a.parts[0].k.reduce((s, x) => s + clamp01(x[5] || 0), 0) / a.parts[0].k.length
    : null;
  const fps = a.fps || 30;
  const frameMs = 1000 / fps;
  const totalMs = a.n * frameMs;
  /* seekTo 重放期间 ST.fastForward=true：把时间原点前移一整段，
     一次性特效直接停在末帧、持续型接着循环相位，不再从头入场。
     以前每切一次上一句/下一句都会把光效重播一遍（2026-11-09 反馈）。

     持续型还要再往前**预热一个发射周期**：游戏里冷気/邪気 一出现就是
     "已经飘了一会儿"的稠密状态（剧本里是一条 ShowScreenEffect 挂着不动），
     不是从空场淡入。以前从 0 开始演，前 1~2 秒只有零零几颗，
     看起来就是"弹出来一团"（2026-11-10 用户反馈）。 */
  /* hold 型停在**峰值帧**：从发射器挂点的 RateOpacity 数组里取最大值的帧。
     cold 是第 15 帧（pop=1.0，此时 all_ef=0.751，接近它自己的峰值 0.76）。 */
  let holdPeak = 0;
  if (hold) {
    let best = -1;
    for (const em of emitters) {
      if (!Array.isArray(em.pop)) continue;
      em.pop.forEach((v, i) => { if (v > best) { best = v; holdPeak = i; } });
    }
  }
  let prewarmMs = 0;
  /* 预热只给**粒子时钟**，不能挪动画时间原点。 */
  if (loop || hold) {
    for (const em of emitters) {
      const cyc = Math.round((em.duration || 0) * frameMs);
      if (cyc > prewarmMs) prewarmMs = cyc;
    }
    prewarmMs = Math.min(prewarmMs, 6000);
  }
  const t0 = performance.now() - (ST.fastForward ? totalMs : (loop ? prewarmMs : 0));
  const handle = { host, raf: 0 };
  fxHandles.set(which, handle);

  const draw = ts => {
    if (fxHandles.get(which) !== handle || !host.parentNode) return;
    const elapsedMs = Math.max(0, ts - t0);
    /* 一次性特效播完就撤掉整层。停在末帧是不对的：末帧 RateOpacity 不为 0，
       粒子也停在可见帧，画面会一直挂着白色羽毛/三角形（2026-11-09 用户截图）。
       **状态型（cold）例外**：它播一次后要停在末帧等剧本清除 —— 末帧上
       雾已经衰减到看不见、滤镜还留着，正好是玩家看到的"雾先散、滤镜后散"。 */
    if (!loop && !hold && elapsedMs >= totalMs) {
      fxHandles.delete(which);
      if (screffLive[which] === host) stopScreff(which, 0);
      return;
    }
    const elapsed = elapsedMs / frameMs;
    /* hold 型：部件帧固定停在峰值，不再走 0→1→0 脉冲（不闪）；
       粒子时钟在下面照样循环，所以雾持续向外扩散。 */
    const f = loop ? (Math.floor(elapsed) % a.n)
      : hold ? holdPeak
      : Math.min(Math.floor(elapsed), a.n - 1);
    for (const it of items) {
      const k = it.p.k[f];
      if (!k) continue;
      const sx = Math.abs(k[2]), sy = Math.abs(k[3]);
      const d = it.d;
      /* 逐帧 cell：电磁ウェブ的闪电部件每 2~3 帧换一格图（TableValue 有 13 项），
         以前只取第一帧，整段是一张静止闪电。ck 只在真正会变的部件上出现。 */
      const ci = it.p.ck ? it.p.ck[f] : it.p.c;
      const c = (it.map && it.map.cells[ci]) || it.cell;
      d.style.width = `${(c ? c[2] : 0) * sx}px`;
      d.style.height = `${(c ? c[3] : 0) * sy}px`;
      const t = it.t;
      if (it.tint) {
        /* 「着色贴图」也要按 cell 定位（图集里只取这一格）；不再用 mask */
        d.style.backgroundSize = `${t.w * sx}px ${t.h * sy}px`;
        d.style.backgroundPosition =
          `${-(c ? c[0] : 0) * sx}px ${-(c ? c[1] : 0) * sy}px`;
      } else if (t) {
        if (it.p.pcSrc && ci === it.p.c) {
          /* pcSrc 是导出时**按 cell 裁好**的独立小图（heal 的 obi 只有 16×10），
             直接铺满元素即可。以前用 `cell宽 × sx` 算尺寸，元素宽度和
             backgroundSize 走的是两条路径，实测会出现 2484 vs 1908 的偏差
             → 图被放大 1.3 倍、显示的是其中一块（2026-11-09 审计）。 */
          d.style.backgroundSize = '100% 100%';
          d.style.backgroundPosition = '0px 0px';
        } else {
          d.style.backgroundSize = `${t.w * sx}px ${t.h * sy}px`;
          d.style.backgroundPosition =
            `${-(c ? c[0] : 0) * sx}px ${-(c ? c[1] : 0) * sy}px`;
        }
      }
      // Unity 坐标 y 向上为正；pivot 让部件的第 (px,py) 像素落在 (x,y)
      d.style.transform =
        `translate(${k[0] - (c ? c[4] : 0) * sx}px, ` +
        `${-k[1] - (c ? c[5] : 0) * sy}px) rotate(${k[4]}deg)`;
      /* 逐帧 RateOpacity 原值 × 顶点色 alpha（MUL 部件的单色由 backgroundColor 承担，
         它的 alpha 在 p.pc[0][3] 里）。加色强度由部件级 plus-lighter 决定。 */
      const vca = it.tint && it.vc ? it.vc[3] : 1;
      /* 遮罩没解码完 → 这一帧不画；刚解码完 → 250ms 内淡入（不要一次跳到位） */
      let partOp = mistOp != null ? clamp01(mistOp * vca) : clamp01(k[5] * vca);
      if (d.dataset.maskPending) {
        partOp = 0;
      } else if (d.dataset.maskAt) {
        const a = (performance.now() - num(d.dataset.maskAt, 0)) / 250;
        if (a >= 1) delete d.dataset.maskAt; else partOp *= Math.max(0, a);
      }
      d.style.opacity = String(partOp);
    }
    for (const it of iitems) {
      const hk = it.inst.host[Math.min(f, it.inst.host.length - 1)];
      /* 子动画按 Instance.RateTime 播放（1.0 = 原速、2.0 = 双倍）。
         以前写死 f % sub.n，rate≠1 的那几条（火 00 的 1.5/2.0）节奏是错的。 */
      const rate = it.inst.rate || 1;
      const sf = Math.floor(f * rate) % it.inst.sub.n;
      const k = it.p.k[sf];
      if (!hk || !k) continue;
      const c = it.cell, t = it.t, d = it.d;
      const sx = Math.abs(k[2]) * Math.abs(hk[2]);
      const sy = Math.abs(k[3]) * Math.abs(hk[3]);
      const x = hk[0] + k[0] * hk[2];
      const y = hk[1] + k[1] * hk[3];
      d.style.width = `${(c ? c[2] : 0) * sx}px`;
      d.style.height = `${(c ? c[3] : 0) * sy}px`;
      if (it.tint) {
        d.style.backgroundSize = `${t.w * sx}px ${t.h * sy}px`;
        d.style.backgroundPosition =
          `${-(c ? c[0] : 0) * sx}px ${-(c ? c[1] : 0) * sy}px`;
      } else if (t) {
        if (it.p.pcSrc) {
          /* pcSrc 是**按 cell 裁好**的独立小图（火焰 102×113、光带 128×48）。
             用图集尺寸（t.w=512）当 backgroundSize 会把小图再放大 5 倍，
             元素里只看到左上角一小块 —— 火焰整根几乎看不见。
             这点在宿主部件里已经踩过一次，子 prefab 部件同样要按 100% 铺满。 */
          d.style.backgroundSize = '100% 100%';
          d.style.backgroundPosition = '0px 0px';
        } else {
          d.style.backgroundSize = `${t.w * sx}px ${t.h * sy}px`;
          d.style.backgroundPosition =
            `${-(c ? c[0] : 0) * sx}px ${-(c ? c[1] : 0) * sy}px`;
        }
      }
      d.style.transform =
        `translate(${x - (c ? c[4] : 0) * sx}px, ` +
        `${-y - (c ? c[5] : 0) * sy}px) rotate(${k[4] + hk[4]}deg)`;
      d.style.opacity = String(clamp01(k[5] * hk[5]
        * (it.tint && it.vc ? it.vc[3] : 1)));
    }
    /* 粒子时间轴用**累计时间**，不能用取模后的动画帧：
       image_loop 的动画只有 61 帧，粒子 DurationParticle=120，
       用 f%61 的话 age 永远停在 0~60，全卡在刚出生（alpha=0）的状态。
       `Effect.FrameStart` 的单位是**毫秒**（illumination=15、cold=120、
       image_loop=180），以前当帧用 → image_loop 要等 6 秒才开始撒粒子。 */
    /* canvas 模式：每帧先把画布清空、把原点挪到舞台中心（粒子的 px/py
       是「相对中心、y 向上为正」的坐标，与 DOM 版完全同一套数值）。 */
    for (const cvEl of cvSet) {
      const g = cvEl.getContext('2d');
      g.setTransform(1, 0, 0, 1, 0, 0);
      g.clearRect(0, 0, 1280, 720);
      g.globalCompositeOperation = 'source-over';   // 加色交给画布本身（见创建处）
      g.translate(640, 360);
    }
    for (const it of pitems) {
      const em = it.em;
      /* hold 型：粒子一出现就是"已经跑了一个周期"的稠密状态（预热），
         时钟**继续循环**，雾才会像邪気那样持续往四周扩散；
         冻结在片尾会让粒子全部 age 超限 → 整层消失（旧 bug）。 */
      const pf = still ? (prewarmMs / frameMs)
        : Math.max(0, (elapsedMs - it.fx[0]) * it.fx[1]) / frameMs
          + (hold ? prewarmMs / frameMs : 0);
      let age;
      // 持续型（循环 + hold）都按发射器周期循环粒子
      if (loop || hold) {
        age = ((pf + it.phase) % it.cycle) - it.birth;
        if (age < 0) age += it.cycle;         // 发射器按 duration 循环
      } else {
        /* 一次性：patOff 是**出生时刻**，不取模 —— 冷気 128 颗按 0,1,2,… 逐帧
           进场，以前全 0 = 全部同帧出生，整团白雾一起亮一起灭（闪屏）。 */
        age = pf + it.phase - it.birth;
      }
      if (age < 0 || age >= em.life) {
        if (it.d) it.d.style.opacity = '0';
        continue;
      }
      const t = age / em.life;
      // AlphaFadeStart / End：前段淡入、后段淡出
      const fi = em.fade[0] > 0 ? t / em.fade[0] : 1;
      const fo = em.fade[1] > 0 ? (1 - t) / em.fade[1] : 1;
      /* 挂点部件的 RateOpacity 也要乘进来（数据里有 px）：
         空想四个边线 0.5 / 空想／黒 0.2 / 冷気 是 0→1→0 的淡入淡出。
         数组按**当前动画帧**取（c.k 是逐帧的，不是粒子寿命）。 */
      const pop = Array.isArray(it.pop)
        ? clamp01(it.pop[Math.min(f, it.pop.length - 1)] ?? 1)
        : clamp01(it.pop == null ? 1 : it.pop);
      /* alpha 三件事相乘：AlphaFade 的淡入淡出 × 粒子自己的 ColorVertex.a ×
         挂点部件的 RateOpacity(pop)。**颜色恒定的粒子也必须乘 `pa`** ——
         以前只有"染色"分支乘，纯白粒子（冷気/邪気 的白云、煙 的灰云）
         把 ColorVertex.a（0~1 的随机值）整个丢掉，全部按不透明画 → 白爆。 */
      const alpha = clamp01(Math.min(1, fi, fo) * (it.pa == null ? 1 : it.pa) * pop);
      /* opacity 等 px/py 算完再写：mist 特效要按官方遮罩的椭圆 alpha 逐颗衰减。 */
      /* Speed 沿 Angle 发射 + GravityDirectional 加速（雪/烟/火星的运动全靠它）。
         Angle 的原点在 +X，但**y 轴方向是屏幕系（向下为正）**：煙 的发射器
         Angle=300 起、挂点又放在画面下缘之外，只有把 300° 当成"右上"，
         白烟才会从下方飘满整屏；按数学系(上为正)读成"右下"就全飘出画面
         （2026-11-10 用户对比：游戏是满屏白烟，我们只有左下一点点）。
         GravityDirectional 仍是 Unity 的 y 向上为正（雪 -0.02 下落、光柱 +1.5 上升）。 */
      /* 方向直接用官方 Angle：cold_P 是 0~360 全向，且出生点已按同一个
         角度铺满椭圆面，速度同向就自然是从中心往四边外溢，不需要再
         自编一个「径向」方向去覆盖数据。 */
      const dx = Math.cos(it.ang) * it.spd * age;
      const dy = -Math.sin(it.ang) * it.spd * age;
      const gx = em.grav[0], gy = em.grav[1];
      let px = it.cx + dx + 0.5 * gx * age * age;
      let py = it.cy + dy + 0.5 * gy * age * age;
      /* GravityPointPosition / Power：点重力，把粒子吸向该点。
         attack_water 的发射器 Position.y = -768（在画面下方之外），
         全靠 Power=70 把它们拉回画面中心，少了这一项水花永远看不见
         （2026-11-09 用户截图：第21句「ぱしゃっ」没有蓝白爆散）。 */
      const gpx = em.grav[2], gpy = em.grav[3], gpw = em.grav[4];
      if (gpw) {
        const vx = gpx - it.cx, vy = gpy - it.cy;
        const d = Math.hypot(vx, vy) || 1;
        const a = 0.5 * gpw * age * age;
        px += (vx / d) * a;
        py += (vy / d) * a;
      }
      // ScaleRateStart → ScaleRateEnd 随时间插值。
      // cold_P：起始尺寸已是 ScaleStart，寿命内长到 ×ScaleRateStart（2.5~3），
      // 配合 Angle 方向的位移，就是"雾团一边变大一边往屏幕外扩散"。
      const sc = it.s0 + (it.s1 - it.s0) * t;
      const ratio = it.coldP
        ? 1 + (Math.max(1, it.s0) - 1) * t
        : (it.s0 > 0 ? Math.max(0.02, sc / it.s0) : 1);
      /* 按云**实际覆盖的范围**判定可见度（ratio 是寿命内生长倍率）：
         小云仍被中心挖空、伸到画框区的大云才可见，四个方向都有动效。 */
      const vis = alpha * edgeAt(px, py, it.w * ratio / 2, it.h * ratio / 2);
      if (it.d) it.d.style.opacity = String(vis);
      /* 旋转：个体初始角（Rotation.Main + rand*Sub）恒定。
         以前这里还加了 `RotationFluctuationRateTime × t` —— 那个字段是
         「波动的时间参数」，不是角度，混进角度里等于自造旋转。 */
      if (it.cv) {
        /* canvas：pivot 落在 (px,-py)，绕它旋转/缩放后画这一格。
           与 DOM 版 transform:translate(px-ox,-py-oy) rotate() scale() 等价。 */
        const g = it.cv.getContext('2d');
        const img = it.tint ? tintCanvas(em.src, it.tint) : spriteImage(em.src);
        if (img) {
          const c = it.cell || [0, 0, it.w, it.h, 0, 0];
          g.globalAlpha = vis;
          g.save();
          g.translate(px, -py);
          g.rotate(it.rot * Math.PI / 180);
          g.scale(ratio, ratio);
          /* 与 CSS 版**逐字对应**：元素窗口 = w×h，左上角在 (-ox,-oy)；
             背景图按 em.w*kx × em.h*ky 缩放，左上角是「窗口左上角 +
             backgroundPosition(-cell*k)」，超出窗口的部分裁掉。
             以前只写 -cell*k、漏掉窗口原点 -ox/-oy，整批粒子被推到窗口右下、
             裁得只剩底部一条雾（就是用户说的"全是方块、而且向右飘"）。
             也不能改成按 cell 取源矩形：cell 是图集坐标，em.src 是 256 的裁切版。 */
          g.beginPath();
          g.rect(-it.ox, -it.oy, it.w, it.h);
          g.clip();
          g.drawImage(img,
            -it.ox - c[0] * it.kx, -it.oy - c[1] * it.ky,
            em.w * it.kx, em.h * it.ky);
          g.restore();
        }
        continue;
      }
      it.d.style.transform =
        `translate(${px - it.ox}px, ${-py - it.oy}px) ` +
        `rotate(${it.rot}deg) scale(${ratio})`;
    }
    handle.raf = requestAnimationFrame(draw);
  };
  /* 先**同步**跑一遍：把第一帧的 opacity/transform 在首次上屏之前就写好。
     只排 rAF 的话，浏览器可能先用元素默认值（opacity:1、无变换）画一帧，
     Time=0 的邪気/冷気 就会"整层全亮闪一下"再恢复（2026-11-11 用户反馈）。
     draw 末尾自己会接着排下一帧，这里只是抢在首次绘制之前。 */
  draw(performance.now());
  return true;
}

function screffSpec(name, tex) {
  const dir = /／(左|右|上|下)$/.exec(name);
  const all = /全体/.test(name);
  if (/集中線/.test(name)) {
    return { mode: 'lines', dir: dir ? dir[1] : '', all, tint: null };
  }
  /* haze 兜底全删：这些 Name 都由 SCREFF_FX 走逐帧数据。
     以前 playScreenFx 在重放时跳过一次性特效后会落到这里，画出一片
     CSS 平铺的烟 —— 表现为「切句子才出现，正常播放什么都没有」
     （2026-11-09 用户反馈）。没有可渲染部件就留白，不拿别的图顶替。 */
  /* 攻击/回复/爆発类不再走 CSS 兜底：那几张是拿 ef_hit_ima 平铺的近似，
     画面很违和（2026-11-09 用户：「这些效果看起来都很幽默」）。
     它们现在由 SCREFF_FX + playScreenFx 按自己的逐帧数据播，
     没有可渲染部件时留白，不用别的素材顶替。 */
  return null;
}

function showScreenEffect(m, isBack) {
  const which = isBack ? 'back' : 'front';
  const nm0 = String(m.Name || '');
  /* 同一层已经挂着**同一个 prefab** 就直接沿用：不管这次是重放恢复
     还是重新执行到这条指令，都不要撤掉再建一遍。
     以前 stopScreff 先把层删掉，导致每切一次上一句/下一句（__testSeek
     都会重新执行该句）烟都要重新加载、重新淡入（2026-11-09 反馈）。 */
  const p0 = SCREFF_FX[nm0];
  const live0 = screffLive[which];
  if (nm0 && p0 && live0 && live0.parentNode &&
      live0.dataset.fx === p0) return;
  const nm = nm0;
  /* Name 为空 = **清除**（全站 10055 条），这条是对的：
     char_204 第 1 段里成对出现 —— 211 开启「集中線」、219 空 Name 关掉；
     338 再开、381 再关。曾经误判成"无名特效"并给它画了速度线，
     结果所有场景都被斜线糊住（2026-11-09 用户反馈）。
     清除必须在"换层交叠"之前单独处理：否则 stopScreff 被调两次，第二次
     会把正在淡出的旧层也删掉，反而清出一帧空画面。 */
  if (!nm) { stopScreff(which, ST.fastForward ? 0 : 120); return; }
  /* `Time` 是剧本给的淡入秒数，0 = 立即就位（全站 93% 的指令都是 0）。
     以前用 Math.max(..., 0.12) 强制最小淡入，把所有 Time=0 的常驻特效
     都做成"先空一帧再淡进来"，切句时就是一次可见闪屏（2026-11-10 用户反馈）。 */
  /* 冷気/邪気这一批视觉状态在游戏里是**切句瞬间一起提交**的；播放器按各自
     Time 逐条淡入，就会在句中留下「一个淡一个浓」的台阶（用户指出 jaki415
     f70 2.80s 淡 / f72 2.88s 浓）。这三个 prefab 同帧瞬时就位，背景染色也已由
     syncBatchMul 同步到同一帧；交叠淡出仍由下面的 cross 保留。 */
  const fadeIn = MIST_PREFABS.has(SCREFF_FX[nm] || '')
    ? 0 : Math.max(num(m.Time, 0), 0);
  /* 同层换特效时旧层**不能啪地消失**：剧本 `Time` 是新层的淡入秒数，旧层
     瞬间删掉就会留出整整一段空画面 —— char_415 p3#36 的
     「光る／紫／全体」(midx 375) → 「邪気」(midx 387) 就是这样闪的。
     让旧层在新层淡入期间交叠淡出，画面始终有一层在；游戏里这两条本来
     就是同一个连续演出的渐变，不是删掉重放。 */
  const cross = ST.fastForward ? 0
    : Math.min(1200, Math.max(240, Math.round(fadeIn * 1000)));
  stopScreff(which, cross);      // 同层永远只留一个 live，旧层本体留着淡出
  const A = window.__ADVFX || {};
  const tex = A.tex || {};
  /* 优先走包里的逐帧数据（da_/dc_adv_effect_*），按帧摆部件，不做任何近似。
     没有可渲染部件的（粒子型）返回 false，落到下面的素材实现。 */
  const prefab = SCREFF_FX[nm];
  if (prefab && playScreenFx(which, prefab, m, fadeIn)) return;
  const spec = screffSpec(nm, tex);
  /* 映射不到对应素材的 Name：不画。上面 stopScreff 已经撤掉同层旧特效，
     拿别的素材顶替等于改剧情（2026-11-09 用户要求）。 */
  if (!spec) return;
  const host = document.createElement('div');
  host.className = 'screff';
  /* 同 playScreenFx：重放恢复 + 剧本 Time=0 都直接就位，不演淡入。 */
  if (ST.fastForward || fadeIn <= 0) {
    host.style.opacity = 1;
  } else {
    host.style.opacity = 0;
    host.style.transition = `opacity ${fadeIn}s`;
  }

  if (spec.mode === 'lines') {
    /* 「集中線」：**形态**是从一点向外放射的线条（光芒四射），
       但**动态**不是整体放大/爆发 —— 用户 2026-11-09 明确纠正过：
       "游戏内是来回交替隐藏一部分线条做成的动效，你现在做的是以立绘为中心
        向外不断发射光线，太违和，而且太靠近立绘"。

       看数据就明白了：`da_adv_effect_concentration line_up` 只有一段
       8 帧 @30fps 的循环，逐帧唯一变化的是 part_1 的 Scaling X：
       **2.8 ↔ -2.8**（水平镜像来回），也就是线条在**原地翻面** ——
       视觉上就是一部分线隐去、另一部分亮起。所以这里按
       "多条三角射线 + 各条错开相位的镜像翻转" 来做，整体不缩放、不位移。

       锚点用指令自带的 PosX/PosY（全站 99% 是 640,360 = 屏幕正中），
       **不是立绘位置** —— 以前跟着立绘走，光就贴在人身上了。 */
    /* 用户确认过两张"对"的效果图（18:38:43 / 19:34:49），共同点是：
       **整张 ima_line 放射细线图铺满画面**，不是拿三棱贴图拼三角锥
       （三角锥被否过两次）。所以这里回到那张图。
       动态按数据来：8 帧 @30fps 循环里线条是"一部分隐去、另一部分亮起"，
       用**两层互补扇形遮罩交替明暗**实现（同一瞬间一半扇区亮、一半暗，来回交替），
       整体不缩放、不位移，不会出现"往外发射"的违和感。
       ⚠️ 必须保留 .screff 的 mix-blend-mode:screen —— 这张图是半透明白线，
       normal 混合下会被明亮背景吃掉（这就是我上次误判"看不见"的原因）。 */
    const src = tex.lineWide;
    const cx = num(m.PosX, 640);
    const cy = num(m.PosY, 360);
    if (src) {
      for (let k = 0; k < 2; k++) {
        const layer = document.createElement('i');
        layer.className = 'sln';
        /* 同一张图**叠两层**（层间用 screen 合成），而不是继续抬 alpha：
           原图线的 alpha 从中心 5 一路升到边缘 145，中间那段本来就淡，
           游戏里是靠加色叠加让它亮起来的。叠两层后不透明度变成 1-(1-a)²，
           中间段（a≈0.2）从 20% 提到 36%，边缘接近实心，
           而**曲线的单调性不变** → "越靠中心越透明"仍然保留。
           这样就不用再动导出参数、也不用编新系数。 */
        /* 同一张图**叠三层** + 层间 screen 合成，而不是抬高图片的 alpha：
           叠加后不透明度 = 1-(1-a)^3（a=0.16 → 41%，a=0.02 → 6%），
           中间段够亮，而 a=0 仍是 0 —— 尖端保持渐隐，不会被"切平"。
           这是唯一能同时满足"看得见"和"末端渐变到透明"的做法。 */
        layer.style.backgroundImage =
          `url(${src.src}), url(${src.src}), url(${src.src})`;
        layer.style.backgroundBlendMode = 'screen';
        /* **拉伸铺满**（100% 100%），不要用 auto 等比：
           图是 1024²，等比放大到 210% 后那圈线（半径 0.7R–1.0R）在垂直方向上
           整段跑到 720 的画面外，只剩左右和四角有一点点 —— 这就是"只有外围
           能看到光线"的真正原因（不是亮度，是几何位置）。
           拉成 16:9 后，空洞变成椭圆（水平 192 / 垂直 108），线正好铺满四周。 */
        layer.style.backgroundSize = '100% 100%';
        layer.style.backgroundPosition = 'center';
        layer.style.backgroundRepeat = 'no-repeat';
        layer.style.filter = 'contrast(1.15)';
        // 两层扇形互补：k=0 亮 0–4.5°，k=1 亮 4.5–9°；相位差半周期 → 交替
        const mask = `repeating-conic-gradient(from ${k * 4.5}deg at 50% 50%,` +
          ' #000 0deg 4.5deg, transparent 4.5deg 9deg)';
        layer.style.maskImage = mask;
        layer.style.webkitMaskImage = mask;
        layer.style.animationDelay = `${-k * 0.1335}s`;
        host.appendChild(layer);
      }
      // 锚点跟 PosX/PosY（屏幕坐标，99% 是正中心）
      host.style.transform = `translate(${cx - 640}px, ${cy - 360}px)`;
    } else {
      host.style.background = 'repeating-conic-gradient(from 0deg at 50% 50%,' +
        'rgba(255,255,255,.85) 0deg .6deg, rgba(255,255,255,0) .6deg 3deg)';
      const m1 = 'radial-gradient(circle at 50% 50%, transparent 28%, #000 52%)';
      host.style.maskImage = m1;
      host.style.webkitMaskImage = m1;
    }
    /* 不覆盖 mix-blend-mode：.screff 的 screen 是这张半透明白线图能看见的前提
       （normal 时线会被明亮背景吞掉，我上次就是在这里误判"图没用"的）。 */
    // 方向变体：只留一半画面（集中線／左 就是从左侧压过来）
    const d = spec.dir;
    if (d === '左') host.style.clipPath = 'inset(0 50% 0 0)';
    else if (d === '右') host.style.clipPath = 'inset(0 0 0 50%)';
    else if (d === '上') host.style.clipPath = 'inset(0 0 50% 0)';
    else if (d === '下') host.style.clipPath = 'inset(50% 0 0 0)';
    host.classList.add('scr-lines');
  } else if (spec.mode === 'sparkle') {
    /* 撒一把柔边圆斑：位置/大小/色相/节奏都错开，才像游戏里那种"漫天光斑"。
       平铺一张圆斑会排成整齐的网格，一眼就假。 */
    const t = tex.illum1 || tex.disc;
    const n = spec.count || 12;
    /* 全屏柔光：游戏里キラキラ不是"只有几个光斑"，整个画面会同时被提亮、
       带上暖色泛光（对比用户给的截图：天空更白、沙滩更亮、整体对比度下降）。
       只画光斑不加这层，永远是"贴了几张圆片"的感觉。 */
    const wash = document.createElement('i');
    wash.className = 'spk-wash';
    host.appendChild(wash);
    for (let i = 0; i < n; i++) {
      const s = document.createElement('i');
      s.className = 'spk';
      const size = 90 + (i * 37) % 150;
      const hue = (i * 47) % 360;
      s.style.left = `${(i * 137 + 40) % 1160}px`;
      s.style.top = `${(i * 211 + 30) % 650}px`;
      s.style.width = `${size}px`;
      s.style.height = `${size}px`;
      if (t) {
        /* 贴图是**灰度**圆斑（ima_illumination_01 原图就是灰白），
           单靠 hue-rotate 是染不上的（灰度无色相）。必须再叠一层纯色渐变、
           用 screen 混合，才出得来粉/蓝/绿的彩色光斑。 */
        s.style.backgroundImage =
          `url(${t.src}), linear-gradient(hsl(${hue} 95% 72%), hsl(${hue} 95% 72%))`;
        s.style.backgroundBlendMode = 'screen';
        s.style.backgroundSize = '100% 100%, 100% 100%';
        s.style.backgroundRepeat = 'no-repeat, no-repeat';
      } else {
        s.style.background = `radial-gradient(circle, hsl(${hue} 90% 75%), transparent 70%)`;
      }
      s.style.animationDelay = `${-(i * 0.23).toFixed(2)}s`;
      s.style.animationDuration = `${(2.2 + (i % 5) * 0.45).toFixed(2)}s`;
      host.appendChild(s);
    }
  } else if (spec.mode === 'glow') {
    const t = tex[spec.tex] || tex.illum2;
    const size = spec.all ? 1500 : (spec.mode === 'sparkle' ? 260 : 900);
    /* 两层背景：上层是星芒/光斑贴图，下层是**径向渐变**的颜色层。
       以前把颜色直接刷成 backgroundColor，结果整个矩形被染满（"光る／赤／全体"
       变成一块红色蒙版），而光应该是中心浓、边缘透明的。 */
    const col = spec.tint
      || (spec.all ? 'rgba(255,252,235,.55)' : 'rgba(255,255,240,.72)');
    const img = t ? `url(${t.src})` : 'linear-gradient(transparent,transparent)';
    host.style.backgroundImage = `${img}, radial-gradient(circle at 50% 50%, ${col}, transparent ${spec.all ? 72 : 58}%)`;
    host.style.backgroundSize = `${size}px ${size}px, 100% 100%`;
    host.style.backgroundPosition = 'center, center';
    host.style.backgroundRepeat = (spec.mode === 'sparkle' ? 'repeat' : 'no-repeat') + ', no-repeat';
    host.classList.add('scr-glow');
  } else if (spec.mode === 'heal') {
    /* 回復：defo_effect 里的放射光环（ring）+ 一层绿色径向光。
       原作里治疗是"从脚下升起的光环"，所以元素底部对齐、向上漂。 */
    const t = spec.tex ? tex[spec.tex] : null;
    const col = spec.tint || 'rgba(150,255,190,.72)';
    if (t) {
      host.style.backgroundImage =
        `url(${t.src}), radial-gradient(circle at 50% 72%, ${col}, transparent 68%)`;
      host.style.backgroundSize = `auto ${spec.all ? 105 : 62}%, 100% 100%`;
      host.style.backgroundPosition = `center ${spec.all ? 50 : 78}%, center`;
      host.style.backgroundRepeat = 'no-repeat, no-repeat';
    } else {
      host.style.background = `radial-gradient(circle at 50% 78%,${col},transparent ${spec.all ? 70 : 52}%)`;
    }
    host.style.mixBlendMode = 'screen';
    host.classList.add('scr-heal');
  } else if (spec.mode === 'haze') {
    const t = spec.tex ? tex[spec.tex] : null;
    if (t && spec.tile) {
      /* 雪 / 蝶 / 音符：素材是单个小图，**平铺**才像"漫天飘"，
         配 scr-haze 的缓慢漂移动画。 */
      host.style.backgroundImage = `url(${t.src})`;
      host.style.backgroundSize = `${Math.round(t.w * 1.1)}px ${Math.round(t.h * 1.1)}px`;
      host.style.backgroundRepeat = 'repeat';
    } else if (t) {
      // 煙 / 冷気：一整团雾，居中铺开
      host.style.backgroundImage =
        `url(${t.src}), radial-gradient(circle at 50% 58%,${spec.tint || 'rgba(200,220,255,.4)'},transparent 74%)`;
      host.style.backgroundSize = 'auto 92%, 100% 100%';
      host.style.backgroundPosition = 'center, center';
      host.style.backgroundRepeat = 'no-repeat, no-repeat';
    } else {
      host.style.background = `radial-gradient(circle at 50% ${spec.all ? 50 : 62}%,${spec.tint || 'rgba(200,220,255,.45)'},transparent 72%)`;
    }
    host.classList.add('scr-haze');
  } else {
    /* 爆発 / 打撃：真素材（爆炸烟团 / 白色刀光）+ 一层径向色晕。
       贴图本身是白色的，颜色全靠下面那层渐变换。 */
    const t = spec.tex ? tex[spec.tex] : null;
    const col = spec.tint || 'rgba(255,220,150,.85)';
    if (t) {
      host.style.backgroundImage =
        `url(${t.src}), radial-gradient(circle at 50% 50%, ${col}, transparent ${spec.big ? 80 : 62}%)`;
      host.style.backgroundSize = `auto ${spec.big ? 64 : 46}%, 100% 100%`;
      host.style.backgroundPosition = 'center, center';
      host.style.backgroundRepeat = 'no-repeat, no-repeat';
      /* 爆炸的白烟团用 screen 叠加：直接盖上去会糊住角色（实测一整块白），
         叠加才能既看到爆点又不挡人。 */
      host.style.mixBlendMode = 'screen';
      // 不能改 opacity：淡入是 JS 写的内联 opacity，改这里会被覆盖
      host.style.filter = 'brightness(.85)';
      host.classList.add('scr-burst');
    } else {
      host.style.background = `radial-gradient(circle at 50% 50%,${col},transparent ${spec.big ? 78 : 58}%)`;
      host.classList.add('scr-burst');
    }
  }
  $(SCREFF_LAYER[which]).appendChild(host);
  screffLive[which] = host;
  /* 与 playScreenFx 一样：append 后先把 opacity:0 落成计算值，否则浏览器把
     "0 → 1" 当成同一次样式计算，直接跳到 1 —— 整层一帧亮起就是闪。
     下面仍用 setTimeout（后台标签页 rAF 会被节流），这里只补一次强制布局。 */
  if (!ST.fastForward && fadeIn > 0) void host.offsetWidth;
  /* 用 setTimeout 而不是 rAF 触发淡入：rAF 在后台标签页/无焦点窗口里会被节流
     甚至完全不调，元素就永远停在 opacity:0（表现为"特效没出现"）。
     fastForward（跳转重放）时上面已经直接就位，不再排队。 */
  if (!ST.fastForward) {
    setTimeout(() => { if (screffLive[which] === host) host.style.opacity = 1; }, 20);
  }
  /* 一次性特效（攻击/爆発）：播完自己收掉，不等清除指令 —— 数据里这类
     常常没有对应的空名清除，不收就会一直挂在画面中心。 */
  if (spec.once) {
    setTimeout(() => {
      if (screffLive[which] === host) stopScreff(which, 220);
    }, spec.once);
  }
}

/* ================= 文字窗口 ================= */
/* Unity/TMP 富文本：<sprite=0> 之类是「行内图标」（SpriteAsset 图集里的第 N 张）。
   网页端没有对应图集，直接剥掉，否则会把标签原文当成剧情字显示出来。
   但 <sprite=0> 现在**有素材了**：advfx.js 的 msgspr 是游戏里那个粉色心形
   （bundle_font_66_heart_03 的 glyph 0），全站 66884 处 sprite 标签全是 index 0。
   以前连它一起剥掉 → 台词里「……❤❤❤」的心就没了（2026-11-09 用户截图对比）。 */
const RICH_TAG = new RegExp('</?(?:sprite|color|size|material|font|b|i|u|s|ruby|quad|' +
  'link|mark|style|align|cspace|mspace|indent|pos|space|alpha|voffset|' +
  'lowercase|uppercase|smallcaps|nobr|page)(?:=[^<>]*)?>', 'gi');
const SPRITE_TAG = /<sprite=(\d+)>/gi;
function cleanText(s) { return String(s ?? '').replace(RICH_TAG, ''); }
/* 一句台词的最终输出：先查当前语言译文，查不到就用日文原文（i18n 的核心策略）。
   field 将来若要显示别名字段（如 Message_en）可以复用同一个出口。 */
function lineText(m, field) { return cleanText(L10N.msg(m, field)); }
/** 正文用：保留 <sprite=N>（要画成图标），其余富文本标签照旧剥掉。 */
function lineRich(m, field) { return String(L10N.msg(m, field) ?? ''); }
/** 行内图标素材地址；没装「剧情演出特效」时为 null（那就只显示文字）。 */
function spriteIconSrc() {
  const A = window.__ADVFX;
  return (A && A.msgspr && A.msgspr.src) ? A.msgspr.src : '';
}
/** 台词 → [{t:'txt',v:'…'} | {t:'spr',i:0}]，文本段已经剥掉其它富文本标签。 */
function msgParts(s) {
  const txt = String(s ?? '');
  const out = [];
  let last = 0, m;
  SPRITE_TAG.lastIndex = 0;
  while ((m = SPRITE_TAG.exec(txt))) {
    if (m.index > last) {
      const seg = cleanText(txt.slice(last, m.index));
      if (seg) out.push({ t: 'txt', v: seg });
    }
    out.push({ t: 'spr', i: +m[1] });
    last = SPRITE_TAG.lastIndex;
  }
  const tail = cleanText(txt.slice(last));
  if (tail) out.push({ t: 'txt', v: tail });
  return out;
}
function makeSpriteIcon() {
  const src = spriteIconSrc();
  if (!src) return null;
  const i = document.createElement('i');
  i.className = 'mspr';
  i.style.backgroundImage = `url(${src})`;
  return i;
}
/** 一次性把整句画进 #msgText（打字机结束、跳句、切语言都走这里）。 */
function renderMsg(el, text) {
  el.textContent = '';
  for (const p of msgParts(text)) {
    if (p.t === 'spr') {
      const ic = makeSpriteIcon();
      if (ic) el.appendChild(ic);
    } else {
      el.appendChild(document.createTextNode(p.v));
    }
  }
}

function showWindow(m) {
  const w = $('#msgWindow'), n = $('#msgName');
  if (!SET.showWin) return Promise.resolve();
  w.style.transition = '';        // 清掉上一次擦除留下的内联过渡/位移
  w.style.transform = '';
  w.classList.add('on');
  const who = spkName(m);
  if (who) { n.textContent = who; n.classList.add('on'); }
  else n.classList.remove('on');
  const t = $('#msgText');
  t.classList.remove('partNotice');      // 段间提示的样式只属于提示那一行
  t.classList.toggle('narr', !who);
  return wait(winFadeMs(m));
}
/* 关窗的 Time 很多时候**不是时长而是擦除方向**（实测 145252 条里：
   右ワイプ 6860 / 中 6412 / 右 1334 / 左 1273 / 黒画面 683 / 上ワイプ 295 / 下ワイプ 231）。
   以前一律当"时长解析失败"兜底成 0.2s 淡出，方向信息全丢。
   这里按方向做位移+淡出；「黒画面」是直接切（黑场）。 */
/* ShowStandFull：全屏大立绘从画面下方升起（全站只有 3 条，都在结局段落）。
   数据 {StandFullImage:"77", PosX:"50", PosY:"-400", Move:"700"}：
   图片按 id 落在 media/img/standfull/，PosY 是最终纵向位置、Move 是上升距离。 */
function showStandFull(m, instant) {
  const id = String(m.StandFullImage || '').replace(/[^\d]/g, '');
  if (!id) return Promise.resolve();
  const wrap = document.createElement('div');
  wrap.className = 'standfull';
  const img = document.createElement('img');
  img.alt = '';
  img.onerror = () => wrap.remove();
  img.src = `media/img/standfull/${id}.png`;
  wrap.appendChild(img);
  const to = num(m.PosY, 0);
  /* instant：跳转/换段重放用。全屏立绘是**持续显示**的状态，重放只要
     落到最终位置，不能再从画面外滑 2.4 秒（会和新台词抢画面，
     而且 resetStage 已经把 #standLayer 清空过，这里必须补回来）。 */
  if (instant) {
    img.style.transform = `translateY(${to}px)`;
    $('#standLayer').appendChild(wrap);
    return Promise.resolve();
  }
  const from = to + Math.abs(num(m.Move, 700));
  img.style.transform = `translateY(${from}px)`;
  $('#standLayer').appendChild(wrap);
  requestAnimationFrame(() => {
    img.style.transition = 'transform 2.4s ease-out';
    img.style.transform = `translateY(${to}px)`;
  });
  return wait(400);
}

/* 两个立绘互换站位。它改的是**持续状态**（谁站哪）：
   跳转/换段重放时必须从头按顺序再走一遍，否则 seek 过去左右人物会反。
   以前它只在 FX_REPLAY_ONCE 里（只补目标句区间那一条），整段更早的互换
   不会被恢复 —— 全站 128 句受影响。 */
function swapStand(m) {
  const a = m.Position0 || 'Left', b = m.Position1 || 'Center';
  const ea = ST.stands.get(a), eb = ST.stands.get(b);
  if (ea) ST.stands.delete(a);
  if (eb) ST.stands.delete(b);
  if (ea) { placeStand(ea, b); ST.stands.set(b, ea); }
  if (eb) { placeStand(eb, a); ST.stands.set(a, eb); }
}

function hideWindow(m) {
  const w = $('#msgWindow');
  const raw = String(m.Time ?? '');
  const isDir = !/^[\d.]+$/.test(raw) && /ワイプ|^[中左右]$|黒画面/.test(raw);
  if (!isDir) {
    w.classList.remove('on');
    return wait(winFadeMs(m));
  }
  const dur = 0.24;
  let tf = 'scale(.92)';
  if (/右ワイプ|^右$/.test(raw)) tf = 'translateX(70px) scale(.96)';
  else if (/左ワイプ|^左$/.test(raw)) tf = 'translateX(-70px) scale(.96)';
  else if (/上ワイプ/.test(raw)) tf = 'translateY(-46px) scale(.96)';
  else if (/下ワイプ/.test(raw)) tf = 'translateY(46px) scale(.96)';
  else if (/黒画面/.test(raw)) tf = '';
  w.style.transition = tf
    ? `opacity ${dur}s ease, transform ${dur}s ease` : `opacity ${dur / 2}s`;
  if (tf) w.style.transform = tf;
  w.classList.remove('on');
  return wait(dur * 1000).then(() => {
    w.style.transition = '';
    w.style.transform = '';
  });
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
  /* 打字机按「字」推进，但 <sprite=0> 是**一个整体图标**：
     拆成 token 后文字逐字追加、图标一次性插进来，
     否则标签会被当正文一个个字符打出来。 */
  const toks = [];
  for (const p of msgParts(text)) {
    if (p.t === 'spr') toks.push(p);
    else for (const ch of Array.from(p.v)) toks.push({ t: 'txt', v: ch });
  }
  return new Promise(res => {
    let i = 0;
    const finish = () => {
      ST.typing = false;
      if (gen === ST.run) renderMsg(el, text);      // 已被跳转取代就别覆盖新台词
      res();
    };
    const step = () => {
      if (gen !== ST.run) { ST.typing = false; res(); return; }
      if (ST.typeSkip || i >= toks.length) { finish(); return; }
      const tk = toks[i++];
      let ch = '';
      if (tk.t === 'spr') {
        const ic = makeSpriteIcon();
        if (ic) el.appendChild(ic);
      } else {
        ch = tk.v;
        el.appendChild(document.createTextNode(ch));
      }
      const base = /[、。！？…]/.test(ch) ? 85 : (/[\u3000\s]/.test(ch) ? 40 : 22);
      setTimeout(step, base / (ST.speed * ST.typeRate));
    };
    step();
  });
}

/* ================= 音频 ================= */
let bgmAudio = null, loopSe = null;

/* ---------- 声音解锁：浏览器拦截"无手势自动播放" ----------
 * 链接里带 ?autoplay=1 直接开播时页面没有用户手势，浏览器会拒绝一切**出声**的
 * play()（NotAllowedError）。以前这些异常被 `.catch(() => {})` 静默吞掉，于是
 * 整段剧情从头到尾没 BGM、没语音；等用户点一次「重播 / 上一句」（有了手势）
 * 才突然正常 —— 2026-11-09 用户反馈的正是这个。
 * 现在：被拦下的音频先记账，任何一次真实手势后立刻补播，并给一句能点的提示。 */
const blockedAudio = new Set();
let audioHint = null;

function showAudioHint() {
  if (audioHint) return;
  audioHint = document.createElement('div');
  audioHint.id = 'audioHint';
  audioHint.className = 'audio-hint';
  audioHint.textContent = '浏览器拦下了自动播放的声音 —— 点这里开启';
  audioHint.onclick = () => unlockAudio();
  document.body.appendChild(audioHint);
}
function hideAudioHint() {
  if (!audioHint) return;
  audioHint.remove();
  audioHint = null;
}
/** 有手势了：把被拦下的声音补播（BGM / 循环音效立刻回来；当前这句语音若还没播完也补上） */
function unlockAudio() {
  const list = [...blockedAudio];
  blockedAudio.clear();
  hideAudioHint();
  list.forEach(a => {
    /* 只补"当前还该响的"那一个：被拦之后剧情可能已经换了 BGM / 换了台词，
       把旧元素一起补播会变成两首曲子/两句语音叠着放。 */
    const kind = a.dataset.kind;
    if (kind === 'voice' && (a !== ST.voice || a._done)) return;
    if (kind === 'bgm' && a !== bgmAudio) return;
    if (kind === 'loop' && a !== loopSe) return;
    if (!kind) return;                 // 短音效：补播没有意义
    try { a.play().catch(() => { }); } catch (e) { }
  });
}
/** 出声的 play()：被拦就记账 + 提示。retry=false 用于短音效（补播没有意义） */
function safePlay(el, retry) {
  let p;
  try { p = el.play(); } catch (e) { return; }
  if (!p || !p.catch) return;
  p.then(() => {
    // 出声成功了、也没有别的在等解锁 → 提示条留着会让人误会，收掉
    if (!blockedAudio.size) hideAudioHint();
  }).catch(err => {
    if (!err || err.name !== 'NotAllowedError') return;
    if (retry !== false) blockedAudio.add(el);
    showAudioHint();
  });
}
/* 任意一次真实手势就解锁（用户不必去点提示条） */
['pointerdown', 'keydown', 'touchstart'].forEach(ev => {
  document.addEventListener(ev, () => {
    if (blockedAudio.size || audioHint) unlockAudio();
  }, { capture: true, passive: true });
});

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
      safePlay(bgmAudio);
    }
    return;
  }
  const fade = num(m.FadeTime, 0.6);
  const old = bgmAudio;
  bgmAudio = new Audio(src);
  bgmAudio.dataset.src = src;      // 记录下来源，供去重与恢复判断
  bgmAudio.dataset.kind = 'bgm';   // 手势解锁时只补"当前这曲"（见 unlockAudio）
  bgmAudio.loop = true;
  bgmAudio.volume = 0;
  safePlay(bgmAudio);
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
  safePlay(a, false);   // 短音效：被拦就跳过，补播没有意义
}

function playLoopSe(m) {
  const src = SFX[m.Sound];
  if (!src) return;
  if (loopSe) { try { loopSe.pause(); } catch (e) { } }
  loopSe = new Audio(src);
  loopSe.dataset.src = src;        // 记录下来源：跳转时同一段循环音效不要重启
  loopSe.dataset.kind = 'loop';    // 手势解锁时只补"当前这段循环音"
  loopSe.loop = true;
  loopSe.volume = SET.seVol;
  safePlay(loopSe);
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
  a.dataset.kind = 'voice';        // 手势解锁时只补"当前这句"的语音（见 unlockAudio）
  ST.voice = a;
  return new Promise(res => {
    let done = false;
    const fin = () => { if (done) return; done = true; a._done = true; a._fin = null; res(); };
    /* 把"结束这次等待"暴露给外部：按住 K 快进时要能立刻放开，
       否则 ShowMessage 里的 await voiceP 只能等 40 秒硬兜底 ——
       表现就是"按住 K 不动、语音还卡在半路"（2026-11-09 用户反馈）。 */
    a._fin = fin;
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
    a.play().then(arm).catch(err => {
      /* 被浏览器拦下（无手势）：记下来，等手势后若这句还在播就补上；
         同时按"没有语音"的节奏放行，别让剧情卡住 */
      if (err && err.name === 'NotAllowedError') {
        blockedAudio.add(a);
        showAudioHint();
      }
      arm(); setTimeout(fin, 1500 / ST.speed);
    });
  });
}

/* ================= 指令分派 =================
 * 注意：画面/文字/语音指令只「启动」并把 Promise 塞进 pendingAll，
 * 真正等待只发生在 WaitMotion / Wait 上。
 */
const P = p => { if (p && typeof p.then === 'function') ST.pendingAll.push(p); return p; };

/* 同一句「演出批」里的背景染色跟着画面特效一起上屏。
   剧本 char_190 p3#105：618 `ShowScreenEffect{冷気,Time:0}` → 622
   `MulColorBackground{r150,g150,b255,Time:1}` → 625 台词。播放器是逐条
   立即上屏，于是雾先到、滤镜晚 0.2 秒在句中「啪」地出现 —— 用户指出的
   段落感。游戏客户端是把这批视觉状态在切句瞬间一起提交的。 */
const MUL_EARLY = new Set();
function syncBatchMul(midx, fx) {
  if (midx == null || !ST.methods || !ST.methods.length) return;
  if (MUL_EARLY.size > 64) MUL_EARLY.clear();
  const t = num(fx.Time, 0);
  for (let i = midx + 1; i < ST.methods.length; i++) {
    const x = ST.methods[i];
    const M = x.Method;
    if (M === 'ShowMessage') break;            // 只提前到本句台词之前
    /* ⚠ 绝不能跨过换背景提前应用：MulColorBackground 是给「当前 #bgLayer
       里那张 <img>」上色的，换背景会替换这张 img。
       char_221 第6段第20句：150 ShowScreenEffectBack → 151 ShowBackground
       → 157 MulColorBackground{r255,g100,b100} 红光。以前在 150 就把滤镜
       挂到旧图上、又标记 157 已处理，新图永远没染色 —— 用户反馈「首次
       启动从播放记录自动播放到这句红光不生效，切句后又出来」（切句走
       seekTo，会清空标记并按「先背景后滤镜」重放，所以又正常）。 */
    if (M === 'ShowBackground' || M === 'ShowDoubleBackground'
        || M === 'ShowDoubleBackgroundOffset' || M === 'HideBackground'
        || M === 'ShowEventCg' || M === 'HideEventCg') break;
    if (M === 'MulColorBackground') {
      mulColorBg({ ...x, Time: t });            // 与画面特效同一条淡入时间线
      MUL_EARLY.add(i);
    }
  }
}

function runOne(m, midx, isSeek) {
  const M = m.Method;
  switch (M) {
    /* --- 画面：启动即返回 --- */
    case 'ShowBackground': P(setBackground(m.BackImage, m)); return;
    /* 换场把画面特效收掉：无名特效没有"关闭"指令，游戏里也是靠换场消失的
       （char_204 第 1 段：速度线 → HideBackground 转场）。 */
    case 'HideBackground':
      stopScreff('front', 150); stopScreff('back', 150);
      P(hideBackground(m)); return;
    case 'ShowDoubleBackground':
    case 'ShowDoubleBackgroundOffset':
      P(setDoubleBackground(m.LBackImage, m.RBackImage)); return;
    case 'MulColorBackground':
      if (MUL_EARLY.delete(midx)) return;   // 已随同句画面特效一起上屏
      mulColorBg(m); return;
    case 'ShowStand': P(showStand(m)); return;
    case 'ShowStandAndSit': P(showStandAndSit(m)); return;
    case 'ShowStandFull': P(showStandFull(m)); return;
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
      autoTalk(ST.curName);       // 说话人以外的立绘压暗（不依赖 TalkCharacter）
      P(showWindow(m));
      return;
    case 'HideMessageWindow': P(hideWindow(m)); return;
    case 'ShowMessage': {
      /* 正文保留 <sprite=0>（要画成心形图标）；停顿计时仍按纯文本长度算。 */
      const text = lineRich(m);
      const plain = cleanText(text);
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
        renderMsg($('#msgText'), text);
        ST.typing = false;
        return;
      }
      const gen = ST.run;
      P((async () => {
        if (hasVoice) {
          /* 有配音：等待时长完全由语音长度决定
           * 打字机与语音并行，语音播完立即补齐文字，再停「语音后停顿」 */
          const typeP = SET.instantText
            ? (renderMsg($('#msgText'), text), Promise.resolve())
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
            renderMsg($('#msgText'), text);
            await wait(0);
          } else {
            await typeText(text);
          }
          if (gen !== ST.run) return;
          await wait(SET.pauseBase + plain.length * SET.pausePerChar);
        }
      })());
      return;
    }
    /* --- 语音 --- */
    case 'PlayVoice':          // 150 条，字段与 VOICE 完全一致，以前漏了这条别名
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
      if (!isSeek) syncBatchMul(midx, m);
      showScreenEffect(m, false); return;
    case 'ShowScreenEffectBack':
      if (!isSeek) syncBatchMul(midx, m);
      showScreenEffect(m, true); return;
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
    case 'SwapStand': swapStand(m); return;
    case 'TalkCharacter': talkCharacter(m); return;
    /* ClearTalkCharacter = 显式区间的结束：从这里往后重新交给 autoTalk 猜 */
    case 'ClearTalkCharacter':
      ST.talkExplicit = false;
      ST.talk = ['1', '1', '1'];
      applyTalk();
      return;
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
  'TalkCharacter', 'ClearTalkCharacter',
  // 画面特效是「持续到被清除」的：跳转后必须按顺序重放开/关两条，
  // 否则跳过去要么一片空白、要么残留上一句的光。表情符号（ShowStandEffect）
  // 是一次性动画，故意不在这里 —— 重放会让它每次都重新冒出来。
  'ShowScreenEffect', 'ShowScreenEffectBack',
  /* 下面四条同样是**持续状态**，不是"当时那一下"：
     全屏立绘（ShowStandFull）、立绘互换站位（SwapStand）、
     台词字号放大/还原（ToUpper/ToLower）。
     `resetStage` 会清空 #standLayer、把 #msgText 的 fontSize 复位，
     只有按整段顺序重放才能还原。以前它们只挂在 FX_REPLAY_ONCE 上
     （只补目标句区间那一条），整段更早发生的就永远不恢复 ——
     全站统计 ToUpper 28551 句 / ToLower 898 / SwapStand 128 / ShowStandFull 62。
     一次性动画（Shake / MotionStand / ScreenFlash / ShowStandEffect…）
     仍留在 FX_REPLAY_ONCE，别混进来。 */
  'ShowStandFull', 'SwapStand', 'ToUpper', 'ToLower',
  ...CAM_REPLAY,
]);
/* 背景/CG 这两类只重放「最后一条」：中间的先 Show 再 Hide 再 Show 会互相抵消，
   逐条重放等于把同一张图拆了建好几次，每一次都可能有一帧没有图（闪黑）。 */
const BG_REPLAY = new Set(['ShowBackground', 'HideBackground',
  'ShowDoubleBackground', 'ShowDoubleBackgroundOffset']);
const CG_REPLAY = new Set(['ShowEventCg', 'HideEventCg']);

/* 切上一句/下一句时，游戏会把**目标句附带的**一次性演出重新演一遍。
   REPLAY 只负责"状态恢复"（背景 / 立绘 / CG / 持续型特效的最终状态），
   下面这些是"当时那一下"的效果，重放时会整段跳过：
     气泡、画面特效入场、画面/立绘震动、闪屏、地点牌、SE、字号变化…
   所以 seekTo 单独收集目标句区间内的这些指令，等状态重建完按原顺序
   用正常入口 runOne() 再跑一次。

   不收：WaitMotion / Wait / ShowMessage* 等控制流（会阻塞或改台词），
   PlayMusic / PlayLoopSound 等持续音频（由 restoreAudioAt 恢复状态，
   重播会让音乐重新起头）。VOICE 已有专门的补播。 */
const FX_REPLAY_ONCE = new Set([
  'ShowStandEffect', 'ShowScreenEffect', 'ShowScreenEffectBack',
  'Shake', 'ShakeStand', 'MotionStand',
  'ScreenFlash', 'ScreenFlashIn', 'ScreenFlashOut',
  'ShowPlace', 'PlaySound',
  /* ToUpper/ToLower、SwapStand、ShowStandFull 已移入 REPLAY：
     它们是持续状态，按整段顺序重放才对；放这里只补目标句区间那一条，
     整段更早发生的状态就丢了（见 REPLAY 里的说明）。 */
  'ZoomOutEventCG',
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
    case 'ShowBackground': setBackground(m.BackImage, m); break;
    case 'HideBackground': hideBackground(m); break;
    case 'ShowDoubleBackground':
    case 'ShowDoubleBackgroundOffset':
      setDoubleBackground(m.LBackImage, m.RBackImage); break;
    case 'MulColorBackground': mulColorBg(m); break;
    case 'ShowStand': showStand(m, true); break;
    case 'ShowStandAndSit': {
      showStand(m, true);
      const el = ST.stands.get(m.Position || 'Center');
      if (el) el.classList.add('sit');
      break;
    }
    case 'HideStand': hideStandInstant(m); break;
    case 'MoveStand': {
      const from = m.PositionFrom || 'Center';
      const to = m.PositionTo || 'Center';
      const el = ST.stands.get(from);
      if (el) {
        placeStand(el, to);
        ST.stands.delete(from);
        ST.stands.set(to, el);
      }
      break;
    }
    case 'MulStand': mulStand(m); break;
    case 'TalkCharacter': talkCharacter(m); break;
    case 'ClearTalkCharacter':
      ST.talkExplicit = false;
      ST.talk = ['1', '1', '1'];
      applyTalk();
      break;
    /* 特效：逐条重放开/关，最终状态自然正确（同层只会剩最后一个开着的）。
       Name 为空的清除指令也会被重放，所以"跳过去之后光还挂着"的问题一并解决。 */
    case 'ShowScreenEffect': showScreenEffect(m, false); break;
    case 'ShowScreenEffectBack': showScreenEffect(m, true); break;
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
    /* 持续状态：按整段顺序重放，最后一条自然生效 */
    case 'ShowStandFull': showStandFull(m, true); break;
    case 'SwapStand': swapStand(m); break;
    case 'ToUpper': $('#msgText').style.fontSize = 'calc(var(--msgFS,21px) * 1.24)'; break;
    case 'ToLower': $('#msgText').style.fontSize = ''; break;
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
  MUL_EARLY.clear();               // 跳转后重新按目标句重放，别留旧的"已提前"标记
  if (seeking || !ST.methods.length) return;
  if (midx == null || midx < 0 || midx >= ST.methods.length) return;
  /* opt.keepPaused：跳过去但**不自动播**（暂停时点进度条用）。
     「自动」关掉时，方向键 / 文本列表点句子都只定位到那一句、停着等下一次操作，
     否则"关了自动播放，点一下下一句又被连着播下去"（2026-11-10 反馈的延续）。
     明确要求连续播放的入口（恢复观看进度、切语言重放、自动化测试钩子）传 forceResume。 */
  const resume = !(opt && opt.keepPaused)
    && (ST.auto || !!(opt && opt.forceResume));
  seeking = true;
  try {
    haltLoop();                    // 只杀循环与语音，保留 BGM
    resumeAudio();                 // 暂停时挂起的 BGM/循环音效续上
    ST.typeSkip = true;            // 掐断还在跑的打字机
    /* 切上一句/下一句时游戏会把**目标句附带的**表情符号重新演一次。
       ShowStandEffect 故意不在 REPLAY 里（否则历史气泡会全部冒出来），
       所以先清掉旧气泡，重放结束后单独播目标句那一条。 */
    stopStandEffect(0);
    resetStage({ keepVisual: true });   // 背景/CG 先留在台上，别拆了再建（会闪黑）
    ST.fastForward = true;
    /* 记下"跳转前层上挂着的是哪一个"，后面据此判断这条特效是这次跳转新生的
       还是历史遗留 —— 新生的（目标句自带的 pendingFx）绝不能当残影清掉。 */
    const liveFrontAtStart = screffLive.front;
    const liveBackAtStart = screffLive.back;

    // 1) 同步重建画面状态（几百条指令也就几十毫秒）
    /* 背景/CG 只重放最终状态：中间的先 Show 再 Hide 再 Show 会互相抵消，
       逐条重放等于把同一张图拆了建好几次，每次都可能有一帧没图（闪黑）。
       MulColorBackground 是给当前背景上色的，只补在最后一条背景指令之后。 */
    /* 目标句区间 = 上一条 ShowMessage 之后 ~ midx。实测全站气泡指令都在
       目标句 ShowMessage 之前 3~13 条、且位于上一条之后。只取这个区间，
       既不会重播历史上所有演出，也不会把很久以前没关掉的特效重新冒出来；
       区间内的空名清除指令按原顺序一起重放，最终状态自然正确。 */
    let prevMsg = -1;
    for (let i = midx - 1; i >= 0; i--) {
      if (ST.methods[i].Method === 'ShowMessage') { prevMsg = i; break; }
    }
    const pendingFx = [];
    for (let i = prevMsg + 1; i < midx; i++) {
      const mk = ST.methods[i];
      if (FX_REPLAY_ONCE.has(mk.Method)) pendingFx.push({ i, m: mk });
    }
    let lastBg = null, lastCg = null, lastCam = null, lastFlash = null;
    /* 这一段里到底有没有出现过背景/CG 指令。没有出现 ≠ 应该没有背景：
       很多段落是「沿用上一段的场景」，段内一条 ShowBackground 都没写
       （char_204 第 2 段第 61 句就是这样）。以前没碰到就按「该处无背景」
       把层清空，表现是快进/跳转过去突然黑屏、回上一句又有了。 */
    let sawBg = false, sawCg = false;
    const muls = [];
    /* 两层各只记**最后一条**画面特效指令（同背景/CG 的口径）。
       画面特效是「开着一直到被清除」，逐条重放会把「清除 → 再开」也演一遍，
       中间那一下就是每切一次句子空想雾闪一下的根源（2026-11-09 反馈）。
       只应用最终状态：层已经挂着同一个 prefab 时 showScreenEffect 直接复用，
       DOM 一动不动；一条都没碰过则说明目标位置本来就不该有。 */
    let lastFrontFx = null, lastBackFx = null;
    for (let i = 0; i < midx; i++) {
      const m = ST.methods[i];
      const M = m.Method;
      if (M === 'ShowScreenEffect') { lastFrontFx = m; continue; }
      if (M === 'ShowScreenEffectBack') { lastBackFx = m; continue; }
      /* ScreenFlashIn / Out 是**跨句**的一对（In 亮起、若干句后 Out 才灭）。
         只重播目标句区间会漏掉更早的 In，或把已灭的闪屏又点亮。
         所以这里额外记全区间最后一次，重播后按它落到最终状态。 */
      if (M === 'ScreenFlashIn' || M === 'ScreenFlashOut') {
        lastFlash = { i, m };
        continue;
      }
      if (!REPLAY.has(M)) continue;
      if (BG_REPLAY.has(M)) { lastBg = { i, m }; sawBg = true; continue; }
      if (CG_REPLAY.has(M)) { lastCg = { i, m }; sawCg = true; continue; }
      if (CAM_REPLAY.has(M)) { lastCam = { i, m }; continue; }
      if (M === 'MulColorBackground') { muls.push({ i, m }); continue; }
      try { replayOne(m); } catch (e) { }
    }
    if (!sawBg) {
      /* 当前段（0..midx）一条背景指令都没有 = 沿用上一段的场景。直接快进/跳转
         到这种段（冷気那句 char_190 第 105 句就是）时背景黑屏 —— 因为台上
         从来就没放上过背景。向前一个段找最后一次 ShowBackground 补回来。 */
      outer: for (let pi = ST.part - 1; pi >= 0; pi--) {
        const pm = S.data.parts[pi].methods;
        for (let i = pm.length - 1; i >= 0; i--) {
          if (pm[i].Method === 'ShowBackground'
              || pm[i].Method === 'ShowDoubleBackground'
              || pm[i].Method === 'ShowDoubleBackgroundOffset') {
            /* i 属于**前一段**，不能拿来和当前段 muls 的 i 比大小：
               前段较长时会把当前段所有 MulColorBackground 判成"旧背景上的"
               而整批跳过，跳转后有 0.2~0.3 秒画面完全没染色（用户看到的
               「演出效果被清空一下」）。这里只借它的 m，索引记 -1。 */
            lastBg = { i: -1, m: pm[i] };
            break outer;
          }
        }
      }
    }
    if (lastBg) { try { replayOne(lastBg.m); } catch (e) { } }
    for (const x of muls) {
      if (lastBg && x.i < lastBg.i) continue;   // 旧背景上的颜色乘算已被最终背景替代
      /* 这是**恢复状态**，不是重演演出：剧本 `Time:1` 的染色淡入在跳转/切句时
         不该再跑一遍。resetStage 刚把 filter 复位成原色，若再花 1 秒慢慢染回去，
         用户看到的就是「演出效果被清空 0.x 秒，然后闪一下变成另一种效果」
         （2026-11-11 char_190 p3#105 冷気 / char_415 p3#36 邪気；游戏录屏里
         冷気是平滑淡入后一直保持，从不重建）。同一同步块里瞬间落到目标色，
         浏览器只绘制最终状态，中间那帧根本不会上屏。 */
      try { mulColorBg({ ...x.m, Time: 0 }); } catch (e) { }
    }
    if (lastCg) { try { replayOne(lastCg.m); } catch (e) { } }
    /* 镜头最后应用：它写的是台上那张 <img> 的内联 transform，得等 CG 定下来才有效。
       镜头指令是绝对赋值（scale 覆盖，不是叠加），所以只取最后一条就是该句的最终画面；
       落在最后一条 CG 指令之前说明那张图早被换掉，忽略。 */
    if (lastCam && lastCam.i > (lastCg ? lastCg.i : -1)) {
      try { replayOne(lastCam.m); } catch (e) { }
    }
    ST.fastForward = false;
    /* 立绘/画面状态已重建完，这时才把目标句的一次性演出按原顺序重播一遍。
       放在 fastForward 之后：runOne 里的音频/特效才不会被"重放时跳过"，
       也不会被重放阶段的 resetStage 清掉。 */
    /* isSeek=true：这批是"重演目标句的一次性演出"，**不是**正常播放，
       不能让其中的 ShowScreenEffect 触发 syncBatchMul —— 那会把本句的
       MulColorBackground 再按剧本 Time 应用一次（上面已经用 Time:0 落定），
       于是切句时颜色重新淡入一遍。全站 32 处命中这个组合。 */
    for (const { i, m } of pendingFx) {
      try { runOne(m, i, true); } catch (e) { }
    }
    /* 画面特效：只把**最终状态**落到层上（开着的原地复用、该关的撤掉）。
       区间里一条都没碰过 = 目标位置本来就不该有，直接清掉（不留残影）。
       ⚠ 一次性特效**不能**这样"恢复"：它播完就没了，不是一个能保持的状态。
       剧本里这类指令常常没有对应的空名清除（char_278 第4段第422句的「打撃」
       一直没被清掉），以前每切一句都把那条打撃重演一遍 —— 画面正中每次都冒
       一下金色光圈（2026-11-10 用户反馈）。只有持续型才需要补。 */
    const keepFx = (m) => {
      if (!m) return null;
      const pf = SCREFF_FX[String(m.Name || '')];
      if (!pf) return m;              // 集中線/未映射：走 screffSpec，行为不变
      /* 循环型 + 状态型（cold）都需要在跳句时补回来；一次性不用 */
      return (isLoopPrefab(pf) || SCREFF_HOLD_RE.test(String(pf || ''))) ? m : null;
    };
    const kf = keepFx(lastFrontFx), kb = keepFx(lastBackFx);
    /* 目标句自己带的一次性特效（pendingFx 刚重播的那个）不能被这一步撤掉：
       只有"这一跳没在层上生出新东西"时才清理。 */
    const liveF = screffLive.front, liveB = screffLive.back;
    if (kf) { try { showScreenEffect(kf, false); } catch (e) { } }
    /* 目标位置不该有特效时也不要"一帧清空"：留 220ms 淡出，和新画面接上。
       以前 stopScreff(...,0) 瞬间 remove，重建又要几十毫秒，中间那 0.1~0.2 秒
       画面上什么特效都没有 —— 用户录屏里指出的「演出效果被清空一下」。 */
    else if (liveF === liveFrontAtStart) stopScreff('front', 220);
    if (kb) { try { showScreenEffect(kb, true); } catch (e) { } }
    else if (liveB === liveBackAtStart) stopScreff('back', 220);
    /* 闪屏按全区间最后一次 In/Out 落定：暂停着切句也不会留一块白屏，
       跳到「In 之后、Out 之前」的句子也能保持游戏里该有的亮度。 */
    if (lastFlash) {
      try {
        if (lastFlash.m.Method === 'ScreenFlashIn') flashIn(lastFlash.m);
        else flashOut(lastFlash.m);
      } catch (e) { }
    }
    /* 只有「重放里确实出现过背景/CG 指令、且最终没留下图」才撤掉这一层。
       一条指令都没碰到的，说明该处背景是沿用前文的（resetStage 已经用
       keepVisual 把画面留着了），必须保留 —— 否则快进/跳会突然黑屏。 */
    if (sawBg && !VIS.bg.hit) { $('#bgLayer').innerHTML = ''; VIS.bg.key = null; VIS.bg.el = null; }
    if (sawCg && !VIS.cg.hit) { $('#cgLayer').innerHTML = ''; VIS.cg.key = null; VIS.cg.el = null; }

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
      // 跳过来也要把"谁在说话"的压暗补上（TalkCharacter 只在有写的地方才重放）
      autoTalk(whoJump);
    }

    // 3) 音频状态（BGM / 循环音效）：跳过的段落里的 PlayMusic 要补上，否则进来没声音
    /* 手动模式是"停着等切句"而不是暂停：BGM / 循环音效照放；
       只有自动模式下的暂停跳转（拖进度条）才把声音按住等点播放。 */
    restoreAudioAt(midx, !resume && ST.auto);

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
        renderMsg($('#msgText'), lineRich(m));
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
  MUL_EARLY.clear();               // 换段后 midx 重新从 0 开始
  // 只换播放循环，不销毁 BGM：重播/切段后音乐还能续上（脚本里的 PlayMusic 会自然换曲）
  haltLoop();
  /* 切段＝上一段的演出到此结束。ShowScreenEffect / ShowScreenEffectBack
     和表情符号都是"开着直到清除"的常驻层，而下面的
     resetStage({keepVisual:true}) 为了让 seekTo 不重复加载会**刻意保留
     .screff**，也完全不碰 #screffBack。不在这里显式停掉，切到新段后
     旧特效会继续挂在画面上、rAF 也还在跑（2026-11-10 用户反馈：
     "切到另外的段了演出还在"）。
     stopScreff 会 cancelAnimationFrame + 清 fxHandles + 移除该层残留；
     同段内跳句走 seekTo，不经过这里，持续特效仍按原逻辑沿用。 */
  stopScreff('front', 0);
  stopScreff('back', 0);
  stopStandEffect(0);
  stopLoopSe();
  resumeAudio();
  await wait(0);
  ST.part = partIdx;
  // 段变了要更新侧栏选中态：合集包里同一个 key 有多条"话"，靠 data-part 区分
  if (typeof refreshSelection === 'function') refreshSelection();
  ST.methods = S.data.parts[partIdx].methods;
  ST.idx = 0;
  /* 「自动」关掉 = galgame 的手动模式：这一段停在第一句，不启动自动循环，
     之后只靠「下一句 / 上一句 / 点台词 / 拖进度条」逐句切（见 setAutoPlay）。 */
  ST.running = ST.auto;
  ST.curMsgIdx = null;
  ST.typing = false;
  /* 换段保留背景/CG：段与段之间绝大多数是同一场景继续，新段开头往往不重发
     ShowBackground。以前这里无脑清空，快进到下一段就黑屏（2026-11-09 反馈）。
     **但换剧情（opt.fresh）必须彻底清空** —— 否则从侧栏切到另一个角色时，
     台上还挂着上一部剧情的背景，而且怎么跳转都不变（2026-11-09 用户反馈）。 */
  resetStage(opt && opt.fresh ? undefined : { keepVisual: true });
  buildLineIndex();
  ST.curName = '';
  renderTabs();
  updateInfo();
  setPlayBtn(ST.running);
  /* 接着上次看：只有"这一段确实留了进度、而且没看到最后一句"才跳，
     否则从开头播（已经看完的段再点进来应该重看，不是停在最后一句话上）。
     手动模式跳过去也照样停住，不会借着"继续上次"又自己播起来。 */
  const rec = (opt && opt.noResume) ? null : (UD ? UD.prog(ST.key) : null);
  if (rec && rec.p === partIdx && rec.mi > 0 && rec.mi < ST.methods.length
      && (rec.n ? rec.l < rec.n - 1 : true)) {
    toast(`继续上次：第 ${partIdx + 1} 段 第 ${UD.lineNo(rec)}/${rec.n} 句`
      + `（${UD.progPct(rec)}%）· 点「⏮ 重播」从头看`);
    seekTo(rec.mi, ST.auto ? { forceResume: true } : undefined);
    return;
  }
  if (!ST.auto) {
    const first = (ST.allLines && ST.allLines.length) ? ST.allLines[0].midx : 0;
    seekTo(first);                 // 停在第一句，等玩家切句
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
 * 3 秒是真实时间（不乘倍速）；按暂停 / 切段 / 点「下一句」/ 关掉「自动」
 * 都会立即打断。三种情况只提示、不自动续播：
 *   1. 角色剧情播完**倒数第二段**（最后一段不自动进，要玩家手动切）；
 *   2. 控制条的「自动」被关掉（含倒数途中现关，见 setAutoPlay）；
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

/** 角色剧情播完倒数第二段（一般是第 5 段）时是否要停住。
 *  默认停：最后一段是成人场景，要玩家自己点「下一段」才进去；
 *  设置页「播完第 5 段暂停」取消勾选后，自动模式就一路连播到第 6 段。 */
const holdBeforeLastPart = (cur, total) =>
  SET.holdLastPart !== false && isCharKey(S.key) && cur === total - 2;

/** 段尾该显示的提示文案（自动续播时带剩余秒数）。切换语言时也用它重画。 */
function partNoticeText(secs) {
  const total = (S.data && S.data.parts) ? S.data.parts.length : 1;
  const cur = ST.part;                 // 刚播完的段（0 基）
  const n = cur + 1;                   // 界面口径（1 基）
  if (n >= total) {
    return L10N.t('part.endLast', `第 ${n} 段播放完毕 · 已是最后一段`, { n });
  }
  const nextNo = n + 1;
  if (!ST.auto) {
    return L10N.t('part.endNoAuto',
      `第 ${n} 段播放完毕 · 手动模式已停在本段末尾，点「下一句」进第 ${nextNo} 段`,
      { n, m: nextNo });
  }
  // 角色剧情：最后一段不自动进，停在倒数第二段播完处（可在设置里关掉）
  if (holdBeforeLastPart(cur, total)) {
    return L10N.t('part.endHold',
      `第 ${n} 段播放完毕 · 已暂停，要播放第 ${nextNo} 段请手动切换`, { n, m: nextNo });
  }
  const s = secs == null ? Math.round(PART_GAP_MS / 1000) : secs;
  return L10N.t('part.endAuto',
    `第 ${n} 段播放完毕 · ${s} 秒后自动播放第 ${nextNo} 段`, { n, s, m: nextNo });
}

/** 一段播完的收尾：提示 + 决定续播还是停下。gen 用来确认这期间没被暂停/切段。 */
async function partBoundary(gen) {
  const total = (S.data && S.data.parts) ? S.data.parts.length : 1;
  const cur = ST.part;
  const stopHere = () => {
    showPartNotice(partNoticeText());
    ST.running = false; setPlayBtn(false);
  };
  const willPlay = cur + 1 < total && ST.auto
    && !holdBeforeLastPart(cur, total);
  if (!willPlay) { stopHere(); return; }
  const next = cur + 1;
  for (let s = Math.round(PART_GAP_MS / 1000); s >= 1; s--) {
    // 倒数途中把「自动」关掉也要立刻停（setAutoPlay 会先打断 gapWait，这里是双保险）
    if (!ST.auto) { stopHere(); return; }
    showPartNotice(partNoticeText(s));
    const ok = await gapWait(1000);
    if (gen !== ST.run || !ST.running) return;      // 被暂停/切段接管
    if (!ST.auto) { stopHere(); return; }
    // 按住 K 快进时不等：直接把下一段接上，别把快进卡在段界上
    if (!ok) { if (ST.rush) playPart(next, { noResume: true }); return; }
  }
  if (!ST.auto) { stopHere(); return; }
  // noResume：自动播下一段就是"接着往下看"，别又跳回下一段自己的旧进度
  playPart(next, { noResume: true });
}

/* opt.keepVisual：跳转重建时保留背景/CG（见 seekTo），避免黑屏闪烁 */
function resetStage(opt) {
  const keepVisual = !!(opt && opt.keepVisual);
  if (keepVisual) {
    /* 保留 `.screff` 层：seekTo 每次都走这里，以前 `innerHTML=''` 把
       持续型特效整个删掉，重放时又从头创建/加载一遍（煙 每次切句子
       都重复加载，2026-11-09 反馈）。同层是否该换由重放里的
       ShowScreenEffect 指令决定，stopScreff 会清掉不同名的层。 */
    Array.from($('#fxLayer').children).forEach(ch => {
      if (!ch.classList.contains('screff')) ch.remove();
    });
    VIS.bg.hit = false; VIS.cg.hit = false;
    /* 台上这张 CG 是**上一段/上一次跳转留下的**：标记成 stale，等下一段
       第一条 ShowBackground 来的时候让它退场（否则它一直盖在新背景上面）。
       本段自己 setCg 时会立刻清掉这个标记，段内 CG 演出不受影响。
       详见 dropCgForBg。 */
    VIS.cg.stale = true;
    // 颜色乘算 / 缩放等一次性效果由重放重新施加，先复位成原始画面
    ['#bgLayer', '#cgLayer'].forEach(s => {
      $(s).querySelectorAll('img').forEach(i => {
        i.style.transition = 'none';
        i.style.filter = '';
        i.style.transform = '';
        i.style.opacity = 1;
        dropMulFilter(i);        // 顺手回收 feColorMatrix 节点，避免跳转多了越积越多
      });
    });
    $('#standLayer').innerHTML = '';
  } else {
    // #screffBack 也要清：它不在 #fxLayer 里，漏掉会让上一段/上一次跳转的
    // 常驻画面特效残留下来，叠在新特效上（实测出现两层集中线）
    ['#bgLayer', '#cgLayer', '#fxLayer', '#standLayer', '#screffBack']
      .forEach(s => { $(s).innerHTML = ''; });
    VIS.bg.key = null; VIS.bg.el = null;
    VIS.cg.key = null; VIS.cg.el = null;
    VIS.cg.stale = false;        // 层已清空：没有遗留 CG 要收
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
  ST.talk = ['1', '1', '1'];
  /* 显式 TalkCharacter 的区间要跟着重放路径重新算：跳转/换段后如果不清掉，
     上一段留下的 talkExplicit=true 会让 autoTalk 永远早退，
     表现为"两个立绘都亮"（用户 2026-11-09 反馈的场景之一）。 */
  ST.talkExplicit = false;
  // keepVisual 时特效层 DOM 还在，引用要留着才能"沿用"；只有彻底清空才重置
  if (!keepVisual) screffLive = { back: null, front: null };
  ST.pendingAll.length = 0;
  ST.voiceHas = false;
  ST.voiceP = null;
  if (ST.voice) { try { ST.voice.pause(); } catch (e) { } }
}

function setPlayBtn(on) {
  const el = $('#btnPlay');
  if (!el) return;
  /* 手动模式没有"暂停/播放"这一对状态：主按钮直接变成「下一句」，
     点它、点画面、按空格都走切句（galgame 的点击推进）。 */
  if (!ST.auto) {
    el.textContent = L10N.t('ctrl.nextLine', '▶ 下一句');
    el.title = L10N.t('ctrl.nextLineTip', '自动播放已关闭：点一下走到下一句');
    return;
  }
  el.removeAttribute('title');
  el.textContent = on ? L10N.t('ctrl.pause', '⏸ 暂停')
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
  if (bgmAudio && bgmAudio.paused) safePlay(bgmAudio);
  if (loopSe && loopSe.paused) safePlay(loopSe);
}

/* ================= 自动播放开关（控制条「自动」= galgame 的 AUTO） =================
 * 用户 2026-11-10 明确的语义：
 *   开 = 像视频一样连续自动播，段尾倒数 3 秒后接下一段；
 *   关 = 手动模式，画面停在某一句，只有「下一句 / 上一句 / 点文本列表 /
 *        拖进度条」才切句；主按钮、点画面、空格 / P 也都变成「下一句」。
 * 所有入口统一走 setAutoPlay：关时立刻停住（连段尾倒数一起打断）、把正在打的
 * 半句补完整、并记住选择；开时回到连续播放。playPart / seekTo / setPlayBtn /
 * btnPlay 都按 ST.auto 分派，别在别处另写一套，否则又会出现"关了还在自动播"。 */
const AUTO_KEY = 'escah_auto';
function autoPref() {
  try { return localStorage.getItem(AUTO_KEY) !== '0'; } catch (e) { return true; }
}
function setAutoPlay(on, opt = {}) {
  on = !!on;
  ST.auto = on;
  const el = $('#auto');
  if (el) el.checked = on;
  if (!opt.noSave) {
    try { localStorage.setItem(AUTO_KEY, on ? '1' : '0'); } catch (e) { }
  }
  setPlayBtn(ST.running);        // 主按钮文案跟着模式变（手动模式是「▶ 下一句」）
  if (opt.init) return;          // 刚载入页面：只恢复勾选，别去动还没开始的播放
  if (on) {
    if (opt.resume === false || !S.data || ST.running) return;
    if (ST.methods.length && ST.idx >= ST.methods.length
        && ST.part + 1 < S.data.parts.length) {
      playPart(ST.part + 1, { noResume: true });
    } else {
      $('#btnPlay').click();
    }
    return;
  }
  const atPartEnd = !!($('#msgText') && $('#msgText').classList.contains('partNotice'));
  pausePlayback();               // haltLoop → skipAll 打断 3 秒倒数与所有句间等待
  ST.typing = false;
  ST.typeSkip = true;
  /* 打字机被掐断会停在半句；手动模式要把当前这一句显示完整再等切句。 */
  if (!atPartEnd && ST.curMsgIdx != null) {
    const m = ST.methods[ST.curMsgIdx], t = $('#msgText');
    if (m && m.Method === 'ShowMessage' && t) {
      t.classList.remove('partNotice');
      renderMsg(t, lineRich(m));
    }
  }
  setPlayBtn(false);
  if (atPartEnd) showPartNotice(partNoticeText());
  if (!opt.silent) {
    toast(L10N.t('ctrl.autoOff',
      '已切换到手动 · 点「▶ 下一句」或按回车 / 方向键切句'));
  }
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
/** 掐断当前语音：停止播放**并立刻结束它在等的 Promise**。
 *  以前只 pause 不 resolve —— 语音停了但 await voiceP 还挂着，只能等 40 秒
 *  硬兜底，所以"在一句正在念台词的地方按住 K"完全推不动，声音还卡在半路。 */
function cutVoice() {
  const a = ST.voice;
  if (!a) return;
  try { a.pause(); } catch (e) { }
  if (a._fin) { try { a._fin(); } catch (e) { } }
}

/** 按住 K 快进：跳过所有等待、掐断打字机与语音，逐句极速推进 */
function setRush(on) {
  on = !!on;
  if (on === ST.rush) return;
  ST.rush = on;
  if (on) {
    if (!ST.auto) {
      // 手动模式没有自动循环可加速：按住 K 就连续切句（别把连续播放偷偷打开）
      rushTimer = setInterval(() => nextLine(), 90);
      return;
    }
    if (!ST.running) togglePlay();          // 暂停状态下按 K 先续播
    ST.typeSkip = true;
    cutVoice();                             // 关键：停语音 + 放开 voiceP 的等待
    rushTimer = setInterval(() => {
      ST.typeSkip = true;
      skipAll();                            // 只清等待；不再反复 pause 语音
    }, 16);
  } else if (rushTimer) {
    clearInterval(rushTimer);
    rushTimer = null;
  }
}

/* ================= 设置 ================= */
/* 立绘相关（尺寸 / 纵向位置 / 水平偏移）已全部从设置页移除：
   游戏演出里立绘就是满高 720、落在官方落点上，任何缩放或位移都只会偏离原作，
   所以这几个键不再存在，CSS 里 var(--standY,0px) / var(--standOX,0px) 兜底即可。 */
const DEFAULTS = { alpha: 90, fs: 21, typeRate: 1, showWin: true,
              bgmVol: 0.5, seVol: 0.5, voiceVol: 0.5, fit: '1x',
              instantText: false, pausePerChar: 90, pauseBase: 400,
              pauseAfterVoice: 250, showPartial: false,
              holdLastPart: true,          // 角色剧情播完倒数第二段就停（默认勾选）
              // 默认按「实装日期 新→旧」：列表里最新的角色在最上面，
              // 比按 ID 排更符合"我想看看最近有什么新内容"的使用习惯
              sortBy: 'date-desc', rarSel: '', useX2: true,
              msgBg: '10,14,22', msgColor: '#f2f5fb',
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
  /* 「立即显示整句」时打字机根本不跑，打字速度（typeRate）没有作用 → 置灰。
     注意「每字停留 / 整句基础停顿 / 语音结束后停顿」仍然有效：它们是每句
     停留多久（ShowMessage 里 pauseBase + 字数×pausePerChar），两种显示方式都走。 */
  if ($('#setType')) $('#setType').disabled = !!SET.instantText;
  // 立绘高度固定 720（舞台高）：游戏演出就是满高，缩小会顶不住画面。
  // --standY / --standOX 也彻底不再写：位置一律按官方落点，CSS 的 var 兜底就是 0。
  st.style.setProperty('--standH', '720px');
  updateStandScale();
  ST.typeRate = SET.typeRate;
  fitStage();
  $('#valA').textContent = SET.alpha + '%';
  $('#valFS').textContent = SET.fs + 'px';
  $('#valType').textContent = Math.round(ST.typeRate * 100) + '%';
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
  $('#setBgm').value = Math.round((SET.bgmVol ?? .7) * 100);
  $('#setSe').value = Math.round((SET.seVol ?? .9) * 100);
  $('#setVoice').value = Math.round((SET.voiceVol ?? 1) * 100);
  $('#setFit').value = SET.fit || '1x';
  $('#setTextMode').value = SET.instantText ? 'instant' : 'type';
  $('#setPpc').value = SET.pausePerChar ?? 90;
  $('#setPb').value = SET.pauseBase ?? 400;
  $('#setPav').value = SET.pauseAfterVoice ?? 250;
  $('#setPartial').checked = !!SET.showPartial;
  const hp = $('#setHoldPart5');
  if (hp) hp.checked = SET.holdLastPart !== false;
  const ux = $('#setUseX2');
  if (ux) ux.checked = SET.useX2 !== false;
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
  // 只碰 SSR/SR/R（带 data-r 的）；第四格「播放记录」不是稀有度，状态由 setHistView 管
  document.querySelectorAll('.rar-row button[data-r]').forEach(b =>
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
  // 立绘尺寸/位置三项已废弃：老 escah_set / prefs 里残留的键直接扔掉，
  // 免得别处（视频导出等）还以为它们有意义。
  delete SET.standH; delete SET.standY; delete SET.standOX;
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

/* ============ 设置页的悬停说明气泡（替代原生 title） ============
 * 原生 title 的气泡字号由浏览器决定（Edge/Chrome 约 12px），比全站 14px 的字号下限
 * 还小，看着像"缩水的说明"。这里自绘一个 14px 气泡：
 *   hover → 把 title 暂存到 dataset.tip 并移除 title（不移除的话原生小气泡
 *          会和新气泡一起弹出来）→ 摆到控件上方，放不下翻到下方；
 *           移开再把 title 还回去。
 * 为什么不改成 data-tip：i18n.js 的 applyDom() 只认 title，切语言时会重写它，
 * 用 title 做唯一数据源，气泡就能自动跟着语言走。 */
function initSettingTips() {
  const P = $('#settings');
  if (!P) return;
  let box = null, timer = 0, cur = null;
  const hide = () => {
    clearTimeout(timer); timer = 0;
    if (box) box.classList.remove('on');
    if (cur && cur.dataset.tip != null) {
      cur.setAttribute('title', cur.dataset.tip);
      delete cur.dataset.tip;
    }
    cur = null;
  };
  const show = el => {
    const txt = el.getAttribute('title');
    if (!txt) return;
    el.dataset.tip = txt;
    el.removeAttribute('title');        // 关掉原生小气泡
    cur = el;
    if (!box) {
      box = document.createElement('div');
      box.id = 'tipBox';
      document.body.appendChild(box);
    }
    box.textContent = txt;
    box.classList.add('on');
    // 先归零量尺寸，再按控件矩形摆位（fixed + getBoundingClientRect 同一坐标系）
    box.style.left = '0px'; box.style.top = '0px';
    const r = el.getBoundingClientRect();
    const bw = box.offsetWidth, bh = box.offsetHeight;
    const x = Math.min(Math.max(8, r.left), innerWidth - bw - 8);
    let y = r.top - bh - 8;
    if (y < 8) y = r.bottom + 8;
    box.style.left = Math.round(x) + 'px';
    box.style.top = Math.round(y) + 'px';
  };
  P.addEventListener('mouseover', e => {
    const el = e.target && e.target.closest ? e.target.closest('[title]') : null;
    if (!el || !P.contains(el) || el === cur) return;
    hide();
    clearTimeout(timer);
    timer = setTimeout(() => show(el), 220);   // 略等一会儿，扫过面板时不刷屏
  });
  P.addEventListener('mouseout', e => {
    if (!cur) { clearTimeout(timer); timer = 0; return; }
    // 控件内部换焦（label → 里面的 select）不算离开
    if (e.relatedTarget && cur.contains(e.relatedTarget)) return;
    hide();
  });
  P.addEventListener('scroll', hide, { passive: true });
  ['#btnSet', '#setClose'].forEach(s => {
    const b = $(s);
    if (b) b.addEventListener('click', hide);   // 面板一关就收掉
  });
  /* 触屏没有 hover：点一下带「?」的那一项就地弹出说明，点面板别处收起。
     只在没有 hover 能力的设备上开，桌面仍然靠鼠标悬停（不然点下拉框会顺带弹气泡）。
     注意这里直接查媒体查询：CAN_HOVER 定义在文件靠后的位置，模块初始化阶段读不到。 */
  const canHover = !!(window.matchMedia
    && window.matchMedia('(hover: hover) and (pointer: fine)').matches);
  if (!canHover) {
    P.addEventListener('click', e => {
      const t = e.target;
      if (!t || !t.closest) return;
      // 点在控件本体上（下拉框 / 复选框 / 滑块）不当作"想看说明"
      if (t.closest('select,input,button')) return;
      const el = t.closest('[title]');
      if (!el) { hide(); return; }
      /* 关键：<label> 里包着 select 时，浏览器的默认动作会把这次点击"转发"给
         里面的控件（弹出下拉框），并在 select 上再派发一次 click —— 那次 click
         往上找不到带 title 的元素就走 hide()，于是气泡闪一下就消失。
         取消 label 的默认动作即可（2026-11-11 手机实测，只有包着下拉框的那项有）。 */
      e.preventDefault();
      hide();
      show(el);
    });
  }
}
initSettingTips();

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
$('#setPartial').onchange = e => { SET.showPartial = e.target.checked; applySettings(); };
if ($('#setHoldPart5')) {
  $('#setHoldPart5').onchange = e => {
    SET.holdLastPart = e.target.checked;
    applySettings();
    toast(SET.holdLastPart
      ? '角色剧情播完第 5 段会停下，最后一段需手动切换'
      : '自动模式下会连播到第 6 段');
  };
}
if ($('#setUseX2')) {
  $('#setUseX2').onchange = e => {
    SET.useX2 = e.target.checked;
    applySettings();
    // 已渲染的图不会自动换，重画当前画面即可
    if (ST.methods.length) seekTo(ST.curMsgIdx ?? 0, { keepPaused: !ST.running });
    toast(SET.useX2 ? '将优先使用超分图' : '只用原图（更省内存）');
  };
}
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
/* 「播放记录」视图开关：点 rar-row 第四格切换，列表换成"最近看过什么"。
   和分类（src）、资源筛选（dlf）、排序/稀有度一样属于"列表筛选条件"，
   刷新后要留住 —— 否则每次打开都得重点一次（用户 2026-11-09 反馈）。
   注意别和 escah_hist 混了：那个是右侧「文本列表」面板的开合，故意不跨设备同步。 */
let HIST_ON = false;
const HIST_KEY = 'escah_hist_view';   // 偏好层不可用时的兜底键

/** 读「播放记录」开关：偏好层优先（能跟 userdata/prefs.json 跨设备），本机老键兜底 */
function readHistPref() {
  const v = (UD && UD.prefValue) ? UD.prefValue('ui', 'hist', null) : null;
  if (v != null) return v === '1' || v === 1 || v === true;
  try { return localStorage.getItem(HIST_KEY) === '1'; } catch (e) { return false; }
}

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

  /* 每一条都跟左侧「角色」列表用**同一套两行卡片**（用户 2026-11-14 要求）：
     横幅 + 名字 + 进度信息，行尾换成「复制名字」和「删记录」两个小图标；
     鼠标移上去同样出 #bnrTip 详情浮窗（横幅原尺寸、左对齐）。 */
  list.forEach(r => {
    const key = r.key;
    const title = r.t || storyTitle(key);
    const isChar = isCharKey(key);
    const cid = isChar ? String(key).slice(5) : '';
    const kind = isChar ? 'char' : String(key).split('_')[1];
    const st = window.PACKSTATE ? PACKSTATE.status(key) : 'unknown';
    const L = imgLayer();
    const inst = st === 'ready' || st === 'soft-missing';
    // 横幅 / 头像都先问本机清单，缺图时退回首字色块，别对不存在的文件发请求
    const bnr = (L.banners || inst) ? bannerOf(key) : '';
    const avs = (cid && (L.avatars || inst)) ? avatarCands(cid) : [];
    const chain = (bnr ? [imgSrc(bnr)] : []).concat(avs);
    const meta = (cid && window.__CHARMETA ? (window.__CHARMETA[cid] || {}) : {});
    const rar = cid ? rarityOf(cid) : '';

    const d = document.createElement('div');
    d.className = 'char-btn bnr hist' + (key === S.key ? ' on' : '');
    d.dataset.key = key;
    if (cid) d.dataset.id = cid;
    d.title = `${title}\n上次看到：第 ${r.p + 1} 段，第 ${UD.lineNo(r)} 句 / 共 ${r.n} 句`
      + `\n${new Date(r.ts).toLocaleString()}`;
    d.innerHTML =
      `<span class="bnr">` + (chain.length ? '<img alt="" loading="lazy">'
        : `<span class="ava-ph">${esc(String(title).slice(0, 1))}</span>`) + `</span>` +
      `<span class="info"><span class="nm">`
      + (isChar ? '' : `<span class="srcTag" data-k="${kind}">${ADV_TAG[kind] || '剧'}</span>`)
      + `${esc(title)}</span>`
      + `<span class="sub"><span>第 ${r.p + 1} 段</span>`
      + `<span class="pg">看到 ${UD.progPct(r)}%</span>`
      + `<span>${UD.lineNo(r)}/${r.n} 句</span>`
      + `<span>${UD.ago(r.ts)}</span></span>`
      + `<span class="cpy" title="复制名字">⧉</span>`
      + `<span class="del" title="从记录里删掉这一条（不影响收藏）">✕</span>`
      + `</span>`;
    if (chain.length) applyChain(d.querySelector('img'), chain);
    bindTip(d, () => {
      const parts = r.n ? `第 ${r.p + 1} 段 · 共 ${r.n} 句` : '';
      return (bnr ? `<img src="${esc(imgSrc(bnr))}" alt="" `
        + `onerror="this.style.display='none'">` : '')
        + `<div style="font-weight:600;margin-bottom:4px">${esc(title)}</div>`
        + `<dl>`
        + (cid ? `<dt>ID</dt><dd>${cid}</dd>` : '')
        + (cid ? `<dt>稀有度</dt><dd>${esc(meta.r || '未收录')}`
          + `${meta.d ? ' · 实装 ' + esc(meta.d) : ''}</dd>` : '')
        + `<dt>剧情</dt><dd>${parts || '—'}</dd>`
        + `<dt>上次看到</dt><dd>第 ${r.p + 1} 段 · 第 ${UD.lineNo(r)}/${r.n} 句`
        + `（${UD.progPct(r)}%，${UD.ago(r.ts)}）</dd>`
        + `<dt>状态</dt><dd>${st === 'missing' ? '资源未下载'
          : st === 'soft-missing' ? '缺公共素材' : '可播放'}</dd>`
        + `</dl>`;
    });
    d.onclick = () => openStory(key);
    d.querySelector('.cpy').onclick = ev => {
      ev.stopPropagation();
      copyText(title, `已复制：${title}`);
    };
    d.querySelector('.del').onclick = ev => {
      ev.stopPropagation();
      UD.clearProg(key);
      renderHistList(box, filter);
    };
    box.appendChild(d);
  });
  endScroll(box, sc);
}

function renderSidebarBody(filter = '') {
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

/* 侧栏的**唯一入口**：画完再补一次"深链收尾定位"。
 * 为什么要在每次重画后都补：侧栏会被很多异步事件重画（安装状态到位、
 * 云端角色清单回来、偏好层 hydrate 回来、语言切换），它们各走各的 renderSidebar，
 * 把深链那一刻做好的分类 / 分组展开 / 选中行滚位全部冲掉 —— 用户看到的就是
 * "链接在播放，左侧列表却没跟着选中"（2026-11-09 反馈）。
 * FOCUS_KEY 为空时（绝大多数情况）settleFocus 立刻返回，不增加开销。 */
function renderSidebar(filter = '') {
  renderSidebarBody(filter);
  settleFocus();
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
/* 用户自己在侧栏里滚/点了 → 放弃深链定位，别再在下一次重画（下载状态更新等）
   时把列表拽回深链那一行。只认真实交互，不认 scroll 事件：revealRow 自己也会滚，
   用 scroll 判断会把它自己的定位当成用户操作。 */
['wheel', 'pointerdown'].forEach(ev => {
  $('#charList').addEventListener(ev, () => { FOCUS_KEY = null; }, { passive: true });
});

/* 搜索防抖：384 个角色 / 227 部剧情，逐字符重渲染会明显掉帧 */
let _searchTimer = 0;
$('#search').oninput = e => {
  FOCUS_KEY = null;              // 用户自己在筛了，深链定位不再插手
  clearTimeout(_searchTimer);
  const v = e.target.value;
  _searchTimer = setTimeout(() => renderSidebar(v), 120);
};
// 搜索框尾部的清除叉（清空后走同一条 input 路径，列表恢复空筛选）
if (window.ESC && ESC.searchClear) ESC.searchClear($('#search'));

/* 来源切换：角色 / 主线 / 活动 / 其他 */
document.querySelectorAll('#srcRow button').forEach(b => {
  b.onclick = () => {
    FOCUS_KEY = null;              // 用户自己点分类了，深链定位不再抢回列表
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

/* ================= 深链定位（?char= / ?adv=） =================
 * 分享链接 / 脚本化打开时，左侧列表必须切到这部剧情**所在的分类**、退出
 * 「播放记录」视图、清掉搜索词，并把那一行滚进视野。
 * 以前只有 ?adv= 会切分类，?char= 不会：用户上次停在「活动」或「播放记录」，
 * 用 ?char=387 打开角色剧情，左侧还是一片别的列表，目标行既画不出来也没有
 * 选中态（2026-11-09 用户反馈）。
 * 只改"本次显示"，**不写**回用户存下来的筛选条件（save=false），
 * 与手点分类按钮的行为区分开 —— 临时导航不该覆盖用户的偏好。 */
let FOCUS_KEY = null, FOCUS_SRC = null;   // 本次深链定位的目标（见 applyRemotePrefsToUI）
function focusSidebarOn(key, opt) {
  if (!key) return;
  opt = opt || {};
  const part0 = Number.isInteger(opt.part) ? opt.part : null;
  let src = opt.src || 'char';
  if (!opt.src && !isCharKey(key)) {
    const a = advByKey(key);
    const kind = a ? a.kind : (String(key).split('_')[1] || 'other');
    src = (kind === 'main' || kind === 'event') ? kind : 'other';
  }
  SRC = src;
  openFocusGroup(key, part0);    // 「其他」折叠分组要先展开，目标行才存在
  FOCUS_KEY = key;
  FOCUS_SRC = src;
  document.querySelectorAll('#srcRow button').forEach(x =>
    x.classList.toggle('on', x.dataset.src === SRC));
  setHistView(false, false);     // 离开「播放记录」视图，但别把开关存成关掉
  const si = $('#search');
  if (si && si.value) si.value = '';
  updateCnt();
  renderSidebar();       // 画完由 settleFocus() 兜"筛选/折叠分组挡住目标行"的情况
}

/** 「其他」视图：把目标那一话所在的入口组展开。
 *  折叠态下那一话根本没画出来，选中态与滚位都无从谈起。
 *  幂等、可反复调用：入口清单（data/othereps.js）是运行时按需加载的，
 *  常常比深链晚到，所以每次重画侧栏都补一次。 */
function openFocusGroup(key, part0) {
  if (HIST_ON || SRC !== 'other' || !key || isCharKey(key)) return;
  const groups = window.__OTHER_EPS || [];
  if (!groups.length) return;          // 清单还没到：等它到了重画时再补
  let want = Number.isInteger(part0) ? part0 : null;
  if (want === null) {
    const qp = +(new URLSearchParams(location.search).get('part') || 0);
    if (qp > 0) want = qp - 1;
  }
  const hasEp = g => (g.eps || []).some(e => e.k === key && (want === null || e.i === want));
  const hasKey = g => (g.eps || []).some(e => e.k === key);
  let hit = false;
  groups.forEach(g => { if (hasEp(g)) { OTHER_OPEN.add(g.no); hit = true; } });
  // 段号对不上（链接里的 ?part= 和实际段数不一致）就退一步：含这个包的组全展开
  if (!hit) groups.forEach(g => { if (hasKey(g)) { OTHER_OPEN.add(g.no); hit = true; } });
  if (!hit) OTHER_OPEN.add(/^adv_(tutorial|op)/.test(key) ? 'tuto' : 'cloud');
}

/** 每次重画侧栏后的"深链收尾"：放开挡路的筛选、展开折叠分组、补齐分片、
 *  把选中行滚进视野。FOCUS_KEY 为空（绝大多数情况）时立刻返回，不增加开销。 */
let _focusFixing = false;
function settleFocus() {
  if (_focusFixing || !FOCUS_KEY || HIST_ON) return;
  if (SRC === 'char') {
    if (!ensureFocusVisible()) revealRow(FOCUS_KEY);      // 放开筛选时它自己会重画 + 定位
    return;
  }
  if (SRC === 'other') {
    const before = OTHER_OPEN.size;
    openFocusGroup(FOCUS_KEY, null);
    if (OTHER_OPEN.size !== before) {     // 刚展开了目标组：得再画一次
      _focusFixing = true;
      try { renderSidebar(); } finally { _focusFixing = false; }
    }
  }
  revealRow(FOCUS_KEY);
}

/** 深链目标被「只看未安装 / 只看已收藏 / 稀有度」筛掉了就临时放开再画一次
 *  （不写回偏好），否则链接打开的剧情在列表里根本不存在，等于没定位。
 *  为什么不能只在深链那一刻判断一次：安装状态（PACKSTATE）是异步来的 ——
 *  启动瞬间所有角色都是 unknown，「只看未安装」会放行全部，看着"目标行在"，
 *  等状态到了重画才被筛掉。所以每次重画侧栏都要补一次。 */
function ensureFocusVisible() {
  if (_focusFixing || !FOCUS_KEY || HIST_ON || SRC !== 'char') return false;
  if (document.querySelector('#charList .char-btn[data-key="' + FOCUS_KEY + '"]')) return false;
  const dl = $('#dlFilter');
  if (!((dl && dl.value) || SET.rarSel)) return false;  // 搜索词挡的另说，不替用户改搜索
  _focusFixing = true;
  try {
    if (dl) dl.value = '';
    SET.rarSel = '';
    document.querySelectorAll('.rar-row button[data-r]').forEach(b => b.classList.remove('on'));
    renderSidebar();
    toast('已临时放开筛选，好让列表定位到当前剧情');
    revealRow(FOCUS_KEY);
  } finally {
    _focusFixing = false;
  }
  return true;
}

/** 把某一行滚进侧栏视野（深链目标可能在几百行之后，用户根本看不见）。
    主线/活动列表是分片渲染的（一次 ADV_BATCH 条），目标还没画出来就继续追加。
    只动侧栏自己的 scrollTop —— scrollIntoView 会连带把整页滚走。 */
function revealRow(key) {
  const box = $('#charList');
  if (!box || !key) return;
  const sel = '.char-btn[data-key="' + key + '"]';
  let row = box.querySelector(sel);
  for (let i = 0; !row && i < 60; i++) {
    if (HIST_ON || SRC === 'char' || SRC === 'other') break;
    if (advCursor >= advAll.length) break;
    appendAdvChunk();
    row = box.querySelector(sel);
  }
  if (!row) return;
  refreshSelection();            // 分片新画出来的行也要带上选中态
  const br = box.getBoundingClientRect(), r = row.getBoundingClientRect();
  if (r.top >= br.top && r.bottom <= br.bottom) return;
  box.scrollTop += (r.top - br.top) - Math.max(0, (br.height - r.height) / 2);
}

$('#sortBy').onchange = e => {
  SET.sortBy = e.target.value;
  applySettings();                 // 排序方式跟着设置一起存
  renderSidebar($('#search').value);
};
if ($('#dlFilter')) {
  $('#dlFilter').onchange = () => {
    FOCUS_KEY = null;            // 用户自己在筛了，深链定位不再插手
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
    FOCUS_KEY = null;            // 用户自己在筛了，深链定位不再插手
    const r = b.dataset.r;
    let arr = SET.rarSel ? SET.rarSel.split(',') : [];
    arr = arr.includes(r) ? arr.filter(x => x !== r) : arr.concat(r);
    SET.rarSel = arr.join(',');
    b.classList.toggle('on', arr.includes(r));
    applySettings();
    renderSidebar($('#search').value);
  };
});

/** 「播放记录」视图开关（rar-row 第四格）
 *  save 默认存：这是筛选条件，刷新/换设备都要还原（启动恢复时传 false，
 *  免得把刚读出来的值又打一次时间戳）。 */
const btnHist = document.querySelector('.rar-row button[data-f="hist"]');
function setHistView(on, save) {
  on = !!on;
  // class 先同步：startup 恢复时 HIST_ON 可能已经是目标值，不能因为提前 return 漏了高亮
  if (btnHist) btnHist.classList.toggle('on', on);
  if (HIST_ON === on) return;
  HIST_ON = on;
  if (save === false) return;
  if (window.UD && window.UD.syncPref) window.UD.syncPref('ui', 'hist', on ? '1' : '0');
  try { localStorage.setItem(HIST_KEY, on ? '1' : '0'); } catch (e) { }
}
if (btnHist) {
  btnHist.onclick = () => {
    FOCUS_KEY = null;            // 用户自己在切视图了，深链定位不再插手
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
  // fresh：换了剧情就清空舞台（背景/CG/立绘），不能沿用上一部剧情的画面
  playPart(Math.min(Math.max(want - 1, 0), S.data.parts.length - 1), { fresh: true });
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
  setPlayBtn(ST.running);                 // 主按钮（手动模式是「下一句」）也跟着语言重画
  if (ST.curMsgIdx != null && ST.methods[ST.curMsgIdx]) {
    // 正在播：整句重放（连带语音）；暂停中：直接把画面上的文字换掉
    if (ST.running) seekTo(ST.curMsgIdx, { forceResume: true });
    else if ($('#msgText').classList.contains('partNotice')) {
      showPartNotice(partNoticeText());     // 停在段界上：提示本身按新语言重画
    } else renderMsg($('#msgText'), lineRich(ST.methods[ST.curMsgIdx]));
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

/* ================= 分享：当前画面 + 勾选台词 → 一张长图 =================
 * 入口是顶栏的「分享」图标（header.js 注入，位置在「局域网二维码」左边）。
 *
 * 图片**不是系统截图**，而是把 #stage 里此刻正在显示的那几张图按各自的
 * 渲染矩形重画一遍（背景 → 立绘 → CG），所以：
 *   · 拿到的是 2560×1440 的清晰图，不受窗口缩放 / 浏览器缩放影响；
 *   · 只有画面、没有对话框 —— 台词按勾选顺序排在图片下方（像电影台词图）。
 * 滤镜只需复刻两类：
 *   · MulColor（feColorMatrix 对角线乘法）→ 读 SVG 的 values，用 multiply 重合成；
 *   · TalkCharacter 压暗 / DrawMode（brightness、sepia…）→ 直接交给 ctx.filter。
 */
/* 成品宽：**1080p = 1920×1080**（默认）/ 720p = 1280×720（游戏原生）。
   注意「1080」指的是高度，不是宽度 —— 用户 2026-11-14 纠正过这一点。 */
const SHARE_W = 1920;
const SHARE_DESIGN = 1080;   // 版式的设计基准宽：所有字号/间距按这个算，再等比放大
/* 舞台截图倍率：2 = 2560×1440（超采样）。输出只有 1920 宽，但立绘大多有 2 倍超分图，
   按 2 倍采再缩回 1920 才不糊；显卡/内存代价只有一瞬间。 */
const SHARE_SHOT = 2;
const SHARE_MAX_PICK = 20;   // 一次最多拼 20 句（再多图会长到没法看）

function shareStoryTitle() {
  return (window.ESC && ESC.storyTitle && ESC.storyTitle(ST.key || ''))
    || (S.data && S.data.title) || '剧情';
}

/** 取 SVG 乘算滤镜（feColorMatrix）的对角线 = R/G/B 三个乘法系数 */
function mulMatrixDiag(id) {
  const node = document.getElementById(id);
  const cm = node && node.querySelector('feColorMatrix');
  if (!cm) return null;
  const v = (cm.getAttribute('values') || '').trim().split(/[\s,]+/).map(Number);
  if (v.length !== 20) return null;
  return [v[0], v[6], v[12]];
}

/** 把 el 到 stage 之间的滤镜拆成两半：可乘算的 RGB 系数 + 其余 CSS 滤镜。
 *  drop-shadow 丢掉（海报里不需要一层阴影，而且投影会被画框裁掉半截）。 */
function shareFilterInfo(el, stop) {
  const mul = [1, 1, 1];
  const parts = [];
  for (let n = el; n; n = n.parentElement) {
    const f = getComputedStyle(n).filter;
    if (f && f !== 'none') {
      const reUrl = /url\(["']?#([^)"']+)["']?\)/g;
      let m;
      while ((m = reUrl.exec(f))) {
        const d = mulMatrixDiag(m[1]);
        if (d) { mul[0] *= d[0]; mul[1] *= d[1]; mul[2] *= d[2]; }
      }
      const reFx = /(brightness|contrast|saturate|grayscale|sepia|hue-rotate)\([^)]*\)/g;
      let m2;
      while ((m2 = reFx.exec(f))) parts.push(m2[0]);
    }
    if (n === stop) break;
  }
  const needMul = mul[0] < 0.999 || mul[1] < 0.999 || mul[2] < 0.999;
  return { mul: needMul ? mul : null, filter: parts.join(' ') };
}

/** 按 object-fit 把一个 <img> 画进 box（box 是它在画布上的矩形） */
function sharePaintImage(g, im, box, fit, alpha, fi) {
  const iw = im.naturalWidth, ih = im.naturalHeight;
  if (!iw || !ih) return;
  let dw = box.w, dh = box.h, dx = box.x, dy = box.y;
  if (fit === 'cover' || fit === 'contain') {
    const s = fit === 'cover' ? Math.max(box.w / iw, box.h / ih)
      : Math.min(box.w / iw, box.h / ih);
    dw = iw * s; dh = ih * s;
    dx = box.x + (box.w - dw) / 2;
    dy = box.y + (box.h - dh) / 2;
  } else if (fit === 'none') {
    dw = iw; dh = ih;
    dx = box.x + (box.w - iw) / 2;
    dy = box.y + (box.h - ih) / 2;
  }

  /* 没有逐图滤镜时直接画（绝大多数情况）。
     有滤镜时必须先画到临时画布：multiply 合成需要"只作用在这张图自身"的层。 */
  if (!fi || (!fi.mul && !fi.filter)) {
    g.save();
    g.globalAlpha = alpha;
    g.beginPath(); g.rect(box.x, box.y, box.w, box.h); g.clip();
    g.drawImage(im, dx, dy, dw, dh);
    g.restore();
    return;
  }
  const tw = Math.max(1, Math.ceil(box.w)), th = Math.max(1, Math.ceil(box.h));
  const t = document.createElement('canvas');
  t.width = tw; t.height = th;
  const tg = t.getContext('2d');
  if (fi.filter) tg.filter = fi.filter;
  tg.drawImage(im, dx - box.x, dy - box.y, dw, dh);
  tg.filter = 'none';
  if (fi.mul) {
    tg.globalCompositeOperation = 'multiply';
    tg.fillStyle = 'rgb(' + fi.mul.map(v => Math.round(Math.min(1, Math.max(0, v)) * 255)).join(',') + ')';
    tg.fillRect(0, 0, tw, th);
    // 乘法会把整块矩形都染上色，再用原图的 alpha 把形状取回来
    tg.globalCompositeOperation = 'destination-in';
    tg.drawImage(im, dx - box.x, dy - box.y, dw, dh);
    tg.globalCompositeOperation = 'source-over';
  }
  g.save();
  g.globalAlpha = alpha;
  g.beginPath(); g.rect(box.x, box.y, box.w, box.h); g.clip();
  g.drawImage(t, box.x, box.y);
  g.restore();
}

/** 把当前舞台画面重画成一张 2560×1440 的画布（失败返回 null） */
function shareStageShot() {
  const stage = $('#stage');
  if (!stage) return null;
  const sr = stage.getBoundingClientRect();
  if (!sr.width || !sr.height) return null;
  const W = 1280 * SHARE_SHOT, H = 720 * SHARE_SHOT;
  const k = W / sr.width;                    // client px → 画布 px
  const cv = document.createElement('canvas');
  cv.width = W; cv.height = H;
  const g = cv.getContext('2d');
  g.fillStyle = '#05060a';
  g.fillRect(0, 0, W, H);

  const boxOf = el => {
    const r = el.getBoundingClientRect();
    return { x: (r.left - sr.left) * k, y: (r.top - sr.top) * k,
      w: r.width * k, h: r.height * k };
  };
  // 自身 + 祖先（到 #stage）的不透明度相乘：立绘淡入/退场、TalkCharacter 都在这条链上
  const alphaOf = el => {
    let a = 1;
    for (let n = el; n; n = n.parentElement) {
      const o = parseFloat(getComputedStyle(n).opacity);
      if (isFinite(o)) a *= o;
      if (n === stage) break;
    }
    return a;
  };
  const paint = (im, prefill) => {
    if (!im.complete || !im.naturalWidth) return;
    const cs = getComputedStyle(im);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    const a = alphaOf(im);
    if (a < 0.02) return;                    // 正在淡出的旧图不画
    const box = boxOf(im);
    if (box.w < 1 || box.h < 1) return;
    if (prefill) { g.fillStyle = prefill; g.fillRect(box.x, box.y, box.w, box.h); }
    sharePaintImage(g, im, box, cs.objectFit || 'fill', a, shareFilterInfo(im, stage));
  };
  // 层序与 #stage 的 DOM 顺序一致：背景 → 立绘 → CG（背面雾效/粒子/闪屏不进图）
  $('#bgLayer').querySelectorAll('img').forEach(im => paint(im));
  $('#standLayer').querySelectorAll('.stand img').forEach(im => paint(im));
  $('#cgLayer').querySelectorAll('img').forEach(im => paint(im, '#05060a'));

  // DrawMode（sepia / 灰度）是挂在 #stage 上的整层滤镜
  const sf = getComputedStyle(stage).filter;
  if (sf && sf !== 'none' && sf.indexOf('url(') < 0) {
    const t = document.createElement('canvas');
    t.width = W; t.height = H;
    const tg = t.getContext('2d');
    tg.filter = sf;
    tg.drawImage(cv, 0, 0);
    g.clearRect(0, 0, W, H);
    g.drawImage(t, 0, 0);
  }
  return cv;
}

/** 台词拼成海报时的断行：**只按画面宽度自适应**，不再沿用剧本里的换行。
 *
 *  剧本里的 `\n` 是为了在游戏那个 500 多 px 宽的对话框里断行，拿到 1080 宽的海报上
 *  照着断开，一句话会被拆成两三行、图白白变高一截（用户 2026-11-14 反馈）。
 *  这里先把这些「排版用换行」合并掉：中日文直接接上；两侧都是拉丁字母/数字时
 *  补一个空格，避免 "Surrey" + "park" 粘成一个词。然后再逐字量宽断行。 */
function shareJoin(text) {
  return String(text || '').replace(/[ \t]*\n+[ \t]*/g, (m, off, all) => {
    const prev = all[off - 1] || '', next = all[off + m.length] || '';
    return (/[A-Za-z0-9,.;:!?'"]/.test(prev) && /[A-Za-z0-9]/.test(next)) ? ' ' : '';
  }).replace(/[ \t]{2,}/g, ' ');
}

/* ---- 台词排版：说话人**和台词同一行**（金色名字 + 全角空格 + 正文），逐字自适应换行 ----
 *  以前名字单独占一行，一句普通台词也要多花 32px，一屏拼下来白白高一截
 *  （用户 2026-11-14 反馈）。现在把名字当成行首的一段（金色、稍小），
 *  换行时一起参与量宽，读起来就像「时贞　「……」」。
 *  中日文没有空格可分，只能逐字量宽；返回的行是 [{s:run, gold:bool}] 片段。 */
function shareLineRuns(meas, who, text, maxW, fs, fsName, fam) {
  const toks = [];
  if (who) toks.push({ s: who, gold: true }, { s: '　', gold: true });
  toks.push({ s: shareJoin(text), gold: false });
  const fontOf = gold => (gold ? '600 ' + fsName : '400 ' + fs) + 'px ' + fam;
  const lines = [];
  let cur = [], curW = 0;
  toks.forEach(t => {
    meas.font = fontOf(t.gold);
    for (const ch of t.s) {
      const w = meas.measureText(ch).width;
      if (curW + w > maxW && cur.length) { lines.push(cur); cur = []; curW = 0; }
      const last = cur[cur.length - 1];
      if (last && last.gold === t.gold) last.s += ch;
      else cur.push({ s: ch, gold: t.gold });
      curW += w;
    }
  });
  if (cur.length) lines.push(cur);
  return lines;
}

/** 画一行（居中），金色名字与白色正文分段上色 */
function shareDrawLine(g, runs, cx, y, fs, fsName, fam) {
  const fontOf = gold => (gold ? '600 ' + fsName : '400 ' + fs) + 'px ' + fam;
  /* 必须显式设回左对齐：首句若走播放器样式，会把 ctx.textAlign 改成
     center/right（跟用户的文字对齐设置走），不还原的话后面每一行都会以
     "左边缘坐标"当中心画，整块台词往左挤在一起（2026-11-14 用户反馈的错乱）。 */
  g.textAlign = 'left';
  let w = 0;
  runs.forEach(r => { g.font = fontOf(r.gold); w += g.measureText(r.s).width; });
  let x = cx - w / 2;
  runs.forEach(r => {
    g.font = fontOf(r.gold);
    g.fillStyle = r.gold ? '#e0b566' : '#f2f5fb';
    g.fillText(r.s, x, y);
    x += g.measureText(r.s).width;
  });
}

/** 读播放器当前对话框的样式（用户在「设置」里调过的那套），单位都是舞台 px（1280 宽）。
 *  首句要按"播放时的样子"画进图里时用它，这样字体/字号/颜色/对齐/描边/对话框外观
 *  都跟用户屏幕上看到的一致，而不是海报自带的白字排版。 */
function shareMsgStyle() {
  const win = $('#msgWindow'), txt = $('#msgText'), stage = $('#stage');
  if (!win || !txt || !stage) return null;
  const cw = getComputedStyle(win), ct = getComputedStyle(txt);
  const nm = $('#msgName'), cn = nm ? getComputedStyle(nm) : null;
  const n = (v, d) => { const x = parseFloat(v); return isFinite(x) ? x : d; };
  const mtx = /matrix\(([^)]+)\)/.exec(ct.transform || '');
  return {
    mode: win.classList.contains('wb-bare') ? 'bare'
      : win.classList.contains('wb-soft') ? 'soft' : 'box',
    left: n(cw.left, 52), right: n(cw.right, 52), bottom: n(cw.bottom, 26),
    minH: n(cw.minHeight, 150),
    padX: n(cw.paddingLeft, 26), padTop: n(cw.paddingTop, 20),
    radius: n(cw.borderTopLeftRadius, 10),
    borderW: n(cw.borderTopWidth, 1), borderColor: cw.borderTopColor,
    bg: cw.backgroundColor,
    family: ct.fontFamily, weight: ct.fontWeight,
    size: n(ct.fontSize, 21), lineH: n(ct.lineHeight, 34),
    color: ct.color, align: ct.textAlign || 'left',
    stroke: n(getComputedStyle(stage).getPropertyValue('--msgStroke'), 1.2),
    shiftX: mtx ? (parseFloat(mtx[1].split(',')[4]) || 0) : 0,
    nameColor: cn ? cn.color : '#ffe9b8',
    nameSize: cn ? n(cn.fontSize, 16.8) : 16.8,
    nameWeight: cn ? cn.fontWeight : 700,
    // 有背景渐变 = 游戏里那个金色名条；无框/无底衬模式下名字只剩金字
    namePill: !!(cn && cn.backgroundImage && cn.backgroundImage.indexOf('gradient') >= 0),
    // 名字的三档对齐（设置里的「话者名 → 对齐」）
    nameAlign: (nm && nm.classList.contains('align-center')) ? 'center'
      : (nm && nm.classList.contains('align-right')) ? 'right' : 'left',
  };
}

/** 纯文本按宽度断行（首句走播放器样式时用，名字另画名条，不混进正文） */
function shareWrapPlain(meas, text, maxW, font) {
  meas.font = font;
  const out = [];
  let cur = '';
  for (const ch of shareJoin(text)) {
    if (cur && meas.measureText(cur + ch).width > maxW) { out.push(cur); cur = ch; }
    else cur += ch;
  }
  if (cur) out.push(cur);
  return out.length ? out : [''];
}

function shareRoundRect(g, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  g.beginPath();
  g.moveTo(x + rr, y);
  g.arcTo(x + w, y, x + w, y + h, rr);
  g.arcTo(x + w, y + h, x, y + h, rr);
  g.arcTo(x, y + h, x, y, rr);
  g.arcTo(x, y, x + w, y, rr);
  g.closePath();
}

/** 把一句话画成「播放器里的对话框」（尺寸/位置/字体都按用户设置等比映射到图片上） */
function shareDrawPlayerMsg(g, W, imgH, st, who, text, scale) {
  const k = scale;                                   // 1280 舞台 → 图片宽
  const boxX = st.left * k;
  const boxW = W - (st.left + st.right) * k;
  const padX = st.padX * k, padTop = st.padTop * k;
  const fs = st.size * k, lh = st.lineH * k;
  const font = st.weight + ' ' + fs.toFixed(1) + 'px ' + st.family;
  const meas = document.createElement('canvas').getContext('2d');
  const lines = shareWrapPlain(meas, text, boxW - padX * 2, font);
  const textH = lines.length * lh;
  const boxH = Math.max(st.minH * k, textH + padTop + Math.round(18 * k));
  const boxY = imgH - st.bottom * k - boxH;

  // 对话框底色：有框模式画面板，无框模式只垫一层柔光（跟 CSS 的 wb-soft 一致）
  if (st.mode === 'box') {
    g.save();
    shareRoundRect(g, boxX, boxY, boxW, boxH, st.radius * k);
    g.fillStyle = st.bg;
    g.fill();
    if (st.borderW > 0) {
      g.lineWidth = Math.max(1, st.borderW * k);
      g.strokeStyle = st.borderColor;
      g.stroke();
    }
    g.restore();
  } else if (st.mode === 'soft') {
    const cx = boxX + boxW / 2, cy = boxY + boxH / 2;
    const rad = Math.max(boxW, boxH) * 0.62;
    const rg = g.createRadialGradient(cx, cy, rad * 0.25, cx, cy, rad);
    rg.addColorStop(0, 'rgba(0,0,0,.58)');
    rg.addColorStop(0.62, 'rgba(0,0,0,.42)');
    rg.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = rg;
    g.fillRect(boxX - padX, boxY - padTop * 2, boxW + padX * 2, boxH + padTop * 4);
  }

  // 正文
  const tx = st.align === 'center' ? boxX + boxW / 2
    : st.align === 'right' ? boxX + boxW - padX : boxX + padX;
  g.textAlign = st.align === 'center' ? 'center' : st.align === 'right' ? 'right' : 'left';
  g.textBaseline = 'middle';
  const shift = st.shiftX * k;
  lines.forEach((t, i) => {
    const y = boxY + padTop + i * lh + lh / 2;
    if (st.stroke > 0.15) {
      g.font = font;
      g.lineWidth = Math.max(1, st.stroke * 2 * k);
      g.lineJoin = 'round';
      g.miterLimit = 2;
      g.strokeStyle = 'rgba(0,0,0,.9)';
      g.strokeText(t, tx + shift, y);
    }
    g.font = font;
    g.fillStyle = st.color;
    g.fillText(t, tx + shift, y);
  });

  // 说话人：有金名条就画名条（游戏原样），无框模式只画金字
  if (who) {
    const nfs = st.nameSize * k;
    const font2 = st.nameWeight + ' ' + nfs.toFixed(1) + 'px ' + st.family;
    g.font = font2;
    g.textAlign = 'left';
    g.textBaseline = 'middle';
    const nw = g.measureText(who).width;
    const padN = 16 * k, pillH = nfs + 10 * k;
    // 名条位置跟「设置 → 话者名 → 对齐」一致：左/中/右三档
    const pillW = nw + padN * 2;
    const nx = st.nameAlign === 'center' ? boxX + (boxW - pillW) / 2
      : st.nameAlign === 'right' ? boxX + boxW - 22 * k - pillW
        : boxX + 22 * k;
    const ny = boxY - (15 * k) - pillH / 2;
    if (st.namePill) {
      const grd = g.createLinearGradient(0, ny - pillH / 2, 0, ny + pillH / 2);
      grd.addColorStop(0, '#f0d090');
      grd.addColorStop(1, '#c99b45');
      shareRoundRect(g, nx, ny - pillH / 2, nw + padN * 2, pillH, 5 * k);
      g.fillStyle = grd;
      g.fill();
      g.fillStyle = '#1a1206';
    } else {
      g.lineWidth = Math.max(1, (st.stroke + 1) * 2 * k);
      g.lineJoin = 'round';
      g.strokeStyle = 'rgba(0,0,0,.92)';
      g.strokeText(who, nx + padN, ny);
      g.fillStyle = st.nameColor;
    }
    g.fillText(who, nx + padN, ny);
  }
}

/** 画面 + 台词 → 成品长图（picks 是 ST.allLines 的下标，已按顺序排好）
 *
 *  版式按「观看页面本来的样子」来（用户 2026-11-14 要求）：
 *    第一句压在**图片底部**（就是游戏里对话框所在的位置，垫一层需要的暗角），
 *    其余句子接在图片下方，最后一行是剧名 · 第 N/M 段。
 *  ownStyle = true 时首句直接按播放器的对话框样式画（字体/颜色/对话框外观都是用户设置），
 *  false 用海报自带的白字排版。
 *  W 可传 720（游戏原生分辨率那档），所有尺寸按 1080 基准等比缩放，
 *  版式完全一样、只是整张图小一圈。 */
function shareCompose(shot, picks, W, ownStyle, fsBase) {
  W = W || SHARE_W;
  const k = W / SHARE_DESIGN;            // 设计基准 1080 → 目标宽（1920 / 1280）
  const imgH = Math.round(W * 9 / 16);
  const pad = Math.round(48 * k);
  /* 间距口径（用户 2026-11-14 指定，以**字号**为单位）：
     · 行间距 = 0.5 字号 → 行高 = 1.5 字号
     · 相邻两句台词之间 = 1.0 字号（即比行间距再多 0.5 字号）
     字号本身可由弹窗里的「字号」下拉调（海报样式下才生效，默认 30）。 */
  const fsBasePx = Math.max(14, Math.min(60, Number(fsBase) || 30));
  const fsTxt = Math.round(fsBasePx * k), fsName = Math.round(fsTxt * 0.83);
  const lhTxt = Math.round(fsTxt * 1.5);          // 行高
  const gapBlock = Math.round(fsTxt * 0.5);       // 句间比行间多出来的部分
  /* 首句（图内）→ 第二句（图下）要"看起来"是 1.0 字号：行框本身已经带了
     (行高 - 字号)/2 的上下留白，这里只补剩下的那一段，一半放在图片里、一半放图片外。 */
  const leadHalf = Math.max(0, Math.round((lhTxt - fsTxt) / 2));
  const edgeGap = Math.max(6, fsTxt - leadHalf * 2);
  const restTopGap = Math.round(edgeGap * 0.6);
  const headBottom = edgeGap - restTopGap;
  const fam = getComputedStyle($('#msgText')).fontFamily || 'sans-serif';
  const meas = document.createElement('canvas').getContext('2d');
  const maxW = W - pad * 2;

  const blocks = [], src = [];
  picks.forEach(gi => {
    const l = ST.allLines[gi];
    if (!l || !String(l.text || '').trim()) return;
    src.push(l);
    blocks.push(shareLineRuns(meas, l.who || '', l.text, maxW, fsTxt, fsName, fam));
  });
  if (!blocks.length) return null;

  const rest = blocks.slice(1);
  let textH = 0;
  rest.forEach((lines, i) => {
    if (i) textH += gapBlock;
    textH += lines.length * lhTxt;
  });
  const footH = Math.round(82 * k);
  const H = imgH + (rest.length ? restTopGap + textH : 0) + footH;

  const cv = document.createElement('canvas');
  cv.width = W; cv.height = H;
  const g = cv.getContext('2d');
  g.fillStyle = '#0a0d14';
  g.fillRect(0, 0, W, H);
  g.drawImage(shot, 0, 0, W, imgH);

  g.textAlign = 'left';
  g.textBaseline = 'middle';

  // 图与文字区的收边渐变先画：首句若走播放器样式（对话框），不能被这层压暗
  if (rest.length) {
    const fadeH = Math.round(imgH * 0.14);
    const grd = g.createLinearGradient(0, imgH - fadeH, 0, imgH + 4);
    grd.addColorStop(0, 'rgba(10,13,20,0)');
    grd.addColorStop(1, 'rgba(10,13,20,1)');
    g.fillStyle = grd;
    g.fillRect(0, imgH - fadeH, W, fadeH + 4);
  }

  /* 第一句压在图片底部：
     · ownStyle → 直接照舞台上的对话框重画（字体/字号/颜色/对齐/描边/面板都是用户设置）
     · 否则 → 海报自带的白字排版 + 暗角 */
  const st = ownStyle ? shareMsgStyle() : null;
  if (st) {
    shareDrawPlayerMsg(g, W, imgH, st, src[0].who || '', src[0].text || '', W / 1280);
  } else {
    const head = blocks[0];
    const headH = head.length * lhTxt;
    const headTop = imgH - headBottom - headH;
    const scrTop = Math.max(0, headTop - Math.round(60 * k));
    const scr = g.createLinearGradient(0, scrTop, 0, imgH);
    scr.addColorStop(0, 'rgba(10,13,20,0)');
    scr.addColorStop(0.55, 'rgba(10,13,20,.72)');
    scr.addColorStop(1, 'rgba(10,13,20,.95)');
    g.fillStyle = scr;
    g.fillRect(0, scrTop, W, imgH - scrTop);
    let y = headTop;
    head.forEach(runs => {
      shareDrawLine(g, runs, W / 2, y + lhTxt / 2, fsTxt, fsName, fam);
      y += lhTxt;
    });
  }

  if (rest.length) {
    /* 金色分隔短线去掉了：首句→第二句只剩 1.0 字号的间距，中间塞一条线太挤，
       图片下沿本身已经把两块分开了。 */
    let yy = imgH + restTopGap;
    rest.forEach((lines, i) => {
      if (i) yy += gapBlock;
      lines.forEach(runs => {
        shareDrawLine(g, runs, W / 2, yy + lhTxt / 2, fsTxt, fsName, fam);
        yy += lhTxt;
      });
    });
  }

  const total = (S.data && S.data.parts.length) || 1;
  g.font = '400 ' + Math.max(13, Math.round(19 * k)) + 'px ' + fam;
  g.fillStyle = '#6b7488';
  g.textAlign = 'center';
  g.fillText(shareStoryTitle() + ' · 第 ' + (ST.part + 1) + '/' + total + ' 段',
    W / 2, H - Math.round(44 * k));
  return cv;
}

/** canvas → Blob（**同步**）。
 *
 *  不用 canvas.toBlob：它是异步编码，个别环境（无头 Edge、部分老 WebView）编
 *  大画布时回调根本不触发 —— 用户点「保存图片」就毫无反应。toDataURL 是同步的，
 *  拿到 base64 再自己解成 Uint8Array，任何环境都一样可靠。
 *  体积上略微多占一份 base64 字符串（约 1.4 倍），1080 宽的图完全可以接受。 */
function shareCanvasBlob(cv, type, quality) {
  let url = '';
  try { url = cv.toDataURL(type, quality); } catch (e) { return null; }
  const m = /^data:([^;,]+)[^,]*,(.*)$/.exec(url);
  if (!m) return null;
  let bin = '';
  try { bin = atob(m[2]); } catch (e) { return null; }
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return new Blob([buf], { type: m[1] });
}

/** 打开分享弹窗：左边是成品预览，右边勾台词 */
function openShare() {
  if (!ST.allLines || !ST.allLines.length) {
    toast('先在左侧选一部剧情、播到有台词的画面再分享');
    return;
  }
  // 暂停：选台词期间画面不能再变，否则预览和画面就对不上了
  // （不自动恢复播放 ——「自动」开关必须由用户自己决定）
  if (ST.running) { pausePlayback(); setPlayBtn(false); }
  const shot = shareStageShot();
  if (!shot) { toast('画面还没准备好，先播放一下再分享'); return; }

  const lines = ST.allLines;
  const sel = new Set();
  /* 默认选中**当前屏幕上这一句**开头，再顺延 3 句（用户 2026-11-14 反馈：
     以前是往前摘 4 句，图里那句跟播放器正在显示的对不上）。
     画面（截图）是当前这一句的，所以图里叠的也必须是同一句。 */
  const cur = (ST.curLine >= 0 && ST.curLine < lines.length) ? ST.curLine : 0;
  for (let i = cur; i < Math.min(lines.length, cur + 4); i++) sel.add(i);

  const mask = document.createElement('div');
  mask.id = 'shareMask';
  mask.innerHTML =
    '<div id="shareBox">' +
      '<div class="shHead"><span class="shT">分享这一段</span>' +
        '<span class="shS">' + esc(shareStoryTitle()) + '</span>' +
        '<button class="shX" title="关闭">✕</button></div>' +
      '<div class="shBody">' +
        '<div class="shPrev" id="shPrev"></div>' +
        '<div class="shPick">' +
          '<div class="shTools"><button data-act="all">全选</button>' +
            '<button data-act="none">全不选</button>' +
            '<button data-act="cur">只要当前句</button>' +
            '<span class="shCnt"></span>' +
            '</div>' +
          '<div class="shList" id="shList"></div>' +
        '</div>' +
      '</div>' +
      '<div class="shFoot"><span class="tip">勾选要放进图里的台词（最多 '
        + SHARE_MAX_PICK + ' 句）</span><span class="sp"></span>' +
        /* 图里那一句的画法（跟分辨率并排，别藏在右上角工具行里）：
           播放器样式 = 完全照「设置」里的字体/字号/颜色/对齐/描边/对话框外观；
           海报样式 = 用分享图自带的白字排版。 */
        '<label class="shFmt" title="海报样式（默认）：用分享图自带的白字排版；'
          + '播放器样式：完全照你在「设置」里调的字体、字号、颜色、对齐和对话框外观来画那一句">'
          + '首句 <select id="shOwn">'
          + '<option value="0">海报样式</option>'
          + '<option value="1">播放器样式</option></select></label>' +
        /* 海报样式下的台词字号（选「播放器样式」时字号跟播放器设置走，这里会置灰）。
           选项直接标数字：这是设计基准 1080 宽下的像素数，出图时按分辨率等比缩放。 */
        '<label class="shFmt" title="海报样式下的台词字号（1080 宽基准的像素）；'
          + '选「播放器样式」时字号跟你在设置里调的一致，这一项会变灰">'
          + '字号 <select id="shFs">'
          + '<option value="20">20</option>'
          + '<option value="24" selected>24</option>'
          + '<option value="28">28</option>'
          + '<option value="32">32</option>'
          + '<option value="36">36</option>'
          + '<option value="40">40</option></select></label>' +
        /* 只出 JPG：分享用图，越轻便越好（用户 2026-11-14 明确不要无损选项）。
           分辨率按行规说：1080p = 1920×1080（默认），720p = 1280×720（游戏原生）。 */
        '<label class="shFmt" title="1080p（1920×1080）：默认，聊天里点开也清楚；'
          + '720p（1280×720）：游戏原生分辨率，体积更小">分辨率 <select id="shSize">'
          + '<option value="1920">1080p · 1920×1080</option>'
          + '<option value="1280">720p · 1280×720</option></select></label>' +
        /* 关闭只在右上角保留一个 ✕（用户 2026-11-14 要求：底部不再重复） */
        '<button id="shSave">保存图片</button>' +
        '<button id="shCopy" class="primary">复制图片</button></div>' +
    '</div>';
  document.body.appendChild(mask);
  const q = s => mask.querySelector(s);

  q('#shList').innerHTML = lines.map((l, i) =>
    '<label class="shRow' + (sel.has(i) ? ' on' : '') + (i === ST.curLine ? ' cur' : '')
      + '" data-gi="' + i + '">'
      + '<input type="checkbox" data-gi="' + i + '"' + (sel.has(i) ? ' checked' : '') + '>'
      + '<span class="who">' + esc(l.who || '旁白') + '</span>'
      + '<span class="txt">' + esc(l.text || '') + '</span></label>').join('');

  let out = null;
  let outW = SHARE_W;                    // 默认 1080p（1920 宽），可切 720p（1280 宽）

  /* 首句画法 / 字号 / 分辨率都记进用户配置（UD 的 ui 区，跟语言、筛选同一套同步机制），
     下次打开弹窗直接复用，不用每次重选（用户 2026-11-14 要求）。 */
  const FS_OPTS = ['20', '24', '28', '32', '36', '40'];
  function prefGet(k, def) {
    return (window.UD && UD.prefValue) ? UD.prefValue('ui', k, def) : def;
  }
  function prefSet(k, v) {
    if (window.UD && UD.syncPref) UD.syncPref('ui', k, v);
  }
  if (q('#shOwn')) q('#shOwn').value = String(prefGet('shareOwn', '0')) === '1' ? '1' : '0';
  if (q('#shFs')) {
    const fs0 = String(prefGet('shareFs', '24'));
    q('#shFs').value = FS_OPTS.indexOf(fs0) >= 0 ? fs0 : '24';
  }
  if (q('#shSize')) {
    const res0 = String(prefGet('shareRes', '1920'));
    q('#shSize').value = res0 === '1280' ? '1280' : '1920';
    outW = q('#shSize').value === '1280' ? 1280 : SHARE_W;
  }

  function paint() {
    const picks = Array.from(sel).sort((a, b) => a - b);
    const cnt = q('.shCnt');
    if (cnt) cnt.textContent = picks.length ? '已选 ' + picks.length + ' 句' : '还没选台词';
    const holder = q('#shPrev');
    out = null;
    if (!picks.length) {
      holder.innerHTML = '<div class="shEmpty">勾一句台词，这里立刻出现成品</div>';
      return;
    }
    const own = q('#shOwn') ? q('#shOwn').value === '1' : false;   // 默认海报样式
    const fsBase = q('#shFs') ? +q('#shFs').value : 24;
    const cv = shareCompose(shot, picks, outW, own, fsBase);
    if (!cv) {
      holder.innerHTML = '<div class="shEmpty">这几句拼不出来，换几句试试</div>';
      return;
    }
    out = cv;
    holder.innerHTML = '';
    holder.appendChild(cv);
  }
  paint();

  const list = q('#shList');
  const curRow = list.querySelector('.shRow.cur');
  if (curRow) curRow.scrollIntoView({ block: 'center' });

  // 勾选：全表只有一个监听（1000+ 行的列表不逐行绑定）
  list.addEventListener('change', e => {
    const cb = e.target;
    if (!cb || cb.type !== 'checkbox') return;
    const gi = +cb.dataset.gi;
    if (cb.checked) {
      if (sel.size >= SHARE_MAX_PICK) {
        cb.checked = false;
        toast('一次最多选 ' + SHARE_MAX_PICK + ' 句，再多一张图就排不下了');
        return;
      }
      sel.add(gi);
    } else sel.delete(gi);
    const row = cb.closest('.shRow');
    if (row) row.classList.toggle('on', cb.checked);
    paint();
  });
  q('.shTools').addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    const act = b.dataset.act;
    sel.clear();
    if (act === 'all') {
      // 全选只取前 SHARE_MAX_PICK 句：否则 canvas 高度会到几万像素，直接画不出来
      for (let i = 0; i < Math.min(SHARE_MAX_PICK, lines.length); i++) sel.add(i);
      if (lines.length > SHARE_MAX_PICK) {
        toast('一张图最多排 ' + SHARE_MAX_PICK + ' 句：已选前 ' + SHARE_MAX_PICK + ' 句');
      }
    } else if (act === 'cur') {
      if (cur >= 0) sel.add(cur);
    }
    list.querySelectorAll('.shRow').forEach(row => {
      const on = sel.has(+row.dataset.gi);
      row.classList.toggle('on', on);
      const cb = row.querySelector('input');
      if (cb) cb.checked = on;
    });
    paint();
  });
  function close() { mask.remove(); }
  mask.addEventListener('click', e => { if (e.target === mask) close(); });
  q('.shX').onclick = close;
  // 分辨率切换：720p = 1280×720（游戏原生），1080p = 1920×1080（默认）
  q('#shSize').onchange = e => {
    const w = parseInt(e.target.value, 10);
    outW = w === 1280 ? 1280 : SHARE_W;
    prefSet('shareRes', String(outW));
    paint();
  };
  // 首句画法切换（播放器样式 ↔ 海报样式）：播放器样式下字号由「设置」决定，字号项置灰
  function syncFsEnabled() {
    const fsSel = q('#shFs');
    if (!fsSel || !q('#shOwn')) return;
    const ownStyle = q('#shOwn').value === '1';
    fsSel.disabled = ownStyle;
    fsSel.parentNode.title = ownStyle
      ? '当前是「播放器样式」：字号跟你在「设置」里调的一致'
      : '海报样式下的台词字号（1080 宽基准，出图时按分辨率等比缩放）';
  }
  if (q('#shOwn')) {
    q('#shOwn').onchange = () => {
      prefSet('shareOwn', q('#shOwn').value);
      syncFsEnabled();
      paint();
    };
    syncFsEnabled();
  }
  if (q('#shFs')) {
    q('#shFs').onchange = () => { prefSet('shareFs', q('#shFs').value); paint(); };
  }
  const sizeText = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB'
    : Math.max(1, Math.round(n / 1024)) + ' KB';
  function shareName(ext) {
    return ('超昂大战_' + shareStoryTitle() + '_第' + (ST.part + 1) + '段.' + ext)
      .replace(/[\\/:*?"<>|]/g, '_');
  }
  /* 保存：只出 JPG（分享用图越轻越好，用户明确不要无损选项）。
     0.90 质量下文字边缘看不出压缩痕，体积只有 PNG 的三成左右。 */
  function saveImage() {
    const blob = shareCanvasBlob(out, 'image/jpeg', 0.90);
    if (!blob) { toast('导出失败：这个浏览器不支持导出图片'); return false; }
    // 浏览器不认 JPEG 时会静默给回 PNG，扩展名跟着真实编码类型走
    const name = shareName(/jpe?g/.test(blob.type) ? 'jpg' : 'png');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    toast('已保存：' + name + '（' + sizeText(blob.size) + '）');
    return true;
  }
  q('#shSave').onclick = () => {
    if (!out) { toast('先勾选要放进图里的台词'); return; }
    saveImage();
  };
  /* 复制：分享时最顺手的一步（发微信/QQ 直接 Ctrl+V）。
     剪贴板只接受 PNG，所以这里另编一份 PNG 给剪贴板，保存的那份还是 JPG。
     http:// 局域网地址不是安全上下文，Clipboard 会被禁 —— 那时退回下载。 */
  q('#shCopy').onclick = () => {
    if (!out) { toast('先勾选要放进图里的台词'); return; }
    const png = shareCanvasBlob(out, 'image/png');
    const fallback = () => {
      toast('这个浏览器不允许直接复制图片，已改为下载');
      saveImage();
    };
    if (!png || !navigator.clipboard || !window.ClipboardItem) { fallback(); return; }
    navigator.clipboard.write([new ClipboardItem({ 'image/png': png })])
      .then(() => {
        /* 成功反馈直接写在按钮上（用户 2026-11-14 要求）：
           按钮变成绿色的「✓ 图片复制成功」，5 秒后自动改回「复制图片」。 */
        const btn = q('#shCopy');
        if (!btn) { toast('图片复制成功'); return; }
        btn.classList.add('ok');
        btn.textContent = '✓ 图片复制成功';
        clearTimeout(btn._okT);
        btn._okT = setTimeout(() => {
          btn.classList.remove('ok');
          btn.textContent = '复制图片';
        }, 5000);
      })
      .catch(fallback);
  };
}
if ($('#btnShare')) $('#btnShare').onclick = () => openShare();

/* ================= 事件 ================= */
$('#btnPlay').onclick = () => {
  /* 手动模式：主按钮、点画面、空格/P 全是「下一句」，
     不会从这里把连续播放重新打开（用户 2026-11-10 明确的 AUTO 语义）。 */
  if (!ST.auto) { nextLine(); return; }
  if (ST.running) {
    pausePlayback();               // 暂停不销毁 BGM/循环音效
    setPlayBtn(false);
    return;
  }
  resumeAudio();
  // 继续：若已播到某句，从该句重建画面后接着播；否则从指令指针继续
  if (ST.curMsgIdx != null && ST.curMsgIdx < ST.methods.length) {
    seekTo(ST.curMsgIdx, { forceResume: true });
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
$('#auto').onchange = e => setAutoPlay(e.target.checked);
/* 记住上次的「自动」选择：以前刷新页面又变回勾选，看着就像自己没关掉。
   初始化只恢复勾选状态，不写盘、也不碰还没开始的播放。 */
setAutoPlay(autoPref(), { init: true, noSave: true });
/* 进来就处于手动模式（上次关掉了「自动」）的用户：先给一次提示。
   主按钮顶着「▶ 下一句」、空格也不再是暂停，很容易被当成"播放键坏了"
   （用户 2026-11-11 反馈）。延迟一会儿，别和启动时的其它 toast 抢位置。 */
if (!ST.auto) {
  L10N.ready.then(() => setTimeout(() => toast(L10N.t('ctrl.autoOff',
    '手动模式：播放键已变成「▶ 下一句」，空格 / P / 点画面也改为切句（要连续播放就勾上「自动」）')), 700));
}
/* ui.json 是异步加载的：等它 applyDom 完再按当前语言重画一次主按钮，
   日文界面下手动模式才不会暂时显示中文的「▶ 下一句」。 */
L10N.ready.then(() => setPlayBtn(ST.running));
/* 控制条 / 设置页 / 侧栏里的 <select>、<input> 改完立刻交还焦点。
   否则控件一直拿着焦点，之后的 ↑↓ / 空格 / 回车全被它自己吃掉：
   选了「倍速」再按 ↑↓ 变成换选项而不是切句，勾完「自动」按空格变成再勾一次，
   拖完滑块按 ↑↓ 变成微调滑块（用户 2026-11-11 反馈「操作控制栏后快捷键失效」）。
   走冒泡阶段（不是捕获），保证元素自己的 onchange 先跑完再失焦。 */
document.addEventListener('change', e => {
  const t = e.target;
  if (!t || !t.matches) return;
  if (t.matches('#controls select, #controls input, #settings select, #settings input, ' +
               '#sidebar select, #sidebar input[type=checkbox]')) {
    t.blur();
  }
});
document.addEventListener('keydown', e => {
  // Esc 关设置面板：关闭按钮的提示里写着「（Esc）」，但这里以前压根没有
  // Escape 分支 —— 提示是假的。放在输入框过滤**之前**，焦点在滑杆上也能关。
  if (e.code === 'Escape' || e.key === 'Escape') {
    const kt = $('#keyTip');
    if (kt) { e.preventDefault(); kt.remove(); return; }
    const sm = $('#shareMask');
    if (sm) { e.preventDefault(); sm.remove(); return; }
    const st = $('#settings');
    if (st && !st.hidden) {
      e.preventDefault();
      st.hidden = true;
      return;
    }
  }
  /* 分享弹窗是模态：开着的时候空格/回车/方向键不该去推剧情
     （焦点在勾选框上时下面的 INPUT 过滤会拦住，但点空白处后焦点就没了） */
  if ($('#shareMask')) return;

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
  if (seekN >= 0) setTimeout(() => seekTo(seekN, { forceResume: true }), 1800);
  /* ?pause=N 供自动化测试：启动 N ms 后暂停（冻结台词推进）。
     一次性画面特效的 rAF 不走主循环，暂停后仍会继续播完 —— 截图要卡在
     它生命期内，budget 需落在 seek(1800)+特效时长(≈2000) 之间。 */
  const pauseMs = +(new URLSearchParams(location.search).get('pause') || 0);
  if (pauseMs > 0) setTimeout(() => { if (ST.running) togglePlay(); }, pauseMs);
  // ?langswitch=zh-Hans 供自动化测试：启动后跑一遍「运行中切换语言」的完整链路
  const ls = new URLSearchParams(location.search).get('langswitch');
  if (ls) setTimeout(() => switchLang(ls === '1' ? undefined : ls), 2500);
})();

/* ================= 启动 ================= */
// ?seek=N 供自动化测试：启动后直接跳到第 N 条指令
window.__testSeek = n => seekTo(n, { forceResume: true });
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
/* 恢复「播放记录」视图：和分类一样是筛选条件，刷新后接着看这份记录列表。
   必须排在下面的 renderSidebar() 之前，否则首次绘制还是普通列表。
   save=false —— 刚读出来的值不用再打时间戳。 */
setHistView(readHistPref(), false);
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
  // 播放记录视图开关也算筛选条件（别的设备点过就跟着切），不写时间戳
  const hist = UD.prefValue('ui', 'hist', null);
  if (hist != null) setHistView(hist === '1' || hist === 1 || hist === true, false);
  if (typeof updateCnt === 'function') updateCnt();
  renderSidebar(($('#search') || {}).value || '');
  /* 本次是深链打开的：hydrate 在深链定位之后才回来，上面按偏好层重设的
     分类 / 播放记录视图会把刚定位好的列表盖掉（用户看到的就是"列表没跟着走"）。
     这里把定位再做一次，深链优先。 */
  if (FOCUS_KEY) {
    focusSidebarOn(FOCUS_KEY, { src: FOCUS_SRC });
    revealRow(FOCUS_KEY);
  }
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
// 常驻型画面特效的贴图预热（延后，别和首页加载抢带宽）
setTimeout(warmScreenFxTextures, 1500);
(() => {
  const qs = new URLSearchParams(location.search);
  const auto = qs.has('autoplay');
  // ?adv=main_1 优先（测试钩子 + 可分享链接），其次 ?char=<id>，最后恢复上次观看
  const qa = qs.get('adv');
  if (qa) {
    const key = /^adv_/.test(qa) ? qa : 'adv_' + qa;
    const parsed = /^adv_(main|event|other|tutorial|op)(?:_(\d+))?$/.exec(key);
    if (parsed) {
      // tutorial / op 在侧栏归到「其他」菜单下；分类用正则判定（advindex 里
      // 序章包的别名可能挂成 kind:main，不能拿它反推该看哪个列表）
      const src = (parsed[1] === 'tutorial' || parsed[1] === 'op') ? 'other' : parsed[1];
      focusSidebarOn(key, { src: src });
      if (auto) openStory(key).then(() => revealRow(key)).catch(() => { });
    }
    return;
  }
  const q = +(qs.get('char') || 0);
  const saved = sessionStorage.getItem('story');
  const h = isCharKey(saved) ? +String(saved).slice(5) : null;
  const id = q && INDEX.some(c => c.id === q) ? q
    : (h && INDEX.some(c => c.id === h) ? h : null);
  // 默认不自动加载剧情：保留 #empty 空状态提示，等用户点列表角色再加载
  if (id && auto) {
    // 深链要看到这个角色所在分类的列表（别被上次留在「活动 / 播放记录」盖住），
    // 并且加载完把那一行滚进视野 —— 384 个角色里目标常常在很下面
    focusSidebarOn('char_' + id, { src: 'char' });
    openChar(id).then(() => revealRow('char_' + id)).catch(() => { });
  }
})();

/* ================= 对外接口（译文 / 新手引导用） ================= */
/* 这些模块不想碰播放器内部状态，所以只暴露最小的控制面。 */
window.PLAYER_API = {
  setAuto(v) { setAutoPlay(v, { silent: true }); },
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
