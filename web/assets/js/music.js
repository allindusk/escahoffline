/* 原声集 / 角色音声播放器
 * ------------------------
 * 两个来源共用一个播放器：
 *   · 原声集  ← data/gallery.js（export_gallery.py）：165 首 BGM，扁平列表
 *   · 角色音声 ← data/voicemap.js（export_voice.py）：每个单位的战斗配音，
 *                按角色分组的横幅列表，点开才展开（跟剧情页侧栏同一个套路）
 *
 * 可播放项统一成 {key, src, title, sub, img, dur, kind}，
 * 播放队列 = 当前列表按显示顺序拉平 —— 这样上一首/下一首在两个来源里都说得通。
 */
(function () {
  'use strict';

  const $ = s => document.querySelector(s);
  // 注意第二个参数是**作用域**：以前这里直接 querySelectorAll(s) 把 root 丢了，
  // 于是 `$$('button', bar)` 拿到的是全页按钮 —— 在音声分类那行绑点击时，
  // 连「原声集 / 角色音声」标签页都被换上了分类的 onclick（2026-11-15 报障）。
  const $$ = (s, root) => Array.from((root || document).querySelectorAll(s));
  // 触摸设备没有"悬停"：tap 也会触发 mouseenter，浮窗会赖在屏幕上不走
  const TOUCH = !!(window.matchMedia && window.matchMedia('(hover: none)').matches);

  // 收藏爱心：一枚**固定尺寸的 SVG 心形**，空心 / 实心只是"填充色"不同。
  // 原来靠 ♥ / ♡ 两个字形，可这两个字的字宽本来就不一样（实心更窄），
  // 点一下就"变小变窄"（2026-11-17 反馈）。控制条那颗大按钮和歌单每一行
  // 右边的小心共用这一份，尺寸/描边由 CSS 按场景给（都只切色，不动盒）。
  const HEART_SVG = '<svg class="heart" viewBox="0 0 24 24" aria-hidden="true">'
    + '<path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3'
    + 'c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5'
    + 'c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>';

  const GAL = window.__GALLERY || { tracks: [] };
  const VMAP = window.__VOICEMAP || { units: [] };
  const NAMES = window.__NAMES || {};
  const MEDIA = window.__MEDIA || null;
  const UD = window.UD || null;
  const AU = $('#au');
  // 全站共用的图片口径（路径 + 横幅尺寸）；mediaui.js 没加载时退回拼路径
  const UI = window.MediaUI || {
    avatar: id => 'media/img/avatar/' + id + '.png',
    banner: stem => 'media/img/story/' + stem + '.webp',
  };

  const TRACKS = (GAL.tracks || []).map(t => ({
    no: +t.no || 0,
    d: +t.d || 0,
    j: t.j || '',
    s: t.s || '',
    t: t.t || '',
    g: t.g || '其他',
    // 详细浮窗要用的元信息：包侧事实 + 音频自带日期（丢了浮窗就只剩分类和时长）
    o: t.o || '', bn: t.bn || '', sz: +t.sz || 0, dt: t.dt || '',
    re: t.re === undefined ? null : +t.re,
  }));

  // 语音单位：{id, k, clips:[[name, path, dur, actCN]]}
  // 2026-11-16：只上线**角色**音声。NPC(1xxxx)/其他(2xxxx) 的语音编号与立绘
  // 是两套独立空间，项目里没有权威对照表（详见 music.js 旧注释），头像无法
  // 正确关联；用户要求连分类一起去掉，不再展示、也不让用户下载。
  const VUNITS = (VMAP.units || []).filter(u =>
    u.k === 'char' && (u.clips || []).length && NAMES[String(u.id)])
    .sort((a, b) => a.id - b.id);

  const GROUPS = ['全部', '战斗', '严肃', '日常', '角色曲', '系统', '主题歌', '其他'];
  // 2026-11-16：音声只剩「角色」一类，分类按钮栏（KINDS / renderKind）已删除。
  const MODES = [
    ['list', '🔁 列表循环', '🔁 循环'],
    ['one', '🔂 单曲循环', '🔂 单曲'],
    ['single', '⏹ 单曲播放', '⏹ 单次'],   // 播完就停，不自动接下一首（手动点 ⏭ 仍可切）
    ['shuffle', '🔀 随机播放', '🔀 随机'],
    ['seq', '➡ 顺序播放', '➡ 顺序'],
  ];
  const RATES = [1, 1.25, 1.5, 2, 0.75, 0.5];

  const ST = {
    src: 'ost',        // ost | voice
    group: '全部',
    chipsOpen: false,  // 分类按钮栏是否展开（默认收起，8 个按钮占两行太占地方）
    sort: 'no',
    q: '',
    curKey: '',
    mode: 'list',
    rate: 1,
    vol: 0.8,
    muted: false,
    pick: new Set(),   // 批量勾选，存可播放项的 key（原声集 / 角色音声通用）
    open: new Set(),   // 展开的音声分组
    list: [],          // 当前显示的行（原声集=曲目；音声=分组）
    queue: [],         // 当前可播放的扁平项
    lst: '',           // 当前播放列表 id
  };

  /* ---------------------------------------------------------------- 播放列表 */
  let LISTS = {};
  const LST_KEY = 'music_lists';

  function loadLists() {
    let d = null;
    try { d = UD ? UD.prefValue('ui', LST_KEY, null) : null; } catch (e) { d = null; }
    if (!d || typeof d !== 'object' || !Object.keys(d).length) {
      d = { d1: { n: '我的列表', t: [], ts: Date.now() } };
    }
    LISTS = d;
    ST.lst = prefGet('curlist', '') || Object.keys(LISTS)[0];
    if (!LISTS[ST.lst]) ST.lst = Object.keys(LISTS)[0];
  }
  function saveLists() {
    try { if (UD) UD.syncPref('ui', LST_KEY, LISTS); } catch (e) { /* 忽略 */ }
  }
  function curList() { return LISTS[ST.lst] || null; }
  function inList(key) {
    const l = curList();
    return !!(l && (l.t || []).indexOf(key) >= 0);
  }
  function toggleInList(key) {
    const l = curList();
    if (!l) return false;
    l.t = l.t || [];
    const i = l.t.indexOf(key);
    if (i >= 0) l.t.splice(i, 1);
    else l.t.push(key);
    saveLists();
    return i < 0;
  }

  /* ---------------------------------------------------------------- 小工具 */
  function fmt(sec) {
    sec = Math.max(0, Math.floor(+sec || 0));
    const m = Math.floor(sec / 60), s = sec % 60;
    return m + ':' + String(s).padStart(2, '0');
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function tname(t) {
    return t.t || ('曲目 ' + String(t.no).padStart(3, '0'));
  }
  /* 播放模式的按钮文案：宽屏写全名（"🔁 列表循环"），
     手机那格只有 86px 宽，全名会被省略成「单曲…」——窄屏改用两字简称，
     具体含义切模式时的 toast 里还会说一遍（2026-11-17 反馈按钮文字被截断）。 */
  function modeText(m) {
    const it = MODES.find(x => x[0] === m) || MODES[0];
    return (window.innerWidth <= 860 && it[2]) ? it[2] : it[1];
  }
  function modeFull(m) {
    return (MODES.find(x => x[0] === m) || MODES[0])[1];
  }
  let toastEl = null;
  function toast(msg, bad) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.id = 'mToast';
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.style.borderColor = bad ? '#a33' : '#2f5478';
    toastEl.style.color = bad ? '#ffb3ba' : '#dff0ff';
    toastEl.style.opacity = '1';
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(() => { toastEl.style.opacity = '0'; }, 3200);
  }
  function setVal(el) {
    const min = +el.min || 0, max = +el.max || 100;
    const p = max > min ? ((+el.value - min) / (max - min)) * 100 : 0;
    el.style.setProperty('--val', p.toFixed(2) + '%');
  }
  /* 界面偏好：UD 在时走它的 ui 命名空间（localStorage 主副本 + 服务端备份），
     纯静态打开 / 老包里没有 UD 时也要能存在本地，别让刷新一次就全丢。 */
  function prefGet(k, def) {
    try {
      if (UD) return UD.prefValue('ui', 'music_' + k, def) ?? def;
      const s = localStorage.getItem('music_' + k);
      return s == null ? def : JSON.parse(s);
    } catch (e) { return def; }
  }
  function prefSet(k, v) {
    try {
      if (UD) UD.syncPref('ui', 'music_' + k, v);
      else localStorage.setItem('music_' + k, JSON.stringify(v));
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------------------------------------------------------- 可播放项 */
  function trackItem(t) {
    return {
      key: 't' + t.no, kind: 'ost', src: t.s, dur: t.d,
      title: tname(t), sub: '第 ' + String(t.no).padStart(3, '0') + ' 首'
        + (t.g ? ' · ' + t.g : ''),
      img: t.j, no: t.no, group: t.g,
      // 详细浮窗要显示的元信息（export_gallery.py 写进来的，含音频自带日期）
      o: t.o || '', bn: t.bn || '', sz: +t.sz || 0, dt: t.dt || '',
      re: t.re === undefined ? null : +t.re,
    };
  }
  function voiceKey(u, c) { return 'v' + u.id + '_' + c[0]; }
  /** 「查看当前列表」打开时返回列表里的 key 集合；没开返回 null。 */
  function listOnlyKeys() {
    const el = $('#lstOnly');
    if (!el || !el.checked) return null;
    return new Set(((curList() || {}).t) || []);
  }
  function voiceItem(u, c) {
    return {
      key: voiceKey(u, c), kind: 'voice', src: c[1], dur: c[2],
      title: c[3] ? c[3] + '（' + c[0].replace(/^voice_\d+_/, '') + '）' : c[0],
      sub: nameOf(u.id) + (c[3] ? ' · ' + c[3] : ''),
      img: voiceBanner(u), no: u.id, group: c[3] || '',
      // 唱片中心用**头像**（列表行同款），横幅只留给悬停浮窗 / 迷你条。
      cover: voiceAvatar(u),
    };
  }
  function nameOf(id) {
    const n = NAMES[String(id)];
    return n ? String(n) : ('#' + id);
  }
  /* 2026-11-16：只保留角色音声，头像/横幅都走官方资源。
     NPC / 其他分类已删除（音声编号与立绘无可靠对照，见文件头）。 */
  function voiceAvatar(u) { return UI.avatar(u.id); }
  function voiceBanner(u) { return UI.banner('char_' + u.id); }
  function itemByKey(key) {
    for (const it of ST.queue) if (it.key === key) return it;
    return null;
  }

  /* 2026-11-16：SD 首帧冻结整套逻辑已注释（头像改成立绘静态图，无需抓帧）
  const SD_THUMBS = new Map();
  const SD_INFLIGHT = new Map();
  function sdFirstFrame(src) { ... }
  function drawFirstFrame(blob) { ... }
  const SD_QUEUE = [];
  function applySdThumbs(box) { ... }
  function pumpSd() { ... }
  */

  /* ---------------------------------------------------------------- 列表构建 */
  function build() {
    if (ST.src === 'voice') buildVoice();
    else buildOst();
    saveQueue();          // 让跨页迷你条也能上一首/下一首
  }

  function buildOst() {
    let list = TRACKS.map(trackItem);
    const keys = listOnlyKeys();
    if (keys) list = list.filter(i => keys.has(i.key));
    if (ST.group !== '全部') list = list.filter(i => i.group === ST.group);
    const q = ST.q.trim().toLowerCase();
    if (q) {
      list = list.filter(i => (i.title || '').toLowerCase().includes(q)
        || String(i.no).includes(q) || ('m' + i.no).includes(q));
    }
    const cmp = {
      no: (a, b) => (a.no - b.no),
      name: (a, b) => String(a.title).localeCompare(String(b.title), 'ja') || (a.no - b.no),
      dur: (a, b) => (b.dur - a.dur) || (a.no - b.no),
    }[ST.sort] || ((a, b) => a.no - b.no);
    list.sort(cmp);
    ST.list = list;
    ST.queue = list;
  }

  function buildVoice() {
    // 音声不按 BGM 的曲风分类筛，只用「单位类别 + 搜索 + 播放列表」过滤。
    // 官方角色只有 385 个；NPC / 敌人是另外的编号段，混在一起会显示
    // 「776 个角色」（2026-11-15 用户反馈）。默认只看角色。
    // VUNITS 已经只含「有官方名字的角色」，不需要再按类别过滤。
    let units = VUNITS.slice();
    const q = ST.q.trim().toLowerCase();
    if (q) {
      units = units.filter(u => nameOf(u.id).toLowerCase().includes(q)
        || String(u.id).includes(q));
    }
    // 「查看当前列表」：分组本身也要过滤。以前只过滤了播放队列，
    // 显示的 ST.list 仍是全部 776 个单位（2026-11-15 反馈）。用 voiceKey
    // 直接算 key，别为两万多条语音各造一个完整 item。
    const keys = listOnlyKeys();
    if (keys) {
      units = units.filter(u => (u.clips || []).concat(u.home || [])
        .some(c => keys.has(voiceKey(u, c))));
    }
    if (ST.sort === 'name') {
      units.sort((a, b) => nameOf(a.id).localeCompare(nameOf(b.id), 'ja'));
    }
    ST.list = units;
    const rows = [];
    for (const u of units) {
      for (const c of (u.clips || []).concat(u.home || [])) {
        const it = voiceItem(u, c);
        if (keys && !keys.has(it.key)) continue;
        rows.push(it);
      }
    }
    ST.queue = rows;
  }

  /* ---------------------------------------------------------------- 渲染 */
  /* 收起态：把分类按钮尽量铺满第一行，放不下的才收进「展开」。
   * 只留一个开关、后面一大片空着不好看（2026-11-17 反馈）——所以按容器实际
   * 宽度动态算能塞几个，桌面侧栏宽 / 手机抽屉窄都能刚好填满一行。
   * 当前分类不必强制留在这一行：开关按钮的文案就是当前分类名（如「战斗 ▾」），
   * 状态一眼可见。用 ResizeObserver 在缩放 / 抽屉开合 / 唱片模式切换后重算。 */
  function fitChips() {
    const box = $('#plChips');
    if (!box || ST.src === 'voice' || ST.chipsOpen) return;
    const btns = [...box.querySelectorAll('button')];
    if (!btns.length) return;
    btns.forEach(b => { b.hidden = false; });          // 先全部显示才能量到真实宽度
    const cs = getComputedStyle(box);
    const pad = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
    const gap = parseFloat(cs.columnGap || cs.gap) || 0;
    const avail = box.clientWidth - pad;
    if (avail <= 0) return;                            // 容器还没布局好，跳过
    let used = 0;
    btns.forEach((b, i) => {
      const w = b.getBoundingClientRect().width;
      // 第一个是开关，必须显示；后面的：加上它仍不超宽就留下，否则收起
      const fits = i === 0 || (used + gap + w) <= avail + 0.5;
      b.hidden = !fits;
      if (fits) used += w + (i === 0 ? 0 : gap);
    });
  }

  function renderChips() {
    if (ST.src === 'voice') {
      // 2026-11-16：音声只剩角色一类，没有可切换的分类，整行隐藏
      $('#plChips').innerHTML = '';
      $('#plChips').style.display = 'none';
      return;
    }
    $('#plChips').style.display = '';
    const have = {};
    for (const t of TRACKS) have[t.g] = (have[t.g] || 0) + 1;
    const groups = GROUPS.filter(g => g === '全部' || have[g]);
    const open = ST.chipsOpen;
    const label = ST.group === '全部' ? '全部分类' : ST.group;
    const toggle = '<button id="plChipToggle" class="chip-toggle' + (open ? ' on' : '') +
      '" title="' + (open ? '收起分类' : '展开分类') + '">' +
      label + (open ? ' ▴' : ' ▾') + '</button>';
    const chips = groups.map(g =>
      '<button data-g="' + g + '"' + (ST.group === g ? ' class="on"' : '') + '>' +
      g + (g === '全部' ? '' : ' ' + have[g]) + '</button>');
    $('#plChips').innerHTML = toggle + chips.join('');
    const tg = $('#plChipToggle');
    if (tg) tg.onclick = () => {
      ST.chipsOpen = !ST.chipsOpen;
      prefSet('chipsOpen', ST.chipsOpen);
      renderChips();                       // 只重画按钮栏，不动曲目列表
    };
    $$('#plChips button[data-g]').forEach(b => {
      b.onclick = () => {
        ST.group = b.dataset.g; prefSet('group', ST.group);
        ST.chipsOpen = false; prefSet('chipsOpen', false);  // 选完就收起
        renderChips(); renderList();
      };
    });
    // 收起态下一帧量宽度决定显示几个（innerHTML 刚写入，需等布局完成）
    if (!open) requestAnimationFrame(fitChips);
  }

  function renderList() {
    // 重画会把鼠标所在的那一行整块换掉，mouseleave 永远不会触发，
    // 浮窗就挂在屏幕上不走（移除列表里最后一首歌时最明显：连别的行都没有，
    // 鼠标怎么移动都碰不到新的 onmouseenter）。所以重画前先收起来。
    hideHover();
    build();
    const box = $('#plList');
    if (ST.src === 'voice') renderVoiceList(box);
    else renderOstList(box);
    paintCount();
    paintBulk();
    paintStageInfo();
  }

  function renderOstList(box) {
    if (!ST.list.length) {
      box.innerHTML = '<div class="vempty">' +
        (TRACKS.length
          ? '这个筛选下没有曲目'
          : '还没有解包原声集：到「资源管理 → 基础数据」点「安装」') +
        '</div>';
      return;
    }
    box.innerHTML = ST.list.map((it, i) => {
      const on = ST.curKey === it.key ? ' on' : '';
      const dis = it.src ? '' : ' dis';
      const pick = ST.pick.has(it.key) ? ' checked' : '';
      const fav = inList(it.key);
      return '<div class="row' + on + dis + '" data-i="' + i + '">' +
        '<input class="ck" type="checkbox" data-key="' + esc(it.key) + '"' + pick +
        ' title="勾选后可批量导出 / 加入播放列表">' +
        '<span class="no">' + String(it.no).padStart(3, '0') + '</span>' +
        '<span class="nm">' + esc(it.title) +
        (it.group && it.group !== '其他' ? '<i>' + esc(it.group) + '</i>' : '') + '</span>' +
        '<span class="du">' + fmt(it.dur) + '</span>' +
        '<span class="hf' + (fav ? ' on' : '') + '" data-fav="1" title="' +
        (fav ? '从当前播放列表移除' : '加进当前播放列表') + '">' + HEART_SVG + '</span>' +
        '</div>';
    }).join('');
    bindRows(box);
  }

  function renderVoiceList(box) {
    if (!ST.list.length) {
      box.innerHTML = '<div class="vempty">还没有导出角色音声。<br>' +
        '它们跟着角色剧情一起装（资源管理 → 剧情安装），' +
        '装完刷新本页就能听。</div>';
      return;
    }
    const html = [];
    const keys = listOnlyKeys();
    for (const u of ST.list) {
      const open = ST.open.has(u.id);
      // 展开后只画「当前播放列表里」的语音，和 buildVoice 同一口径
      const visClips = (u.clips || []).filter(c => !keys || keys.has(voiceKey(u, c)));
      const visHome = (u.home || []).filter(c => !keys || keys.has(voiceKey(u, c)));
      const acts = {};
      for (const c of visClips) acts[c[3] || '其他'] = 1;
      const thumb = voiceAvatar(u);
      let ava;
      if (!thumb) {
        ava = '<span class="ava ava-ph">角色</span>';
      } else {
        // data-lsrc：语音分组有几百条，等滚到视野再取头像
        ava = '<img class="ava" alt="" data-lsrc="' + esc(thumb) + '">';
      }
      html.push('<div class="vgrp' + (open ? ' open' : '') +
        (voiceGroupPlaying(u) ? ' on' : '') + '" data-uid="' + u.id + '">' + ava +
        '<span class="vi"><span class="vn">' + esc(nameOf(u.id)) + '</span>' +
        '<span class="vs">' + Object.keys(acts).slice(0, 4).join(' / ') + '</span></span>' +
        '<span class="vc">' + visClips.length +
        (visHome.length ? ' + ' + visHome.length : '') + ' 段</span>' +
        '<span class="caret">▸</span></div>');
      if (open) {
        const push = c => {
          const it = voiceItem(u, c);
          const on = ST.curKey === it.key ? ' on' : '';
          const fav = inList(it.key);
          // 有 ASR 转写就显示台词，比流水号有用得多（label_voice.py 写进 c[4]）
          const raw = (c[4] ? c[4].slice(0, 40) : '')
            || c[0].replace(/^(voice|sys)_\d*_?/, '').replace(/^80000_\d{4}/, '');
          const pick = ST.pick.has(it.key) ? ' checked' : '';
          html.push('<div class="row vrow' + on + '" data-vkey="' + esc(it.key) + '">' +
            '<input class="ck" type="checkbox" data-key="' + esc(it.key) + '"' + pick +
            ' title="勾选后可批量加入播放列表">' +
            '<span class="no"></span>' +
            '<span class="nm">' + esc(c[3] || raw) +
            '<i>' + esc(raw) + '</i></span>' +
            '<span class="du">' + fmt(c[2]) + '</span>' +
            '<span class="hf' + (fav ? ' on' : '') + '" data-fav="1" title="' +
            (fav ? '从当前播放列表移除' : '加进当前播放列表') + '">' + HEART_SVG +
            '</span></div>');
        };
        for (const c of visClips) push(c);
        if (visHome.length) {
          // 主页那批（取得 / 覚醒 / ホーム画面クリック / 放置 / タイトルコール）
          // 在另一个包里，显示名只存在于服务端表 —— 先按序号列出来：能听、能收藏、
          // 能进播放列表，等映射表拿到再改成游戏里的名字。
          html.push('<div class="vsep">主页 / 其他语音 · ' +
            visHome.length + ' 段（包里无名，按序显示）</div>');
          for (const c of visHome) push(c);
        }
      }
    }
    box.innerHTML = html.join('');
    $$('.vgrp', box).forEach(g => {
      const id = +g.dataset.uid;
      g.onclick = () => {
        if (ST.open.has(id)) ST.open.delete(id); else ST.open.add(id);
        prefSet('open', Array.from(ST.open));
        renderList();
      };
      // 触屏没有 mouseleave：tap 会先合成一个 mouseenter，卡片就永久赖在列表上
      // （2026-11-17 排查：曲目行早就挡掉了，分组横幅这里漏了）
      if (!TOUCH) {
        g.onmouseenter = ev => showHover(ev, voiceHover(+g.dataset.uid));
        g.onmouseleave = hideHover;
      }
    });
    bindRows(box, true);
    // applySdThumbs(box);  // 2026-11-16：头像改立绘静态图，不再抓 SD 首帧
  }

  function voiceGroupPlaying(u) {
    const it = itemByKey(ST.curKey);
    return !!(it && it.kind === 'voice' && it.no === u.id);
  }

  function bindRows(box, isVoice) {
    $$('.row', box).forEach(r => {
      const key = isVoice ? r.dataset.vkey : (ST.list[+r.dataset.i] || {}).key;
      const it = key ? itemByKey(key) : null;
      if (!it) return;
      r.onclick = ev => {
        if (ev.target.classList.contains('ck') || ev.target.dataset.fav !== undefined) return;
        if (!it.src) {
          toast(it.kind === 'ost'
            ? '这首还没安装：到「资源管理 → 基础数据」补全原声集'
            : '这段语音还没装：重装这个角色会补上', true);
          return;
        }
        playItem(it);
        // 手机：歌单是盖住整页的抽屉，选完这首就该让出画面（桌面没有 plOpen）
        if (document.body.classList.contains('plOpen')) closePl();
      };
      // 悬停浮窗只在有鼠标的设备上绑：手机上 tap 会先发 mouseenter，
      // 浮窗会盖在列表行上不放（2026-11-17 反馈）
      if (!TOUCH) {
        r.onmouseenter = ev => showHover(ev, itemHover(it));
        r.onmouseleave = hideHover;
      }
      const fav = r.querySelector('[data-fav]');
      if (fav) {
        fav.onclick = ev => {
          ev.stopPropagation();
          const on = toggleInList(it.key);
          fav.classList.toggle('on', on);
          paintFav();
          if ($('#lstOnly').checked) renderList();
          toast(on ? '已加进「' + (curList() || {}).n + '」'
            : '已从「' + (curList() || {}).n + '」移除');
        };
      }
      const ck = r.querySelector('.ck');
      if (ck) {
        ck.onclick = ev => {
          ev.stopPropagation();
          const key = ck.dataset.key;
          if (ck.checked) ST.pick.add(key); else ST.pick.delete(key);
          paintBulk();
        };
      }
    });
  }

  function paintCount() {
    if (ST.src === 'voice') {
      const n = ST.queue.length;
      const shown = ST.list.length;
      // 385 是有名字的官方角色；其中只有一部分装了音声包，两个数字都摆出来。
      // 2026-11-16：NPC / 其他已删，这里只统计角色。
      const allChars = Object.keys(NAMES).length;
      $('#plCount').innerHTML =
        '音声 · <b style="color:#e0b566">' + shown + '</b> / ' + allChars +
        ' 个角色 · <b style="color:#e0b566">' + n + '</b> 段语音';
    } else {
      const okN = ST.list.filter(i => i.src).length;
      $('#plCount').innerHTML = '共 <b style="color:#e0b566">' + ST.list.length +
        '</b> 首 · 可播放 ' + okN + ' 首';
    }
  }

  function paintStageInfo() {
    const it = itemByKey(ST.curKey);
    if (it) {
      $('#stageInfo').innerHTML = '正在播放 <b>' + esc(it.title) + '</b>';
      return;
    }
    $('#stageInfo').innerHTML = ST.src === 'voice'
      ? '音声库共 <b>' + VUNITS.length + '</b> 个单位'
      : '原声集共 <b>' + TRACKS.length + '</b> 首';
  }

  /* ---------------------------------------------------------------- 悬停预览 */
  /* 悬停预览：跟手显示（不做延迟）。
     以前加了 120ms 防抖，但那会让"鼠标一划过就点什么都没有"；
     行与行紧挨着，enter 立刻重建卡片反而更跟手。 */
  function showHover(ev, info) {
    if (!info) return;
    const el = $('#hov');
    const img = $('#hovImg');
    if (!info.img) {
      img.style.display = 'none';
    } else {
      img.style.display = '';
      img.src = info.img;
      // 2026-11-16：横幅改立绘静态图，不再抓 SD 首帧
    }
    $('#hovTxt').innerHTML = info.html;
    el.classList.add('on');
    // 贴着鼠标，别顶出屏幕（宽度量实际卡片，别再写死 250）
    const w = el.offsetWidth || 298, h = el.offsetHeight || 200;
    let x = (ev.clientX || 0) + 16, y = (ev.clientY || 0) + 12;
    if (x + w > window.innerWidth - 8) x = (ev.clientX || 0) - w - 16;
    if (y + h > window.innerHeight - 8) y = window.innerHeight - h - 8;
    el.style.left = Math.max(8, x) + 'px';
    el.style.top = Math.max(8, y) + 'px';
  }
  function hideHover() {
    $('#hov').classList.remove('on');
  }
  // 兜底：触摸任何位置都收起详情浮窗（触屏笔记本这类混合设备上仍可能残留）
  document.addEventListener('touchstart', hideHover, { passive: true });
  function kv(k, v) {
    return '<div class="kv"><span>' + k + '</span><b>' + esc(v) + '</b></div>';
  }

  /* 详细浮窗：把能查到的事实都摆出来
     （包内占位名、官方包名、包大小、音频自带日期、音频是复用还是独占） */
  function itemHover(it) {
    let html = '<b>' + esc(it.title) + '</b>' +
      '<span class="k">' + esc(it.sub) + '</span>';
    if (it.kind === 'ost') {
      let rows = '';
      if (it.group) rows += kv('分类', it.group);
      if (it.dur) rows += kv('时长', fmt(it.dur));
      if (it.o) rows += kv('包内占位名', it.o);
      if (it.bn) {
        rows += kv('资源包', it.bn.replace(/^bundle_gallerymusic_music_/, 'music_') +
          (it.sz ? ' · ' + (it.sz / 1048576).toFixed(1) + ' MB' : ''));
      }
      if (it.dt) rows += kv('音频自带日期', it.dt);
      if (it.re !== null) {
        rows += kv('音频', it.re ? '复用已下载的 BGM' : '画廊独占曲（单独下过）');
      }
      if (rows) html += '<div class="kvbox">' + rows + '</div>';
    }
    html += '<div class="k" style="margin-top:5px">' +
      (it.src ? '点一下播放' : '<span style="color:#ffb3ba">还没安装</span>') + '</div>';
    return { img: it.img, html: html };
  }
  function voiceHover(id) {
    const u = VUNITS.find(x => x.id === id);
    if (!u) return null;
    const acts = {};
    for (const c of u.clips || []) acts[c[3] || '其他'] = (acts[c[3] || '其他'] || 0) + 1;
    const bits = Object.keys(acts).map(k => k + '×' + acts[k]).join(' · ');
    const home = (u.home || []).length;
    return {
      // 浮窗里显示**剧情页同款 81px 横幅**（列表行才是头像）
      img: voiceBanner(u),
      html: '<b>' + esc(nameOf(u.id)) + '</b>' +
        '<span class="k">' + u.clips.length + ' 段战斗配音' +
        (home ? ' · ' + home + ' 段主页语音' : '') + '</span><br>' +
        '<span class="k">' + esc(bits) + '</span><br>' +
        '<span class="k">点一下展开</span>',
    };
  }

  /* ------------------------------------------------------------ 跨页面续播
     本站是多页应用，换页时 <audio> 会销毁。把「放哪首 + 播到第几秒」写进
     localStorage，别的页面由 assets/js/miniplayer.js 读出来接着放。
     这份状态也是"回到原声集页继续听"的依据。 */
  const NOW_KEY = 'esc_ost_now';
  const QUEUE_KEY = 'esc_ost_queue';

  /* ==================== 全站同一时刻只允许一路音频在响 ====================
     报障（2026-11-17）：手机上放一首歌 → 切到别的页面 → 再回原声集，偶尔会有
     **同一首歌两路音频叠在一起**。

     根因不是一个"显眼的 bug"，而是同一个 origin 下可能同时存在两份 music.html：
     外壳常驻的 #fMusic 是一份；内容 iframe 偶尔也会把 music.html 装进去
     （绕过外壳 hook 的导航），或者旧文档还在 bfcache 里没被真正释放。
     每份都带自己的 <audio>，浏览器不会替我们互斥 —— 谁在放谁就响。

     所以这里不赌"只会有一份"，直接加一张**播放所有权票**：
       · 谁开始播放，就往 localStorage 写一张票（带实例 id + 时间戳）；
       · 别的 music 实例收到 storage 事件，票比自己新就停下自己的音频。
     结果：不管开几份、切多少次页，同一时刻只有一路在响；切歌只是换自己那支
     audio 的 src（playItem 里已经按 key 去重），不会多出声。 */
  const OWN_KEY = 'esc_ost_owner';
  const MY_ID = Math.random().toString(36).slice(2) + '-' + Date.now().toString(36);
  let ownAt = 0;          // 我最后一次拿票的时间
  let resumeOnShow = false;   // 进 bfcache 前是"在播"，回来时要接着播

  function claimAudio() {
    ownAt = Date.now();
    try {
      localStorage.setItem(OWN_KEY, JSON.stringify({ id: MY_ID, at: ownAt }));
    } catch (e) { /* 隐私模式写不了，那只能靠各实例自觉 */ }
  }

  window.addEventListener('storage', e => {
    if (e.key !== OWN_KEY || !e.newValue) return;
    let v = null;
    try { v = JSON.parse(e.newValue); } catch (err) { return; }
    if (!v || v.id === MY_ID) return;
    // 别人的票更新（严格大于，平手不互相掐）→ 让路，只暂停音频，
    // 界面/进度/播放条由下面 AU 的 pause 事件统一收尾
    if ((+v.at || 0) > ownAt && !AU.paused) AU.pause();
  });

  /** 把当前播放队列落盘：跨页迷你条的上一首/下一首靠它。
   *  只在 build() 里写（队列变化时），不塞进每秒的 saveNow，省得反复序列化。 */
  function saveQueue() {
    try {
      localStorage.setItem(QUEUE_KEY, JSON.stringify({
        items: (ST.queue || []).filter(i => i && i.src).map(i => ({
          k: i.key, s: i.src, t: i.title, sub: i.sub || '',
          img: i.img || '', no: i.no || 0, d: i.dur || 0,
          kind: i.kind || '',
        })),
        mode: ST.mode, at: Date.now(),
      }));
    } catch (e) { /* 隐私模式下写不了，忽略 */ }
  }

  function saveNow(playing) {
    const it = currentItem();
    if (!it || !it.src) return;
    try {
      localStorage.setItem(NOW_KEY, JSON.stringify({
        k: it.key, s: it.src, t: it.title, sub: it.sub || '',
        img: it.img || '', no: it.no || 0,
        dur: AU.duration || it.dur || 0,
        pos: AU.currentTime || 0,
        playing: playing == null ? !AU.paused : !!playing,
        vol: ST.vol, muted: ST.muted, rate: ST.rate, mode: ST.mode,
        at: Date.now(),
      }));
    } catch (e) { /* 隐私模式下写不了，忽略 */ }
  }

  function readNow() {
    try {
      const d = JSON.parse(localStorage.getItem(NOW_KEY) || 'null');
      if (!d || !d.s || !d.k) return null;
      if (Date.now() - (+d.at || 0) > 6 * 3600 * 1000) return null;   // 太旧不续
      return d;
    } catch (e) { return null; }
  }

  function itemBySavedKey(key) {
    if (!key) return null;
    if (key.charAt(0) === 't') {
      const t = TRACKS.find(x => 't' + x.no === key);
      return t ? trackItem(t) : null;
    }
    for (const u of VUNITS) {
      for (const c of (u.clips || []).concat(u.home || [])) {
        if ('v' + u.id + '_' + c[0] === key) return voiceItem(u, c);
      }
    }
    return null;
  }

  /** 回到本页时接着上次的进度（自动播会被浏览器拦，失败就让用户点一下）。
   *  音量 / 倍速 / 循环模式也一起同步 —— 用户在别的页用迷你条改过之后，
   *  回来不该又跳回旧值。 */
  function restoreSaved(d) {
    const it = itemBySavedKey(d.k);
    if (!it || !it.src) return false;
    // 迷你条那边可能改过这些
    if (d.vol != null) ST.vol = Math.min(1, Math.max(0, +d.vol));
    if (d.muted != null) ST.muted = !!d.muted;
    if (d.rate) ST.rate = +d.rate;
    if (d.mode && MODES.some(m => m[0] === d.mode)) ST.mode = d.mode;
    AU.volume = ST.muted ? 0 : ST.vol;
    AU.playbackRate = ST.rate;
    paintVolume();
    $('#mode').textContent = modeText(ST.mode);
    $('#mode').classList.toggle('on', ST.mode !== 'list');
    $('#rate').textContent = (String(ST.rate).indexOf('.') < 0 ? ST.rate.toFixed(1) : ST.rate) + '×';

    ST.curKey = it.key;
    AU.dataset.key = it.key;
    AU.src = it.src;
    AU.load();
    const pos = +d.pos || 0;
    if (pos > 0.5) {
      AU.addEventListener('loadedmetadata', () => {
        if (isFinite(AU.duration) && pos < AU.duration - 1) AU.currentTime = pos;
      }, { once: true });
    }
    paintTrack();
    renderList();
    if (d.playing) AU.play().catch(() => {});
    return true;
  }

  /* ---------------------------------------------------------------- 播放 */
  function currentItem() { return itemByKey(ST.curKey); }

  function playItem(it) {
    if (!it || !it.src) return;
    if (AU.dataset.key !== it.key) {
      AU.dataset.key = it.key;
      AU.src = it.src;
      AU.load();
    }
    AU.playbackRate = ST.rate;
    const p = AU.play();
    if (p && p.catch) p.catch(() => { toast('浏览器拦下了自动播放：请再点一次播放键', true); });
    ST.curKey = it.key;
    // 用户主动放了歌 → 别的页面该重新出现迷你条
    try { sessionStorage.removeItem('esc_ost_mini_closed'); } catch (e) { /* 忽略 */ }
    saveNow(true);
    paintTrack();
    renderList();
  }

  function paintTrack() {
    const it = currentItem();
    const jk = $('#jacket');
    if (!it) {
      $('#tTitle').textContent = '还没有选曲';
      // 手机上没有"左侧歌单"，写"左侧"会让人找不存在的区域
      $('#tSub').textContent = window.innerWidth <= 860
        ? '点「☰ 列表」挑一首开始' : '在左侧点一首开始播放';
      jk.classList.add('none');
      jk.classList.remove('native');
      jk.removeAttribute('src');
      paintFav();
      return;
    }
    $('#tTitle').textContent = it.title;
    const bits = [it.sub];
    if (it.dur) bits.push(fmt(it.dur));
    $('#tSub').textContent = bits.filter(Boolean).join(' · ');
    // 角色音声：唱片贴头像，且按原始像素显示（native 类见 music.css）。
    // 原声集仍用专辑封面，保留原来的等比填充。
    const art = it.cover || it.img;
    jk.classList.toggle('native', !!it.cover);
    if (art) {
      jk.classList.remove('none');
      // 2026-11-16：头像改立绘静态图，不再抓 SD 首帧
      jk.src = art;
    } else {
      jk.classList.add('none');
      jk.removeAttribute('src');
    }
    paintFav();
    paintStageInfo();
    try {
      if ('mediaSession' in navigator) {
        navigator.mediaSession.metadata = new window.MediaMetadata({
          title: it.title,
          artist: '超昂大战 原声集',
          album: it.group || '',
          artwork: it.img ? [{ src: it.img, sizes: '512x512' }] : [],
        });
      }
    } catch (e) { /* 老浏览器没有 Media Session */ }
  }

  // 收藏按钮那颗心：用上面那份固定尺寸的 SVG，只有填充色随状态变
  // （HEART_SVG 已在文件头定义，行内小心跟这里用的是同一枚）。
  function paintFav() {
    const it = currentItem();
    const b = $('#fav');
    const on = it ? inList(it.key) : false;
    b.classList.toggle('on', on);
    if (!b.querySelector('.heart')) b.innerHTML = HEART_SVG;
    b.title = on ? '从当前播放列表移除' : '加进当前播放列表';
  }

  function setPlayingUI(on) {
    // 2026-11-17：不再用 ▶ / ⏸ 字形当图标 —— 字形自带左右留白不对称，
    // 22px 的字塞进 54px 的圆里怎么调都"看着不居中"（用户两次反馈）。
    // 改成空壳 + CSS 画形状：三角形的包围盒就落在圆心，不依赖字体。
    const b = $('#play');
    if (!b.querySelector('.ic')) b.innerHTML = '<span class="ic"></span>';
    b.classList.toggle('is-playing', !!on);
    $('#vinylBox').classList.toggle('playing', on);
    $('.vinyl').classList.toggle('playing', on);
  }

  function step(d) {
    const q = ST.queue;
    if (!q.length) return null;
    const i = q.findIndex(x => x.key === ST.curKey);
    if (ST.mode === 'shuffle') {
      if (q.length === 1) return q[0];
      let k = i;
      while (k === i) k = Math.floor(Math.random() * q.length);
      return q[k];
    }
    if (i < 0) return q[0];
    const n = i + d;
    if (n >= 0 && n < q.length) return q[n];
    if (ST.mode === 'seq') return null;
    return q[(n + q.length) % q.length];
  }

  function next() {
    const it = step(1);
    if (!it) { setPlayingUI(false); return; }
    playItem(it);
  }

  function prev() {
    if (AU.currentTime > 3) { AU.currentTime = 0; return; }
    const it = step(-1);
    if (it) playItem(it);
  }

  /* ---------------------------------------------------------------- 进度 / 音量 */
  let dragging = false;

  function paintProgress() {
    const it = currentItem();
    const d = AU.duration || (it ? it.dur : 0) || 0;
    const t = AU.currentTime || 0;
    $('#curTime').textContent = fmt(t);
    $('#totTime').textContent = fmt(d);
    if (!dragging) {
      const seek = $('#seek');
      seek.value = d ? Math.round((t / d) * 1000) : 0;
      setVal(seek);
    }
  }

  function paintVolume() {
    const v = $('#vol');
    v.value = Math.round((ST.muted ? 0 : ST.vol) * 100);
    setVal(v);
    $('#mute').textContent = ST.muted || ST.vol === 0 ? '🔇' : (ST.vol < 0.5 ? '🔉' : '🔊');
  }

  /* ---------------------------------------------------------------- 批量操作
     行首勾选以前只服务「导出原声集」，而且用曲目编号当 key —— 角色音声的
     单位编号会跟曲目号撞车。现在统一存可播放项的 key（t123 / v123_xxx），
     一处勾选同时管「批量导出」和「批量加 / 移出播放列表」。
     切换来源时另一来源的勾选会留着，但计数与批量操作只算当前这一份。 */
  function currentPicks() {
    const valid = new Set(ST.queue.filter(i => i.src).map(i => i.key));
    return Array.from(ST.pick).filter(k => valid.has(k));
  }

  function paintBulk() {
    const n = currentPicks().length;
    const total = ST.queue.filter(i => i.src).length;
    const c = $('#bulkCount');
    if (c) c.textContent = '已选 ' + n + ' 项';
    const all = $('#bulkAll');
    if (all) {
      all.textContent = (total > 0 && n >= total) ? '清空' : '全选';
      all.disabled = !total;
    }
    ['#bulkAdd', '#bulkDel'].forEach(s => {
      const b = $(s);
      if (b) b.disabled = !n;
    });
  }

  function toggleAll() {
    const keys = ST.queue.filter(i => i.src).map(i => i.key);
    if (!keys.length) return;
    const allOn = keys.every(k => ST.pick.has(k));
    keys.forEach(k => allOn ? ST.pick.delete(k) : ST.pick.add(k));
    // 只同步画出来的勾选框，不整表重绘：音声有两万多段，重绘会很慢。
    // 之后展开的分组由 renderVoiceList 直接从 ST.pick 读，状态不会丢。
    $$('.ck').forEach(ck => { ck.checked = ST.pick.has(ck.dataset.key); });
    paintBulk();
    toast(allOn ? '已清空勾选' : '已选中当前列表的 ' + keys.length + ' 项');
  }

  async function bulkList(want) {
    const keys = currentPicks();
    if (!keys.length) { toast('先在行首勾选要操作的曲目', true); return; }
    if (keys.length > 500
      && !(await Dlg.confirm('一次' + (want ? '加入' : '移出') + ' ' + keys.length
        + ' 项，可能要等几秒。\n\n继续？',
      { anchor: want ? $('#bulkAdd') : $('#bulkDel') }))) return;
    const l = curList();
    if (!l) return;
    l.t = l.t || [];
    let changed = 0;
    for (const k of keys) {
      const i = l.t.indexOf(k);
      if (want && i < 0) { l.t.push(k); changed++; }
      else if (!want && i >= 0) { l.t.splice(i, 1); changed++; }
    }
    saveLists();
    renderLists();
    renderList();
    paintFav();
    const name = '「' + (l.n || '列表') + '」';
    toast(changed
      ? (want ? '已把 ' + changed + ' 项加进' + name
        : '已从' + name + '移出 ' + changed + ' 项')
      : name + '里这些项本来就不用改');
  }

  /** 导出接口只认曲目编号，把勾选里的原声集 key 还原成编号。 */
  function ostPickedNos() {
    const out = [];
    for (const k of ST.pick) {
      if (k[0] !== 't') continue;
      const no = +k.slice(1);
      const t = TRACKS.find(x => x.no === no);
      if (t && t.s) out.push(no);
    }
    return out.sort((a, b) => a - b);
  }

  /* ---------------------------------------------------------------- 播放列表 UI */
  function renderLists() {
    const sel = $('#lstSel');
    const ids = Object.keys(LISTS);
    sel.innerHTML = ids.map(id =>
      '<option value="' + id + '"' + (id === ST.lst ? ' selected' : '') + '>' +
      esc(LISTS[id].n || '未命名') + '（' + ((LISTS[id].t || []).length) + '）' +
      '</option>').join('');
  }

  /* ---------------------------------------------------------------- 导出 */
  async function doExport() {
    if (!window.PACKSTATE || !PACKSTATE.online) {
      await Dlg.alert('导出需要本地服务：请用「启动.bat」打开本页面。');
      return;
    }
    if (ST.src === 'voice') {
      await Dlg.alert('角色音声是一段段短语音（已解成 m4a），暂不支持打包导出。\n' +
        '要拿源文件可以到 media/audio/voice/<角色编号>/ 目录里复制。');
      return;
    }
    let picks = ostPickedNos();      // 勾选统一存 key，导出接口要的是曲目编号
    if (!picks.length) {
      const ok = ST.list.filter(i => i.src).map(i => i.no);
      if (!ok.length) { await Dlg.alert('当前筛选里没有可导出的曲目。'); return; }
      if (!(await Dlg.confirm('没有勾选任何曲目，是否导出当前列表的全部 ' + ok.length + ' 首？\n\n' +
        '（先在行首勾选可以只导选中的那几首）\n\n继续？',
        { anchor: $('#btnExport') }))) return;
      picks = ok;
    }
    const btn = $('#btnExport');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '打包中…';
    try {
      const r = await fetch('api/export/audio', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tracks: picks }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
      const jid = (d.job || {}).id;
      const t = setInterval(async () => {
        try {
          const rr = await fetch('api/jobs', { cache: 'no-store' });
          const dd = await rr.json();
          const job = (dd.jobs || []).find(j => j.id === jid);
          if (!job) return;
          if (job.state === 'done') {
            clearInterval(t);
            toast('导出完成：见 userdata/export/');
            btn.disabled = false; btn.textContent = old;
          } else if (job.state === 'failed') {
            clearInterval(t);
            toast('导出失败：' + (job.error || '未知'), true);
            btn.disabled = false; btn.textContent = old;
          } else {
            btn.textContent = '打包 ' + (job.percent || 0).toFixed(0) + '%';
          }
        } catch (e) { /* 忽略 */ }
      }, 800);
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  /* ------------------------------------------------- 歌单抽屉 / 唱片模式
   * 桌面：歌单常驻，plOpen / focus 这两个类不会造成任何视觉变化之外的东西。
   * 手机：歌单是盖住整页的抽屉，所以必须"能关"——遮罩、✕、Esc、选完曲都收起；
   * 唱片模式会把歌单藏起来，因此点「列表」要先退出唱片模式再开抽屉，
   * 否则按钮看起来像坏的（2026-11-17 反馈）。
   */
  function closePl() { document.body.classList.remove('plOpen'); }
  function togglePl() { document.body.classList.toggle('plOpen'); }

  function setFocus(on, remember) {
    const was = document.body.classList.contains('focus');
    document.body.classList.toggle('focus', !!on);
    const b = $('#btnFocus');
    if (b) {
      b.classList.toggle('on', !!on);
      b.textContent = on ? '退出唱片模式' : '唱片模式';
    }
    if (on) closePl();          // 抽屉和唱片模式互斥，别让抽屉浮在唱片上
    // 只有用户真的点了才落盘；深链 ?focus 属于"这次就看看"，不写偏好
    if (remember && was !== !!on) prefSet('focus', !!on);
  }

  /* ---------------------------------------------------------------- 初始化 */
  function init() {
    ST.mode = prefGet('mode', 'list');
    ST.rate = +prefGet('rate', 1) || 1;
    ST.vol = Math.min(1, Math.max(0, +prefGet('vol', 0.8)));
    ST.muted = !!prefGet('muted', false);
    /* 分类 / 排序 / 搜索 / 单位类别 / 只看列表 / 展开的分组也要记住：
       刷新 shell.html?p=music.html 时全退回默认很烦（2026-11-15 反馈）。 */
    const g0 = prefGet('group', '全部');
    ST.group = GROUPS.indexOf(g0) >= 0 ? g0 : '全部';
    ST.chipsOpen = !!prefGet('chipsOpen', false);
    const s0 = prefGet('sort', 'no');
    ST.sort = ['no', 'name', 'dur'].indexOf(s0) >= 0 ? s0 : 'no';
    ST.q = String(prefGet('q', '') || '');
    // 2026-11-16：分类按钮已删，不再读/写 vkind 偏好
    const savedOpen = prefGet('open', []);
    ST.open = new Set(Array.isArray(savedOpen) ? savedOpen.map(Number) : []);
    const qs0 = new URLSearchParams(location.search);
    ST.src = qs0.get('src') || prefGet('src', 'ost');
    if (!MODES.some(m => m[0] === ST.mode)) ST.mode = 'list';
    if (ST.src !== 'voice') ST.src = 'ost';
    // 深链：?voice=11 直接展开某个角色的音声
    const vopen = +(qs0.get('voice') || 0);
    if (vopen) { ST.src = 'voice'; ST.open.add(vopen); }

    // 回填控件：只改 ST 不够，下拉框 / 输入框 / 复选框也得显示存下来的值
    $('#plSort').value = ST.sort;
    $('#mq').value = ST.q;
    $('#lstOnly').checked = !!prefGet('lstOnly', false);

    AU.volume = ST.muted ? 0 : ST.vol;
    AU.playbackRate = ST.rate;
    paintVolume();
    paintProgress();
    loadLists();
    renderLists();
    paintFav();

    $$('.pl-tabs button').forEach(b => {
      b.classList.toggle('on', b.dataset.src === ST.src);
      b.onclick = () => {
        ST.src = b.dataset.src;
        $$('.pl-tabs button').forEach(x => x.classList.toggle('on', x === b));
        prefSet('src', ST.src);
        ST.group = '全部'; prefSet('group', ST.group);
        $('#btnExport').style.display = ST.src === 'voice' ? 'none' : '';
        renderChips();
        renderList();
      };
    });
    $('#btnExport').style.display = ST.src === 'voice' ? 'none' : '';

    $('#mode').textContent = modeText(ST.mode);
    $('#rate').textContent = (String(ST.rate).indexOf('.') < 0 ? ST.rate.toFixed(1) : ST.rate) + '×';

    renderChips();
    renderList();
    paintTrack();
    setPlayingUI(false);
    /* 容器宽度变了（窗口缩放 / 手机抽屉开合 / 唱片模式隐藏再显示）就重算
     * 收起态能塞几个分类。观察 plChips 自身即可：display:none→block 会触发。 */
    try {
      const box = $('#plChips');
      let lastW = box.clientWidth;
      const onSize = () => {
        const w = box.clientWidth;
        if (w === lastW) return;            // 只看宽度，避免高度变化引起的抖动/递归
        lastW = w;
        if (!ST.chipsOpen) fitChips();
      };
      if (window.ResizeObserver) new ResizeObserver(onSize).observe(box);
      else window.addEventListener('resize', onSize);
    } catch (e) { /* 老浏览器忽略，仍有 renderChips 时的首次 fit */ }

    /* ---- 播放列表操作 ---- */
    $('#lstSel').onchange = e => {
      ST.lst = e.target.value;
      prefSet('curlist', ST.lst);
      paintFav();
      renderList();
    };
    $('#lstNew').onclick = async () => {
      const n = await Dlg.prompt('新播放列表的名字：',
        { value: '列表 ' + (Object.keys(LISTS).length + 1), anchor: $('#lstNew') });
      if (n === null) return;
      const id = 'l' + Date.now().toString(36);
      LISTS[id] = { n: (n || '未命名').slice(0, 24), t: [], ts: Date.now() };
      ST.lst = id;
      saveLists(); renderLists(); paintFav(); renderList();
      toast('已新建「' + LISTS[id].n + '」');
    };
    $('#lstRen').onclick = async () => {
      const l = curList();
      if (!l) return;
      const n = await Dlg.prompt('改名：', { value: l.n, anchor: $('#lstRen') });
      if (n === null) return;
      l.n = (n || '未命名').slice(0, 24);
      saveLists(); renderLists();
    };
    $('#lstDel').onclick = async () => {
      const l = curList();
      if (!l) return;
      if (Object.keys(LISTS).length <= 1) { toast('至少要留一个播放列表', true); return; }
      if (!(await Dlg.confirm('删除播放列表「' + l.n + '」？\n\n（只删这个列表里的曲目记录，不影响已下载的音频）',
        { danger: true, ok: '删除', anchor: $('#lstDel') }))) return;
      delete LISTS[ST.lst];
      ST.lst = Object.keys(LISTS)[0];
      saveLists(); renderLists(); paintFav(); renderList();
    };
    $('#lstOnly').onchange = e => {
      prefSet('lstOnly', e.target.checked);
      renderList();
    };

    /* ---- 播放控制 ---- */
    $('#play').onclick = () => {
      const it = currentItem();
      if (!it) {
        const first = ST.queue.find(x => x.src);
        if (!first) { toast('没有可播放的曲目', true); return; }
        playItem(first);
        return;
      }
      if (AU.paused) AU.play().catch(() => {}); else AU.pause();
    };
    $('#prev').onclick = prev;
    $('#next').onclick = () => next();
    $('#mode').onclick = () => {
      const i = MODES.findIndex(m => m[0] === ST.mode);
      ST.mode = MODES[(i + 1) % MODES.length][0];
      $('#mode').textContent = modeText(ST.mode);
      $('#mode').classList.toggle('on', ST.mode !== 'list');
      prefSet('mode', ST.mode);
      // 按钮上可能是两字简称，toast 里给全名，别让人猜
      toast(modeFull(ST.mode));
    };
    $('#rate').onclick = () => {
      const i = RATES.indexOf(ST.rate);
      ST.rate = RATES[(i + 1) % RATES.length];
      AU.playbackRate = ST.rate;
      $('#rate').textContent =
        (String(ST.rate).indexOf('.') < 0 ? ST.rate.toFixed(1) : ST.rate) + '×';
      $('#rate').classList.toggle('on', ST.rate !== 1);
      prefSet('rate', ST.rate);
    };
    $('#fav').onclick = () => {
      const it = currentItem();
      if (!it) return;
      const on = toggleInList(it.key);
      paintFav();
      renderList();
      toast(on ? '已加进「' + (curList() || {}).n + '」'
        : '已从「' + (curList() || {}).n + '」移除');
    };

    /* ---- 进度条 ---- */
    const seek = $('#seek');
    seek.addEventListener('input', () => {
      dragging = true;
      setVal(seek);
      const d = AU.duration || 0;
      if (d) $('#curTime').textContent = fmt((seek.value / 1000) * d);
    });
    const commit = () => {
      if (!dragging) return;
      dragging = false;
      const d = AU.duration || 0;
      if (d) {
        AU.currentTime = (seek.value / 1000) * d;
        paintProgress();
        saveNow();
      }
    };
    ['change', 'pointerup', 'mouseup', 'touchend'].forEach(ev =>
      seek.addEventListener(ev, commit));

    const vol = $('#vol');
    vol.addEventListener('input', () => {
      ST.vol = (+vol.value) / 100;
      ST.muted = ST.vol <= 0;
      AU.volume = ST.muted ? 0 : ST.vol;
      AU.muted = false;
      setVal(vol);
      paintVolume();
      prefSet('vol', ST.vol);
      prefSet('muted', ST.muted);
    });
    $('#mute').onclick = () => {
      ST.muted = !ST.muted;
      AU.volume = ST.muted ? 0 : ST.vol;
      paintVolume();
      prefSet('muted', ST.muted);
      toast(ST.muted ? '已静音' : '已取消静音');
    };

    // 进度每隔一秒往 localStorage 落一次：换页时别的页面能接着播
    let _saveT = 0;
    AU.addEventListener('timeupdate', () => {
      paintProgress();
      const now = Date.now();
      if (now - _saveT > 900) { _saveT = now; if (!AU.paused) saveNow(true); }
    });
    AU.addEventListener('durationchange', paintProgress);
    AU.addEventListener('loadedmetadata', paintProgress);
    // 任何一路开始播放（用户点、外壳命令、续播、上一首下一首）都在这里拿票，
    // 让别的 music 实例立刻闭嘴 —— 这是"同一时刻只有一路音频"的唯一入口
    AU.addEventListener('play', () => { claimAudio(); setPlayingUI(true); saveNow(true); });
    AU.addEventListener('pause', () => { setPlayingUI(false); saveNow(false); });
    AU.addEventListener('error', () => {
      if (!AU.src) return;
      setPlayingUI(false);
      toast('这段音频读不到：可能还没装全，去「基础数据」补全一次', true);
    });
    AU.addEventListener('ended', () => {
      if (ST.mode === 'one') { AU.currentTime = 0; AU.play().catch(() => {}); return; }
      saveNow(false);
      // 单曲播放：播完就停在这里（不回到开头、不接下一首）
      if (ST.mode === 'single') { setPlayingUI(false); return; }
      next();
    });

    $('#plSort').onchange = e => {
      ST.sort = e.target.value; prefSet('sort', ST.sort); renderList();
    };
    let qt = 0;
    $('#mq').oninput = e => {
      clearTimeout(qt);
      qt = setTimeout(() => {
        ST.q = e.target.value; prefSet('q', ST.q); renderList();
      }, 140);
    };
    // 搜索框尾部的清除叉：走 common.js 那份统一实现（剧情页 / CG 页 / 控制台
    // 列表都用它），点一下清空并派发 input，列表按空筛选重画（2026-11-16 反馈）
    if (window.ESC && ESC.searchClear) ESC.searchClear($('#mq'));
    $('#btnExport').onclick = doExport;
    $('#bulkAll').onclick = toggleAll;
    $('#bulkAdd').onclick = () => bulkList(true);
    $('#bulkDel').onclick = () => bulkList(false);
    paintBulk();
    $('#btnFocus').onclick = () => setFocus(!document.body.classList.contains('focus'), true);
    // 手机上的「☰ 列表」：唱片模式下列表是 display:none 的，
    // 先退出唱片模式再开抽屉，不然点了完全没反应
    $('#btnList').onclick = () => {
      if (document.body.classList.contains('focus')) setFocus(false, true);
      togglePl();
    };
    // #plClose / #plMask 是 v1.1.7 才加进 music.html 的。为什么要判空：升级是由
    // **升级前那版**的更新器执行的，1.1.6 的白名单不含 music.html —— 老用户可能
    // 拿到新 JS + 旧页面，直接 `$('#plClose').onclick` 会抛错，把后面整段初始化
    // （落盘、唱片模式恢复）都带停。判空后最多少一个「✕ 收起」，页面照常可用。
    for (const [sel, fn] of [['#plClose', closePl], ['#plMask', closePl]]) {
      const el = $(sel);
      if (el) el.onclick = fn;
    }

    // 关页面/切后台：把位置落盘（pagehide 在移动端也比 beforeunload 靠谱）
    // 进 bfcache 的文档**不会自己停**：旧文档还在响、新文档又续播，就是"两路同一首歌"。
    // 所以入 bfcache 前先暂停，位置照旧记"在播放"，回来（pageshow persisted）再抢票续播。
    window.addEventListener('pagehide', ev => {
      saveNow();
      if (ev.persisted && !AU.paused) {
        resumeOnShow = true;
        AU.pause();
        saveNow(true);      // 状态仍记"在播"，别的页面 / 回来时据此续播
      }
    });
    window.addEventListener('pageshow', ev => {
      if (!ev.persisted || !resumeOnShow) return;
      resumeOnShow = false;
      claimAudio();
      AU.play().catch(() => {});
    });

    /* ---- 跨页播放设置：这条设置由 shell.html 读，控制"别的页面"那条播放条 ---- */
    const CFG_KEY = 'esc_ost_shell_cfg';
    /* 默认值与归一化都集中在这一处。shell.html 的 applyCfg() 有一份**等价**实现，
       跨 document 没法共用函数 —— 加字段时两边一起改，否则会出现"面板存了、
       播放条不认"的静默失效。 */
    const loadCfg = () => {
      let d = null;
      try { d = JSON.parse(localStorage.getItem(CFG_KEY) || 'null'); } catch (e) { /* 忽略 */ }
      d = d || {};
      const ph = String(d.pauseHide == null ? 'instant' : d.pauseHide);
      return {
        showOther: d.showOther !== false,
        pos: ['center', 'left', 'right'].indexOf(d.pos) >= 0 ? d.pos : 'center',
        pauseOnStory: d.pauseOnStory !== false,
        // 暂停后多久收起这条播放条：立刻 / 3 / 5 / 10 秒 / 一直留着
        pauseHide: ['instant', '3', '5', '10', 'keep'].indexOf(ph) >= 0 ? ph : 'instant',
        alwaysOpen: !!d.alwaysOpen,          // 不用鼠标悬停就展开控制面板
        showCover: d.showCover !== false,    // 迷你条上的封面缩略图
        showTime: d.showTime !== false,      // 0:00 / 0:00
      };
    };
    $('#plSet').onclick = async () => {
      const c = loadCfg();
      const v = await Dlg.form({
        title: '跨页播放设置',
        msg: '控制原声集在其它页面时，底部那条常驻播放条怎么显示。',
        /* 按「显示 → 位置与内容 → 播放行为」分组，一屏看完不用来回找；
           每组的开关作用范围一致，避免一堆开关不分主次。 */
        fields: [
          { type: 'head', label: '显示' },
          { key: 'showOther', label: '在别的页面显示播放条',
            type: 'checkbox', value: c.showOther },
          { key: 'pauseHide', label: '暂停后多久收起', type: 'select', value: c.pauseHide,
            options: [['instant', '立刻收起'], ['3', '3 秒后'],
              ['5', '5 秒后'], ['10', '10 秒后'], ['keep', '一直留着']] },
          { key: 'alwaysOpen', label: '常驻展开（不用鼠标悬停）',
            type: 'checkbox', value: c.alwaysOpen },

          { type: 'head', label: '位置与内容' },
          { key: 'pos', label: '播放条位置', type: 'select', value: c.pos,
            options: [['center', '底部居中'], ['left', '左下'], ['right', '右下']] },
          { key: 'showCover', label: '显示封面', type: 'checkbox', value: c.showCover },
          { key: 'showTime', label: '显示时间', type: 'checkbox', value: c.showTime },

          { type: 'head', label: '播放行为' },
          { key: 'pauseOnStory', label: '开始播放剧情时自动暂停原声集',
            type: 'checkbox', value: c.pauseOnStory },
        ],
        anchor: $('#plSet'),      // 贴 ⚙ 按钮弹出，不用把鼠标移到屏幕中央
      });
      if (!v) return;
      const next = {
        showOther: !!v.showOther,
        pos: v.pos,
        pauseOnStory: !!v.pauseOnStory,
        pauseHide: v.pauseHide,
        alwaysOpen: !!v.alwaysOpen,
        showCover: !!v.showCover,
        showTime: !!v.showTime,
      };
      try {
        localStorage.setItem(CFG_KEY, JSON.stringify(next));
        if (window.parent !== window)
          window.parent.postMessage({ t: 'ost:cfg', d: next }, location.origin);
      } catch (e) { /* 忽略 */ }
      toast('跨页播放设置已保存');
    };

    /* ---- 快捷键 ----
       用**捕获阶段**收键盘：之前用冒泡阶段，焦点落在某个按钮/滑杆上时，
       浏览器先把 ←/→ 交给那个控件（滑杆被左右拨、按钮被空格点一下），
       播放器这边就"没反应"（2026-11-15 反馈）。
       现在除了真正的文字输入框，一律拦下来自己处理，并 preventDefault
       阻止控件吃掉按键。 */
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('keydown', onKeyEscBlur, true);

    try {
      if ('mediaSession' in navigator) {
        navigator.mediaSession.setActionHandler('previoustrack', prev);
        navigator.mediaSession.setActionHandler('nexttrack', () => next());
        navigator.mediaSession.setActionHandler('play', () => AU.play().catch(() => {}));
        navigator.mediaSession.setActionHandler('pause', () => AU.pause());
      }
    } catch (e) { /* 忽略 */ }

    const qs = new URLSearchParams(location.search);
    const want = qs.get('play');
    if (!want) {
      // 从别的页面回来：接着上次那首、那个位置（自动播被拦就等用户点 ▶）
      const sn = readNow();
      if (sn) restoreSaved(sn);
    }
    if (want) {
      const it = TRACKS.find(t => String(t.no) === want);
      if (it) playItem(trackItem(it));
    }
    // 唱片模式：先把按钮文案对齐，再按「上次的选择 / 深链 ?focus」恢复。
    // 深链只用这一次，不写偏好（截图和调试经常带这个参数）
    setFocus(false, false);
    if (qs.get('focus') || prefGet('focus', false)) setFocus(true, false);
  }

  //: 这些控件里敲字是正常的，键盘事件不能抢
  function isTyping(t) {
    if (!t) return false;
    const tag = (t.tagName || '').toUpperCase();
    if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (tag === 'INPUT') {
      const ty = (t.type || 'text').toLowerCase();
      return ['text', 'search', 'number', 'email', 'password', 'url', 'tel'].indexOf(ty) >= 0;
    }
    return !!t.isContentEditable;
  }

  function onKeyEscBlur(ev) {
    if (ev.key === 'Escape') {
      hideHover(); closePl();
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    }
  }

  function onKey(ev) {
    if (window.Dlg && Dlg.busy) return;   // 自绘弹窗打开时别抢按键
    if (isTyping(ev.target)) return;
    const k = ev.key;
    if (ev.code === 'Space' || k === ' ') {
      ev.preventDefault();
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      $('#play').click();
      return;
    }
    if (k === 'ArrowRight' && ev.shiftKey) {
      ev.preventDefault();
      AU.currentTime = Math.min(AU.duration || 0, AU.currentTime + 5);
    } else if (k === 'ArrowLeft' && ev.shiftKey) {
      ev.preventDefault();
      AU.currentTime = Math.max(0, AU.currentTime - 5);
    } else if (k === 'ArrowRight') {
      ev.preventDefault();
      next();
    } else if (k === 'ArrowLeft') {
      ev.preventDefault();
      prev();
    } else if (k === 'ArrowUp' || k === 'ArrowDown') {
      ev.preventDefault();
      const d = k === 'ArrowUp' ? 0.05 : -0.05;
      ST.vol = Math.min(1, Math.max(0, ST.vol + d));
      ST.muted = false;
      AU.volume = ST.vol;
      paintVolume();
      prefSet('vol', ST.vol);
      prefSet('muted', false);
    } else if (k === 'l' || k === 'L') {
      $('#fav').click();
    } else if (k === '/') {
      ev.preventDefault();
      $('#mq').focus();
    }
  }

  // 调试/测试钩子（?probe=1 之外的脚本也用它读状态）
  window.MUSICPAGE = {
    count: () => ST.list.length,
    queue: () => ST.queue.length,
    src: () => ST.src,
    state: () => ({
      src: ST.src, group: ST.group, sort: ST.sort, mode: ST.mode, rate: ST.rate,
      vol: ST.vol, list: ST.list.length, queue: ST.queue.length,
      lists: Object.keys(LISTS).length, cur: ST.curKey,
    }),
    play: key => { const it = ST.queue.find(x => x.key === key); if (it) playItem(it); },
  };

  /* ------------------------------------------------ 全站外壳 shell.html
     music.html 常驻在一个 iframe 里**从不卸载**，所以 <audio> 是同一个元素，
     换页面时播放真的不中断。外壳底部那条全站播放条通过 postMessage 读写这里：
     它只发命令，队列/循环/音量/播放列表这些状态始终留在本页。 */
  var IN_SHELL = window.parent !== window;
  function pushShell() {
    if (!IN_SHELL) return;
    var it = currentItem();
    try {
      window.parent.postMessage({
        t: 'ost:state',
        d: {
          title: it ? it.title : '', sub: it ? it.sub : '', img: it ? it.img : '',
          dur: AU.duration || 0, pos: AU.currentTime || 0, paused: AU.paused,
          vol: AU.volume, muted: AU.muted, rate: AU.playbackRate,
          mode: ST.mode, playing: !!it,
        },
      }, location.origin);
    } catch (e) { /* 外壳不存在（独立打开）就算了 */ }
  }
  if (IN_SHELL) {
    ['timeupdate', 'durationchange', 'play', 'pause', 'volumechange',
      'ratechange', 'ended'].forEach(function (e) {
      AU.addEventListener(e, pushShell);
    });
    // 隐藏的 iframe 里 timeupdate 可能被节流，兜底每秒一次
    setInterval(pushShell, 1000);
    window.addEventListener('message', function (ev) {
      if (ev.origin !== location.origin) return;
      var m = ev.data;
      if (!m || m.t !== 'ost:cmd') return;
      var v = m.v;
      switch (m.a) {
        case 'state': break;
        case 'toggle':
          if (AU.paused) AU.play().catch(function () {}); else AU.pause();
          break;
        case 'pause': AU.pause(); break;   // 开始播放剧情时让 BGM 让路
        case 'src':
          // 常驻播放器不重载，外壳把链接里的 ?src=voice 转成这条命令
          ST.src = v === 'voice' ? 'voice' : 'ost';
          ST.group = '全部';
          prefSet('src', ST.src); prefSet('group', ST.group);
          $$('.pl-tabs button').forEach(function (b) {
            b.classList.toggle('on', b.dataset.src === ST.src);
          });
          $('#btnExport').style.display = ST.src === 'voice' ? 'none' : '';
          renderChips(); renderList();
          break;
        case 'next': next(); break;
        case 'prev': prev(); break;
        case 'seek':
          if (isFinite(AU.duration) && AU.duration > 0) {
            AU.currentTime = v * AU.duration; paintProgress(); saveNow();
          }
          break;
        case 'vol':
          ST.vol = Math.min(1, Math.max(0, +v)); ST.muted = false;
          AU.volume = ST.vol; AU.muted = false; paintVolume(); saveNow();
          break;
        case 'volup': case 'voldown':
          ST.vol = Math.min(1, Math.max(0, ST.vol + (m.a === 'volup' ? 0.05 : -0.05)));
          ST.muted = false; AU.muted = false; AU.volume = ST.vol;
          paintVolume(); saveNow();
          break;
        case 'mute':
          ST.muted = !ST.muted;
          if (!ST.muted && ST.vol === 0) ST.vol = 0.8;
          AU.muted = ST.muted; paintVolume(); saveNow();
          break;
        case 'rate': {
          var i = RATES.findIndex(function (r) {
            return Math.abs(r - AU.playbackRate) < 0.001;
          });
          ST.rate = RATES[(i + 1) % RATES.length];
          AU.playbackRate = ST.rate;
          $('#rate').textContent = (String(ST.rate).indexOf('.') < 0
            ? ST.rate.toFixed(1) : ST.rate) + '×';
          prefSet('rate', ST.rate); saveNow();
          break;
        }
        case 'mode': {
          var j = MODES.findIndex(function (x) { return x[0] === ST.mode; });
          ST.mode = MODES[(j + 1) % MODES.length][0];
          $('#mode').textContent = modeText(ST.mode);
          $('#mode').classList.toggle('on', ST.mode !== 'list');
          prefSet('mode', ST.mode); saveNow();
          break;
        }
        case 'ff': AU.currentTime = Math.min(AU.duration || 0, AU.currentTime + 5); break;
        case 'rw': AU.currentTime = Math.max(0, AU.currentTime - 5); break;
        case 'playno': {
          var t = TRACKS.find(function (x) { return x.no === +v; });
          if (t) playItem(trackItem(t));
          break;
        }
        case 'voice':
          ST.src = 'voice'; ST.open.add(+v);
          $$('.pl-tabs button').forEach(function (b) {
            b.classList.toggle('on', b.dataset.src === 'voice');
          });
          renderChips(); renderList();
          break;
      }
      pushShell();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
