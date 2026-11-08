/* 剧情列表组件（控制台 ②剧情安装 ③提高CG画质 ④导出 ⑤翻译 四页共用）
 *
 * 为什么单独抽一层
 * ───────────────
 * 这些页都要"挑剧情"，以前各写一套：分类只有「角色剧情 / 主线·活动」两分法、
 * 行样式每页一种、还把内部 key（char_415）直接印在名字后面 ——
 * 而角色只有 384 个、ID 却到 415，用户看到 char_415 会以为"角色缺了 31 个"。
 *
 * 现在统一成和播放器侧栏（index.html 的 #sidebar）同一套口径：
 *   · 四个分类：角色 / 主线 / 活动 / 其他（新手教程、开场动画归「其他」）
 *   · 排序默认「实装日期 新→旧」，另有 旧→新 / 按名字 / 按 ID
 *   · 状态筛选默认「全部剧情」，可切 只看未安装 / 只看已安装
 *   · 稀有度 SSR / SR / R 只在「角色」分类下出现
 *   · 公共素材里的「剧情横幅」下载之后才显示横幅图（和播放器一样），
 *     之前只显示名字（否则就是几百个 404 + 空白画框）
 *   · 名字一律用人话（角色名 / 章节标题）；内部 key 只在 data-key 里，不上屏
 *
 * 用法：
 *   const inst = SL.create({
 *     bar: '#packBar',              // 控件容器（分类 / 搜索 / 排序 / 状态 / 稀有度）
 *     list: '#packList',            // 列表容器
 *     sel: ST.sel,                  // 选中集合（外部持有，组件读写）
 *     src: 'char',
 *     srcs: ['char'],               // 只显示这几类（默认四类全上；CG 导出只有角色侧）
 *     status: key => 'ready' | 'soft-missing' | 'partial' | 'missing',
 *     noStatus: true,               // 不显示「全部/未安装/已安装」筛选（如只列已装的页）
 *     units: src => [...],          // 该分类下全部单元 key
 *     banners: () => true,          // 剧情横幅是否已下载
 *     disabled: key => false,       // 该行不可勾选
 *     badge: key => ({ cls, text }),// 行尾状态徽标
 *     extra: key => ['9 组 · 62 张'],// 副标题里的额外信息（CG 导出用来报组数/张数）
 *     actions: key => [{ act, label, title, cls }],
 *     onAction: (key, act, ev) => {},
 *     onChange: (key, checked) => {},
 *     onSrc: src => {},             // 切分类（选择跨类无效的页面用来自清）
 *     empty: state => '…',          // 空列表文案
 *   });
 *   inst.refresh(); inst.selectAll(); inst.clear();
 *   inst.setSrc('main'); inst.getSrc(); inst.items();
 */
