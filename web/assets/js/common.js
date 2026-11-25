// 三个页面共用的工具。放这里是为了让"排序口径"只有一份实现 ——
// 之前播放器、CG 页、安装列表各写一套，结果同一种排序在三处表现不一样
// （比如未下载的角色：剧情页沉底、CG 页却混在中间）。
(function () {
  'use strict';

  const ESC = (window.ESC = window.ESC || {});

  /* ---------------------------------------------------------------- 选中文字 ≠ 点击
   * 全站的卡片 / 列表行 / 按钮都是整块可点（onclick）。用户想复制角色名或曲名时，
   * 按下拖动选字、松手那一刻 click 照样触发 → 卡片打开 / 列表重绘，选区当场
   * 消失，根本来不及 Ctrl+C（2026-11-15 用户反馈）。
   *
   * 在**捕获阶段**吞掉"拖选结束带出的那一次 click"：
   *   · 拖选完松手：选区非空 → 这次 click 不再往下传，选中的字留住；
   *   · 输入框 / 文本域 / 可编辑区不拦（那里点一下是定位光标或选词）。
   * 注意 click 的 detail：鼠标单击/双击是 1/2，键盘触发是 0，后者不拦。
   *
   * 2026-11-16 修正：判定条件从"click 时选区非空"改成"这一次按下才拖出来的选区"。
   * 旧写法假设"点别处时 mousedown 会先清掉旧选区"，但 **Chrome 在 <button> 上按下
   * 鼠标并不清选区** —— 于是拖选一段文字之后，点「关闭」这类按钮时选区还在，
   * click 被一直吞掉，表现就是"关闭按钮有时候点不动"（要再点一次空白处才恢复）。
   * 现在在 mousedown（捕获）记下按下那一刻的选区文本：
   *   · 与 click 时相同 → 这次不是拖选，是"点别处"：清掉选区并放行 click；
   *   · 不同（或按下时为空）→ 是这次按下拖出来的 → 照旧吞掉。
   */
  var selAtDown = '';
  function selText() {
    var s = document.getSelection ? document.getSelection() : null;
    if (!s || s.isCollapsed) return '';
    return String(s.toString() || '').trim();
  }
  document.addEventListener('mousedown', function () { selAtDown = selText(); }, true);
  document.addEventListener('click', function (e) {
    if (!e.detail) return;
    var cur = selText();
    if (!cur) return;
    var t = e.target;
    if (t && t.closest && t.closest(
      'input, textarea, select, [contenteditable]:not([contenteditable="false"])')) {
      return;
    }
    var pre = selAtDown;
    selAtDown = '';
    if (pre === cur) {
      // 按下之前就有同一个选区 = 用户想点别处，不是要选字：清选区，放行
      try { document.getSelection().removeAllRanges(); } catch (err) { /* 忽略 */ }
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
  }, true);

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
   *  - 日期排序时，``nodate: true`` 的一组（没有实装日）按"**最早**"处理 ——
   *    升序（旧→新）排最前、降序（新→旧）排最后，组内按 ID；
   *    其余"有日期但值为空"的同样按最早处理；
   *  - 名字用日文排序（あいうえお）；
   *  - ID 用自然序（char_2 在 char_10 前面）；
   *  - 传了 haveOf，**未下载的一律沉底**（以前只有播放器这么做，
   *    CG 页会把未下载的混在中间，同一个下拉框在两页结果不同）。
   */
  ESC.sortItems = function (list, mode, pick, opts) {
    const arr = (list || []).slice();
    const p = typeof pick === 'function' ? pick : (it => it || {});
    const m = mode || 'date-desc';
    // 并列时的兜底顺序：按 ID 自然序（同一天 / 同一张数也不会随机）
    const byId = (a, b) => String(p(a).id || '').localeCompare(
      String(p(b).id || ''), 'en', { numeric: true });

    if (m === 'date-desc' || m === 'date-asc') {
      const dir = m === 'date-desc' ? -1 : 1;
      arr.sort(function (a, b) {
        const x = String(p(a).date || '');
        const y = String(p(b).date || '');
        /* 「没有实装日」这一组当**最早**处理（用户 2026-11-25 明确）：
           编号 < 100 的无实装日角色都是开局那批老角色，所以 旧→新 时排最前、
           新→旧 时排最后 —— 和原来"空日期 = 最早"的约定一致。
           组内再按包日期排（见 ESC.datePick），不跟有实装日的那组混。 */
        const na = !!p(a).nodate, nb = !!p(b).nodate;
        if (na !== nb) return na ? -dir : dir;
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

    // 「按张数」也用同一份实现：立绘页以前自己写了一个 cmp，
    // CG 页另写了一段 sort，两边口径不同（2026-11-15 反馈「搞那么多套」）。
    if (m === 'count') {
      const cnt = it => +p(it).count || 0;
      arr.sort((a, b) => cnt(b) - cnt(a) || byId(a, b));
      return arr;
    }

    if (m === 'name') {
      arr.sort(function (a, b) {
        return String(p(a).name || '').localeCompare(String(p(b).name || ''), 'ja');
      });
      return arr;
    }

    // id / 默认
    arr.sort(byId);
    return arr;
  };

  /** 侧栏「怎么排」的选项表 —— 立绘页与 CG 页共用这一份。
   *  role = 排角色（编号 / 名字 / 张数 / 实装日期）
   *  story = 排剧情（剧情顺序 / 剧情名 / 张数）
   *  以前两页各写一套（CG 页还在 HTML 里内置了一份），同样的"按名字"
   *  在两边措辞、顺序、可选项都不一致。 */
  ESC.SORT_OPTS = {
    role: [['id', '按编号'], ['name', '按名字（あいうえお）'], ['count', '按张数'],
      ['date-desc', '实装日期 新→旧'], ['date-asc', '实装日期 旧→新']],
    story: [['id', '按剧情顺序'], ['name', '按剧情名'], ['count', '按张数']],
  };

  /** 把统一选项填进 <select>，返回最终生效的值（旧值不在表里就退回 id） */
  ESC.fillSort = function (sel, mode, keep) {
    if (!sel) return keep || 'id';
    const list = ESC.SORT_OPTS[mode] || ESC.SORT_OPTS.role;
    sel.innerHTML = list.map(o =>
      '<option value="' + o[0] + '">' + o[1] + '</option>').join('');
    sel.value = list.some(o => o[0] === keep) ? keep : 'id';
    return sel.value;
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
    return ESC.charMeta(ESC.charId(unit)).d || '';
  };

  /* ------------------------------------------------------------ 包日期（p）
   * Wiki 有时候**没有**实装日（尤其是开局那批老角色）。按用户 2026-11-25 的规则，
   * 这种角色记一个「包日期」= 资源包**源文件**的日期（CDN Last-Modified，对所有
   * 人一致；绝不是本机下载时间），存进 CHARMETA 的 ``p``。
   *
   * 它是"参考日期"，**不是实装日**，所以：
   *   · 编号 < 100 的老角色：页面上留空，只拿它参与「按实装日期」排序；
   *   · 编号 >= 100：可以显示，但文案必须写「包日期」，不能写成实装日。
   */
  /* 元信息表的全局名是 **window.__CHARMETA**（charmeta.js 写的就是这个）；
     以前这里只认 window.CHARMETA —— 那个名字根本没人定义，于是所有角色都查成
     空对象：日期排序退化成 ID 排序（2026-11-25 用户看到 神騎プラチネル/id 1
     排在最前就是它）、「包日期」也永远显示不出来。两个名字都认，别只认一个。 */
  ESC.charMeta = function (cid) {
    const t = (typeof window !== 'undefined')
      ? (window.__CHARMETA || window.CHARMETA || {}) : {};
    return t[cid] || {};
  };
  /** 「按日期」排序用的 ``{date, nodate}``（配合 :func:`ESC.sortItems`）。
   *
   *  分成两组（用户 2026-11-25）：
   *    · **有实装日组**（nodate=false）= 有 Wiki 实装日 ``d``；编号 >= 100 的角色
   *      还可以用「包日期」``p`` 混进这一组一起排（新角色 Wiki 常晚几天才收录）；
   *    · **无实装日组**（nodate=true）= 其余：编号 < 100 的包日期**不许**冒充实装日，
   *      只能在本组内部按包日期排；连包日期都没有的排组末、内部按 ID。
   *
   *  排序时「无实装日组」按**最早**处理：旧→新排最前（这批都是开局老角色）、
   *  新→旧排最后；组内按包日期排，绝不与有实装日的一组混排。
   *  日期是 ``YYYY/MM/DD``，字符串序=时间序。
   */
  ESC.datePick = function (cid) {
    const m = ESC.charMeta(cid);
    const canMix = !!m.p && +cid >= 100;
    return { date: m.d || m.p || '', nodate: !(m.d || canMix) };
  };
  /** 该角色的「包日期」显示文案；没有、或编号 < 100 时返回 ''（页面留空） */
  ESC.packDateLabel = function (cid) {
    const m = ESC.charMeta(cid);
    if (m.d || !m.p) return '';
    return (+cid >= 100) ? ('包日期 ' + m.p) : '';
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
    // 漫画式活动（整集零台词、逐格 CG 翻页，官方资源里也没有文字标题）：
    // 没有名字时显示「漫画剧情 第N话」，比「活动剧情 第N话」更准确
    // （2026-11-23 用户要求）。
    manga: '漫画剧情',
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

  const _mainWarned = new Set();     // 未登记章节只警告一次（切列表会反复调 storyTitle）

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
      /* 超出核对过的章节表（MAIN_SECTIONS 只到 61）：部号只写在章节横幅图上，
         剧本 JSON 里没有，硬猜（"接着上一部往下数"）猜错比不写更糟。
         这里退回「主线 第N章」——术语对、不含部号，等横幅核对完再补表。 */
      const k2 = 'main_' + m[2];
      if (!_mainWarned.has(k2)) {
        _mainWarned.add(k2);
        try {
          console.warn('[story] 主线新章节未在 MAIN_SECTIONS 里登记：' + k2
            + '（部号需看章节横幅，见 common.js 的表）');
        } catch (e) { /* 忽略 */ }
      }
      return '主线 第' + (+m[2]) + '章';
    }
    /* 漫画式活动：advindex 里有 manga 标记（服务端解析出来的），老数据没有
       这个字段时按「零台词但有内容」推断 —— 这样不用等用户重装目录也能改名。 */
    const manga = !!(adv && (adv.manga
      || (adv.lines === 0 && adv.parts > 0)));
    const base = (manga ? ADV_LABEL.manga : ADV_LABEL[m[1]]) || m[1];
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

  /* --------------------------------------------- 随包数据表的自动同步（全站共用）
   * 问题：names.js / charmeta.js / index.js / cgmap.js / standmap.js 这些都是
   * <script> 在文档加载时**一次性读进内存**的（window.__NAMES / __CHARMETA /
   * __INDEX / __CGMAP …）。而「更新剧情目录」「装一部剧情」会在后台重写它们 ——
   * 页面不重新加载就永远用旧的一份。2026-11-18 用户反馈：剧情播放页刷新出来了，
   * CG / 立绘 / 原声集的列表还是老名字，还得自己一页一页 F5。
   *
   * 做法（和 mediaindex 的 /api/mediaver 同一思路）：
   *   · 服务端 /api/dataver 给出这组数据表「改没改」的指纹（只 stat，3 秒缓存）；
   *   · 页面注册自己关心的文件，页面重新可见 / 从后退缓存恢复 / 窗口获得焦点时
   *     比对一次（≥15 秒一次，隐藏的标签页不比）；
   *   · 变了就**原地重载**并通知页面重画 ——"原地"是关键，见 mergeInPlace。
   *
   * 用法（各页 init 里调一次）：
   *   ESC.watchData({
   *     hot: { 'names.js': '__NAMES', 'charmeta.js': '__CHARMETA' },
   *     cold: ['index.js'],            // 结构变了、内存里换不掉的 → 提示刷新
   *     onReload: () => renderList(),  // hot 变更后重画
   *     onStale: () => toast('有新内容，刷新页面即可看到'),
   *   });
   */
  const DATA_STAMP_URL = 'api/dataver';
  //: base = 数据表指纹基准，media = 素材指纹基准（/api/mediaver），at = 上次比对时间
  const _watch = { groups: [], base: null, media: null, at: 0 };

  /** 递归合并：把新值塞回**同一个对象**。
   *
   *  为什么必须这样：重新执行 data/*.js 会 `window.__NAMES = {...}` 整体换成一个
   *  **新对象**，而各页在文件顶部写的是 `const NAMES = window.__NAMES` —— 换完全局，
   *  页面手里那份引用还指着旧对象，重画出来照样是旧名字（"重载了却没变"就是这么来的）。
   *  这里把新内容灌回旧对象（数组也一样，原地 push），老引用自然就看到了。 */
  function mergeInPlace(oldV, newV, depth) {
    if (oldV === newV || newV == null) return oldV;
    if (depth > 6) return newV;
    if (Array.isArray(oldV) && Array.isArray(newV)) {
      oldV.length = 0;
      for (let i = 0; i < newV.length; i++) oldV.push(newV[i]);
      return oldV;
    }
    if (typeof oldV === 'object' && typeof newV === 'object') {
      Object.keys(oldV).forEach(k => { if (!(k in newV)) delete oldV[k]; });
      Object.keys(newV).forEach(k => {
        oldV[k] = (k in oldV) ? mergeInPlace(oldV[k], newV[k], depth + 1) : newV[k];
      });
      return oldV;
    }
    return newV;
  }

  /** 重新执行 data/ 下的数据表，并把新数据合并回原来的全局对象。
   *  @param {Object} map  ``{ 'names.js': '__NAMES', … }``（文件名 → 全局变量名） */
  ESC.reloadData = function (map) {
    return Promise.all(Object.keys(map || {}).map(function (f) {
      const g = map[f];
      const prev = g ? window[g] : null;
      return new Promise(function (resolve) {
        const s = document.createElement('script');
        s.src = 'data/' + f + '?_r=' + Date.now();
        s.onload = function () {
          s.remove();
          if (g && prev && window[g] && window[g] !== prev) {
            window[g] = mergeInPlace(prev, window[g], 0);
          }
          resolve();
        };
        s.onerror = function () { s.remove(); resolve(); };   // 少一个文件不该卡住整页
        document.head.appendChild(s);
      });
    }));
  };

  /** 重新拉一次素材清单（mediaindex.js）并通知各页重画。
   *
   *  为什么单独一个入口：装完公共素材 / 提高 CG 画质之后，**文件名没变、内容变了**
   *  或者**多了几张新图** —— 数据表（names/charmeta）一个字节都没动，只看
   *  /api/dataver 是发现不了的。素材自己有自己的指纹：/api/mediaver 和
   *  mediaindex.js 里的 ``__MEDIA_VER``。 */
  ESC.refreshMedia = function () {
    if (!window.MediaUI || !MediaUI.reload) return Promise.resolve(false);
    return MediaUI.reload().then(function (ok) {
      if (ok) {
        try { window.dispatchEvent(new CustomEvent('media:reloaded')); } catch (e) { }
      }
      return ok;
    });
  };

  /** 显式比对一次（外壳切回常驻原声集时也会调它）。
   *
   *  一次查两样东西：
   *    · 数据表指纹（names / charmeta / index / cgmap / standmap …）；
   *    · 素材指纹（/api/mediaver）——「公共素材补全 / 提高 CG 画质」改的是图片
   *      文件本身，数据表可能一个字都没变。2026-11-18 用户反馈：补全跑完，别的
   *      页面 F5 之后横幅和头像还是出不来，要 Ctrl+F5 才行。
   *
   *  ``fresh``：要求服务端现 stat、不吃它那 3 秒指纹缓存。任务刚结束那一下必须
   *  用它 —— 后端缓存里还是写文件之前的指纹，普通请求会让页面误判"没变化"。
   *  事件驱动的一次请求，不需要任何补查/定时重试。 */
  ESC.checkData = function (force, fresh) {
    if (!_watch.groups.length) return Promise.resolve(false);
    if (!force && document.hidden) return Promise.resolve(false);
    const now = Date.now();
    if (!force && now - _watch.at < 15000) return Promise.resolve(false);
    _watch.at = now;
    const getJson = url => fetch(url, { cache: 'no-store' })
      .then(r => (r.ok ? r.json() : null)).catch(() => null);
    return Promise.all([
      getJson(DATA_STAMP_URL + (fresh ? '?fresh=1' : '')),
      getJson('api/mediaver'),
    ])
      .then(function (res) {
        const v = res[0] && res[0].ver;
        const mv = res[1] && res[1].ver;
        const base = _watch.base;
        const mbase = _watch.media;
        _watch.base = v || base;
        _watch.media = mv || mbase;
        // 第一次只记基准（页面刚加载，本来拿到的就是最新的）
        const mediaChanged = !!(mbase && mv && mbase !== mv);
        if (!base || !v) {
          if (mediaChanged) return ESC.refreshMedia().then(() => false);
          return false;
        }
        const hot = {};
        let stale = false;
        _watch.groups.forEach(function (g) {
          Object.keys(g.hot || {}).forEach(function (f) {
            if (base[f] !== v[f]) hot[f] = g.hot[f];
          });
          (g.cold || []).forEach(function (f) { if (base[f] !== v[f]) stale = true; });
        });
        const fire = () => _watch.groups.forEach(function (g) {
          try { if (g.onReload) g.onReload(); } catch (e) { /* 单页重画失败别拖累别人 */ }
        });
        const fireStale = () => _watch.groups.forEach(function (g) {
          try { if (g.onStale) g.onStale(); } catch (e) { }
        });
        const changedData = Object.keys(hot).length > 0;
        if (!changedData && !stale && !mediaChanged) return false;
        // 数据表变了先合并数据表；素材变了再重下清单 —— 两步都做完再重画，
        // 否则重画时拿到的还是旧图片地址（旧 ?v=），画出来照样是占位。
        const p1 = changedData ? ESC.reloadData(hot) : Promise.resolve();
        return p1.then(function () {
          return mediaChanged ? ESC.refreshMedia() : false;
        }).then(function () {
          fire();
          if (stale) fireStale();
          return true;
        });
      })
      .catch(() => false);              // 纯静态托管 / 服务没起来：静默跳过
  };

  /** 告诉用户"有更新，刷新一下"，并给一个**能点的刷新按钮**。
   *
   *  为什么不用各页的 toast：这句话的重点是"让用户动手刷新"，纯文字还得让他自己按
   *  F5（2026-11-18 用户："更新完还得手动 F5，那你得提示用户刷新啊"）。这条自带
   *  样式、不依赖任何页面，同一时刻只留一条；用户点「刷新」= location.reload()。 */
  ESC.askReload = function (msg) {
    const old = document.getElementById('escReloadBar');
    if (old) {
      const t = old.querySelector('.t');
      if (t) t.textContent = msg || t.textContent;
      return;
    }
    const bar = document.createElement('div');
    bar.id = 'escReloadBar';
    bar.style.cssText = 'position:fixed;left:50%;bottom:26px;transform:translateX(-50%);'
      + 'z-index:10600;display:flex;align-items:center;gap:10px;max-width:92vw;'
      + 'padding:9px 12px;border-radius:10px;background:rgba(20,24,34,.97);'
      + 'border:1px solid #33445c;color:#e6e9f0;'
      + 'font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif;'
      + 'box-shadow:0 8px 26px rgba(0,0,0,.55)';
    bar.innerHTML = '<span class="t" style="min-width:0"></span>'
      + '<span class="h" style="flex:none;max-width:150px;color:#8b93a6;font-size:14px">'
      + '还看不到就按 Ctrl+F5</span>'
      + '<button type="button" class="go" style="flex:none;height:28px;padding:0 12px;'
      + 'border-radius:7px;border:1px solid #e0b566;background:#e0b566;color:#141822;'
      + 'font:inherit;font-weight:600;cursor:pointer">刷新</button>'
      + '<button type="button" class="x" title="先不刷新" style="flex:none;height:28px;'
      + 'width:28px;border-radius:7px;border:1px solid #3a4a60;background:#1c2230;'
      + 'color:#c7d0e0;font:inherit;cursor:pointer">✕</button>';
    bar.querySelector('.t').textContent = msg || '有新内容，刷新后即可看到';
    bar.querySelector('.go').onclick = function () { location.reload(); };
    bar.querySelector('.x').onclick = function () { bar.remove(); };
    document.body.appendChild(bar);
  };

  /** 注册本页依赖的数据表（各页 init 里调一次即可）。 */
  ESC.watchData = function (opt) {
    opt = opt || {};
    _watch.groups.push({
      hot: opt.hot || {}, cold: opt.cold || [],
      onReload: opt.onReload, onStale: opt.onStale,
    });
    if (_watch.groups.length > 1) return;
    const poke = function () { ESC.checkData(); };
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) poke();
    });
    window.addEventListener('pageshow', function (e) { if (e.persisted) poke(); });
    window.addEventListener('focus', poke);
    /* 外壳（shell.html）切页/切回常驻原声集时发这条：iframe 里的文档永远是"可见"
       状态，收不到 visibilitychange，只能由外壳点名提醒。 */
    window.addEventListener('message', function (ev) {
      if (ev.origin !== location.origin) return;
      if (!ev.data || ev.data.t !== 'esc:metacheck') return;
      _watch.at = 0;                     // 外壳点名 = 强制比对一次（跳过节流）
      /* 一定是「刚有事件发生」（切页 / 任务结束），要现 stat 的指纹，别吃 3 秒缓存。 */
      ESC.checkData(true, true);
    });
    poke();                              // 记一次基准
  };

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
