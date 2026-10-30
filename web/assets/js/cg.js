(function(){
'use strict';

const GROUPS = window.__CGMAP || [];
const NAMES  = window.__NAMES || {};
const META   = window.__CHARMETA || {};

const $ = s => document.querySelector(s);
const el = (t, c) => { const e = document.createElement(t); if (c) e.className = c; return e; };
/* 图片候选链：本地超分 → 本地转码 → 出厂映射 → 直接拼路径。
   改造前这里硬编码 media/img_x2/，只下原图的新用户会满屏裂图。 */
const IMGX2 = window.__IMGX2 || {};
const IMGBASE = window.__IMGBASE || {};
const IMGMAP = window.__IMGMAP || {};

function cgCands(f) {
  const key = f.replace(/\.(jpg|jpeg|png)$/i, '');
  const out = [];
  const push = p => { if (p && out.indexOf(p) < 0) out.push(p); };
  push((IMGX2.cg || {})[key]);
  push((IMGBASE.cg || {})[key]);
  push((IMGMAP.cg || {})[key]);
  push('media/img/cg/' + encodeURIComponent(f));
  // 原图可能是 .png/.jpg（旧版导出），补几个扩展名兜底
  push('media/img/cg/' + encodeURIComponent(key + '.webp'));
  push('media/img/cg/' + encodeURIComponent(key + '.png'));
  push('media/img_x2/cg/' + encodeURIComponent(key + '.webp'));
  push('media/img_x2/cg/' + encodeURIComponent(key + '.png'));
  return out;
}

function avatarCands(id) {
  const out = [];
  const push = p => { if (p && out.indexOf(p) < 0) out.push(p); };
  push((IMGX2.avatar || {})[id]);
  push((IMGBASE.avatar || {})[id]);
  push((IMGMAP.avatar || {})[id]);
  // 头像导出的是 **.png**（原图格式），不是 .webp。
  // 只写 .webp 的话，全新环境里所有头像 404、侧栏一片空白。
  // 导出的是 **.png**，先试 png 再试 webp（顺序反了会先吃一个 404 才回退）
  push('media/img/avatar/' + id + '.png');
  push('media/img/avatar/' + id + '.webp');
  push('media/img/avatar/' + id + '.jpg');
  push('media/img_x2/avatar/' + id + '.webp');
  push('media/img_x2/avatar/' + id + '.png');
  return out;
}

/** 挂候选链：加载失败自动换下一个，全失败隐藏自身 */
function setChain(img, list) {
  if (!img || !list.length) return;
  img.dataset.chain = list.slice(1).join('|');
  img.onerror = function () {
    const rest = (this.dataset.chain || '').split('|').filter(Boolean);
    if (!rest.length) { this.onerror = null; this.style.visibility = 'hidden'; return; }
    this.dataset.chain = rest.slice(1).join('|');
    this.src = rest[0];
  };
  img.src = list[0];
}

/* ---------- 建立角色索引 ---------- */
const chars = new Map();   // id -> {id, name, zh, r, groups: [groupIndex...], total}

for (const c of Object.keys(NAMES).concat(Object.keys(META))) {
  const id = +c;
  if (!chars.has(id)) {
    chars.set(id, {
      id,
      name: NAMES[c] || ('ID ' + c),
      zh: (META[c] && META[c].zh) || '',
      r: (META[c] && META[c].r) || '',
      t: (META[c] && META[c].t) || '',
      d: (META[c] && META[c].d) || '',
      groups: [],
      total: 0
    });
  }
}
GROUPS.forEach((g, gi) => {
  let ch = chars.get(g.c);
  if (!ch) {
    ch = { id: g.c, name: 'ID ' + g.c, zh: '', r: '', d: '', groups: [], total: 0 };
    chars.set(g.c, ch);
  }
  ch.groups.push(gi);
  ch.total += g.f.length;
});

/* 实装日期比较：没有日期的角色一律排最后（asc 用 'z' 当哨兵，与剧情页同口径） */
function cmpDate(a, b, asc) {
  const da = (a && a.d) || (asc ? 'z' : '');
  const db = (b && b.d) || (asc ? 'z' : '');
  return asc ? da.localeCompare(db) : db.localeCompare(da);
}

/* 只保留真正有 CG 的角色 */
const withCG = [...chars.values()].filter(c => c.groups.length > 0)
  .sort((a, b) => a.id - b.id);

/* ---------- 状态 ---------- */
const PAGE = 120;        // 每次渲染的卡片数，滚动到底自动追加
let curChar = null;      // null = 全部
let keyword = '';
let sortBy = 'id';
/* 每部剧情的本地状态（char_N -> ready/soft-missing/missing），用来做"只看已下载" */
let dlState = {};

async function loadDlState() {
  try {
    const r = await fetch('api/cdn/units', { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    dlState = d.state || {};
    renderChars();
  } catch (e) { /* 纯静态打开时没有服务端，忽略 */ }
}

function dlHave(cid) {
  const st = dlState['char_' + cid] || 'missing';
  return st === 'ready' || st === 'soft-missing' || st === 'partial';
}
let visible = [];        // 当前可见的 group 索引
let shownCount = 0;      // 已渲染的卡片数
const lb = { g: -1, i: 0 };   // 灯箱：在 visible 中的位置 + 组内张数

/* URL 参数：?char=377 直接定位角色；?g=事件组序号 直接开灯箱 */
const usp = new URLSearchParams(location.search);
if (usp.get('char') && chars.has(+usp.get('char'))) curChar = +usp.get('char');

/* ---------- 侧栏 ---------- */
function thumbOf(c) {
  return avatarCands(c.id)[0];
}

function renderChars() {
  const box = $('#charList');
  box.innerHTML = '';

  const all = el('div', 'char-btn all' + (curChar === null ? ' on' : ''));
  all.textContent = `全部角色（${withCG.length}）`;
  all.onclick = () => { curChar = null; renderChars(); renderGrid(); };
  box.appendChild(all);

  let list = withCG;
  // 已下载 / 未下载筛选（和播放器、安装列表同一套口径）
  const dlf = ($('#dlFilter') || {}).value || '';
  if (dlf === 'have') list = list.filter(c => dlHave(c.id));
  if (dlf === 'miss') list = list.filter(c => !dlHave(c.id));

  if (keyword) {
    const k = keyword.toLowerCase();
    list = list.filter(c =>
      c.name.toLowerCase().includes(k) ||
      c.zh.toLowerCase().includes(k) ||
      String(c.id) === keyword.replace(/^id\s*/i, '') ||
      String(c.id).includes(keyword)
    );
  }
  // 排序走共用函数（和播放器、安装列表同一套规则：
  // 按日期排时**没有日期的沉底**，以前 CG 页是自己写的，未下载的会混在中间）
  if (sortBy === 'count') {
    list = [...list].sort((a, b) => b.total - a.total || a.id - b.id);
  } else {
    list = ESC.sortItems(list, sortBy, c => ({ id: c.id, name: c.name, date: c.d }));
  }
  // 未下载的沉底（播放器一直这么做，CG 页以前没有 → 同一个下拉两页结果不同）
  list = ESC.haveFirst(list, c => dlHave(c.id));

  for (const c of list) {
    const d = el('div', 'char-btn' + (curChar === c.id ? ' on' : ''));
    /* 按日期排序时把日期显示出来，否则看不出排序依据 */
    const dTxt = (c.d && sortBy.startsWith('date')) ? ' · ' + c.d : '';
    // 未下载的角色不请求头像（否则全新环境会刷出上百个 404），用首字色块
    const have = dlHave(c.id);
    d.innerHTML =
      (have
        ? `<img alt="" loading="lazy">`
        : `<span class="ava-ph">${(c.zh || c.name || '?').trim().slice(0, 1)}</span>`) +
      `<span class="info"><span class="nm">${c.name}</span>` +
      `<span class="sub">${c.zh ? c.zh + ' · ' : ''}${c.t ? c.t + ' · ' : ''}${c.groups.length} 组 / ${c.total} 张${dTxt}</span></span>`;
    // 必须走候选链：头像实际是 .png，只写死一个 src 会 404 且不会回退
    if (have) setChain(d.querySelector('img'), avatarCands(c.id));
    d.onclick = () => { curChar = c.id; renderChars(); renderGrid(); };
    box.appendChild(d);
  }
}

/* ---------- 卡片墙 ---------- */
function renderGrid() {
  visible = [];
  for (let gi = 0; gi < GROUPS.length; gi++) {
    const g = GROUPS[gi];
    if (curChar !== null && g.c !== curChar) continue;
    if (keyword) {
      const k = keyword.toLowerCase();
      const ch = chars.get(g.c);
      const hit = g.k.toLowerCase().includes(k) ||
        (ch && (ch.name.toLowerCase().includes(k) || ch.zh.toLowerCase().includes(k)));
      if (!hit) continue;
    }
    visible.push(gi);
  }

  if (sortBy === 'count') {
    visible.sort((a, b) => GROUPS[b].f.length - GROUPS[a].f.length);
  } else {
    // 走共用排序：同一角色的多个事件组靠 sort 的稳定性保持原顺序，
    // 未下载（没有日期）的沉底 —— 和播放器、安装列表一致
    const keyOf = (i) => {
      const c = chars.get(GROUPS[i].c);
      return {
        id: String(GROUPS[i].c).padStart(6, '0'),
        name: c ? c.name : '',
        date: (c && c.d) || '',
      };
    };
    const sorted = ESC.sortItems(visible, sortBy, keyOf);
    visible.length = 0;
    sorted.forEach(i => visible.push(i));
  }

  const totalImgs = visible.reduce((s, gi) => s + GROUPS[gi].f.length, 0);
  $('#barInfo').innerHTML =
    `<b>${visible.length}</b> 组 · <b>${totalImgs}</b> 张` +
    (curChar !== null ? ` · 角色 <b>${(chars.get(curChar) || {}).name || curChar}</b>` : '');
  $('#hdrSub').textContent = ` ${GROUPS.length} 组 / ${GROUPS.reduce((s, g) => s + g.f.length, 0)} 张`;

  const grid = $('#grid');
  grid.innerHTML = '';
  shownCount = 0;
  if (!visible.length) {
    grid.innerHTML = '<div id="empty">没有匹配的 CG</div>';
    return;
  }
  appendCards();
}

function appendCards() {
  const grid = $('#grid');
  const next = visible.slice(shownCount, shownCount + PAGE);
  if (!next.length) return;

  const frag = document.createDocumentFragment();
  for (const gi of next) {
    const g = GROUPS[gi];
    const ch = chars.get(g.c) || { name: 'ID ' + g.c, zh: '' };
    const card = el('div', 'card');
    // 该角色没下载 → 缩略图必然 404，直接给占位底色（点开会有提示）
    const have = dlHave(g.c);
    card.innerHTML =
      `<div class="thumb${have ? '' : ' noimg'}">` +
        (have
          ? `<img alt="" loading="lazy" decoding="async">`
          : `<span class="noimg-tip">未下载</span>`) +
        `<span class="badge">${g.f.length}</span>` +
      `</div>` +
      `<div class="cap">` +
        `<span class="cn">${ch.name}</span>` +
        (ch.zh ? `<span class="cz">${ch.zh}</span>` : '') +
        `<span class="ck"><span>${g.k}</span><span>ID ${g.c}</span></span>` +
      `</div>`;
    card.onclick = () => openLB(gi, 0);
    if (have) setChain(card.querySelector('.thumb img'), cgCands(g.f[0]));
    frag.appendChild(card);
  }
  grid.appendChild(frag);
  shownCount += next.length;
}

/* ---------- 灯箱 ---------- */
function openLB(groupIdx, imgIdx) {
  const pos = visible.indexOf(groupIdx);
  if (pos < 0) return;
  lb.g = pos;
  lb.i = imgIdx || 0;
  $('#lb').classList.add('on');
  paintLB();
}

function paintLB() {
  if (lb.g < 0 || lb.g >= visible.length) return;
  const g = GROUPS[visible[lb.g]];
  const ch = chars.get(g.c) || { name: '', zh: '' };
  lb.i = Math.max(0, Math.min(lb.i, g.f.length - 1));

  const img = $('#lbImg');
  img.alt = '';
  setChain(img, cgCands(g.f[lb.i]));

  $('#lbTitle').innerHTML = `<b>${ch.name}</b>${ch.zh ? ' <span style="color:var(--gold);opacity:.8">' + ch.zh + '</span>' : ''}`;
  $('#lbCount').textContent = `${lb.i + 1} / ${g.f.length}`;
  $('#lbGroup').textContent = `${g.k} · 组 ${lb.g + 1} / ${visible.length}`;

  const dots = $('#lbBottom');
  dots.innerHTML = '';
  g.f.forEach((_, i) => {
    const d = el('div', 'dot' + (i === lb.i ? ' on' : ''));
    d.onclick = () => { lb.i = i; paintLB(); };
    dots.appendChild(d);
  });
}

function moveImg(d) {
  if (lb.g < 0) return;
  const g = GROUPS[visible[lb.g]];
  const n = lb.i + d;
  if (n < 0 || n >= g.f.length) return;
  lb.i = n;
  paintLB();
}

function moveGroup(d) {
  if (lb.g < 0) return;
  const n = lb.g + d;
  if (n < 0 || n >= visible.length) return;
  lb.g = n;
  lb.i = 0;
  paintLB();
}

function closeLB() {
  $('#lb').classList.remove('on');
  $('#lbImg').src = '';
  lb.g = -1;
}

/* ---------- 事件绑定 ---------- */
$('#lbClose').onclick = closeLB;
$('#lbPrev').onclick = e => { e.stopPropagation(); moveImg(-1); };
$('#lbNext').onclick = e => { e.stopPropagation(); moveImg(1); };
$('#lb').onclick = e => { if (e.target.id === 'lb' || e.target.id === 'lbBody') closeLB(); };

let qt = null;
$('#q').oninput = e => {
  clearTimeout(qt);
  const v = e.target.value.trim();
  qt = setTimeout(() => {
    keyword = v;
    renderChars();
    renderGrid();
  }, 160);
};

/* 排序方式记忆（与卡片大小同套做法） */
const SORT_KEY = 'escah_cg_sort';
try {
  const s = localStorage.getItem(SORT_KEY);
  if (s) { sortBy = s; $('#sortBy').value = s; }
} catch (e) { /* file:// 下可能被禁 */ }

$('#sortBy').onchange = e => {
  sortBy = e.target.value;
  try { localStorage.setItem(SORT_KEY, sortBy); } catch (e2) { }
  renderChars();
  renderGrid();
};

/* 已下载 / 未下载筛选：状态要问本地服务，拿不到就当全部未下载 */
const DL_KEY = 'escah_cg_dl';
if ($('#dlFilter')) {
  try {
    const v = localStorage.getItem(DL_KEY);
    if (v) $('#dlFilter').value = v;
  } catch (e) { /* ignore */ }
  $('#dlFilter').onchange = () => {
    try { localStorage.setItem(DL_KEY, $('#dlFilter').value); } catch (e) { }
    renderChars();
    renderGrid();
  };
  loadDlState();
}

/* 滚动到底部附近时追加下一批卡片 */
$('#grid').addEventListener('scroll', () => {
  const g = $('#grid');
  if (g.scrollTop + g.clientHeight > g.scrollHeight - 500) appendCards();
}, { passive: true });

/* 卡片大小 */
const SZ_KEY = 'escah_cg_size';
function applySize(v) {
  $('#grid').style.setProperty('--cardMin', v + 'px');
  document.querySelectorAll('#bar .sz').forEach(b =>
    b.classList.toggle('on', b.dataset.sz === String(v)));
  try { localStorage.setItem(SZ_KEY, v); } catch (e) { /* file:// 下可能被禁 */ }
}
document.querySelectorAll('#bar .sz').forEach(b => {
  b.onclick = () => applySize(+b.dataset.sz);
});
let initSz = 320;
try { const s = +localStorage.getItem(SZ_KEY); if (s > 0) initSz = s; } catch (e) { }
applySize(initSz);

document.addEventListener('keydown', e => {
  if (lb.g >= 0) {
    if (e.key === 'Escape') { closeLB(); e.preventDefault(); return; }
    if (e.key === 'ArrowRight') { moveImg(1); e.preventDefault(); return; }
    if (e.key === 'ArrowLeft') { moveImg(-1); e.preventDefault(); return; }
    if (e.key === 'ArrowDown') { moveGroup(1); e.preventDefault(); return; }
    if (e.key === 'ArrowUp') { moveGroup(-1); e.preventDefault(); return; }
    return;
  }
  if (e.key === 'Escape' && curChar !== null) { curChar = null; renderChars(); renderGrid(); }
});

/* ---------- 对外接口（导出 CG 用） ---------- */
window.CGPAGE = {
  /** 当前筛选的角色 id（null = 全部） */
  cur: () => curChar,
  /** 当前可见的组索引，导出时用得上 */
  visible: () => [...visible],
  counts: () => ({ chars: withCG.length, total: withCG.reduce((a, c) => a + c.total, 0) }),
  nameOf: id => (chars.get(id) || {}).name || String(id),
};

/* ---------- 启动 ---------- */
renderChars();
renderGrid();
/* ?g=N 表示打开当前筛选结果中的第 N 组（从 0 开始） */
if (usp.has('g')) {
  const pos = +usp.get('g');
  if (pos >= 0 && pos < visible.length) openLB(visible[pos], 0);
}

})();
