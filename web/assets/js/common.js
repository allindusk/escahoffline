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
   *   活动/其他 → 数据里自带的 title，例如「第47话」
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
})();
