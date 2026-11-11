// 三个页面共用的工具。放这里是为了让"排序口径"只有一份实现 ——
// 之前播放器、CG 页、安装列表各写一套，结果同一种排序在三处表现不一样
// （比如未下载的角色：剧情页沉底、CG 页却混在中间）。
(function () {
  'use strict';

  const ESC = (window.ESC = window.ESC || {});

  /**
   * 统一排序。
   *
   * @param {Array} list 要排的数组（不会被原地修改）
   * @param {string} mode 'date-desc' | 'date-asc' | 'name' | 'id'
   * @param {Function} pick (item) => { id, name, date }
   *        date 形如 '2021-03-31'，没有就传 '' 或 null
   * @param {Object} [opts] { haveOf: (item)=>boolean }
   *        给了 haveOf 就再按"已下载优先"分一层
   * @returns {Array} 新数组
   *
   * 规则（三页一致）：
   *  - 日期排序时，**空日期按"最早"处理**（升序排最前、降序排最后），
   *    空日期之间再按 ID 升序排；
   *  - 名字用日文排序（あいうえお）；
   *  - ID 用自然序（char_2 在 char_10 前面）；
   *  - 传了 haveOf，**未下载的一律沉底**（以前只有播放器这么做，
   *    CG 页会把未下载的混在中间，同一个下拉框在两页结果不同）。
   */
  ESC.sortItems = function (list, mode, pick, opts) {
    const arr = (list || []).slice();
    const p = typeof pick === 'function' ? pick : (it => it || {});
    const m = mode || 'date-desc';

    if (m === 'date-desc' || m === 'date-asc') {
      const dir = m === 'date-desc' ? -1 : 1;
      const byId = (a, b) => String(p(a).id || '').localeCompare(
        String(p(b).id || ''), 'en', { numeric: true });
      arr.sort(function (a, b) {
        const x = String(p(a).date || '');
        const y = String(p(b).date || '');
        // 都没日期：同一档，内部按 ID 升序
        if (!x && !y) return byId(a, b);
        // 空日期当作"早于任何有日期的"：升序排最前、降序排最后
        if (!x) return -dir;
        if (!y) return dir;
        // 同一天再按 ID，避免同日期时顺序随机
        return dir * x.localeCompare(y) || byId(a, b);
      });
      return arr;
    }

    if (m === 'name') {
      arr.sort(function (a, b) {
        return String(p(a).name || '').localeCompare(String(p(b).name || ''), 'ja');
      });
      return arr;
    }

    // id / 默认
    arr.sort(function (a, b) {
      return String(p(a).id || '').localeCompare(String(p(b).id || ''), 'en',
        { numeric: true });
    });
    return arr;
  };

  /** 已下载的排前面（稳定排序，各自内部顺序不变） */
  ESC.haveFirst = function (list, haveOf) {
    if (typeof haveOf !== 'function') return list;
    const arr = (list || []).slice();
    arr.sort((a, b) => (haveOf(b) ? 1 : 0) - (haveOf(a) ? 1 : 0));
    return arr;
  };

  /** 从 char_12 里取 12；非 char 返回 0 */
  ESC.charId = function (unit) {
    const s = String(unit || '');
    return s.indexOf('char_') === 0 ? +s.slice(5) || 0 : 0;
  };

  /** 取实装日期（CHARMETA 里是 d 字段），没有返回 '' */
  ESC.dateOf = function (unit) {
    const meta = (window.CHARMETA || {})[ESC.charId(unit)] || {};
    return meta.d || '';
  };

  /* ------------------------------------------------------------ 剧情标题
   * 「哪部剧情叫什么」只能有一份实现：播放器侧栏、控制台 ②③⑤、导出列表
   * 各写一份的结果是同一部剧情在两处叫法不同，用户会以为是两部。
   *
   *   角色剧情  → 角色名；实在没有名字才退回「ID 415」
   *               （384 个角色、ID 到 415 —— 写成「角色 415」会被读成
   *                "第 415 个角色"，这也是用户明确要求去掉的写法）
   *   主线      → 「第N部 第X章」（剧本里没有文字标题，靠编号推算，见下表）
   *   活动/其他 → 数据里自带的 title，例如「別次元から来たあのこ」
   *               （官方剧本 JSON 没有 title 字段：标题要么在 ``_0`` 段台词里，
   *                要么只存在于封面横幅的美术字，由 server/cdn/data/story_titles.json
   *                补进来；还剩 110 条没有任何文本，只能退回「第N话」）
   */
  const ADV_LABEL = {
    main: '主线剧情', event: '活动剧情', other: '其他剧情',
    tutorial: '新手教程', op: '开场动画',
  };
  ESC.advLabel = ADV_LABEL;

  /** 主线的「部 / 章」结构。
   *
   *  这套层级**不在剧本数据里**（剧本 JSON 只有 9 个字段），是逐张看章节横幅图
   *  topic_chapter_<n> 核对出来的：编号 1..61 连续，部只在横幅上写「第N部」。
   *  0 / 0b / 0c 是三部的序章横幅，没有独立剧本包。
   */
  const MAIN_SECTIONS = [
    [0, 0, '第1部', '序章'],
    [1, 18, '第1部', '第{n}章'],
    [19, 19, '第1部', '終章'],
    [20, 20, '第1部', '外伝（忍者）'],
    [21, 21, '第1部', '外伝（戦士）'],
    [22, 22, '第1部', '外伝（魔女）'],
    [23, 23, '第1部', '外伝（神騎）'],
    [24, 40, '第2部', '第{n}章', 23],
    [41, 41, '第2部', '終章'],
    [42, 42, '第2部', '外伝2（戦士）'],
    [43, 43, '第2部', '外伝2（忍者）'],
    [44, 44, '第2部', '外伝2（魔女）'],
    [45, 57, '第3部', '第{n}章', 44],
    [58, 58, '第3部', '終章'],
    [59, 59, '第3部', '閑話1'],
    [60, 60, '第3部', '閑話2'],
    [61, 61, '第3部', '閑話3'],
  ];

  ESC.mainSection = function (no) {
    const row = MAIN_SECTIONS.find(r => no >= r[0] && no <= r[1]);
    if (!row) return '';
    const prev = row[4] || 0;
    return row[2] + ' ' + row[3].replace('{n}', String(no - prev));
  };

  ESC.storyTitle = function (unit) {
    const k = String(unit || '');
    if (k.indexOf('char_') === 0) {
      const id = k.slice(5);
      return (window.__NAMES || {})[id] || ('ID ' + id);
    }
    const A = window.__ADV || [];
    // 优先整包条目（没有 part 的）：序章会以「同 key + part」额外挂几条别名
    const adv = A.find(a => a.key === k && !Number.isInteger(a.part))
      || A.find(a => a.key === k);
    const m = /^adv_(\w+?)(?:_([0-9]+[a-z]?))?$/.exec(k);
    if (!m) return k;
    if (adv && adv.title && (m[1] !== 'main' || /序章/.test(adv.title))) {
      return adv.title;
    }
    if (m[1] === 'main' && m[2] && /^\d+$/.test(m[2])) {
      const sec = ESC.mainSection(+m[2]);
      if (sec) return sec;
    }
    const base = ADV_LABEL[m[1]] || m[1];
    return (m[2] ? base + ' 第' + m[2] + '话' : base) + (adv ? '' : '（未接入）');
  };

  /* ------------------------------------------------------------ 前端错误上报
   * 浏览器里的未捕获异常只打在控制台，用户看不到、我们也拿不到；而"点了没反应
   * / 白屏 / 播到某句卡住"这类反馈，没有堆栈基本查不动。这里把它们发到
   * POST /api/clientlog 落进 app.log（tag=web）。
   *   · 只在 http(s) 下工作（file:// 没有后端）
   *   · 同一消息只报一次；每分钟最多 20 条 —— 页面进了死循环也不会刷爆日志
   *   · 资源加载失败（img/script 404）不报：服务端已经有 static 404 日志
   */
  const _sent = new Set();
  let _budget = 20;
  let _budgetAt = Date.now();

  function reportError(level, msg, src, stack) {
    try {
      if (!/^https?:$/.test(location.protocol)) return;
      const key = level + '|' + msg + '|' + (src || '');
      if (_sent.has(key)) return;
      if (_sent.size > 500) _sent.clear();
      const now = Date.now();
      if (now - _budgetAt > 60000) { _budgetAt = now; _budget = 20; }
      if (_budget <= 0) return;
      _budget -= 1;
      _sent.add(key);
      fetch('/api/clientlog', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        keepalive: true,
        body: JSON.stringify({
          level: level,
          msg: String(msg).slice(0, 500),
          src: src || '',
          stack: stack ? String(stack).slice(0, 1200) : '',
          page: location.pathname + location.search,
        }),
      }).catch(function () { /* 上报失败不能再抛 */ });
    } catch (e) { /* 同上 */ }
  }

  window.addEventListener('error', function (ev) {
    // ev.message 为空 = 资源加载失败。图片 404 服务端已有记录，跳过；
    // 但 **<script> 加载失败**必须处理：data/index.js 挂了就是 INDEX=[]，
    // 角色列表会永远转圈（程序更新重启的那 1~2 秒里打开页面必然踩到）。
    if (!ev.message) {
      const t = ev.target;
      if (t && t.tagName === 'SCRIPT') {
        const key = 'escah_retry_' + (t.src || '');
        let last = 0;
        try { last = +(sessionStorage.getItem(key) || 0); } catch (e) { }
        if (Date.now() - last < 10000) return;        // 10 秒内只自动重试一次
        try { sessionStorage.setItem(key, String(Date.now())); } catch (e) { }
        location.reload();
      }
      return;
    }
    reportError('error', ev.message,
      (ev.filename || '') + ':' + (ev.lineno || 0) + ':' + (ev.colno || 0),
      ev.error && ev.error.stack ? ev.error.stack : '');
  }, true);

  window.addEventListener('unhandledrejection', function (ev) {
    const r = ev.reason;
    reportError('error', 'unhandledrejection: ' +
      ((r && (r.message || r)) || ''),
      '', (r && r.stack) ? r.stack : '');
  });

  ESC.reportError = reportError;

  /* ------------------------------------------------------------ 全屏时转横屏
   * 需求（用户 2026-11-03）：平时页面按手机默认（竖屏），点「全屏」自动转横屏看，
   * 退出全屏再转回竖屏。页面本身**不加任何 orientation 锁**，所以非全屏状态
   * 永远跟随系统方向（就是竖屏）。
   *
   * 能真正改方向的只有 ScreenOrientation.lock()，且有两个硬限制：
   *   1) 只能锁**当前处于全屏的元素**所在屏幕 → 必须在真全屏成功之后再调；
   *   2) iOS Safari 完全没有这个 API（也没有元素级 fullscreen），
   *      桌面浏览器调用一律 reject。
   * 所以这里全是"尽力而为"：失败静默，不弹错、不影响播放；
   * 调用方可用 ESC.orient.canLock() 判断要不要提示用户手动横过来。
   */
  ESC.orient = {
    /** 尝试锁横屏（只能在真全屏之后调用） */
    lockLandscape: function () {
      try {
        const so = screen.orientation;
        if (so && so.lock) {
          const p = so.lock('landscape');
          if (p && p.catch) p.catch(function () { /* 非全屏/iOS/桌面：忽略 */ });
        }
      } catch (e) { /* 同上 */ }
    },
    /** 退出全屏时解锁，让屏幕回到系统默认方向 */
    unlock: function () {
      try {
        const so = screen.orientation;
        if (so && so.unlock) so.unlock();
      } catch (e) { /* 同上 */ }
    },
    /** 这台设备/浏览器能不能替用户转屏（不能就只能提示手动旋转） */
    canLock: function () {
      try { return !!(screen.orientation && screen.orientation.lock); } catch (e) { return false; }
    },
  };

  /* ------------------------------------------------------------ 本机媒体清单
   * 头像和剧情横幅**不随程序分发**：用户 media/ 里到底有哪些图，只有磁盘知道。
   *
   * 以前前端全靠猜，两条路都会漏：
   *   · avatarsReady()/bannersReady() 按"整层下载到 98% 就算齐"判定 ——
   *     官方一上新角色（1.0.9 的 char_416），384/385 = 99.7% 仍然算齐，
   *     于是对这个唯一没图的角色照样发请求；
   *   · 映射里查不到时硬拼 <id>.png/.webp/.jpg 逐个试 —— 一个角色白送 4 个 404。
   * 结果是用户什么都没点，启动后 cmd 窗口先刷一串 404，以为程序坏了
   * （2026-11-06 反馈）。调阈值/加白名单永远追不上官方上新，所以改成
   * **只请求磁盘上确实存在的图**：服务端每次启动/每导出一张图就重扫
   * media/img/{avatar,story}，写成 data/mediaindex.js（server/media.py）。
   *
   *   ESC.media.has('avatar', 416) → true  磁盘上有，正常请求
   *                                → false 确实没有，别发请求，画占位
   *                                → null  清单不存在（纯静态托管/旧包），按老逻辑
   */
  const _mediaSets = {};
  function mediaSet(kind) {
    const M = window.__MEDIA;
    if (!M || !Object.prototype.hasOwnProperty.call(M, kind)) return null;
    let s = _mediaSets[kind];
    if (!s) {
      s = new Set((M[kind] || []).map(String));
      _mediaSets[kind] = s;
    }
    return s;
  }
  ESC.media = {
    /** 清单里有没有这个分类（没有就是"不知道"，调用方退回老逻辑） */
    known: function (kind) { return mediaSet(kind) !== null; },
    /** 'avatar' 传角色 id，'story' 传文件名主干（char_416 / main_1 / etc_5） */
    has: function (kind, key) {
      const s = mediaSet(kind);
      return s ? s.has(String(key)) : null;
    },
  };

  /* ------------------------------------------------------------ 搜索框「清除 ✕」
   * 需求（用户 2026-11-09）：所有搜角色 / 搜剧情的搜索框后面都加一个打叉，
   * 点了就清空并把列表恢复成"没搜索之前"的样子。
   *
   * 三处搜索框（播放器侧栏 #search、CG 页 #q、控制台 5 个列表的 .sl-search）
   * 布局各不相同，各写一套必然走样，所以统一在这里做：插按钮 + 注入一次样式。
   * 这里只负责"清空 + 派发 input 事件"，重筛仍由页面自己的防抖处理（都是
   * input → render），所以点叉之后列表就是空筛选的结果，与手动删字完全一致。
   */
  let _clearCss = false;
  function injectClearCss() {
    if (_clearCss) return;
    _clearCss = true;
    const st = document.createElement('style');
    st.id = 'escClearCss';
    st.textContent = [
      /* 叉要跟输入框"正中"对齐，只能以输入框自己的高度为准：
         wrap 只当定位容器，高度必须严格等于 input —— 输入框的外边距全部搬到
         wrap 上（见 searchClear），再把 input 变成块级，去掉行盒基线留的空隙。
         以前只搬左右外边距，块级 input 的 margin-bottom 留在里面、wrap 又不会
         和它塌陷，wrap 就比输入框高出 8~10px，叉看着就是歪的。 */
      '.esc-clear-wrap{position:relative;display:block}',
      '.esc-clear-wrap>input{display:block;width:100%;box-sizing:border-box;margin:0}',
      /* top/bottom:0 + margin:auto = 几何居中：不用百分比、不会有半像素模糊，
         wrap 高多少都正好压在中间。 */
      '.esc-clear-wrap>.esc-clear{position:absolute;top:0;bottom:0;right:6px;',
      '  margin:auto 0;width:18px;height:18px;display:flex;align-items:center;',
      '  justify-content:center;border:0;padding:0;border-radius:50%;cursor:pointer;',
      '  background:rgba(255,255,255,.07);color:#b6c0d2;',
      '  transition:background .15s,color .15s}',
      /* ✕ 用两条伪元素斜线画，别用字符：字符带字体基线，圆里会偏下 1~2px */
      '.esc-clear-wrap>.esc-clear::before,.esc-clear-wrap>.esc-clear::after{',
      '  content:"";position:absolute;left:50%;top:50%;width:8px;height:1.5px;',
      '  border-radius:1px;background:currentColor;',
      '  transform:translate(-50%,-50%) rotate(45deg)}',
      '.esc-clear-wrap>.esc-clear::after{transform:translate(-50%,-50%) rotate(-45deg)}',
      '.esc-clear-wrap>.esc-clear:hover{background:rgba(255,255,255,.22);color:#fff}',
      '.esc-clear-wrap>.esc-clear:focus-visible{outline:2px solid var(--purple,#8b7cf6)}',
      /* display:flex 会盖掉 UA 的 [hidden]{display:none}，必须自己写一条 */
      '.esc-clear-wrap>.esc-clear[hidden]{display:none}',
    ].join('');
    document.head.appendChild(st);
  }

  /**
   * 给一个搜索输入框加上"清除 ✕"。
   * @param {Element} input 搜索用的 input
   * @returns {Element|null} 清除按钮（便于测试/自动化）
   */
  ESC.searchClear = function (input) {
    if (!input || input._escClear) return null;
    input._escClear = true;
    injectClearCss();

    const cs = getComputedStyle(input);

    /* 输入框可能原来是 flex 子项（控制台 .sl-search{flex:1 1 200px}）：
       包一层后这些尺寸属性要挪到外层，否则那一行会塌成一个窄框。 */
    const flexItem = parseFloat(cs.flexGrow) > 0
      || (cs.flexBasis && cs.flexBasis !== 'auto' && cs.flexBasis !== '0%');

    const wrap = document.createElement('span');
    wrap.className = 'esc-clear-wrap';
    if (flexItem) {
      if (cs.flex) wrap.style.flex = cs.flex;
      if (cs.minWidth && cs.minWidth !== 'auto' && parseFloat(cs.minWidth)) {
        wrap.style.minWidth = cs.minWidth;
      }
    }
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    /* 四边外边距全部搬到外层：只要有一边留在输入框上，wrap 就会比输入框高一截
       （块级父元素在这里不会和子元素发生外边距塌陷），叉必然偏上或偏下 ——
       这正是之前播放器页偏下 5px、CG 页偏上 4px 的原因。搬完 wrap 高度
       就等于输入框高度，叉的居中才是真的几何居中。 */
    ['Top', 'Right', 'Bottom', 'Left'].forEach(function (side) {
      const v = cs['margin' + side];
      if (v && v !== '0px') wrap.style['margin' + side] = v;
      input.style.setProperty('margin-' + side.toLowerCase(), '0px', 'important');
    });
    /* 宽度一律用行内样式，别赌级联：各页选择器权重不一样（CG 页是
       `#sidebar #q{width:auto}`），统一成"撑满 wrap 的 border box"，
       顺便保证 content-box 的页不会撑出外层把叉挤到框外。 */
    input.style.width = '100%';
    input.style.boxSizing = 'border-box';
    input.style.display = 'block';
    input.style.paddingRight = '30px';      // 让文字别压到叉上

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'esc-clear';
    btn.hidden = true;
    btn.title = (window.L10N && L10N.t) ? L10N.t('search.clear', '清除搜索') : '清除搜索';
    btn.setAttribute('aria-label', btn.title);
    // 叉本身由 CSS 伪元素画（位置写死 50%/50%），不塞字符：字体基线会让 ✕ 偏下
    wrap.appendChild(btn);

    const sync = () => { btn.hidden = !input.value; };
    const clear = ev => {
      if (ev) { ev.preventDefault(); ev.stopPropagation(); }
      if (!input.value) return;
      input.value = '';
      // 派发 input：各页面原有的防抖重筛会跑一遍，等价于用户手动删光
      input.dispatchEvent(new Event('input', { bubbles: true }));
      sync();
      input.focus();
    };
    // mousedown 先拦一下：点叉不会把焦点从输入框抢走（桌面上手一松就能继续打字）
    btn.addEventListener('mousedown', ev => ev.preventDefault());
    btn.addEventListener('click', clear);
    /* 有的地方是代码里直接 `input.value=''`（切分类/离开播放记录），不会发 input
       事件，所以这些时机都顺手同步一次显示状态。 */
    ['input', 'change', 'keyup', 'click', 'focus', 'blur'].forEach(ev =>
      input.addEventListener(ev, sync));
    _clearReg.push(sync);
    sync();
    return btn;
  };

  /* 代码里改 value（不派发 input）时，靠下面这两个捕获阶段监听把叉的显隐补正：
     用户点任何地方/按任何键，都顺手对一遍（几处搜索框，开销可以忽略）。
     setTimeout 那一遍是必需的：切分类这类处理里是**在 click 里**才把 value 清空的，
     捕获阶段先跑，看到的是清空前的老值。 */
  const _clearReg = [];
  const syncAll = () => _clearReg.forEach(f => f());
  ['click', 'keydown'].forEach(ev => {
    document.addEventListener(ev, () => {
      syncAll();
      setTimeout(syncAll, 0);
    }, true);
  });
})();
