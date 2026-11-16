/* 角色立绘浏览页
 * -----------------
 * 骨架照抄 CG 页（侧栏角色列表 + 卡片瀑布流 + 灯箱翻页），数据源：
 *
 *   · 立绘：`data/standmap.js` —— 由 `server/cdn/scripts/export_standmap.py`
 *     扫 `media/img/stand/` 生成，键形如 `stand_<编号>_<服装HEX>_<表情HEX>`。
 *     "组" = 同一角色同一套服装（一张卡片），格子里是这套服装的各个表情。
 *
 *   2026-11-16 起不再处理 SD 小人：SD 数据源、范围、卡片与灯箱分支均已注释。
 *
 * 为什么立绘的"有没有"看 imgmap 而不是 mediaindex：
 *   `imgmap.stand` 就是**本机磁盘扫出来的**（gen_imgmap.py），键在 = 文件在；
 *   而 mediaindex 只登记头像/横幅两类（几百条），塞 1.9 万条立绘进去纯属浪费。
 */
(function () {
  'use strict';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  const NAMES = window.__NAMES || {};
  const META = window.__CHARMETA || {};
  const IMGX2 = window.__IMGX2 || null;
  const IMGBASE = window.__IMGBASE || null;
  const IMGMAP = window.__IMGMAP || null;
  const STAND = (window.__STANDMAP || {}).groups || [];
  // 2026-11-16 起本页不再加载/处理 SD 资源
  // const SD = (window.__SDMAP || {}).units || [];
  // const SDV = (window.__SDMAP || {}).v || 1;
  const UD = window.UD || null;

  // ---------------------------------------------------------------- 小工具
  let toastEl = null;
  function toast(msg) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.id = 'sdToast';
      toastEl.style.cssText = 'position:fixed;left:50%;bottom:34px;transform:translateX(-50%);' +
        'z-index:10500;background:rgba(20,24,34,.96);color:#e6e9f0;border:1px solid #3a4a60;' +
        'border-radius:9px;padding:8px 16px;font-size:14px;box-shadow:0 6px 20px rgba(0,0,0,.5)';
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.style.display = 'block';
    clearTimeout(toast.t);
    toast.t = setTimeout(() => { toastEl.style.display = 'none'; }, 1800);
  }

  const numOf = s => String(s || '').replace(/^0+(?=\d)/, '');
  const pad3 = n => String(n).padStart(3, '0');

  function nameOf(num) {
    const n = NAMES[numOf(num)] || NAMES[String(num)];
    return n ? String(n) : '';
  }

  // ---------------------------------------------------------------- 数据整形
  const STAND_BY_KEY = Object.create(null);   // 立绘键 -> 卡片下标（灯箱反查用）

  function standSrc(key) {
    if (IMGX2 && IMGX2.stand && IMGX2.stand[key]) return IMGX2.stand[key];
    if (IMGBASE && IMGBASE.stand && IMGBASE.stand[key]) return IMGBASE.stand[key];
    if (IMGMAP && IMGMAP.stand && IMGMAP.stand[key]) return IMGMAP.stand[key];
    return '';
  }
  /* 立绘"这一组有没有"：看 mediaindex 的 stand 清单（按 组 = 编号_服装 登记）。
     不能只看 imgmap —— 那里面只有"剧本引用过"的键（9545 条），而磁盘上有 1.9 万张，
     用 imgmap 判定会把一半本来就在本机的立绘标成「未下载」。 */
  const STAND_GROUPS = (window.__MEDIA && Array.isArray(window.__MEDIA.stand))
    ? new Set(window.__MEDIA.stand) : null;

  function standAvail(num, cloth, files) {
    if (STAND_GROUPS) return STAND_GROUPS.has(num + '_' + cloth.toUpperCase());
    // 老后端没这条清单：退回"映射里有就算有"
    return (files || []).some(f => !!standSrc(f.key));
  }

  /** 卡片：`{t:'stand', num, band, files:[{src,w,h,label}], anyAvail, cid}`
      （SD 卡片已注释掉） */
  function buildCards() {
    const out = [];
    // ---- 立绘：每个 (编号, 服装) 一张卡
    for (const g of STAND) {
      const files = [];
      for (const key of g.f || []) {
        files.push({ src: standSrc(key) || ('media/img/stand/' + encodeURIComponent(key) + '.webp'),
                     key, w: 1280, h: 720 });
      }
      if (!files.length) continue;
      const any = standAvail(g.n, g.c, files);
      out.push({
        t: 'stand', num: g.n, band: g.b, cloth: g.c,
        cid: g.b === 'char' ? parseInt(numOf(g.n), 10) : null,
        files, any,
        title: nameOf(g.n) || ('#' + g.n),
        sub: '服装 ' + g.c.slice(0, 8),
      });
    }
    // ---- SD 卡片：2026-11-16 起不再显示 SD 小人（数据源 SD 已注释）
    // for (const u of SD) { ... }
    out.forEach((c, i) => {
      for (const f of c.files) if (f.key) STAND_BY_KEY[f.key] = i;
    });
    return out;
  }

  /* SD 动作名映射与 sdAvail() 已随 SD 一起注释（2026-11-16）
  function sdAvail(id) {
    const m = window.__MEDIA;
    if (m && Array.isArray(m.sd)) return m.sd.indexOf(String(id)) >= 0;
    return true;
  }
  */

  const ALL = buildCards();

  // ---------------------------------------------------------------- 状态
  const ST = {
    scope: 'char-stand',
    sort: 'id',
    filter: '',
    cur: null,          // 选中的角色 id / 编号
    cardMin: 300,
    list: [],           // 当前 scope 的卡片（过滤后）
    visible: [],
    q: '',
  };

  /* 界面偏好：UD 在时走它的 ui 命名空间（localStorage 主副本 + 服务端备份），
     纯静态打开 / 老包里没有 UD 时也要能存在本地，别让刷新一次就全丢。 */
  function pref(ns, k, def) {
    try {
      if (UD) return UD.prefValue('ui', ns + '_' + k, def) ?? def;
      const s = localStorage.getItem(ns + '_' + k);
      return s == null ? def : JSON.parse(s);
    } catch (e) { return def; }
  }
  function setPref(ns, k, v) {
    try {
      if (UD) UD.syncPref('ui', ns + '_' + k, v);
      else localStorage.setItem(ns + '_' + k, JSON.stringify(v));
    } catch (e) { /* 忽略 */ }
  }

  // ---------------------------------------------------------------- 侧栏
  /* 两个范围 = 「角色 / 其他」×「立绘」。SD 范围已注释（2026-11-16）。
     **不再区分 NPC 与敌人**：官方按编号段分的桶本来就不准（旧后端把敌人都记成
     npc，前端只能靠编号 >= 20000 猜），用户要求角色以外统统归「其他」（2026-11-15）。 */
  const SCOPES = {
    'char-stand': { char: true, t: 'stand' },
    // 'char-sd': { char: true, t: 'sd' },
    'other-stand': { char: false, t: 'stand' },
    // 'other-sd': { char: false, t: 'sd' },
  };
  // 老版本存进偏好的值、以及旧深链带过来的值
  const SCOPE_OLD = {
    stand: 'char-stand', /* sd: 'char-sd', */ npc: 'other-stand', enemy: 'other-stand',
  };
  /** 把任意来源的 scope 值归一成上面两种之一（认不出就回「角色立绘」） */
  function normScope(v) {
    return SCOPES[v] ? v : (SCOPE_OLD[v] || 'char-stand');
  }
  /** 当前范围的有效参数（认不出的值回落到「角色立绘」） */
  function curScope() { return SCOPES[ST.scope] || SCOPES['char-stand']; }
  function scopeCards() {
    const s = curScope();
    return ALL.filter(c => (c.band === 'char') === s.char && c.t === s.t);
  }

  function sideEntries() {
    // 角色范围按角色聚合；「其他」范围按编号聚合（它们没有角色名，编号就是标识）
    const cards = scopeCards();
    const byChar = curScope().char;
    const by = new Map();
    for (const c of cards) {
      const key = byChar
        ? (c.cid != null ? 'c' + c.cid : 'n' + c.num)
        : 'n' + c.num;
      let e = by.get(key);
      if (!e) {
        e = { key, cid: c.cid, num: c.num, n: 0, imgs: 0, any: false };
        by.set(key, e);
      }
      e.n += 1;
      e.imgs += c.files.length;
      if (c.any) e.any = true;
    }
    const out = Array.from(by.values());
    out.forEach(e => {
      e.name = e.cid != null ? (nameOf(e.cid) || ('#' + e.cid)) : ('#' + e.num);
      e.sortName = e.name;
      e.idNum = parseInt(numOf(e.num), 10) || 0;
      // 实装日期（公共素材清单里的 d 字段）：给"按实装日期"排序用，
      // 以前立绘页没有这一项，跟 CG 页对不上（2026-11-15 反馈）
      e.date = e.cid != null ? String((META[e.cid] || {}).d || '') : '';
    });
    return out;
  }

  function renderSidebar() {
    const box = $('#charList');
    let list = sideEntries();
    const q = ST.q.trim().toLowerCase();
    if (q) {
      list = list.filter(e => (e.name || '').toLowerCase().includes(q)
        || String(e.idNum).includes(q) || (e.num || '').includes(q));
    }
    // 排序走 common.js 的统一实现（立绘页以前自己写了一套 cmp、选项也少两项，
    // 和 CG 页对不上；2026-11-15 反馈「搞那么多套」）
    list = ESC.sortItems(list, ST.sort, e => ({
      id: e.idNum, name: e.name, count: e.imgs, date: e.date,
    }));

    const rows = [];
    rows.push('<div class="char-btn all' + (ST.cur == null ? ' on' : '') +
      '" data-all="1">全部（' + list.length + '）</div>');
    for (const e of list) {
      const on = ST.cur === e.key ? ' on' : '';
      const ava = e.cid != null ? avatarCands(e.cid) : [];
      // NPC / 敌人没有角色头像：用自己的编号末三位当"脸"，比一个 # 好认
      const ph = e.cid != null
        ? (e.name ? e.name.slice(0, 1) : '#')
        : String(e.idNum).slice(-3);
      const media = ava.length
        ? '<img alt="" src="' + ava[0] + '">'
        : '<span class="ava-ph">' + esc(ph) + '</span>';
      const meta = META[e.cid] || {};
      const sub = [meta.t || '', e.n + ' 组 / ' + e.imgs + ' 张'].filter(Boolean).join(' · ');
      rows.push('<div class="char-btn' + on + '" data-k="' + e.key + '">' + media +
        '<span class="info"><span class="nm">' + esc(e.name) + '</span>' +
        '<span class="sub">' + esc(sub) + '</span></span></div>');
    }
    box.innerHTML = rows.join('');
    $$('.char-btn', box).forEach(b => {
      b.onclick = () => {
        ST.cur = b.dataset.all ? null : b.dataset.k;
        setPref('stand', 'cur', ST.cur);
        renderSidebar();
        renderGrid();
      };
    });
    revealCur();
  }

  /** 把侧栏里选中的那一行滚进视野。
   *  刷新恢复上次选中的角色时，列表从顶部开始画，选中项可能在几百行之外，
   *  只高亮不滚动等于"记忆了但看不见"（2026-11-15 用户反馈）。
   *  只在看不见时才动，用户点的那一项本来就在眼前就不会被拽走。 */
  function revealCur() {
    const box = $('#charList');
    const el = box && box.querySelector('.char-btn.on');
    if (!box || !el) return;
    const b = box.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (r.top >= b.top && r.bottom <= b.bottom) return;
    box.scrollTop += (r.top - b.top) - (b.height - r.height) / 2;
  }

  function avatarCands(id) {
    const out = [];
    if (window.__MEDIA && Array.isArray(window.__MEDIA.avatar)
        && window.__MEDIA.avatar.indexOf(String(id)) < 0) return out;
    if (IMGX2 && IMGX2.avatar && IMGX2.avatar[id]) out.push(IMGX2.avatar[id]);
    if (IMGBASE && IMGBASE.avatar && IMGBASE.avatar[id]) out.push(IMGBASE.avatar[id]);
    if (IMGMAP && IMGMAP.avatar && IMGMAP.avatar[id]) out.push(IMGMAP.avatar[id]);
    out.push('media/img/avatar/' + id + '.png', 'media/img/avatar/' + id + '.webp');
    return out;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  // ---------------------------------------------------------------- 卡片墙
  /** 卡片排序键：字段和侧栏 sideEntries 的那几个一致（id / name / count / date），
   *  这样同一个下拉框在左右两边给出同一个次序。 */
  function cardKey(c) {
    return {
      // 角色卡用角色编号；「其他」单位没有角色 ID，用单位编号（'0012345' → 12345）
      id: c.cid != null ? c.cid : (parseInt(numOf(c.num), 10) || 0),
      name: c.title,
      // 立绘 = 这套服装的表情数
      count: c.files.length,
      date: c.cid != null ? String((META[c.cid] || {}).d || '') : '',
    };
  }

  function filtered() {
    let list = scopeCards();
    if (ST.cur != null) {
      list = list.filter(c => {
        const key = curScope().char && c.cid != null ? 'c' + c.cid : 'n' + c.num;
        return key === ST.cur;
      });
    }
    if (ST.filter === 'have') list = list.filter(c => c.any);
    else if (ST.filter === 'miss') list = list.filter(c => !c.any);
    else if (ST.filter === 'fav') list = list.filter(c => isFav(c));
    /* 卡片墙自己也要排：以前只在 renderSidebar 里排，卡片墙永远按 standmap
       的原始顺序画，改下拉框"看着没反应"（2026-11-16 反馈）。
       同一角色的多套服装靠排序的稳定性保持原顺序。 */
    return ESC.sortItems(list, ST.sort, cardKey);
  }

  const favIdOf = c => c.t + '|' + c.num + '|' + (c.cloth || '');
  function isFav(c) { return UD ? UD.isFav('stand', favIdOf(c)) : false; }

  let shown = 0;
  const PAGE = 120;

  function renderGrid() {
    const grid = $('#grid');
    grid.className = 'stand-layout';
    ST.list = filtered();
    ST.visible = ST.list.map(c => ALL.indexOf(c));
    shown = 0;
    grid.innerHTML = '';
    if (!ST.list.length) {
      grid.innerHTML = '<div id="empty">这里还没有素材。' +
        '立绘跟着角色剧情一起安装，装完刷新本页即可。</div>';
      updateBar();
      return;
    }
    appendCards();
    updateBar();
  }

  function appendCards() {
    const grid = $('#grid');
    const frag = document.createDocumentFragment();
    const end = Math.min(ST.list.length, shown + PAGE);
    for (let i = shown; i < end; i++) {
      const c = ST.list[i];
      const d = document.createElement('div');
      d.className = 'card';
      d.dataset.gi = String(i);
      const first = c.files[0] || null;
      const fav = isFav(c);
      const showImg = !!(c.any && first);
      const body = !showImg
        ? '<span class="noimg-tip">未下载</span>'
        : '<img alt="" loading="lazy" src="' + first.src + '">';
      d.innerHTML =
        '<div class="thumb' + (showImg ? '' : ' noimg') + '">' + body +
        '<span class="badge">' + (c.files.length ? c.files.length + ' 张' : '—') + '</span>' +
        '<span class="fav' + (fav ? ' on' : '') + '" data-fav="1">' + (fav ? '★' : '☆') + '</span>' +
        '</div>' +
        '<div class="cap"><span class="cn">' + esc(c.title) + '</span>' +
        '<span class="ck"><span>' + esc(c.sub) + '</span><span>' + (c.num) + '</span></span></div>';
      const img = d.querySelector('img');
      if (img) {
        // 横构图的立绘（带背景的整幅插图）换 16:9 的框，别挤成一条
        const fitLand = () => {
          if (img.naturalWidth > img.naturalHeight) d.classList.add('land');
        };
        img.addEventListener('load', fitLand);
        if (img.complete && img.naturalWidth) fitLand();
        img.onerror = () => {
          // 映射里查不到时先试一次"磁盘上的同名 webp"，仍失败才退化成占位。
          if (!img.dataset.try) {
            img.dataset.try = '1';
            img.src = 'media/img/stand/' + encodeURIComponent(first.key || '') + '.webp';
            return;
          }
          const thumb = d.querySelector('.thumb');
          thumb.classList.add('noimg');
          img.remove();
          if (!thumb.querySelector('.noimg-tip')) {
            const tip = document.createElement('span');
            tip.className = 'noimg-tip';
            tip.textContent = '未下载';
            thumb.insertBefore(tip, thumb.firstChild);
          }
        };
      }
      d.querySelector('[data-fav]').onclick = ev => {
        ev.stopPropagation();
        if (!UD) return;
        const on = UD.toggleFav('stand', favIdOf(c));
        ev.currentTarget.classList.toggle('on', on);
        ev.currentTarget.textContent = on ? '★' : '☆';
        if (ST.filter === 'fav') renderGrid();
      };
      d.onclick = () => {
        if (!c.files.length) {
          toast('还没下载：装这个角色的剧情时会一起装立绘');
          return;
        }
        openLB(i, 0);
      };
      frag.appendChild(d);
    }
    grid.appendChild(frag);
    shown = end;
  }

  function updateBar() {
    const cards = ST.list.length;
    const imgs = ST.list.reduce((n, c) => n + c.files.length, 0);
    const have = ST.list.filter(c => c.any).length;
    $('#barInfo').innerHTML = '共 <b>' + cards + '</b> 组 / <b>' + imgs +
      '</b> 张 · 已下载 <b>' + have + '</b> 组';
  }

  // ---------------------------------------------------------------- 灯箱
  const lb = { i: 0, j: 0 };

  function openLB(groupIdx, imgIdx) {
    lb.i = Math.max(0, Math.min(groupIdx, ST.list.length - 1));
    if (!ST.list[lb.i] || !ST.list[lb.i].files.length) return;
    lb.j = imgIdx || 0;
    $('#lb').classList.add('on');
    paintLB();
  }

  function paintLB() {
    const c = ST.list[lb.i];
    if (!c) return closeLB();
    const f = c.files[Math.min(lb.j, c.files.length - 1)];
    // 2026-11-16：SD 灯箱分支已注释，本页只显示立绘（一张一张翻）
    $('#lbBody').classList.remove('sd');
    $('#lbRow').innerHTML = '';
    $('#lbImg').src = f.src;
    $('#lbCount').textContent = (lb.j + 1) + ' / ' + c.files.length;
    $('#lbTitle').innerHTML = '<b>' + esc(c.title) + '</b>';
    const bits = [c.sub];
    if (f.label) bits.push(f.label);
    if (!c.any) bits.push('未下载');
    $('#lbGroup').textContent = bits.filter(Boolean).join(' · ');
    const on = isFav(c);
    const fb = $('#lbFav');
    fb.classList.toggle('on', on);
    fb.textContent = on ? '★ 已收藏' : '☆ 收藏';
    const dots = c.files.map((_, k) =>
      '<span class="dot' + (k === lb.j ? ' on' : '') + '" data-k="' + k + '"></span>');
    $('#lbBottom').innerHTML = dots.join('');
    $$('#lbBottom .dot').forEach(d => {
      d.onclick = () => { lb.j = +d.dataset.k; paintLB(); };
    });
    $('#lbHint').innerHTML = '<kbd>←</kbd><kbd>→</kbd> 上一张 / 下一张（到头自动跳下一组）· ' +
      '<kbd>↑</kbd><kbd>↓</kbd> 直接换组 · <kbd>Esc</kbd> 关闭';
  }

  /* SD 灯箱统一缩放：2026-11-16 起本页不显示 SD，整段函数注释保留备查。
  function fitLbRow(files) {
    const row = $('#lbRow');
    if (!row || !files.length) return;
    const apply = () => {
      const imgs = $$('img', row);
      if (!imgs.length) return;
      const capH = 42;                 // 动作说明文字（11 个动作时会折成两行）
      const gap = 8, padX = 20, padY = 14;
      // 行高是 auto（内容撑开），可用高度要看外层 #lbBody
      const availW = row.clientWidth - padX;
      const ws = files.map(x => (+x.w || 173));
      const hs = files.map(x => (+x.h || 250));
      const maxH = Math.max.apply(null, hs) || 1;
      // 2026-11-16：动作数是包内实际数量（408 有 11 个），挤一行会小到看不清。
      // ≤6 张仍一行；>6 张按 paintLB 分好的 .lb-prow 组（11 张 = 6+5），
      // 每张等比同高、每行居中。
      const rowEls = $$('.lb-prow', row);
      const groups = rowEls.length ? rowEls.map(r => $$('img', r)) : [imgs];
      const rows = groups.length;
      const rowGap = 10;
      // 最宽的那一行决定横向缩放（按各图原始宽度累加）
      let needW = 1, idx = 0;
      const groupOf = [];                       // 每个 img 属于哪一行
      groups.forEach((g, r) => {
        let sum = 0;
        g.forEach(() => { sum += ws[idx] || 173; groupOf[idx] = r; idx++; });
        needW = Math.max(needW, sum + gap * Math.max(0, g.length - 1));
      });
      const availH = ($('#lbBody').clientHeight || row.clientHeight) - padY;
      // 每行要同时容纳「图高 + 说明文字」，再乘行数
      const kH = (((availH - rowGap * (rows - 1)) / rows) - capH) / maxH;
      const k = Math.min(1, availW / needW, kH);
      const scale = Math.max(0.15, k);
      imgs.forEach((im, i) => {
        const w = Math.floor(ws[i] * scale), h = Math.floor(hs[i] * scale);
        im.style.width = w + 'px';
        im.style.height = h + 'px';
        // 图更窄时，figure 也跟着窄到图宽，说明文字在自身宽度里折行，
        // 否则会被 special_2 这类长名字撑宽、整行溢出被裁（2026-11-16）
        const fig = im.closest('figure');
        if (fig) { fig.style.maxWidth = w + 'px'; fig.style.minWidth = '0'; }
      });
    };
    apply();
    requestAnimationFrame(apply);       // 浮动层刚显示时 clientHeight 还不准
  }
  */

  function moveImg(d) {
    const c = ST.list[lb.i];
    if (!c) return;
    const n = lb.j + d;
    if (n >= 0 && n < c.files.length) { lb.j = n; paintLB(); return; }
    // 到头了：换到相邻的组（跟 CG 页同一套行为）
    const gi = lb.i + (d > 0 ? 1 : -1);
    if (gi < 0 || gi >= ST.list.length) return;
    lb.i = gi;
    lb.j = d > 0 ? 0 : ST.list[gi].files.length - 1;
    paintLB();
    const card = $('#grid .card[data-gi="' + gi + '"]');
    if (card) card.scrollIntoView({ block: 'nearest' });
  }

  function moveGroup(d) {
    const gi = lb.i + d;
    if (gi < 0 || gi >= ST.list.length) return;
    lb.i = gi; lb.j = 0;
    paintLB();
    const card = $('#grid .card[data-gi="' + gi + '"]');
    if (card) card.scrollIntoView({ block: 'nearest' });
  }

  function closeLB() { $('#lb').classList.remove('on', 'fs'); }

  // ---------------------------------------------------------------- 交互
  function setScope(v) {
    ST.scope = v;
    ST.cur = null;
    $$('#sdScope button').forEach(b => b.classList.toggle('on', b.dataset.scope === v));
    setPref('stand', 'scope', v);
    setPref('stand', 'cur', null);   // 换了范围，旧的角色选中不该带过来
    applySize(ST.cardMin);
    renderSidebar();
    renderGrid();
  }

  function applySize(px) {
    ST.cardMin = px;
    $('#grid').style.setProperty('--cardMin', px + 'px');
    $$('#bar .sz').forEach(b => {
      b.classList.toggle('on', +b.dataset.sz === px);
      b.disabled = false;
      b.title = '';
    });
    setPref('stand', 'size', px);
  }

  function init() {
    // 老版本存的是 stand/sd/npc/enemy，由 normScope 迁移（SD 值已回落角色立绘）
    ST.scope = normScope(pref('stand', 'scope', 'char-stand'));
    ST.sort = pref('stand', 'sort', 'id');
    ST.cardMin = +pref('stand', 'size', 300) || 300;
    /* 筛选 / 搜索 / 选中的角色以前只存在内存里，刷新 shell.html?p=stand.html
       就全退回默认（2026-11-15 反馈）。一并存进偏好并回填控件。 */
    ST.filter = pref('stand', 'filter', '');
    ST.q = String(pref('stand', 'q', '') || '');
    ST.cur = pref('stand', 'cur', null);
    if (['', 'have', 'miss', 'fav'].indexOf(ST.filter) < 0) ST.filter = '';
    // 选项统一由 common.js 提供，和 CG 页共用一份（含"按实装日期"）
    ST.sort = ESC.fillSort($('#sortBy'), 'role', ST.sort);
    $('#dlFilter').value = ST.filter;
    $('#q').value = ST.q;
    applySize(ST.cardMin);
    $$('#sdScope button').forEach(b => {
      b.classList.toggle('on', b.dataset.scope === ST.scope);
      b.onclick = () => setScope(b.dataset.scope);
    });
    $('#sortBy').onchange = e => {
      ST.sort = e.target.value; setPref('stand', 'sort', ST.sort);
      renderSidebar();
      renderGrid();          // 卡片墙也要跟着重排（以前只重画了左侧列表）
    };
    $('#dlFilter').onchange = e => {
      ST.filter = e.target.value; setPref('stand', 'filter', ST.filter);
      renderGrid();
    };
    const q = $('#q');
    let qt = 0;
    q.oninput = () => {
      clearTimeout(qt);
      qt = setTimeout(() => {
        ST.q = q.value; setPref('stand', 'q', ST.q);
        renderSidebar();
        renderGrid();          // 卡片墙也要跟着筛选（以前只重画了左侧列表）
      }, 120);
    };
    // 搜索框尾部的清除叉：走 common.js 那份统一实现（和剧情页 / CG 页同一份），
    // 点一下清空并派发 input，列表按空筛选重画（2026-11-16 反馈漏了这页）
    if (window.ESC && ESC.searchClear) ESC.searchClear(q);
    $$('#bar .sz').forEach(b => { b.onclick = () => applySize(+b.dataset.sz); });

    $('#lbClose').onclick = closeLB;
    $('#lbPrev').onclick = () => moveImg(-1);
    $('#lbNext').onclick = () => moveImg(1);
    $('#lbFav').onclick = () => {
      const c = ST.list[lb.i];
      if (!UD || !c) return;
      const on = UD.toggleFav('stand', favIdOf(c));
      toast(on ? '已收藏' : '已取消收藏');
      paintLB();
      const card = $('#grid .card[data-gi="' + lb.i + '"]');
      if (card) {
        const star = card.querySelector('[data-fav]');
        if (star) { star.classList.toggle('on', on); star.textContent = on ? '★' : '☆'; }
      }
      if (ST.filter === 'fav') renderGrid();
    };
    $('#lbFs').onclick = toggleLBFull;

    // 窗口尺寸变化 / 进出全屏：SD 统一缩放已注释（本页无 SD）

    document.addEventListener('keydown', ev => {
      if (!$('#lb').classList.contains('on')) {
        if (ev.key === '/' && document.activeElement !== q) { ev.preventDefault(); q.focus(); }
        return;
      }
      if (ev.key === 'Escape') closeLB();
      else if (ev.key === 'ArrowLeft') moveImg(-1);
      else if (ev.key === 'ArrowRight') moveImg(1);
      else if (ev.key === 'ArrowUp') moveGroup(-1);
      else if (ev.key === 'ArrowDown') moveGroup(1);
      else if (ev.key === 'f' || ev.key === 'F') toggleLBFull();
    });

    // 滚动到底自动续画（一屏只画 120 张，两万张图不能一次全塞进 DOM）
    $('#grid').addEventListener('scroll', () => {
      const g = $('#grid');
      if (g.scrollTop + g.clientHeight > g.scrollHeight - 400 && shown < ST.list.length) {
        appendCards();
      }
    });

    // 深链：?scope=sd / npc / enemy 直接切范围；?at=5&i=2 打开第 5 组的第 2 张
    const qs = new URLSearchParams(location.search);
    const sc = qs.get('scope');
    if (sc) {
      ST.scope = normScope(sc);          // 旧值 stand/sd/npc/enemy 也能认（SD 回落立绘）
      setPref('stand', 'scope', ST.scope); // 深链切过来的范围也记住，刷新不跳回
      applySize(ST.cardMin);
    }
    $$('#sdScope button').forEach(b => b.classList.toggle('on', b.dataset.scope === ST.scope));

    renderSidebar();
    renderGrid();

    const at = +(qs.get('at') || 0);
    if (at > 0) openLB(at - 1, +(qs.get('i') || 0));
  }

  function toggleLBFull() {
    const el = $('#lb');
    el.classList.toggle('fs');
    if (el.classList.contains('fs')) {
      try {
        if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
      } catch (e) { /* 忽略 */ }
      if (window.ESC && ESC.orient) ESC.orient.lockLandscape();
    } else {
      try { if (document.fullscreenElement) document.exitFullscreen(); } catch (e) { /* 忽略 */ }
      if (window.ESC && ESC.orient) ESC.orient.unlock();
    }
  }

  /* 按 Esc / 系统手势退出真全屏时，别把 .fs 留在身上（否则再进一次灯箱就是全屏态），
     与 cg.js 的 fullscreenchange 处理保持一致。 */
  document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement) $('#lb').classList.remove('fs');
  });

  // CG 页的移动端抽屉会去找 #cgScope，这里换成自己的 id，写个小兼容
  window.STANDPAGE = {
    scope: () => ST.scope,
    cards: () => ST.list.length,
    setScope,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