window.SL = (() => {
  'use strict';

  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
  const q = s => (typeof s === 'string' ? document.querySelector(s) : s);

  // 分类顺序与播放器侧栏 #srcRow 完全一致
  const SRCS = [
    { k: 'char', label: '角色' },
    { k: 'main', label: '主线' },
    { k: 'event', label: '活动' },
    { k: 'other', label: '其他' },
  ];

  /* advindex 用 Map 索引：分类过滤要对 227 个 key 逐个查 kind，
     线性扫一遍 225 条太浪费（列表每次重画都走这条路）。 */
  let _advMap = null;
  let _advLen = -1;
  function advIndexMap() {
    const A = window.__ADV || [];
    if (_advMap && _advLen === A.length) return _advMap;
    _advMap = new Map();
    // 同一个 key 会有多条别名：序章包 adv_op 就挂了「第1/2/3部 序章」三条
    // kind:main 别名（已不再有 kind:op 的整包条目）。这里按 key 归并出**一条代表项**
    // （优先不带 part 的整包条目），供 srcOf / 名字推算等「按包」判断使用；
    // 列表与计数要「按条」展示序章时，由下面的 keysOf 另外展开。
    A.forEach(function (a) {
      const prev = _advMap.get(a.key);
      if (!prev || (!Number.isInteger(a.part) && Number.isInteger(prev.part))) {
        _advMap.set(a.key, a);
      }
    });
    _advLen = A.length;
    return _advMap;
  }

  function advOf(key) { return advIndexMap().get(key) || null; }

  /** 单元 key → 侧栏分类（tutorial / op 归「其他」，与播放器 ADV_MENU 一致） */
  function srcOf(key) {
    if (String(key).indexOf('char_') === 0) return 'char';
    const a = advOf(key);
    const kind = a ? a.kind : (String(key).split('_')[1] || 'other');
    return (kind === 'main' || kind === 'event') ? kind : 'other';
  }

  /** 稀有度归一化：SSR/SR/R，其余返回 ''（当年收/未收录） */
  function rarityOf(key) {
    if (String(key).indexOf('char_') !== 0) return '';
    const id = String(key).slice(5);
    const r = ((window.__CHARMETA || {})[id] || {}).r || '';
    return (r === 'SSR' || r === 'SR' || r === 'R') ? r : '';
  }

  /* ---------------- 剧情横幅路径
   * 路径按约定拼得出来，但**文件名算得出来 ≠ 磁盘上有**：官方刚上的新角色 /
   * 新章节、用户还没装公共素材，都会拼出一条不存在的路径，直接挂 <img src>
   * 就是一次必然失败的 404（2026-11-06 用户反馈的「启动就报 404」）。
   * 所以统一先问本机清单 data/mediaindex.js（见 assets/js/common.js: ESC.media）：
   * 清单说没有 → 返回空串，行里不挂图，只显示文字（列表照样对齐）。
   * 清单不可用（null，纯静态托管/旧包）→ 照旧拼，退回老行为。 */
  function bnrHas(stem) {
    return !(window.ESC && ESC.media && ESC.media.has('story', stem) === false);
  }
  function bnrPath(stem) {
    return stem && bnrHas(stem) ? 'media/img/story/' + stem + '.webp' : '';
  }
  function bnrKeep(path) {
    const m = /^media\/img\/story\/([^./]+)\./.exec(path || '');
    if (!m) return path || '';
    return bnrHas(m[1]) ? path : '';
  }

  /** 一行要显示的信息。名字取不到时用「ID 415」而不是「角色 415」：
   *  前者是事实（游戏内部编号），后者会被读成"第 415 个角色"。
   *  item 既可以是 key 字符串，也可以是 advindex 的别名条目（一个包挂了多条
   *  同分类别名时，keysOf 会把它们展开成多条，这里直接用它自己的标题/段数）。 */
  function info(item) {
    const key = (typeof item === 'string') ? item : ((item && item.key) || '');
    if (key.indexOf('char_') === 0) {
      const id = key.slice(5);
      const nm = (window.__NAMES || {})[id] || '';
      const m = (window.__CHARMETA || {})[id] || {};
      return {
        name: nm || ('ID ' + id),
        rar: (m.r === 'SSR' || m.r === 'SR' || m.r === 'R') ? m.r : '',
        date: m.d || '',
        cover: bnrPath('char_' + id),
        adv: false, parts: 0, lines: 0,
        order: '',
      };
    }
    // 别名条目（adv_op 的「第1/2/3部 序章」）：直接用条目自身的标题/段数
    const alias = !!(item && typeof item === 'object' && item.key
      && item.key === key && advOf(key) !== item);
    const a = alias ? item : advOf(key);
    // 标题统一走 common.js 的 ESC.storyTitle：主线章节标题是「编号推算」出来的
    // （官方剧本 JSON 只有 9 个字段、没有 title，标题只能从 `_0` 段台词或封面图
    //  识读补进 server/cdn/data/story_titles.json；现在 225 条里还剩 110 条是空的，
    //  直接拿 a.title 会显示成「第 null 话」），
    // 播放器侧栏、控制台 ②③⑤、导出列表必须显示同一个名字。
    // 别名条目一定带 title（序章），优先用，避免三条序章显示成同一个名字。
    if (a) {
      return {
        name: (alias && a.title) ? a.title : ESC.storyTitle(key),
        rar: '', date: '', cover: bnrKeep(a.cover || ''), adv: true,
        parts: a.parts || 0, lines: a.lines || 0,
        order: String((a.order == null ? 999 : a.order)).padStart(4, '0'),
      };
    }
    // advindex 里没有（官方新上、还没装过）→ ESC.storyTitle 会加「（未接入）」。
    // 横幅：解包/公共素材里就是 media/img/story/<kind>_<no>.webp（与 sync_advindex
    // 推 cover 的规则完全一致），所以这里按约定直接拼 —— 图没下过时 rowHtml 的
    // onerror 会把横幅收起来，不会留破图；拼了才有"新活动一更新就带横幅"。
    // 排序：主线的编号**就是章序**，按它排；**活动/其他不行** —— adv_event_<n> 是
    // bundle 批次号，和官方发表顺序无关（实测 adv_event_4 是官方第 16 个活动），
    // 按它排会插错位置，所以一律排到本类末尾（与 sync_advindex 的 1000+n 同口径）。
    const m2 = /^adv_(\w+?)(?:_(\d+))?$/.exec(key) || [];
    const kind2 = m2[1] || '';
    const no2 = m2[2] ? +m2[2] : null;
    const canBnr = no2 !== null && kind2 !== 'tutorial' && kind2 !== 'op';
    return {
      name: ESC.storyTitle(key),
      rar: '', date: '',
      cover: canBnr ? bnrPath(kind2 + '_' + no2) : '',
      adv: true, parts: 0, lines: 0,
      order: (kind2 === 'main' && no2 !== null ? String(no2) : '9999').padStart(4, '0'),
    };
  }

  function create(cfg) {
    cfg = cfg || {};
    const bar = q(cfg.bar);
    const list = q(cfg.list);
    const sel = cfg.sel || new Set();
    const state = {
      src: cfg.src || 'char',
      sort: cfg.sort || 'date-desc',
      status: '',
      rar: new Set(),
      q: '',
    };
    let timer = 0;
    const groupOpen = new Set();   // 折叠分组展开态（内存态，默认收起/展开见 groupDefaultOpen）

    const unitsOf = src => (cfg.units ? cfg.units(src) : []) || [];
    // 别名条目（adv_op 序章）展开后传入的是对象，安装/状态一律按它真正的包 key 走
    const pkgKey = k => (typeof k === 'object' && k ? k.key : k);
    const statusOf = k => (cfg.status ? cfg.status(pkgKey(k)) : 'missing');
    const isHave = k => statusOf(k) !== 'missing';
    const bannersOn = () => (cfg.banners ? !!cfg.banners() : false);
    /** 显示名：页面可以覆盖（CG 导出用 cglist 自己的名字，
     *  负数 id 的「NPC / 非角色 CG」分组在那里才是对的） */
    const nameOf = k => (cfg.name && cfg.name(k)) || info(k).name;

    /* 一个 key 下、归类为某 src 的全部别名条目：advindex 里同一个 key 可能挂多条
     * kind 相同的别名，例如 adv_op 的「第1/2/3部 序章」。返回这些条目本身，
     * 供 keysOf 把序章分别列出来（而不是被去重成一条）。 */
    function advAliasEntries(key, src) {
      const A = window.__ADV || [];
      return A.filter(function (e) {
        return e.key === key && (e.kind === src
          || (src === 'other' && e.kind !== 'main' && e.kind !== 'event'));
      });
    }

    /** 某一类的单元（页面给的清单可能把角色/主线/活动/其他混在一起，
     *  这里按 key 自己归类，保证四个页签各归各的）。
     *  一个包挂了多条同分类别名（序章包 adv_op）时，展开成多条，让主线把
     *  第1/2/3部 序章分别列出来，计数与播放器侧栏一致（主线 64 部）。 */
    function keysOf(src) {
      const all = unitsOf(src);
      if (src === 'char') return all.filter(k => String(k).indexOf('char_') === 0);
      const out = [];
      all.forEach(function (k) {
        if (String(k).indexOf('char_') === 0) return;
        const aliases = advAliasEntries(k, src);
        if (aliases.length > 1) aliases.forEach(a => out.push(a));
        else if (srcOf(k) === src) out.push(k);
      });
      return out;
    }

    /* ---------------------------------------------------------- 过滤 + 排序 */
    function filtered() {
      let keys = keysOf(state.src);
      if (state.status === 'miss') keys = keys.filter(k => !isHave(k));
      if (state.status === 'have') keys = keys.filter(isHave);
      if (state.src === 'char' && state.rar.size) {
        keys = keys.filter(k => state.rar.has(rarityOf(k)));
      }
      const s = (state.q || '').trim().toLowerCase();
      if (s) {
        keys = keys.filter(k => {
          const i = info(k);
          return (nameOf(k) || '').toLowerCase().indexOf(s) >= 0
            || String(i.date || '').indexOf(s) >= 0
            || String(k).toLowerCase().indexOf(s) >= 0;   // 允许按内部 ID 搜（只是搜，不上屏）
        });
      }
      // 排序口径走 common.js，和播放器、CG 页、安装列表同一份实现
      return ESC.sortItems(keys, state.sort, k => {
        const i = info(k);
        return { id: i.adv ? i.order : k, name: nameOf(k), date: i.date };
      });
    }

    /* ---------------------------------------------------------- 控件条 */
    /** 要显示哪几类：默认四类全上；只列角色的页面（CG 导出）只给它一个 */
    const SRCS_ON = SRCS.filter(s => !cfg.srcs || cfg.srcs.indexOf(s.k) >= 0);
    const SHOW_STATUS = cfg.noStatus !== true;

    function barHtml() {
      return '<div class="sl-src">'
        + SRCS_ON.map(s => '<button type="button" data-src="' + s.k + '">'
          + s.label + ' <small></small></button>').join('')
        + '</div>'
        + '<input class="sl-search" placeholder="搜索名字 / ID…" autocomplete="off">'
        + '<div class="sl-filters">'
        + '<select class="sl-sort" title="排序方式">'
        + '<option value="date-desc">实装日期 新→旧</option>'
        + '<option value="date-asc">实装日期 旧→新</option>'
        + '<option value="name">按名字</option>'
        + '<option value="id">按 ID</option>'
        + '</select>'
        + (SHOW_STATUS
          ? '<select class="sl-status" title="按安装状态筛选">'
            + '<option value="">全部剧情</option>'
            + '<option value="miss">只看未安装</option>'
            + '<option value="have">只看已安装</option>'
            + '</select>'
          : '')
        + '<div class="sl-rar">'
        + '<button type="button" data-r="SSR">SSR</button>'
        + '<button type="button" data-r="SR">SR</button>'
        + '<button type="button" data-r="R">R</button>'
        + '</div>'
        + '</div>'
        + '<span class="sl-hit"></span>';
    }

    function bindBar() {
      if (!bar) return;
      bar.querySelectorAll('.sl-src button').forEach(function (b) {
        b.onclick = function () {
          if (state.src === b.dataset.src) return;
          state.src = b.dataset.src;
          state.q = '';
          const inp = bar.querySelector('.sl-search');
          if (inp) inp.value = '';
          if (cfg.onSrc) cfg.onSrc(state.src);
          render();
        };
      });
      const inp = bar.querySelector('.sl-search');
      if (inp) {
        inp.oninput = function () {
          const v = inp.value;
          clearTimeout(timer);
          // 384 个角色 / 227 部剧情，逐字符重画出会明显掉帧
          timer = setTimeout(function () { state.q = v; render(); }, 120);
        };
      }
      const so = bar.querySelector('.sl-sort');
      if (so) so.onchange = function () { state.sort = so.value; render(); };
      const st = bar.querySelector('.sl-status');
      if (st) st.onchange = function () { state.status = st.value; render(); };
      bar.querySelectorAll('.sl-rar button').forEach(function (b) {
        b.onclick = function () {
          const r = b.dataset.r;
          if (state.rar.has(r)) state.rar.delete(r); else state.rar.add(r);
          render();
        };
      });
    }

    /** 只同步"状态"，不重建控件（否则搜索框每打一个字就失焦） */
    function syncBar() {
      if (!bar) return;
      bar.querySelectorAll('.sl-src button').forEach(function (b) {
        const k = b.dataset.src;
        b.classList.toggle('on', k === state.src);
        const n = keysOf(k).length;
        const small = b.querySelector('small');
        if (small) small.textContent = n ? '(' + n + ')' : '';
      });
      // 稀有度只对角色有意义，切到主线/活动/其他就收起来（省一行高度）
      const rr = bar.querySelector('.sl-rar');
      if (rr) rr.style.display = state.src === 'char' ? '' : 'none';
      const so = bar.querySelector('.sl-sort');
      if (so && so.value !== state.sort) so.value = state.sort;
      const st = bar.querySelector('.sl-status');
      if (st && st.value !== state.status) st.value = state.status;
      bar.querySelectorAll('.sl-rar button').forEach(function (b) {
        b.classList.toggle('on', state.rar.has(b.dataset.r));
      });
    }

    /* ---------------------------------------------------------- 行 */
    function rowHtml(item) {
      const key = (typeof item === 'string') ? item : (item && item.key) || '';
      const i = info(item);
      const on = sel.has(key);
      const dis = cfg.disabled ? !!cfg.disabled(key) : false;
      const badge = cfg.badge ? cfg.badge(key) : null;
      const acts = (cfg.actions ? cfg.actions(key) : null) || [];
      const showBnr = !!(bannersOn() && i.cover);
      const sub = [];
      if (i.rar) sub.push('<span class="sl-rar-i" data-r="' + i.rar + '">' + i.rar + '</span>');
      if (i.date) sub.push('<span>实装 ' + esc(i.date) + '</span>');
      else if (!i.adv) sub.push('<span>实装日期未收录</span>');
      if (i.adv && i.parts) {
        sub.push('<span>' + i.parts + ' 段 · ' + i.lines + ' 句</span>');
      }
      // 页面自带的额外信息（CG 导出用来报「N 组 · M 张」）
      const extra = (cfg.extra ? cfg.extra(key) : null) || [];
      extra.forEach(function (t) {
        if (t) sub.push('<span>' + esc(t) + '</span>');
      });
      if (badge) {
        sub.push('<span class="sl-badge ' + esc(badge.cls || '') + '">'
          + esc(badge.text) + '</span>');
      }
      // 别名条目（adv_op 序章）用自己的标题，否则走页面覆盖名（其他分类的入口清单名）
      const label = (typeof item === 'object' && item.key) ? i.name : nameOf(key);
      const title = label
        + (i.date ? '\n实装 ' + i.date : '')
        + (i.adv && i.parts ? '\n' + i.parts + ' 段 · ' + i.lines + ' 句' : '')
        + (extra.length ? '\n' + extra.join(' · ') : '')
        + (cfg.rowTitle ? '\n' + cfg.rowTitle(key) : '');
      return '<label class="sl-row' + (on ? ' on' : '') + (dis ? ' dis' : '')
        + (showBnr ? ' bnr' : '') + '" data-key="' + esc(key) + '"'
        + ' title="' + esc(title) + '">'
        + '<input type="checkbox" class="sl-chk" data-key="' + esc(key) + '"'
        + (on ? ' checked' : '') + (dis ? ' disabled' : '') + '>'
        + (showBnr
          ? '<span class="sl-bnr"><img alt="" loading="lazy" src="' + esc(i.cover) + '"'
            + (i.adv ? ' class="adv"' : '')
            + ' onerror="var r=this.parentNode.parentNode;'
            + 'this.parentNode.style.display=\'none\';if(r)r.classList.remove(\'bnr\')">'
            + '</span>'
          : '')
        + '<span class="sl-info"><span class="sl-nm">' + esc(label) + '</span>'
        + '<span class="sl-sub">' + sub.join('') + '</span></span>'
        + acts.map(a => '<button type="button" class="sl-act'
          + (a.cls ? ' ' + a.cls : '') + '" data-act="' + esc(a.act)
          + '" data-key="' + esc(key) + '"'
          + (a.title ? ' title="' + esc(a.title) + '"' : '') + '>'
          + esc(a.label) + '</button>').join('')
        + '</label>';
    }

    function bindList() {
      if (!list) return;
      list.querySelectorAll('.sl-chk').forEach(function (cb) {
        cb.onchange = function () {
          const k = cb.dataset.key;
          if (cb.checked) sel.add(k); else sel.delete(k);
          const row = cb.closest('.sl-row');
          if (row) row.classList.toggle('on', cb.checked);
          if (cfg.onChange) cfg.onChange(k, cb.checked);
        };
      });
      list.querySelectorAll('.sl-act').forEach(function (b) {
        b.onclick = function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          if (cfg.onAction) cfg.onAction(b.dataset.key, b.dataset.act, ev);
        };
      });
      list.querySelectorAll('.og-head').forEach(function (h) {
        h.onclick = function () {
          const id = h.dataset.gid;
          if (groupOpen.has(id)) groupOpen.delete(id); else groupOpen.add(id);
          render();
        };
      });
    }

    /* ---------------------------------------------------------- 分组渲染
     * 把 keys 按 cfg.groupOf 归到各分组，每组前插一个可折叠的分组头。
     * 空分组 id（''）直接平铺，便于「非 other 分类」或「入口清单未就绪」时退回普通列表。 */
    function groupHtml(keys) {
      const map = new Map();
      keys.forEach(function (k) {
        const g = (cfg.groupOf ? cfg.groupOf(k) : '') || '';
        if (!map.has(g)) map.set(g, []);
        map.get(g).push(k);
      });
      const rawIds = [...map.keys()];
      let ids = cfg.groupOrder ? cfg.groupOrder(rawIds) : rawIds;
      // groupOrder 可能漏掉 ''（非分组类）或未登记的 etc_?，这里补回，避免整页空白
      rawIds.forEach(function (id) { if (ids.indexOf(id) < 0) ids.push(id); });
      const forceOpen = !!String(state.q || '').trim() || state.rar.size > 0 || !!state.status;
      let html = '';
      ids.forEach(function (id) {
        const gk = map.get(id);
        if (!gk || !gk.length) return;
        if (id === '') { html += gk.map(rowHtml).join(''); return; }   // 无分组：平铺
        const open = forceOpen
          || (cfg.groupDefaultOpen === false ? groupOpen.has(id) : !groupOpen.has(id));
        // 分组横幅（如播放页侧栏「其他」的入口横幅）：cfg.groupBanner(id) 返回图片地址，
        // 没下过公共素材时返回 ''，分组头就退回纯文字一行。
        const bnr = cfg.groupBanner ? cfg.groupBanner(id) : '';
        html += '<div class="og-head' + (open ? ' open' : '') + (bnr ? ' has-bnr' : '') + '" data-gid="' + esc(id) + '">'
          + (bnr ? '<span class="bnr"><img alt="" loading="lazy" src="' + esc(bnr) + '"'
            + ' onerror="this.closest(\'.bnr\').style.display=\'none\'"></span>' : '')
          + '<span class="row"><span class="nm">' + esc(cfg.groupLabel ? cfg.groupLabel(id) : id) + '</span>'
          + '<span class="cnt">' + gk.length + '</span>'
          + '<span class="arw">' + (open ? '▾' : '▸') + '</span></span></div>';
        if (open) html += '<div class="og-body">' + gk.map(rowHtml).join('') + '</div>';
      });
      return html;
    }

    /* ---------------------------------------------------------- 渲染 */
    function render() {
      if (!list) return;
      const keys = filtered();
      const active = !!String(state.q || '').trim() || state.rar.size > 0 || !!state.status;
      const hit = bar ? bar.querySelector('.sl-hit') : null;
      if (hit) {
        // 一条都没有时，同一句话已经在列表中央的 .sl-empty 里说过了，这里再挂一条红色
        // 「没有匹配的剧情」纯属重复噪音（用户会以为两处说的是两件事）。
        hit.textContent = (active && keys.length) ? '命中 ' + keys.length + ' 部剧情' : '';
        hit.className = 'sl-hit' + ((active && keys.length) ? ' on' : '');
      }
      if (!keys.length) {
        /* 空列表的提示必须说**当前事实**，别一律劝用户去改筛选：
           2026-11-06 用户反馈，明明这一类 385 部全装好了，「只看未安装」下空得理所当然，
           文案却在说"把筛选切回全部剧情查看"，读起来像出了故障。 */
        const all = keysOf(state.src).length;
        const filtering = !!String(state.q || '').trim() || state.rar.size > 0;
        let hint;
        if (cfg.empty) hint = cfg.empty(state);
        else if (state.status === 'miss')
          hint = '这一类' + (all ? ' ' + all + ' 部' : '') + '全部已安装，没有未安装的剧情';
        else if (state.status === 'have')
          hint = '这一类还没有装任何剧情 —— 切回「全部剧情」挑一部装';
        else if (filtering) hint = '没有匹配的剧情';
        /* 没开任何筛选却空：这类页面（④导出只列已装、③只列有图的）各有各的原因，
           只说事实、别猜原因，免得和安装页的"先下载剧情目录"对上号。 */
        else hint = '这一类还没有可选的剧情';
        list.innerHTML = '<div class="sl-empty">' + esc(hint) + '</div>';
      } else if (cfg.groupOf) {
        list.innerHTML = groupHtml(keys);
        bindList();
      } else {
        list.innerHTML = keys.map(rowHtml).join('');
        bindList();
      }
      syncBar();
      if (cfg.onRender) cfg.onRender(keys);
    }

    if (bar && !bar.dataset.slBuilt) {
      bar.dataset.slBuilt = '1';
      bar.innerHTML = barHtml();
      bindBar();
    }

    return {
      state: state,
      sel: sel,
      render: render,
      refresh: render,
      items: filtered,
      count: function () { return filtered().length; },
      getSrc: function () { return state.src; },
      setSrc: function (src) {
        state.src = src;
        state.q = '';
        if (bar) {
          const inp = bar.querySelector('.sl-search');
          if (inp) inp.value = '';
        }
        render();
      },
      /** 全选"当前筛选结果"里可勾选的行（不会越界到别的分类） */
      selectAll: function () {
        filtered().forEach(function (k) {
          const key = pkgKey(k);
          if (cfg.disabled && cfg.disabled(key)) return;
          sel.add(key);
        });
        render();
      },
      clear: function () { sel.clear(); render(); },
    };
  }

  return { create, info, srcOf, rarityOf, SRCS };
})();
