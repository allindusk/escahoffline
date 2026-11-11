/* 资源管理控制台
 *
 * 与播放器的关系：完全独立的一页，只通过 /api/* 与后端通信。
 * 所有耗时操作都是"提交任务 → 订阅 SSE → 底部进度条"，页面可以随便刷新，
 * 任务在服务端继续跑。
 */
(() => {
  const $ = s => document.querySelector(s);
  const $$ = s => Array.from(document.querySelectorAll(s));

  const ST = {
    state: null, manifest: null, cg: null,
    sel: new Set(),           // 用户主动勾选的剧情单元
    cat: 'char',              // ② 列表默认停在哪一类（storylist 里切）
    cgSel: new Set(), vidSel: new Set(), jobs: {}, updUrl: '', links: null,
    updShown: false,          // 启动检查的结果只回显一次
    // 云端可安装的单元与它们的本地状态（由 /api/cdn/units 提供）
    units: { char: [], adv: [], state: {}, loaded: false, at: 0, needManifest: false },
    pub: null,          // 公共素材（背景/音乐/音效/头像/剧情横幅）已导出数量
  };

  // ---------------------------------------------------------------- 工具
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  /* 极简 Markdown→HTML（只给「更新说明」用，来源是本项目自己的 Release 正文）。
   * 先整体转义再套少量规则，杜绝 XSS。支持：#~#### 标题、**粗体**、`代码`、
   * 无序列表（-/* 起头）、空行分段。 */
  function renderMd(src) {
    if (!src) return '';
    const inline = s => esc(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
    const out = [];
    let items = [];                 // 正在收集的 <ul> 条目
    const flushList = () => {
      if (!items.length) return;
      out.push('<ul>' + items.map(s => `<li>${inline(s)}</li>`).join('') + '</ul>');
      items = [];
    };
    /* 折行续写：Release 说明是手工折行的（一条 bullet 写在 2–3 行里），
       以前每行单独成一个 <p>，列表被打断、还带着行首缩进，看起来像一堵乱码。
       现在续行并回上一条 bullet / 上一段。 */
    const appendText = t => {
      if (items.length) { items[items.length - 1] += ' ' + t; return; }
      const last = out.length - 1;
      if (last >= 0 && out[last].startsWith('<p>')) {
        out[last] = out[last].replace(/<\/p>$/, ' ') + inline(t) + '</p>';
        return;
      }
      out.push(`<p>${inline(t)}</p>`);
    };
    for (const line of String(src).split(/\r?\n/)) {
      const t = line.trim();
      if (!t) { flushList(); continue; }
      const h = t.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        flushList();
        const lv = h[1].length;
        out.push(`<h${lv}>${inline(h[2])}</h${lv}>`);
        continue;
      }
      const li = line.match(/^\s*[-*]\s+(.*)$/);
      if (li) { items.push(li[1]); continue; }
      if (t.startsWith('>')) continue;      // 引用块是模板自用说明，不给用户看
      appendText(t);
    }
    flushList();
    return out.join('');
  }

  function human(n) {
    n = +n || 0;
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(i > 1 ? 2 : 1)) + ' ' + u[i];
  }
  const secs = s => {
    s = Math.round(s || 0);
    if (s < 60) return s + ' 秒';
    const m = Math.floor(s / 60), r = s % 60;
    if (m < 60) return `${m} 分 ${r} 秒`;
    return `${Math.floor(m / 60)} 时 ${m % 60} 分`;
  };

  async function api(path, opt) {
    const r = await fetch('api/' + path, Object.assign({
      headers: { 'Content-Type': 'application/json' },
    }, opt || {}));
    const txt = await r.text();
    let d = {};
    try { d = txt ? JSON.parse(txt) : {}; } catch (e) { d = { error: txt.slice(0, 200) }; }
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    return d;
  }

  function toastMsg(msg, bad) {
    let el = $('#setupToast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'setupToast';
      el.style.cssText = 'position:fixed;left:50%;top:66px;transform:translateX(-50%);' +
        'z-index:9800;padding:9px 18px;border-radius:10px;font-size:14.6px;' +
        'background:#16283c;border:1px solid #2f5478;color:#dff0ff;' +
        'box-shadow:0 10px 30px rgba(0,0,0,.5);transition:opacity .3s';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.borderColor = bad ? '#a33' : '#2f5478';
    el.style.color = bad ? '#ffb3ba' : '#dff0ff';
    el.style.opacity = '1';
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.style.opacity = '0'; }, 3200);
    // 顶部气泡 3 秒就没了，右下角留一份记录，方便回看刚做了什么
    notify(msg, bad ? 'err' : 'info');
  }

  /* ---------------------------------------------------------- 操作记录浮窗
   * 右下角记录操作与提示；有新消息自动展开、5 秒后自动收起；
   * 不想看就点标题栏或「收起」手动折叠。
   */
  const ACT = { rows: [], max: 150, hideTimer: null, auto: true, unread: 0 };
  const apPanel = $('#actPanel');
  const apBody = $('#apBody');
  const apToggle = $('#apToggle');

  /** delay：多久后自动收起；0 = 不自动收（错误这类必须让人看见的结论） */
  function actExpand(delay) {
    if (!apPanel) return;
    apPanel.classList.remove('collapsed');
    apPanel.classList.remove('has-new');     // 展开即视为已读
    ACT.unread = 0;
    if (apToggle) apToggle.textContent = '收起';
    clearTimeout(ACT.hideTimer);
    // 只有开了「自动收起」才计时；关掉就一直留着，等用户自己收
    const wait = (delay === undefined) ? 5000 : delay;
    if (ACT.auto && wait > 0) ACT.hideTimer = setTimeout(actCollapse, wait);
  }

  function actCollapse() {
    if (!apPanel) return;
    apPanel.classList.add('collapsed');
    // 收起来时里面还有没展开看过的记录 → 标题旁点个圆点，否则完全看不出来
    if (ACT.unread) apPanel.classList.add('has-new');
    if (apToggle) apToggle.textContent = '展开';
    clearTimeout(ACT.hideTimer);
  }

  /** 操作记录里只放**一行摘要**：错误码和前几十个字，够看出"是什么错"。
   *  想看全文/截图/复制 → 点「详情」开浮窗（用户 2026-11-06 的建议：
   *  一条长报错铺在记录里，会把好几条历史挤走，截图也不完整）。 */
  const AP_BRIEF = 62;
  function actBrief(r) {
    const s = String(r.text || '');
    return s.length > AP_BRIEF ? s.slice(0, AP_BRIEF) + '…' : s;
  }
  function actHasDetail(r) {
    return r.lv === 'err' || !!r.sub || String(r.text || '').length > AP_BRIEF;
  }

  function renderAct() {
    if (!apBody) return;
    apBody.innerHTML = ACT.rows.length
      ? ACT.rows.map(function (r, i) {
        return '<div class="apRow ' + r.lv + '">'
          + '<b>' + r.at + '</b><div class="apMsg">'
          + '<span class="apText">' + esc(actBrief(r)) + '</span>'
          + (actHasDetail(r) ? '<div class="apBtns">'
            + '<button class="apMini apDetail" data-i="' + i
            + '" title="查看完整错误信息">详情</button>'
            + (r.lv === 'err' ? '<button class="apMini apCopy" data-i="' + i
              + '" title="复制这条完整的错误文字">复制</button>' : '')
            + '</div>' : '')
          + '</div></div>';
      }).join('')
      : '<div class="apRow"><span>暂无记录</span></div>';
    apBody.scrollTop = apBody.scrollHeight;
  }

  /** 一条记录 → 可粘贴的纯文本（错误 + 运行环境）。只做"复制文字"，不做任何引导。 */
  function actOneText(r) {
    const L = ['【ESCAH 报错】' + (r.at || ''), String(r.text || '')];
    if (r.sub) L.push('环境：' + r.sub);
    return L.join('\n');
  }

  function copyText(text, msg) {
    const done = function () { toastMsg(msg || '已复制', false); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () { fb(); });
    } else { fb(); }
    function fb() {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); }
      catch (e) { toastMsg('复制失败，请手动选中这段文字复制', true); }
      ta.remove();
    }
  }

  /* ---------------- 报错详情浮窗 ----------------
   * 独立一层（不挤在右下角小框里）：宽度够、字号够、可选中文本，
   * 用户想截图或整段复制都会舒服很多。Esc / 点空白 / 「关闭」都能收起。 */
  const apMask = $('#apDetailMask');
  const apDBody = $('#apDetailBody');
  const apDFoot = $('#apDetailFoot');
  const apDCode = $('#apDetailCode');
  let ACT_DETAIL = null;                 // 当前打开的那条（复制用）

  function openActDetail(i) {
    const r = ACT.rows[i];
    if (!r || !apMask) return;
    ACT_DETAIL = r;
    if (apDCode) {
      apDCode.textContent = (r.at || '') + (r.lv === 'err' ? ' · 报错' : '');
    }
    if (apDBody) {
      apDBody.innerHTML = '<div class="apDText">' + esc(r.text) + '</div>'
        + (r.sub
          ? '<div class="apDLabel">运行环境</div>'
            + '<div class="apDSub">' + esc(r.sub) + '</div>'
          : '');
    }
    if (apDFoot) {
      apDFoot.textContent = '上面是程序记录的原始错误内容，可选中文字或点「复制这段」。';
    }
    apMask.hidden = false;
    // 操作记录本身收起来，免得挡住浮窗
    if (apPanel) actCollapse();
  }

  function closeActDetail() {
    if (apMask) apMask.hidden = true;
    ACT_DETAIL = null;
  }

  if (apMask) {
    apMask.addEventListener('click', function (e) {
      if (e.target === apMask) closeActDetail();     // 点空白处关闭
    });
  }
  const apDClose = $('#apDClose');
  if (apDClose) apDClose.onclick = closeActDetail;
  const apDCopy = $('#apDCopy');
  if (apDCopy) {
    apDCopy.onclick = function () {
      if (ACT_DETAIL) copyText(actOneText(ACT_DETAIL), '已复制');
      else closeActDetail();
    };
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && apMask && !apMask.hidden) closeActDetail();
  });

  /** 行内「详情」「复制」：委托绑定一次，别每次渲染重新挂。 */
  if (apBody) {
    apBody.addEventListener('click', function (e) {
      const t = e.target;
      if (!t || !t.classList || !t.dataset) return;
      if (t.classList.contains('apDetail')) {
        openActDetail(+t.dataset.i);
        return;
      }
      if (t.classList.contains('apCopy')) {
        const r = ACT.rows[+t.dataset.i];
        if (r) copyText(actOneText(r), '已复制');
      }
    });
  }

  /** 记一条操作/提示。level: info | ok | warn | err
   *  sub：附加的「环境指纹」行（版本/系统/路径/盘余/卡在哪一步），
   *      失败时由后端 jobs._diag() 给 —— 用户截图就能带上排错线索。 */
  function notify(text, level, sub) {
    if (!text) return;
    const t = new Date();
    const at = [t.getHours(), t.getMinutes(), t.getSeconds()]
      .map(function (n) { return String(n).padStart(2, '0'); }).join(':');
    ACT.rows.push({ at: at, text: String(text), lv: level || 'info',
      sub: sub || '' });
    if (ACT.rows.length > ACT.max) ACT.rows = ACT.rows.slice(-ACT.max);
    ACT.unread++;
    renderAct();
    /* 窄屏（手机）一展开就横着盖住大半屏幕。默认保持收起成右下角小框，
       只在标题旁打个点提示有新记录，想看再点开（用户 2026-11-02 要求）。
       桌面：自动展开，并且给够时间看 —— 出错不自动收（5 秒一闪就没了等于没提示），
       完成类留 12 秒。 */
    const keep = level === 'err' ? 0 : (level === 'ok' ? 12000 : 6000);
    if (window.matchMedia('(max-width: 860px)').matches) {
      if (apPanel) apPanel.classList.add('has-new');
    } else {
      actExpand(keep);
    }
  }

  /** 底部任务条会占位置，浮窗跟着抬高，避免叠在一起 */
  function syncActBottom() {
    if (!apPanel) return;
    const bar = $('#jobbar');
    const h = bar ? Math.round(bar.getBoundingClientRect().height) : 0;
    apPanel.style.bottom = (h > 0 ? h + 60 : 54) + 'px';
  }

  if (apToggle) {
    apToggle.onclick = function (e) {
      e.stopPropagation();
      if (apPanel.classList.contains('collapsed')) actExpand();
      else actCollapse();
    };
  }
  const apClear = $('#apClear');
  if (apClear) {
    apClear.onclick = function (e) {
      e.stopPropagation();
      ACT.rows = [];
      renderAct();
      actCollapse();
    };
  }
  /* 「复制错误信息」：把版本 / 系统 / 路径特征 / 清单时间 / 最近的失败任务 +
     日志尾部拼成一段文本，一键复制。纯"看错误"的工具，不做任何引导。 */
  const apFb = $('#apFeedback');
  if (apFb) {
    apFb.onclick = function (e) {
      e.stopPropagation();
      if (apFb.disabled) return;
      const old = apFb.textContent;
      apFb.disabled = true;
      apFb.textContent = '收集中…';
      api('diag?log=60').then(function (d) {
        copyText(d.text || '', '已复制');
      }).catch(function (err) {
        toastMsg('复制失败：' + (err.message || err), true);
      }).then(function () {
        apFb.disabled = false;
        apFb.textContent = old;
      });
    };
  }
  // 「自动收起」开关：默认开。关掉后新消息只展开、不再自动收。
  const apAuto = $('#apAuto');
  const apAutoWrap = $('#apAutoWrap');
  if (apAutoWrap) {
    // 点开关只改设置，别冒泡到标题栏（那会顺带切换展开/收起）
    apAutoWrap.onclick = function (e) { e.stopPropagation(); };
  }
  if (apAuto) {
    try { ACT.auto = localStorage.getItem('escah_act_auto') !== '0'; } catch (e) { }
    apAuto.checked = ACT.auto;
    apAuto.onchange = function () {
      ACT.auto = apAuto.checked;
      try {
        localStorage.setItem('escah_act_auto', ACT.auto ? '1' : '0');
      } catch (e) { }
      clearTimeout(ACT.hideTimer);
      // 切回「自动」时，如果现在是展开的，重新开始 5 秒计时
      if (ACT.auto && apPanel && !apPanel.classList.contains('collapsed')) {
        ACT.hideTimer = setTimeout(actCollapse, 5000);
      }
    };
  }
  const apHead = $('#apHead');
  if (apHead) {
    apHead.onclick = function () {
      if (apPanel.classList.contains('collapsed')) actExpand();
      else actCollapse();
    };
  }
  renderAct();

  // ---------------------------------------------------------------- 数据
  async function loadAll() {
    // 先把容量 / 耗时文案取回来：下面的渲染与确认弹窗直接用 FACT.t(...)，
    // 没等它就会拼出"约 部剧情""约 MB"这种空数字
    if (window.FACT) await FACT.ready();
    const [state, manifest, links, pub] = await Promise.all([
      api('state'), api('manifest'), api('links').catch(() => null),
      api('cdn/public').catch(() => null),   // 公共素材进度（背景/BGM/音效各下了多少）
    ]);
    ST.state = state;
    ST.manifest = manifest;
    ST.links = links;
    ST.pub = pub;
    /* 重画会重建整块列表的 innerHTML，滚动容器（main）的内容一高一低，
       滚动位置就被夹回顶端 —— 任务结束后列表自动刷新一次，用户正看到一半的
       位置就丢了。这里记一下再放回去（内容变短时由浏览器自己夹紧）。 */
    const sc = document.querySelector('main');
    const top = sc ? sc.scrollTop : 0;
    renderAll();
    if (sc && top) sc.scrollTop = top;
  }

  const depIndex = () => {
    const m = {};
    ((ST.manifest && ST.manifest.depots) || []).forEach(d => { m[d.id] = d; });
    return m;
  };
  const installed = () => (ST.state && ST.state.installed) || {};

  function renderAll() {
    // 顶栏只留版本号：已装部数 / 资源包缓存在下面「当前状态」里，顶栏不再重复
    $('#hdrSub').textContent = 'v' + ((ST.state && ST.state.version) || '?');
    renderOverview();
    renderPacks();
    renderQuality();
    renderExport();
    renderTrans();
    renderSettings();
  }

  // ---------------------------------------------------------------- 概览
  function renderOverview() {
  const cov = (ST.state && ST.state.coverage) || {};
  const cdn = (ST.state && ST.state.cdn) || {};
  const us = ST.units.state || {};
  const done = Object.values(us).filter(v => v !== 'missing').length;
  const total = (ST.units.char.length + ST.units.adv.length) || 0;

  // 容量一律问 FACT（= server/facts.py），这里不再写死数字与单位：
  // 以前"每部 45 MB、全部约 X GB"算在 JS 里，改一次得改五处
  // HTML 前缀已经写了"每部约"，这里只给区间（不要再带" / 部"）
  $('#ovMin').textContent = FACT.t('unitEach');
  $('#ovCore').textContent = cdn.manifestReady ? '已就绪' : '需联网刷新';
  // 注意 HTML 里已经写了"约 <b id=ovAll>"，这里不要再加"约" ——
  // 之前两边各写一次，页面上就成了「约 约 24.7 GB」
  $('#ovAll').textContent = total ? FACT.unitsSize(total) : '—';

  const cards = [
    ['已安装剧情', `${done}`, `/ ${total || '?'} 部`],
    ['资源包缓存', human((cov.bundleCache || {}).bytes),
      `${(cov.bundleCache || {}).files || 0} 个包`],
    ['剩余磁盘', human((ST.state && ST.state.free) || 0), ''],
    ['翻译', `${(ST.state.trans && ST.state.trans.messages) || 0}<small>句</small>`,
      `${(ST.state.trans && ST.state.trans.units) || 0} 部剧情`],
    ['下载来源',
      cdn.manifestReady ? '官方地址' : '还没弄好',
      (cdn.cdn || '').replace(/^https?:\/\//, '').split('/')[0] || '点「更新」页更新剧情目录'],
  ];
  $('#ovStats').innerHTML = cards.map(function (c) {
    return '<div class="stat"><div class="k">' + esc(c[0]) + '</div>' +
      '<div class="v">' + c[1] + '</div><div class="k">' + esc(c[2]) + '</div></div>';
  }).join('');

  $('#ovInstallMin').onclick = () => {
    ST.cat = 'char';
    ST.sel.clear();
    goto('packs');
    renderPacks();
    if (packList) packList.setSrc('char');
    toastMsg('在列表里挑一个角色勾上，然后点「开始安装」');
  };
  $('#ovInstallCore').onclick = async () => {
    $('#btnRefreshManifest') && $('#btnRefreshManifest').click();
  };
  $('#ovInstallAll').onclick = () => {
    const keys = ST.units.char.concat(ST.units.adv)
      .filter(k => (ST.units.state[k] || 'missing') === 'missing');
    if (!keys.length) { toastMsg('所有剧情都已安装'); return; }
    if (!confirm('将安装全部 ' + keys.length + ' 部剧情，约 ' +
      FACT.unitsSize(keys.length) + '。\n\n建议先装一两个试试再全量。确定吗？')) return;
    // 注意：**不能**替换 Set 对象 —— storylist 组件握着这个 Set 的引用，
    // 换成新对象后勾选状态就和组件脱钩了（列表看着没勾上）。
    ST.sel.clear();
    keys.forEach(k => ST.sel.add(k));
    ST.cat = 'char';
    goto('packs');
    renderPacks();
    toastMsg('已全选未安装的剧情，确认后点「开始安装」');
  };
}

  // ---------------------------------------------------------------- 剧情安装
  /* 这里选的是「剧情」，不是我们自己打包的资源包 ——
     安装时直接从官方 CDN 拉 AssetBundle，再在本机解包成播放器能用的
     webp / m4a / js（实现见 server/cdn/install.py）。
     所以界面上不再有 core / char_1.zip 这类概念，只有"哪部剧情装了没装"。
     列表本身由 storylist.js 渲染（与播放器侧栏同一套分类 / 排序 / 筛选）。 */

  async function loadUnits(force) {
    if (ST.units.loaded && !force && Date.now() - ST.units.at < 30000) return;
    const d = await api('cdn/units');
    ST.units.needManifest = !!d.needManifest;
    ST.units.char = (d.units || {}).char || [];
    ST.units.adv = (d.units || {}).adv || [];
    ST.units.state = d.state || {};
    ST.units.loaded = true;
    ST.units.at = Date.now();
  }

  /** 触发一次带进度的后台任务，完成后刷新界面 */
  async function startJob(path, body, tip) {
    try {
      await api(path, { method: 'POST', body: JSON.stringify(body || {}) });
      toastMsg(tip || '已开始，进度见底部');
      connectJobs(true);
    } catch (e) {
      toastMsg('启动失败：' + e.message, true);
    }
  }

  /** 任务结束后（renderJobs 里检测到 done）重新拉一次数据 */
  async function reloadAfterJobs() {
    ST.units.loaded = false;
    try {
      await loadAll();                 // 里面已经重画过列表（含 renderPacks）
    } catch (e) {
      /* 整体刷新失败**不能静默吞**：任务刚结束时 /api/state / /api/manifest
         偶尔会慢或失败，以前这里 catch 一下就完事，页面于是停在上一次的数字上
         ——「补全缺失」按钮一直亮着，用户手动刷新页面才对（2026-11-10 反馈）。
         现在至少把公共素材那一行单独补上，并留一条 console 便于排查。 */
      try { console.error('[reload] 任务结束后刷新失败：', e); } catch (e2) { }
      try {
        const p = await api('cdn/public');
        if (p && !p.error) { ST.pub = p; renderPacks(); }
      } catch (e3) {
        try { console.error('[reload] 公共素材进度也没取到：', e3); } catch (e4) { }
      }
      return;
    }
    try {
      const sc = document.querySelector('main');
      const top = sc ? sc.scrollTop : 0;
      renderPacks();                   // 再补一次：目录刚拉回来时列表还是空的
      if (sc && top) sc.scrollTop = top;
    } catch (e) { /* 重画失败不影响数据 */ }
  }

  /* 名字一律用人话（角色名 / 章节标题）。**不再拼「角色 415」「char_415」**：
     角色一共 384 个、ID 却到 415，序号化的写法会让人以为"角色缺了 31 个"。
     取名 / 稀有度 / 实装日期 / 横幅路径统一在 storylist.js 里（②③⑤ 三页共用），
     剧情标题的推算（主线第N部第X章）统一在 common.js 的 ESC.storyTitle。 */
  function unitName(key) { return SL.info(key).name; }

  const stateOf = key => ST.units.state[key] || 'missing';

  // 软缺（装好了，但官方源没有语音包）的文案按剧情类别区分：
  //  · 其他剧情：本来就没有语音轨 → 「无语音」
  //  · 活动剧情：官方没放出语音资源包，离线只能占位 → 「官方未提供资源包，占位符」
  function softMissingLabel(key) {
    const m = /^adv_([a-z]+)/.exec(key);
    const kind = m ? m[1] : '';
    if (kind === 'event') return '官方未提供资源包，占位符';
    return '无语音';
  }

  // 「其他」分类按入口分组（与播放页侧栏 #sidebar 同一份入口清单 data/othereps.js）。
  let _otherEps = null;
  function ensureOtherEps() {
    if (_otherEps || window.__OTHER_EPS) { _otherEps = window.__OTHER_EPS || _otherEps; return Promise.resolve(); }
    // 与播放页一致的按需加载：清单没随更新包下来就拉一次，缺失则静默降级。
    // cache:'no-store' 同理 —— 装新「其他」剧情后后端会改写这个文件（归组/补话名），
    // 固定 ?v= 会让页面一直读旧副本。
    return fetch('data/othereps.js?v=1118a', { cache: 'no-store' })
      .then(r => r.ok ? r.text() : null)
      .then(t => { if (t) { try { (0, eval)(t); } catch (e) {} } _otherEps = window.__OTHER_EPS || null; })
      .catch(() => {});
  }
  function otherEpsMaps() {
    const eps = _otherEps || window.__OTHER_EPS || [];
    const keyNo = {}, noName = {}, title = {};
    eps.forEach(function (g) {
      noName[g.no] = g.name;
      g.eps.forEach(function (e) {
        if (!(e.k in keyNo)) keyNo[e.k] = g.no;
        if (!(e.k in title)) title[e.k] = e.t;   // 每部单元取第一话标题作为行名
      });
    });
    return { keyNo: keyNo, noName: noName, title: title, groups: eps };
  }

  /** 某个分类（char / main / event / other）下的全部剧情单元 */
  function packUnits(src) {
    return src === 'char' ? ST.units.char : ST.units.adv;
  }

  /** 剧情横幅属于「公共素材」：下过才显示横幅图，没下过列表只显示名字。
   *  判据用**磁盘上的横幅图**（bannerImg），不用 bundle 缓存计数 ——
   *  清过资源包缓存后缓存计数会归零，但图还在，列表不该莫名退回纯文字。 */
  function bannersReady() {
    // ?nobanner=1 是测试/排错钩子：模拟"还没下公共素材"，列表退回纯文字
    try {
      if (new URLSearchParams(location.search).has('nobanner')) return false;
    } catch (e) { }
    const p = ST.pub || {};
    return +((p.bannerImg != null ? p.bannerImg : p.banner) || 0) > 0;
  }

  const UNIT_BADGE = {
    ready: ['ok', '已安装'],
    'soft-missing': ['must', '缺语音'],
    partial: ['no', '不完整'],
    missing: ['no', '未安装'],
  };


  /* ---------------------------------------------------------------- 基础数据
   * 就是"基础包"：浏览剧情**必须先有**的东西。现在它只有一个成员 ——
   * 官方资源清单（体积见 facts.CATALOG_BYTES）。索引/目录树/图片映射随程序自带，
   * 不需要下载。
   * 单独成块而不是混在剧情列表里，是因为它和"哪个角色"无关，属于全局前提。
   */
  function renderBasePack() {
    const box = $('#basePack');
    if (!box) return;
    const cdn = (ST.state && ST.state.cdn) || {};
    const ready = !!cdn.manifestReady;

    const pub = ST.pub || {};
    /* 「已就绪」的判齐口径（2026-11-04 用户反馈：头像 384/385、横幅 556/557 还写已就绪）：
       · 音乐 / 音效 / 头像 / 横幅 都是「一个包一张（批）图」的 1:1 关系 →
         必须**严格相等**。以前统一给 2% 余量，385 里差 1 个只有 0.26%，直接被当成齐了，
         于是新角色的头像/横幅没下也显示"已就绪"，「补全」按钮还被藏起来。
       · 背景例外：没核对过引用时分子分母是"包数口径"（601/612 —— 差的 11 个是
         没有任何已装剧情引用的包），这种差是正常的，所以未核对时保留 2% 余量；
         一旦后端按"引用视角"核对过（backVerified），也改成严格相等。 */
    const exact = (a, b) => (b ? (a || 0) >= b : true);
    const nearBg = (a, b) => (b ? (a || 0) >= b * 0.98 : true);
    const pubAny = (pub.back || 0) + (pub.music || 0) + (pub.sfx || 0)
      + (pub.avatar || 0) + (pub.banner || 0) + (pub.advfx || 0) > 0;
    // 后端结构版本：v<2 说明这个服务进程还是加 banner 之前的旧代码
    // （本地服务是常驻进程，改完 Python 必须重启程序；否则前端会看到缺字段，
    //   以前被 `|| 0` 兜成「横幅 0/0」，看起来像"没有横幅"）
    const pubVer = +(pub.v || 0);
    // v3 = 横幅改成"按图数、角色/章节分开"；低于它说明后端还是旧进程，
    // 缺 bannerChar/bannerStory 字段，别把缺字段当成 0
    const pubStale = pubAny && pubVer < 3;
    const cnt = (have, total) => (total == null ? '—'
      : (have || 0) + '/' + (total || 0));
    /* 「已就绪」还要求**引用到的图都不缺**：分母已经按引用算（见后端 public_status），
       但被引用却缺失的图会体现在分子上；单看比例还不够严，
       这里再卡一道 backRefMiss —— 缺一张就不该说"已就绪"。 */
    const backRefMiss = +(pub.backRefMiss || 0);
    const bgOk = (pub.backVerified ? exact : nearBg)(pub.back, pub.backTotal);
    /* 演出特效（表情符号逐帧数据 + 图集）是 v6 才有的类别：后端进程还没重启时
       没有 advfx 字段，此时不能判成"缺"，否则老用户会看到一个永远补不齐的 0/1。 */
    const advfxOk = pubVer >= 6 ? exact(pub.advfx, pub.advfxTotal) : true;
    const pubAll = ready && !pubStale && bgOk && backRefMiss === 0
      && exact(pub.music, pub.musicTotal) && exact(pub.sfx, pub.sfxTotal)
      && exact(pub.avatar, pub.avatarTotal) && exact(pub.banner, pub.bannerTotal)
      && advfxOk;

    // 清单描述去掉"已就绪"前缀 —— 状态统一由右侧徽标表达，别在一行里说两遍
    const mfTxt = (cdn.manifest || '').replace(/^已就绪\s*·?\s*/, '');

    /* 横幅要分两半写：角色是「一包一张图」（384），章节是「一个包里一批
       标题图」（主线/活动/其他共 172 张）。以前只报包数（384 + 5 = 389），
       看着就像"横幅一共 389 张"，跟实际对不上（用户 2026-11-02 反馈）。 */
    const bannerTxt = '横幅 ' + cnt(pub.banner, pub.bannerTotal)
      + (pub.bannerStoryTotal
        ? '（角色 ' + (pub.bannerChar || 0)
          + ' · 章节 ' + (pub.bannerStory || 0) + '）'
        : '');

    /* 下过就报实数；没下过给一个总量预估 —— 总量与分项都来自 facts.PUBLIC_PARTS，
       别在这里手抄"205 + 539 + 31 + 20 + 40"（改一处就漏一处）。 */
    /* 背景后面跟一句「另有 N 个未引用」：清单里有、但没有任何已装剧情用到的包不会
       导出成图，它们**不进分母**。不写这句的话，用户看到分母比清单包数小会以为漏了
       （2026-11-04 反馈：「背景明明有缺失，却显示已就绪」—— 那 11 个正是未被引用的包）。 */
    const backNote = (+pub.backUnused > 0)
      ? '（另有 ' + pub.backUnused + ' 个未引用）' : '';
    const pubLine = pubAny
      ? '背景 ' + cnt(pub.back, pub.backTotal) + backNote
      + ' · 音乐 ' + cnt(pub.music, pub.musicTotal)
      + ' · 音效 ' + cnt(pub.sfx, pub.sfxTotal)
      + ' · 头像 ' + cnt(pub.avatar, pub.avatarTotal)
      + ' · ' + bannerTxt
      + (pubVer >= 6 ? ' · 演出特效 ' + cnt(pub.advfx, pub.advfxTotal) : '')
      + (pubStale ? ' · <b style="color:#f0c05a">程序该重启了</b>' : '')
      : '约 <b>' + FACT.t('public') + '</b>（' + FACT.breakdown() + '）';

    // 「全部安装完」：所有剧情单元（角色 + 主线/活动/其他）既不是「未安装」也不是
    // 「不完整（解包中断）」。soft-missing（源缺语音）算装好了 —— 上一轮已当「最完整状态」。
    // 清单未就绪（ST.units 还是空）时 allUnits 长度为 0，不误判成"已全部安装"。
    const allUnits = (ST.units.char || []).concat(ST.units.adv || []);
    const allInstalled = allUnits.length > 0 && allUnits.every(function (k) {
      const s = stateOf(k);
      return s === 'ready' || s === 'soft-missing';
    });

    box.innerHTML =
      '<div class="bp-head"><b>基础数据（必需）</b>' +
      '</div>' +

      // ① 剧情目录
      '<div class="bp-row"><span class="nm">剧情目录</span>' +
      '<span class="dim">' +
      (ready ? esc(mfTxt || '已下载')
        : '约 ' + FACT.t('catalog') + ' · 决定"现在有哪些剧情能装"') +
      '</span>' +
      (ready ? '<span class="st ok">已就绪</span>'
        : '<button class="primary" id="bpGet">下载</button>') +
      '</div>' +

      // ② 公共素材
      '<div class="bp-row"><span class="nm">公共素材（背景图 / 背景音乐 / 音效 / 头像 / 剧情横幅）</span>' +
      '<span class="dim" title="背景按「已装剧情引用到的张数」算（= 已导出 + 引用但缺图）；'
      + '音乐/音效/头像按「已导出 / 清单里的包」算；'
      + '横幅按图片张数算（角色一包一张，主线/活动/其他的章节标题图一个包一批）。'
      + '清单里有、但没有任何已装剧情引用的背景包不会导出成图 —— 它们不进分母，'
      + '只在后面标注「另有 N 个未引用」；引用到了却没有图才会显示成缺。'
      + '官方更新后点「补全」只会下新增的，已有的跳过">' +
      pubLine + '</span>' +
      // 已就绪后不再给「更新」入口：公共素材的增量补齐已经并进
      // 「更新 → 刷新资源清单」了（那边检测到新增会自动补），
      // 这里再放一个按钮就是同一件事的第二个出口，用户会不知道点哪个。
      (pubAll
        ? '<span class="st ok">已就绪</span>'
        : '<button id="bpPub" class="' + (ready ? '' : 'primary') + '" '
          + 'title="已有的会跳过，重复点不会重下">'
          + (pubAny ? '补全缺失' : '下载') + '</button>') +
      '</div>' +

      // ③ 剧情本体
      '<div class="bp-row"><span class="nm">剧情本体（立绘 / 语音 / 剧本）</span>' +
      '<span class="dim">按剧情单独装，见下方列表</span>' +
      (allInstalled ? '<span class="st ok">已全部安装</span>'
        : '<span class="st none">按剧情</span>') + '</div>';

    /* 「补全」只补**缺的类别**（键名与后端 facts.PUBLIC_PARTS 一致）。
       以前这里一律不传 parts → 后端把 6 个类别全部重走一遍：明明只差一个
       演出特效，也要把 602 个背景包逐个打开检查、385 个头像强制重裁，
       几十分钟起步，进度条还得在 60% 停很久（用户 2026-11-10 反馈：
       "只缺特效也要全量重跑，那这个『补全』还有什么意义"）。
       已经齐的类别不传 → 连解包阶段都不会碰它们。 */
    const missParts = [];
    if (!(bgOk && backRefMiss === 0)) missParts.push('bg');
    if (!exact(pub.music, pub.musicTotal)) missParts.push('bgm');
    if (!exact(pub.sfx, pub.sfxTotal)) missParts.push('sfx');
    if (!exact(pub.avatar, pub.avatarTotal)) missParts.push('avatar');
    if (!exact(pub.banner, pub.bannerTotal)) missParts.push('banner');
    if (!advfxOk) missParts.push('advfx');
    const PART_CN = {
      bg: '背景', bgm: '音乐', sfx: '音效', avatar: '头像',
      banner: '剧情横幅', advfx: '演出特效',
    };
    const bp = $('#bpPub');
    if (bp) {
      bp.onclick = () => {
        const list = missParts.map(k => PART_CN[k] || k).join(' / ');
        if (!confirm('补齐公共素材？\n\n'
          + '· 这次只补缺的部分：' + (list || '全部')
          + '\n· 已经齐的类别不重跑 —— 不重新下载，也不重新解包'
          + '\n· 只差一两项时通常十几秒就完；缺头像会慢些（385 张要重裁）'
          + '\n· 期间可关掉页面，任务在后台继续\n\n开始？')) return;
        // 空对象 = 后端按默认全量（只有清单没读到时才会落到这里）
        startJob('install/base',
          missParts.length ? { parts: missParts } : {},
          '开始补齐公共素材：' + (list || '全部'));
      };
    }

    const ovc = $('#ovInstallCore');
    if (ovc) {
      ovc.hidden = !!ready;
      ovc.textContent = '下载基础数据';
      ovc.classList.toggle('primary', !ready);
    }

    const b = $('#bpGet');
    if (b) {
      b.onclick = () => {
        b.disabled = true;
        b.textContent = '下载中…';
        startJob('manifest/refresh', {}, '开始下载资源清单');
      };
    }
  }

  function renderPacks() {
    renderBasePack();
    const box = $('#packList');

    // ① 缺依赖：只提示怎么办，不在这里"帮用户装"
    //    （在服务进程里跑 pip 装完要重启才生效，用户会以为没修好；
    //      而且容易装进全局环境。正确入口是 启动.bat。）
    const dep = ((ST.state && ST.state.cdn) || {}).missingDeps || [];
    if (dep.length) {
      box.innerHTML = '<div style="padding:22px">' +
        '<div class="warn" style="margin:0 0 14px">' +
        '缺少运行依赖：<b>' + esc(dep.join('、')) + '</b><br>' +
        '没有它们无法解包 Unity 资源，装剧情会直接失败。</div>' +
        '<div style="line-height:2">' +
        '<b>用便携包的：</b>便携版自带这些依赖，出现这个通常是解压不完整、' +
        '或者被杀软隔离了某个文件 —— 请把整个文件夹加进杀软白名单，重新解压一次。<br>' +
        '<b>从源码克隆的：</b>关掉本窗口，双击 <code>启动.bat</code> ' +
        '（它会先测速选最快的镜像再装），或手动执行：<br>' +
        '<code style="color:#9fe0ff">pip install -r requirements.txt</code>' +
        '</div></div>';
      updateSelBar();
      return;
    }

    // ② 清单还没下载（仓库不附带，必须现拉最新的）
    if (ST.units.needManifest) {
      box.innerHTML = '<div style="padding:22px">' +
        '<div class="warn" style="margin:0 0 14px">还没有剧情目录。<br>' +
        '目录决定"现在有哪些剧情能装"，官方出新剧情它会变，所以不随程序附带，' +
        '需要先下载一次（约 ' + FACT.t('catalog') + '）。</div>' +
        '<button class="primary" id="btnGetManifest">下载剧情目录</button>' +
        '<div class="dim" style="margin-top:10px">约 ' + FACT.t('secCatalog')
        + '。</div></div>';
      $('#btnGetManifest').onclick = () => startJob('manifest/refresh', {},
        '开始更新剧情目录');
      updateSelBar();
      return;
    }

    if (!ST.units.loaded) {
      box.innerHTML = '<div style="padding:22px;color:#7b90a8">正在读取剧情目录…</div>';
      loadUnits().then(() => { renderPacks(); renderOverview(); })
        .catch(e => {
          box.innerHTML = '<div style="padding:22px;color:#ff9aa5">目录读取失败：' +
            esc(e.message) + '<br>点「更新 → 更新剧情目录」重试。</div>';
        });
      updateSelBar();
      return;
    }

    // 装好 / 补齐之后把"已经没问题"的从勾选里剔掉：否则「开始安装」一直是灰的，
    // 看着像卡住了（而 selSummary 又不认它们，用户只能手动取消勾选）
    ST.sel.forEach(function (k) { if (stateOf(k) === 'ready') ST.sel.delete(k); });

    ensurePackList().refresh();
    updateSelBar();
  }

  /* ---------------------------------------------------------------- 列表实例
   * ②③⑤ 三页的"挑剧情"列表都是 storylist.js 那一套（角色 / 主线 / 活动 / 其他，
   * 实装日期排序、安装状态筛选、稀有度 SSR/SR/R）。这里只负责给它数据和身份。
   */
  let packList = null;      // ② 剧情安装
  let trListEl = null;      // ⑤ 翻译（命名避开转写用的 trList 变量）
  let upListEl = null;      // ③ 提高CG画质
  /* ④ 导出视频的列表：**声明必须在这里**，不能挪到它自己的小节去 ——
     connectJobs() 在 ④ 之前就引用了它，若声明留在后面，一旦哪天变成
     同步调用就会踩 TDZ（Cannot access 'vidList' before initialization），
     而现在只是因为轮询回调是异步的才没炸。 */
  let vidList = null;

  function ensurePackList() {
    if (packList) return packList;
    packList = SL.create({
      bar: '#packBar', list: '#packList', sel: ST.sel, src: ST.cat || 'char',
      units: packUnits,
      status: stateOf,
      banners: bannersReady,
      /* 「其他」分类按入口分组（RAID BOSS / 広域戦 / 記念イベント …）+ 末尾「教程 / 开场」，
        与播放页侧栏完全一致；行名也用入口清单里的话名，不再显示 "其他剧情 第N话"。
        char / main / event 不分组（groupOf 返回 ''），保持平铺。 */
      name: k => otherEpsMaps().title[k] || SL.info(k).name,
      groupOf: k => {
        if (!(_otherEps || window.__OTHER_EPS)) return '';   // 入口清单未就绪：先平铺
        const m = /^adv_([a-z]+)/.exec(k);
        const kind = m ? m[1] : '';
        if (kind === 'tutorial' || kind === 'op') return 'tuto';
        if (kind === 'other') {
          const no = otherEpsMaps().keyNo[k];
          return no != null ? 'etc_' + no : 'etc_?';
        }
        return '';   // char / main / event 不分组
      },
      groupLabel: id => {
        if (id === 'tuto') return '教程 / 开场';
        if (id.indexOf('etc_') === 0) {
          // 横幅里已印着 RAID BOSS / 広域戦，名字里不再重复（和播放页侧栏一致）
          const nm = otherEpsMaps().noName[id.slice(4)] || '其他剧情';
          return nm.replace(/^RAID BOSS\s*/, '').replace(/^広域戦\s*/, '広域 ');
        }
        return id;
      },
      groupOrder: ids => {
        const order = otherEpsMaps().groups.map(g => 'etc_' + g.no).concat(['tuto']);
        return order.filter(x => ids.indexOf(x) >= 0);
      },
      // 入口横幅也是「公共素材」解包产物：没下过就不发图（否则「其他」一展开一片 404）。
      // 只用正数入口号：0=投票说明、-1=未归类（新剧情）没有游戏横幅，不显示。
      // 还要按本机清单核这张具体在不在 —— 整层"下过"不代表每个入口都导出了。
      groupBanner: id => {
        if (!bannersReady()) return '';
        if (id.indexOf('etc_') === 0) {
          const no = +id.slice(4);
          if (no > 0 && ESC.media.has('story', 'etc_' + no) !== false) {
            return 'media/img/story/etc_' + no + '.webp';
          }
        }
        return '';   // 教程 / 开场 无横幅
      },
      groupCollapsible: true,
      groupDefaultOpen: true,
      /* 不可勾选 = 已经"装到不能再装"两类：
         · ready        齐全（剧本+语音都在）
         · soft-missing 官方源本身就没有这语音包，重跑也补不出来（后端会直接跳过）
         这两类点「开始安装」都是白跑，所以直接禁勾。
         只有 partial（解包中断）能勾 —— 重新下载并解包即可修复。 */
      disabled: k => stateOf(k) === 'ready' || stateOf(k) === 'soft-missing',
      badge: k => {
        const st = stateOf(k);
        if (st === 'soft-missing') return { cls: 'must', text: softMissingLabel(k) };
        const b = UNIT_BADGE[st] || UNIT_BADGE.missing;
        return { cls: b[0], text: b[1] };
      },
      rowTitle: k => {
        const st = stateOf(k);
        if (st === 'soft-missing') {
          return '剧本与立绘已装好、能正常播放，但' + softMissingLabel(k)
            + '—— 已是最完整状态，无法补齐';
        }
        if (st === 'partial') {
          return '剧本文件异常小（上次解包中断）—— 勾上后点「开始安装」重下并重新解包';
        }
        return '';
      },
      actions: k => {
        const st = stateOf(k);
        if (st === 'missing') return [];
        const out = [{ act: 'rm', label: '卸载', cls: 'rm', title: '删除已解包的资源，释放空间' }];
        // 只给「不完整（解包中断）」补齐：重新下载并解包即可修复。
        // 「软缺（缺语音）」是官方源里压根没有这语音包，重跑也补不出来，
        // 后端会直接跳过 —— 这里不再挂补齐按钮，免得看着像"还没装完"。
        if (st === 'partial') {
          out.unshift({ act: 'fill', label: '补齐', cls: 'ok',
            title: '只重新下载缺的包并重新解包，不用整个重装' });
        }
        return out;
      },
      onAction: (k, act) => {
        // 补齐：和「开始安装」走同一个任务，只是这一部、只补缺的
        if (act === 'fill') {
          runInstall([k], '正在补齐「' + unitName(k) + '」缺的部分…');
          return;
        }
        if (act !== 'rm') return;
        if (!confirm('卸载「' + unitName(k) + '」？\n\n'
          + '会删除：剧本、语音、头像、该角色独占的立绘（通常几十 MB）。\n'
          + '背景 / CG 可能被别的剧情共用，会保留。\n\n'
          + '资源包缓存会保留 —— 以后重装不用重新下载。')) return;
        startJob('uninstall', { units: [k] }, '正在卸载…');
      },
      onChange: () => updateSelBar(),
    });
    // 「其他」分类要按入口分组，依赖入口清单；清单就绪后重画一次让分组生效
    ensureOtherEps().then(() => { if (packList) packList.refresh(); });
    return packList;
  }

  /** 勾选统计：fresh = 全新安装，fill = 已装但可修复（只有 partial 解包中断）。
   *  soft-missing 不算 fill：官方源缺语音，补不出来，禁勾且不应计入「补齐」。 */
  function selSummary() {
    const fresh = [];
    const fill = [];
    ST.sel.forEach(function (k) {
      const st = stateOf(k);
      if (st === 'missing') fresh.push(k);
      else if (st === 'partial') fill.push(k);
    });
    return { fresh: fresh.length, fill: fill.length,
             todo: fresh.length + fill.length,
             keys: fresh.concat(fill) };
  }

  function updateSelBar() {
    const s = selSummary();
    const bar = $('#selInfo');
    if (!bar) return;
    if (s.fresh && s.fill) {
      bar.innerHTML = '将安装 <b>' + s.fresh + '</b> 部 · 补齐 <b>' + s.fill +
        '</b> 部 · 约 <b>' + FACT.unitsSize(s.fresh) +
        '</b>（补齐只下缺的包，通常几 MB）';
    } else if (s.fresh) {
      bar.innerHTML = '将安装 <b>' + s.fresh + '</b> 部剧情 · 约 <b>' +
        FACT.unitsSize(s.fresh) + '</b>（按每部 ' + FACT.t('unitEst') + ' 估算）';
    } else if (s.fill) {
      bar.innerHTML = '将补齐 <b>' + s.fill +
        '</b> 部剧情缺的语音 / 图片（只下缺的包，通常几 MB）';
    } else {
      bar.innerHTML = ST.sel.size
        ? '选中的都装好了（要重装先卸载）'
        : '未选择';
    }
    const btn = $('#btnInstall');
    if (btn) btn.disabled = !s.todo;
  }

  if ($('#pickAll')) {
    $('#pickAll').onclick = () => {
      // 只勾"当前分类 + 当前筛选"里还没装的那些（全选不会越界到别的分类）
      ensurePackList().selectAll();
      updateSelBar();
    };
  }
  if ($('#pickNone')) {
    $('#pickNone').onclick = () => {
      ST.sel.clear();
      ensurePackList().refresh();
      updateSelBar();
    };
  }
  if ($('#optSoft')) $('#optSoft').hidden = true;
  if ($('#optWebp')) $('#optWebp').closest('.optrow').hidden = true;

  function updateThreadHint() {
    // 打开页面时用「设置」里的默认并发，两处不要各写各的
  if ($('#optThreads') && !$('#optThreads').dataset.touched) {
    const cfg = (ST.state && ST.state.config) || {};
    if (cfg.threads) $('#optThreads').value = String(cfg.threads);
  }
  const n = +$('#optThreads').value;
    const map = {
      8: '最稳，但最慢（网络很差时选这个）',
      16: '比较稳，速度中等',
      32: '最快最省时间',
      64: '最快，但网络不稳时容易失败',
    };
    $('#threadHint').textContent = map[n] || '';
  }
  if ($('#optThreads')) {
    $('#optThreads').onchange = () => {
      $('#optThreads').dataset.touched = '1';   // 用户改过就不再被设置值覆盖
      updateThreadHint();
    };
    updateThreadHint();
  }

  // ---------------------------------------------------------------- 安装
  /** 提交一次安装任务。安装与「补齐」走同一个后端流程：
   *  下载层按磁盘上已有文件跳过，所以对已装的剧情重跑只会补缺的包。 */
  async function runInstall(units, msg, withUpscale) {
    const threads = (+($('#optThreads') || {}).value) || 32;
    try {
      await api('download', {
        method: 'POST',
        body: JSON.stringify({
          selected: units,
          threads: threads,
          upscale: withUpscale === false ? false
            : ($('#optUpscale') ? $('#optUpscale').checked : false),
          scale: 2,
        }),
      });
      toastMsg(msg || '已开始安装，进度见底部');
      connectJobs(true);
      return true;
    } catch (e) {
      toastMsg('启动失败：' + e.message, true);
      return false;
    }
  }

  $('#btnInstall').onclick = async () => {
    const s = selSummary();
    if (!s.todo) return;
    const threads = +$('#optThreads').value;
    const what = [];
    if (s.fresh) what.push('安装 ' + s.fresh + ' 部（约 ' + FACT.unitsSize(s.fresh) + '）');
    if (s.fill) what.push('补齐 ' + s.fill + ' 部（解包中断，只下缺的包重新解包）');
    if (!confirm('即将' + what.join(' + ') + '。\n\n' +
      '· 每部角色剧情约 ' + FACT.t('unitEach') + '（语音 + 背景 + 立绘 + CG）\n' +
      '· 下载速度：同时下 ' + threads + ' 个\n' +
      '· 单部约 ' + FACT.t('secUnit') + '装好\n' +
      '· 已经下过的文件会跳过，重复点不会重新下载\n\n开始？')) return;
    runInstall(s.keys, '已开始安装，进度见底部');
  };

  // ---------------------------------------------------------------- 任务
  /* 用轮询而不是 SSE：SSE 是长连接，会让"页面加载完成"永远悬着
     （headless 截图、某些代理/杀软都会卡住），而任务进度 1 秒一次完全够用。
     页面隐藏时停轮询，切回来立刻补一次。 */
  let jobTimer = null;
  function connectJobs(now) {
    if (jobTimer && !now) return;
    if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }
    const tick = async () => {
      if (document.hidden) return;
      try {
        const d = await api('jobs');
        ST._fail = 0;
        // 服务重启过（程序更新：主进程退出 → 更新器覆盖文件 → 拉起新进程）→
        // 当前页面已经是旧 DOM，必须自动刷新，否则用户看到的是"更新没生效"。
        if (d.boot) {
          if (!ST.boot) {
            ST.boot = d.boot;
          } else if (ST.boot !== d.boot && !ST._reloading) {
            ST._reloading = true;
            ST.boot = d.boot;
            const v = d.version ? ' v' + d.version : '';
            toastMsg('程序已重启' + v + '，正在刷新页面…');
            notify('程序已重启' + v + ' → 自动刷新页面', 'ok');
            setTimeout(function () { location.reload(); }, 1500);
            return;
          }
        }
        renderJobs(d.jobs || []);
        syncActBottom();          // 任务条高度变了，浮窗跟着抬高
      } catch (e) {
        // 连续取不到任务：更新期间会短暂这样（主进程退出、新进程还没起来）。
        // 超过约 15 秒仍不恢复，就明确告诉用户该手动重启，而不是让页面干等。
        // console 也留一条：以前这里静默吞异常，界面不动、日志里也没有，
        // 排查只能靠猜（"操作记录不刷新"就是这样被埋掉的）。
        try { console.error('[jobs] 轮询失败：', e); } catch (e2) { }
        ST._fail = (ST._fail || 0) + 1;
        if (ST._fail === 1) console.warn('[jobs] 第一次轮询就失败，界面可能不刷新');
        if (ST._fail === 13) {
          notify('本地服务已停止。若一直不恢复，请关掉启动窗口后重新双击「启动.bat」',
            'warn');
        }
      }
    };
    tick();
    jobTimer = setInterval(tick, 1200);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  }

  function renderJobs(list) {
    const bar = $('#jobbar');
    /* 任务刚结束 → 重拉状态。
       这段原先放在函数末尾，用的还是被 filter + slice(0,3) 裁过的列表：
       底部条只留最近 3 条，被裁掉的终态任务就永远触发不了刷新 ——
       「下载公共素材」明明完成了，页面上的「补全缺失」按钮还亮着、
       数字也还是旧的，得手动刷新页面才对（2026-11-10 用户反馈）。
       现在用**原始列表**，并放在下面所有 return 之前。 */
    ST._reloaded = ST._reloaded || {};
    const ended = (list || []).filter(j => (j.state === 'done' || j.state === 'failed')
      && !ST._reloaded[j.id]);
    if (ended.length) {
      ended.forEach(j => { ST._reloaded[j.id] = 1; });
      if (Object.keys(ST._reloaded).length > 200) ST._reloaded = {};
      /* 公共素材（kind=base）收尾：单独重拉一次 public 并重画「基础数据」那几行。
         不等下面那次整体 loadAll —— 整体刷新任何一步失败都会让它白等，
         而这几个数字就是用户判断"能不能开始看"的依据。 */
      if (ended.some(j => j.kind === 'base')) {
        api('cdn/public').then(p => {
          if (!p || p.error) return;
          ST.pub = p;
          renderPacks();
        }).catch(() => { });
      }
      if (!ST._refreshT) {
        ST._refreshT = setTimeout(() => {
          ST._refreshT = null;
          reloadAfterJobs();
        }, 1200);
      }
    }
    // 扫描/清理任务的结果（任务本身几秒后会从底部条消失，结果要留在页面上）
    const pr = (list || []).find(j => j.kind === 'prune' && j.state === 'done'
      && j.detail && j.detail.prune);
    if (pr) {
      const r = pr.detail.prune;
      const info = $('#pruneInfo');
      if (info) {
        info.innerHTML = r.unused
          ? '发现 <b>' + r.unused + '</b> 个未引用的包，共 <b>' + human(r.bytes) + '</b>'
            + (r.deleted ? '（已删除 ' + r.deleted + ' 个）' : '')
          : '没有多余的缓存，很干净';
      }
      const g = $('#btnPruneGo');
      if (g) g.hidden = !(r.unused && r.dry);
      if (!r.dry && ST._pruneDone !== pr.id) {
        ST._pruneDone = pr.id;
        _space = null;
        loadStats(true);
      }
    }

    // 清理空间的执行结果（任务几秒后从底部条消失，结论要留在页面上）
    const cl = (list || []).find(j => j.kind === 'cleanup' && j.state === 'done'
      && j.detail && j.detail.clean);
    if (cl) {
      const r = cl.detail.clean;
      const box = $('#cleanResult');
      if (box) {
        box.innerHTML = '上次清理：<b>' + esc(r.label || r.what) + '</b> —— '
          + (r.dry
            ? '预演，共 ' + r.files + ' 个文件 / ' + human(r.bytes) + '（未删除）'
            : '删除 ' + r.deleted + ' 个文件，释放 <b>' + human(r.bytes) + '</b>'
              + (r.skipped ? '，跳过 ' + r.skipped + ' 个（被占用）' : ''));
      }
      // 清理完数字肯定变了：强制重算一次（否则页面还显示清理前的占用）
      if (ST._cleanDone !== cl.id) {
        ST._cleanDone = cl.id;
        _space = null;
        loadStats(true);
      }
    }

    // 批量导入译文：任务跑完把汇总写进导入面板（任务几秒后从底部条消失，
    // 结论要留在页面上）。进行中底部进度条已经实时显示「已导入 i/n」。
    const imp = (list || []).find(j => j.kind === 'imptrans' && j.state === 'done'
      && j.detail && j.detail.result);
    if (imp && ST._impDone !== imp.id) {
      ST._impDone = imp.id;
      trRenderBulk(imp.detail.result);
    }
    const impF = (list || []).find(j => j.kind === 'imptrans' && j.state === 'failed');
    if (impF && ST._impFail !== impF.id) {
      ST._impFail = impF.id;
      $('#trResult').textContent = '失败：' + (impF.error || '未知错误');
    }

    // 「重新登记高清图」跑完：共登记了多少张要留在页面上 —— 这个按钮点完
    // 只有一两秒，底部条一闪就没了，用户会怀疑"到底有没有生效"。
    // 只认**用户刚点的这一次**（created 在点击之后）：不然页面一打开，
    // 历史任务里的那条"已登记 N 张"会自己冒出来，像是刚刚才跑过。
    const rbAsk = ST._rbAskAt || Infinity;
    const rb = (list || []).find(j => j.kind === 'rebuild' && j.state === 'done'
      && j.created >= rbAsk);
    if (rb && ST._rbDone !== rb.id) {
      ST._rbDone = rb.id;
      const info = $('#x2RebuildInfo');
      if (info) info.innerHTML = '<b>' + esc(rb.message || '已完成') + '</b>';
      _space = null;                       // 「高清化进度」的数字要重算
      loadStats(true);
    }
    const rbF = (list || []).find(j => j.kind === 'rebuild' && j.state === 'failed'
      && j.created >= rbAsk);
    if (rbF && ST._rbFail !== rbF.id) {
      ST._rbFail = rbF.id;
      const info = $('#x2RebuildInfo');
      if (info) info.textContent = '没成功：' + (rbF.error || '未知错误');
    }

    // 导出任务「刚跑完」→ 按用户设置自动弹出文件夹。
    // **必须按状态跃迁判断**：以前是"列表里存在任意 done 的 video/cg 任务"就打开，
    // 而去重标记 ST._expShown 只在内存里 —— 每次刷新/重进页面都会把历史上的
    // 已完成任务当成"刚完成"，于是无缘无故自动弹一次文件夹。
    ST._jobSeen = ST._jobSeen || {};
    const jobsNow = list || [];
    for (const j of jobsNow) {
      if (!j || !j.id) continue;
      const prev = ST._jobSeen[j.id];
      ST._jobSeen[j.id] = j.state;
      if (prev === undefined) {
        // 首次见到：只有"正在跑"的才记为本次操作（页面打开时列表里那些历史
        // 已完成的任务不再回顾，否则一进页面就刷一屏）。
        // 但**刚刚失败的必须补一条**：清单过期 / 缺依赖 / 找不到这个角色的包
        // 这类错误是秒级结束的，页面两次轮询之间就失败了 —— 只认 running 的话，
        // 用户会遇到"报错了，但操作记录里一个字都没有"（2026-11-06 反馈）。
        if (j.state === 'running' || j.state === 'queued') {
          notify('开始：' + (j.title || j.kind || '任务'), 'info');
        } else if (j.state === 'failed' && j.ended
          && (Date.now() / 1000 - j.ended) < 90) {
          notify('失败：' + (j.title || j.kind || '任务')
            + (j.error ? '：' + j.error : ''), 'err',
            (j.detail && j.detail.diag && j.detail.diag.text) || '');
        }
        continue;
      }
      if (prev === j.state) continue;                        // 状态没变 → 不动
      const ttl = j.title || j.kind || '任务';
      // 导出类任务的结论由 afterExportDone 统一写（要带上文件名和保存位置），
      // 这里只报"开始 / 失败"
      const isExp = (j.kind === 'video' || j.kind === 'cg' || j.kind === 'transzip');
      if (j.state === 'running') notify('开始：' + ttl, 'info');
      else if (j.state === 'done') {
        if (!isExp) {
          let msg = '完成：' + ttl;
          // 安装类任务：把「多少部剧情的 剧情/语音/立绘 已就绪」写进操作记录
          const bd = j.detail && j.detail.breakdown;
          if (bd) {
            msg += ` · 剧情${bd.story}/${bd.t_story}`
                 + ` · 语音${bd.voice}/${bd.t_voice}`
                 + ` · 立绘${bd.stand}/${bd.t_stand}`;
          }
          notify(msg, 'ok');
        }
      }
      else if (j.state === 'failed') {
        // 失败时把后端生成的「环境指纹」一起写进操作记录：
        // 版本 / 系统 / 路径特征 / 盘余 / 卡在哪一步 —— 截图就能判断方向，
        // 不必再让用户去翻日志文件。
        const dg = (j.detail && j.detail.diag) || null;
        notify('失败：' + ttl + (j.error ? '：' + j.error : ''), 'err',
          (dg && dg.text) || '');
      }
      if (j.state === 'done' && isExp) afterExportDone(j);
    }
    // 清掉已经不在列表里的 id，别让这个表无限膨胀
    const alive = new Set(jobsNow.map(j => j && j.id));
    for (const id of Object.keys(ST._jobSeen)) {
      if (!alive.has(id)) delete ST._jobSeen[id];
    }
    // 自动更新失败（所有线路不通）→ 给可点的 GitHub 链接与手动步骤
    const up = (list || []).find(j => j.kind === 'update' && j.detail && j.detail.manual);
    if (up && ST._manualJob !== up.id) {
      ST._manualJob = up.id;
      const m = up.detail.manual;
      const box = $('#updResult');
      if (box) {
        box.innerHTML =
          '<div class="warn" style="margin:12px 0;line-height:1.9">' +
          '<b>自动更新失败</b>（' + esc(m.reason || '内置线路都不通') + '）<br>' +
          '已停止重试，不再让你干等。请手动下载：' +
          '<ol style="margin:8px 0 4px 20px">' +
          '<li>打开 <a href="' + esc(m.page || '#') + '" target="_blank" ' +
          'rel="noopener">' + esc(m.page || 'Releases 页面') + '</a></li>' +
          '<li>下载 <code>' + esc(m.file || '') + '</code>' +
          '（更新包 ' + FACT.t('update') + '；也可下全量包）</li>' +
          '<li>解压后覆盖到程序目录 —— <code>userdata/</code>、素材、译文都不会被覆盖 —— ' +
          '再双击 <code>启动.bat</code></li>' +
          '</ol></div>';
      }
    }
    // 有「清单」任务在跑 → 把所有"下载/刷新清单"入口禁掉，避免用户连点、
    // 每次都排一个重复任务（之前可以连出好几个一模一样的）。
    const mfRunning = (list || []).some(
      j => j.kind === 'manifest' && (j.state === 'running' || j.state === 'queued'));
    ['#bpGet', '#btnRefreshManifest', '#ovInstallCore'].forEach(sel => {
      const el = $(sel);
      if (!el) return;
      if (mfRunning) {
        el.disabled = true;
        el.dataset.autoOff = '1';
      } else if (el.dataset.autoOff) {
        el.disabled = false;
        delete el.dataset.autoOff;
      }
    });

    // 「刷新清单与角色信息」是同一个任务：把第二步的结果也显示出来
    const mfJob = (list || []).find(j => j.kind === 'manifest' && j.state === 'done'
      && j.detail && (j.detail.scraped || j.detail.metaError));
    if (mfJob && ST._metaShown !== mfJob.id) {
      ST._metaShown = mfJob.id;
      const md = mfJob.detail;
      const box = $('#wikiResult');
      if (md.scraped) {
        if (box) box.textContent = '角色资料已更新 ' + md.scraped + ' 条';
      } else if (box) {
        box.textContent = '角色资料没更新成功（详情见底部「操作记录」）';
        box.title = md.wiki_error || md.metaError || '';
        if (md.proxy_needed) {
          const tip = $('#wikiProxyTip');
          if (tip) tip.style.display = 'block';
        }
      }
    }
    // 从 GitHub / Wiki 更新角色信息：只有用户点按钮才会跑，不做后台自动请求。
    // 连不上（GitHub 与 Wiki 都失败）时 → 提示开代理补下载；成功 → 显示条数
    const wk = (list || []).find(j => j.kind === 'wiki');
    const wtip = $('#wikiProxyTip');
    const wres = $('#wikiResult');
    if (wk) {
      const d = wk.detail || {};
      if (wk.state === 'failed') {
        // GitHub 和直连 Wiki 都失败才会到这；直连 Wiki 失败才可能 proxy_needed
        if (d.proxy_needed && wtip) wtip.style.display = 'block';
        if (wres) {
          wres.textContent = '没获取到角色资料（详情见底部「操作记录」）';
          wres.title = d.wiki_error || '';
        }
      } else if (wk.state === 'done') {
        if (wtip) wtip.style.display = 'none';
        if (wres) wres.textContent = '角色资料已更新 ' + (d.scraped || '') + ' 条';
      } else if (wk.state === 'running' || wk.state === 'queued') {
        if (wres) wres.textContent = '正在获取角色资料…';
      }
    }

    // 安装 / 补齐任务「刚到终态」→ 必须重拉单元状态并刷新所有列表。
    // 否则 stateOf 还停留在装之前的旧值，补齐/卸载按钮永远亮着、还能重复点，
    // 用户根本看不出"到底装没装上、是不是软缺（源里没语音）"。后端其实装成功了，
    // 只是前端没刷新而已。每个任务只刷一次（用 id 去重）。
    const instJob = (list || []).find(
      j => j.kind === 'install' && (j.state === 'done' || j.state === 'failed'));
    if (instJob && ST._instJobId !== instJob.id) {
      ST._instJobId = instJob.id;
      loadUnits(true).then(() => {
        if (packList) packList.refresh();
        if (upListEl) upListEl.refresh();
        if (trListEl) trListEl.refresh();
        if (vidList) vidList.refresh();
        updateSelBar();
        renderOverview();
        renderBasePack();   // 装完后「剧情本体」行可能从「按剧情」变「已全部安装」
      }).catch(() => { });
    }

    if (!list || !list.length) { bar.innerHTML = ''; return; }
    const now = Date.now() / 1000;
    // 只有"还在跑"的常驻；结束的也在条上多停一会儿再收：
    // 以前 done 只留 6 秒，刚看到"完成"两个字就没了，用户根本来不及读。
    // 现在完成留 15 秒、失败留 40 秒（失败更要看得见）；结果同时写进
    // 操作记录和页面上的固定位置，不依赖这一条。
    // 另外以前只过滤 done，导致**取消的任务永远挂在底部条上**不走。
    list = list.filter(j => {
      if (j.state === 'running' || j.state === 'queued') return true;
      const endAt = (j.ended || (j.started || j.created) + (j.elapsed || 0));
      const keep = j.state === 'failed' ? 40 : 15;
      return now - endAt < keep;
    }).slice(0, 3);
    if (!list.length) { bar.innerHTML = ''; return; }
    bar.innerHTML = list.map(j => {
      const pct = j.state === 'done' ? 100 : (j.percent || 0);
      const eta = (j.eta && j.state === 'running') ? j.eta : 0;
      /* 是不是"下载之后"的阶段（解包）：后端 phase(label="解包") 会把字节口径
         清空并给出 phaseLabel。这个判断要早于进度条渲染 —— 解包阶段要给
         进度条换颜色、并在 60% 处画阶段分隔线。 */
      const extracting = !(j.bytesTotal) && !!(j.phaseLabel);
      const cls = [j.state === 'failed' ? 'failed' : '',
        j.state === 'done' ? 'done' : '',
        j.paused ? 'paused' : '',
        extracting ? 'extracting' : ''].filter(Boolean).join(' ');
      // 速度只在右侧数值区显示一次（之前这里也拼了一次，导致两处重复、把布局挤乱）
      // 结束态前面加个符号 + 加亮（见 setup.css .job.done / .failed .jm）：
      // 以前成功和失败都是一行淡灰字，扫一眼分不出结果
      const msg = j.error ? ('⚠ ' + j.error)
        : (j.state === 'done' ? '✓ ' + (j.message || '完成') : (j.message || ''));
      // 进度条要说清四件事：百分比、已下/总量（字节与个数）、速度、剩余
      const bd = j.bytesDone || 0;
      const bt = j.bytesTotal || 0;
      /* 字节是否已经下完（剩下的阶段只有解包 / 建索引，不再有网络流量）。
         下完时：
           · 不写「38.93 MB / 38.93 MB」—— 它和左边的 47.5% 互相打架，看着像"下完了
             却卡住"；改成「已下载 38.93 MB」；
           · 不报网速 —— 以前后端把整段平均速度一直带进索引阶段，数字还在跳，
             用户会以为程序在偷跑流量（2026-11-04 反馈："像中了木马"）。 */
      const bDone = bt > 0 && bd >= bt;
      const bsp = bDone ? 0 : (j.bytesSpeed || 0);
      /* 非下载阶段说明：这时**一个字节都不在传**，网速必然是 0。若还照下载
         阶段的样子显示「60.0% · 0 B/s」，用户会以为"下载停了但没到 100%，
         是不是卡住了"。所以这一段改报「解包 120/2100 个」，并且不显示网速与
         剩余时间（解包按包数估的剩余，大小包差异太大，估出来多半是错的）。

         字节那一行也必须自己说清"这是下载量"：进度条把前 60% 留给下载、
         后 40% 留给解包，所以「900 MB / 1 GB」旁边配的百分比只有 54%。
         不写"下载"两个字，用户会以为进度算错了（2026-11-10 反馈）。
         进度条上另有一条分隔线标出 60% 的边界（见下面 seg）。 */
      const line1 = [pct.toFixed(1) + '%',
        (bDone ? `下载完成 ${human(bd)}`
          : (bt ? `下载 ${human(bd)} / ${human(bt)}` : ''))].filter(Boolean).join(' · ');
      // 次级行：多单元安装优先显示「第 i/N 部」+ 当前这批「包 x/y」，
      // 这样装 70 部剧情时进度条只从 0 平稳爬到 100%，不再反复冲到 100%。
      const parts = [];
      if (j.itemsTotal && j.itemsTotal > 1) {
        parts.push(`第 ${j.itemsDone || 0}/${j.itemsTotal} 部`);
      }
      if (j.meta && j.meta.unit) {
        parts.push(esc(j.meta.unit));
      }
      if (j.subTotal && j.subTotal > 0) {
        parts.push(`包 ${Math.round(j.subDone)}/${Math.round(j.subTotal)}`);
      }
      /* 单单元 / 公共素材：退回「个数 x/y」显示。
         **必须由后端明确给出单位才显示**（j.unit 非空）：以前缺单位就按"个"
         兜底，于是下载任务把字节总量当个数渲染成「0/933450 个」—— 一个
         0.9 MB 的应用更新报出九十多万个文件，用户只会怀疑中病毒。
         计数单位现在由 set_total(n, "个"/"张"/"部"/"包") 明确声明，
         字节量只走 bytesDone/bytesTotal（就是左边那行 x MB / y MB）。 */
      if ((!j.itemsTotal || j.itemsTotal <= 1) && j.total && j.unit) {
        parts.push((extracting ? j.phaseLabel + ' ' : '')
          + `${Math.round(j.done)}/${Math.round(j.total)} ${j.unit}`);
      }
      if (!extracting) {
        if (bsp) parts.push(`${human(bsp)}/s`);
        if (eta) parts.push(`剩 ${secs(eta)}`);
      }
      const line2 = parts.filter(Boolean).join(' · ');
      /* 阶段分隔线：下载占前 60%、解包占后 40%（后端 phaseBase/phaseSpan 给的）。
         没有它的话，用户看到「下载 900 MB / 1 GB」而进度条才 54%，
         会以为百分比算错了（2026-11-10 反馈）。有了这条线，
         "前一段是下载、后一段是解包"一眼能看出来。 */
      const segBase = (j.phaseSpan > 0 && j.phaseSpan < 100) ? (j.phaseBase || 0) : 0;
      const seg = segBase ? `<u style="left:${segBase}%"></u>` : '';
      const segTip = segBase
        ? `前 ${Math.round(segBase)}% 是下载，后 ${Math.round(100 - segBase)}% 是解包`
          + '（解包＝把资源包解成图片和音频，不走网络）'
        : '';
      return `<div class="job${cls ? ' ' + cls : ''}">
        <span class="jt">${esc(j.title)}</span>
        <span class="jm" title="${esc(msg)}">${esc(msg)}</span>
        <span class="jp"${segTip ? ` title="${esc(segTip)}"` : ''}${segBase ? ` style="--seg:${segBase}%"` : ''}><i style="width:${pct}%"></i>${seg}</span>
        <span class="jv"><b>${line1 || '0.0%'}</b>${
          line2 ? `<i>${esc(line2)}</i>` : ''}</span>
        ${j.state === 'running' || j.state === 'queued'
          ? `<button class="jx" data-cancel="${esc(j.id)}" title="停止这个任务">取消</button>`
        + (j.state === 'running'
          ? `<button class="jx jpz" data-pause="${esc(j.id)}"
             data-on="${j.paused ? '1' : ''}">${j.paused ? '继续' : '暂停'}</button>` : '')
      : ''}
      </div>`;
    }).join('');
    // 暂停 / 继续（软暂停：已完成的包不回退，正在传的会传完）
    bar.querySelectorAll('[data-pause]').forEach(b => {
      b.onclick = () => {
        const paused = b.dataset.on === '1';
        api('jobs/' + b.dataset.pause + (paused ? '/resume' : '/pause'),
          { method: 'POST' })
          .then(() => toastMsg(paused ? '已继续下载' : '已暂停（正在传的包会传完）'))
          .catch(e => toastMsg(e.message, true));
      };
    });
    bar.querySelectorAll('[data-cancel]').forEach(b => {
      b.onclick = () => api('jobs/' + b.dataset.cancel + '/cancel', { method: 'POST' })
        .then(() => toastMsg('已请求取消')).catch(e => toastMsg(e.message, true));
    });
    /* 终态任务的刷新逻辑已移到函数开头（见那里的注释）：
       放在这里的话，列表被 slice(0,3) 裁掉的任务永远触发不了。 */
  }

  // ---------------------------------------------------------------- 画质
  /* ③ 也有一个"挑剧情"列表：不选 = 处理全部已装剧情（老行为），
     选了就只超分这几部剧情**引用到的图**（后端按 data/<unit>.js 里的
     立绘 / 背景 / CG 引用算，不会多跑一张）。 */
  ST.upSel = ST.upSel || new Set();

  function updateUpInfo() {
    const el = $('#upSelInfo');
    if (!el) return;
    el.innerHTML = ST.upSel.size
      ? '只处理所选 <b>' + ST.upSel.size + '</b> 部剧情引用到的图'
      : '未选择：处理全部已装剧情';
  }

  function ensureUpList() {
    if (upListEl) return upListEl;
    upListEl = SL.create({
      bar: '#upBar', list: '#upList', sel: ST.upSel, src: 'char',
      units: packUnits,
      status: stateOf,
      banners: bannersReady,
      disabled: k => stateOf(k) === 'missing',   // 没装的没图可超分
      badge: k => {
        const st = stateOf(k);
        if (st === 'soft-missing') return { cls: 'must', text: softMissingLabel(k) };
        const b = UNIT_BADGE[st] || UNIT_BADGE.missing;
        return { cls: b[0], text: b[1] };
      },
      onChange: () => { updateUpInfo(); refreshPlan(); },
      empty: () => '还没有已安装的剧情 —— 先到「剧情安装」装一部再做高清',
    });
    return upListEl;
  }

  function renderQuality() {
    const eng = (ST.state && ST.state.engine) || {};
    const el = $('#engineInfo');
    const okEng = !!eng.kind && eng.kind !== 'none';
    const okPy = !!(ST.state && ST.state.pillow);
    if (el) {
      el.textContent = (okEng && okPy)
        ? '高清化引擎已就绪，可以放心用'
        : (okEng ? '⚠ 缺少一个组件（Pillow），暂时不能超分'
          : '⚠ 这台电脑上没找到高清化引擎，暂时不能超分');
      // 引擎名和参数只放在鼠标悬停里，正文不展示
      el.title = (eng.note || '未知引擎') + (okPy ? ' / Pillow 已就绪' : ' / 缺少 Pillow');
    }
    if (ST.units.loaded) {
      ensureUpList().refresh();
    } else {
      loadUnits().then(() => {
        if (upListEl) upListEl.refresh();
        updateUpInfo();
        refreshPlan();
        cmpButtonHint();
      }).catch(() => { });
    }
    updateUpInfo();
    refreshPlan();
    cmpButtonHint();
  }

  /** 按钮副标题跟着"到底有没有装过剧情"变：一部都没装就别让人白点一回 */
  function cmpButtonHint() {
    const btn = $('#btnCmpDemo');
    if (!btn || !ST.units.loaded) return;       // 列表还没回来就别乱改文案
    const st = ST.units.state || {};
    const n = Object.keys(st).filter(k => st[k] && st[k] !== 'missing').length;
    const sm = btn.querySelector('small');
    if (sm) sm.textContent = n ? '左边原图 · 右边高清' : '要先装一部剧情';
    btn.title = n ? '左边原图，右边超分 2 倍，中间可拖动'
      : '还没装剧情 —— 点一下会告诉你去哪装';
  }

  /* 超分预估：多少张 / 原图多大 / 放大到多少 / 估计要多久
     超分是最容易让人后悔的操作（几分钟、CPU 满载），光写"很慢"没说服力。 */
  /** 当前"只处理这些剧情"的选择（空数组 = 全部已装剧情） */
  function upUnitsParam() {
    return Array.from(ST.upSel || []);
  }

  async function refreshPlan() {
    const box = $('#upPlan');
    if (!box) return;
    const kinds = $$('.upk').filter(c => c.checked).map(c => c.value);
    if (!kinds.length) {
      box.innerHTML = '<span class="dim">没有选择处理范围</span>';
      ST._plan = null;
      return;
    }
    box.innerHTML = '<span class="dim">统计中…</span>';
    const units = upUnitsParam();
    try {
      const d = await api('upscale/plan?kinds='
        + encodeURIComponent(kinds.join(','))
        + '&scale=' + encodeURIComponent($('#upScale').value)
        + (units.length ? '&units=' + encodeURIComponent(units.join(',')) : ''));
      ST._plan = d;
      if (!d.total) {
        box.innerHTML = '<span class="dim">'
          + esc(d.hint || '没有需要超分的图（已做过的会自动跳过）') + '</span>';
        return;
      }
      const LAB = { stand: '立绘', back: '背景', cg: 'CG', standfull: '全身立绘' };
      const by = Object.keys(d.byKind || {})
        .map(k => (LAB[k] || k) + ' ' + d.byKind[k]).join(' · ');
      // 每类图尺寸完全不同（立绘多为 300×628，背景/CG 是 1280×720）。
      // 以前只拿一张样本报"原图 W×H"，等于把一张图的尺寸说成全部。
      const sz = d.byKindSize || {};
      const szTxt = Object.keys(sz).map(function (k) {
        const v = sz[k];
        return (LAB[k] || k) + ' ' + v.w + '×' + v.h
          + ' → <b>' + v.outW + '×' + v.outH + '</b>';
      }).join(' ｜ ');
      const scope = units.length
        ? '范围：所选 <b>' + units.length + '</b> 部剧情'
        : '范围：全部已装剧情';
      box.innerHTML =
        '<b>本次待超分 ' + d.total + ' 张</b>（' + esc(by) + '）· ' + scope + '<br>'
        + (szTxt ? '尺寸：' + szTxt + '（' + d.scale + ' 倍）<br>' : '')
        + '平均单张约 <b>' + d.perImageSec + ' 秒</b>，预计共 <b>' + esc(d.etaText) + '</b>'
        + '<div class="dim" style="margin-top:6px">按当前电脑估的，实际可能有 ±30% 出入。'
        + '跑的时候电脑会变卡、风扇会响；随时能取消，做完的不会白做。</div>';
    } catch (e) {
      box.innerHTML = '<span class="dim">预估失败：' + esc(e.message) + '</span>';
      ST._plan = null;
    }
  }

  $$('.upk').forEach(c => { c.onchange = () => refreshPlan(); });
  if ($('#upScale')) $('#upScale').onchange = () => refreshPlan();
  if ($('#upAll')) {
    $('#upAll').onclick = () => {
      ensureUpList().selectAll();
      updateUpInfo();
      refreshPlan();
    };
    $('#upNone').onclick = () => {
      ST.upSel.clear();
      ensureUpList().refresh();
      updateUpInfo();
      refreshPlan();
    };
  }

  /* ---------------------------------------------------------- 空间账本
   * 数据来自 /api/space：服务端**一次遍历**把整个程序目录按用途分桶
   * （语音 / 原图 / 超分图 / 剧本 / 资源包缓存 / 导出…），27 万个文件约 1–3 秒，
   * 服务端缓存 2 分钟。
   * 为什么不再用 /api/stats 拼页面：那个接口只给三个 media 目录的数字，
   * 用户算不出"30 GB 都花在哪"，也看不到"哪些能删"。
   */
  let _statsAt = 0;
  let _space = null;
  async function loadStats(force) {
    if (!force && _space && Date.now() - _statsAt < 20000) { renderSpace(_space); return; }
    _statsAt = Date.now();
    const disk = $('#diskStats');
    if (!disk) return;
    disk.innerHTML = '<div class="dim">统计中…（首次约 '
      + FACT.t('secScan') + '）</div>';
    const detail = $('#diskDetail');
    if (detail) detail.innerHTML = '';
    try {
      const d = await api('space' + (force ? '?force=1' : ''));
      _space = d;
      renderSpace(d);
    } catch (e) {
      disk.innerHTML = '<div class="dim">统计失败：' + esc(e.message) + '</div>';
    }
  }

  function renderSpace(d) {
    const disk = $('#diskStats');
    if (!disk) return;
    const by = {};
    (d.groups || []).forEach(g => { by[g.key] = g; });
    const size = k => (by[k] ? by[k].bytes : 0);
    const cnt = k => (by[k] ? by[k].files : 0);
    const dk = d.disk || {};
    const usedPct = dk.total ? Math.round(100 * dk.used / dk.total) : 0;

    const cells = [
      ['本程序总占用', human((d.root || {}).bytes),
        `${(d.root || {}).files || 0} 个文件`],
      ['剧情语音', human(size('audio')), `${cnt('audio')} 个文件`],
      ['剧情图片（原图）', human(size('img')), `${cnt('img')} 个文件`],
      ['超分高清图', human(size('x2')), `${cnt('x2')} 个文件`],
      ['剧本数据', human(size('script')), `${cnt('script')} 个文件`],
      ['可删的缓存', human(size('bundles')), `${cnt('bundles')} 个压缩包`],
      ['可安全清理', human(d.reclaim), '缓存 · 原图 · 导出 · 日志'],
    ];
    disk.innerHTML = cells.map(function (c) {
      return '<div class="stat"><div class="k">' + esc(c[0]) + '</div>' +
        '<div class="v">' + esc(c[1]) + '</div>' +
        '<div class="k">' + esc(c[2]) + '</div></div>';
    }).join('')
      // 磁盘余量单独一格，带一条占用率条：用户真正关心的是"还能装几部"
      + '<div class="stat"><div class="k">所在磁盘</div>'
      + '<div class="v">' + human(dk.free) + '<small>可用</small></div>'
      + '<div class="k">已用 ' + usedPct + '% · 共 ' + human(dk.total) + '</div>'
      + '<div class="bar"><i style="width:' + usedPct + '%"></i></div></div>';

    // 逐项明细（像账单一样一行一项）：只列有内容的桶。
    // 目录直接排在体积**后面**（小字、灰色、过长省略号，悬停看全）：
    // 以前只放在 title 里，得把鼠标停在整行上才看得到，对不上号（2026-11-03 用户反馈）。
    const rows = (d.groups || []).map(function (g) {
      return '<div class="kv">'
        + '<span class="nm">' + esc(g.label) + '</span>'
        + '<span class="sz">' + human(g.bytes) + '</span>'
        + (g.path
          ? '<span class="pth" title="' + esc(g.path) + '">' + esc(g.path) + '</span>'
          : '')
        + '</div>';
    }).join('');

    // "为什么图片数量对不上"：两个口径不同（磁盘上全部图片 vs 映射表里可超分的图），
    // 不解释的话用户一定会问"为什么 2.8 万个文件只有 3500 张要超分"。
    const up = d.upscale || null;
    const bk = d.byKindFiles || {};
    const standAll = bk.stand || 0;
    const cgAll = bk.cg || 0;
    const standMap = (up && up.byKind && up.byKind.stand && up.byKind.stand.total) || 0;
    const cgMap = (up && up.byKind && up.byKind.cg && up.byKind.cg.total) || 0;
    const rest = [
      ['未被任何剧情引用的立绘', Math.max(0, standAll - standMap)],
      ['剧情插图', bk.story || 0],
      ['头像', bk.avatar || 0],
      ['全身立绘', bk.standfull || 0],
      ['未登记的背景', Math.max(0, (bk.back || 0)
        - ((up && up.byKind && up.byKind.back && up.byKind.back.total) || 0))],
      ['未登记的 CG', Math.max(0, cgAll - cgMap)],
    ].filter(function (x) { return x[1] > 0; });
    const outside = rest.reduce(function (s, x) { return s + x[1]; }, 0);
    const note = up
      ? '<div class="dim" style="margin-top:12px;line-height:1.85">'
        + '<b>高清化进度：</b>能做 <b>' + up.total + '</b> 张'
        + '（立绘 ' + standMap + ' · 背景 ' + ((up.byKind.back || {}).total || 0)
        + ' · CG ' + cgMap + '），已做 <b>' + up.done + '</b>，还剩 <b>' + up.left + '</b>。'
        + (outside ? '另有 ' + outside + ' 张不参与高清化（头像、剧情插图等）。' : '')
        + '<span style="display:block;margin-top:4px">统计于 ' + esc(d.at || '-') + '</span>'
        + '</div>'
      : '';
    const detail = $('#diskDetail');
    if (detail) {
      detail.innerHTML = '<div class="kvList">' + rows + '</div>' + note;
    }

    // 清理区顶部的"这项占多少"
    const fmt = (k) => {
      const g = by[k];
      return g ? human(g.bytes) + ' · ' + g.files + ' 个文件' : '0 B（没有可删的）';
    };
    const setTxt = (sel, txt) => { const el = $(sel); if (el) el.textContent = txt; };
    setTxt('#cBundles', fmt('bundles'));
    setTxt('#cX2', fmt('x2'));
    setTxt('#cExport', fmt('export'));
    const og = d.originals || {};
    setTxt('#cOriginals', og.bytes
      ? human(og.bytes) + ' · ' + og.files + ' 张'
      : '0 B（没有可删的）');
    const junk = ['logs', 'downloads', 'backup']
      .map(k => size(k)).reduce(function (a, b) { return a + b; }, 0);
    setTxt('#cJunk', junk ? human(junk) : '0 B（没有可删的）');
  }

  $('#btnUpscale').onclick = async () => {
    const kinds = $$('.upk').filter(c => c.checked).map(c => c.value);
    if (!kinds.length) { toastMsg('至少选一类图', true); return; }
    const p = ST._plan;
    const units = upUnitsParam();
    const eta = (p && p.total)
      ? `\n本次 ${p.total} 张，单张约 ${p.perImageSec} 秒，预计 ${p.etaText}` : '';
    const scope = units.length ? `所选 ${units.length} 部剧情里的` : '已下载的';
    if (!confirm(`将对${scope}【${kinds.join(' / ')}】做超分（${$('#upScale').value} 倍）。${eta}\n\n` +
      `这会占用大量 CPU，期间电脑会变卡；可以随时取消，已完成的不会重做。继续？`)) return;
    try {
      await api('upscale', {
        method: 'POST',
        body: JSON.stringify({
          kinds: kinds, scale: +$('#upScale').value, units: units,
        }),
      });
      toastMsg('超分已开始');
      connectJobs(true);
    } catch (e) { toastMsg(e.message, true); }
  };
  /* 「重新登记高清图」（旧名：重建超分索引）
   * 用户看不懂"索引"是什么，也不知道什么时候该点。所以：先讲清"它不做什么"
   * （不重新超分、不改图、不删图、不占时间/空间），再讲"什么时候才值得点"，
   * 点之前还要确认一次 —— 免得有人以为这是超分的第二步、每次都要点。
   */
  const btnRebuildX2 = $('#btnRebuildX2');
  if (btnRebuildX2) {
    btnRebuildX2.onclick = async () => {
      if (!confirm('重新登记高清图？\n\n'
        + '· 只是把硬盘上已经做好的高清图数一遍，重新登记给播放器用\n'
        + '· 不会重新做超分，不会改动或删除任何图片，硬盘占用不变\n'
        + '· 大约 ' + FACT.t('secRebuild') + '，随时能关页面\n\n'
        + '平时不用点它。只有「高清图明明做了、播放器里看着还是糊」或者'
        + '「从别的电脑拷了高清图过来」的时候才用。继续？')) return;
      const info = $('#x2RebuildInfo');
      if (info) info.textContent = '正在登记…';
      ST._rbAskAt = Date.now() / 1000 - 2;    // 只认这一次的结果（留 2 秒时钟余量）
      try {
        await api('rebuild-x2', { method: 'POST', body: '{}' });
        toastMsg('正在登记高清图，约 ' + FACT.t('secRebuild'));
        connectJobs(true);
      } catch (e) {
        if (info) info.textContent = '没成功：' + e.message;
        toastMsg(e.message, true);
      }
    };
  }

  /* ---------------------------------------------------------- 画质对比图
   * 左边原图、右边超分 2 倍，用的是**同一段剧情的同一个画面**（背景 + 立绘），
   * 只有分辨率不同 —— 并排一看就知道超分值不值得做，比读说明直观。
   * 首次点：只对这两张图做超分（几秒～十几秒），做完就存在磁盘上了；
   * 之后再点直接显示，不再重跑（ready 命中的分支就是这么来的）。
   */
  const CMP = {
    scene: null, jobId: null, timer: null, hintT: null,
    ratio: 16 / 9, split: 50, healAt: 0,
  };

  /** 全屏后提示"怎么退出"：每次真正出图时露一下，6 秒后淡出 */
  function cmpShowHint() {
    const el = $('#cmpFade');
    if (!el) return;
    el.classList.add('on');
    clearTimeout(CMP.hintT);
    CMP.hintT = setTimeout(() => el.classList.remove('on'), 6000);
  }

  /** 竖线位置（0~100%）：上层那张原图按它把右半边裁掉 */
  function cmpSetSplit(p) {
    p = Math.max(2, Math.min(98, +p || 50));
    CMP.split = p;
    const top = $('#cmpTopA');
    if (top) top.style.clipPath = 'inset(0 ' + (100 - p) + '% 0 0)';
    const line = $('#cmpSplit');
    if (line) line.style.left = p + '%';
  }

  /** 舞台按图片比例塞进可用区域（不够就留黑边，绝不给拉变形） */
  function cmpFit() {
    const mask = $('#cmpMask'), wrap = $('#cmpBody'), stage = $('#cmpStage');
    if (!mask || !wrap || !stage || mask.hidden) return;
    const ar = CMP.ratio || 16 / 9;
    const aw = Math.max(240, wrap.clientWidth - 20);
    const ah = Math.max(140, wrap.clientHeight - 12);
    let w = aw, h = aw / ar;
    if (h > ah) { h = ah; w = ah * ar; }
    stage.style.width = Math.round(w) + 'px';
    stage.style.height = Math.round(h) + 'px';
  }

  /** 两张图铺进上下两层：下层超分、上层原图（按竖线裁） */
  function cmpPaint(sc) {
    const put = (layerSel, bgSrc, stSrc, onLoad, onErr) => {
      const box = $(layerSel);
      if (!box) return;
      const bg = box.querySelector('.cmpBg');
      const st = box.querySelector('.cmpSt');
      bg.onload = () => onLoad(bg);       // 先挂回调再给 src，缓存命中才不会漏掉
      bg.onerror = () => {
        const n = $('#cmpNote');
        if (n) n.textContent = '这张图读不出来 —— 可能刚在「清理空间」里被删掉了。';
        if (onErr) onErr();
      };
      bg.src = bgSrc;
      // 立绘缺失不影响看背景，别让一个破图标把画面毁了
      st.onerror = () => { st.style.display = 'none'; };
      if (stSrc) { st.style.display = ''; st.src = stSrc; }
      else { st.style.display = 'none'; }
    };
    /* 超分图可能在别处被删了（清理空间 / 我们自己重新生成过）：
       发现读不出来就把内存里的结论作废、重走一遍"检查→必要时重做"，
       别让按钮一直拿着一张已经不存在的图秒开。15 秒内只自愈一次，防死循环。 */
    const heal = () => {
      const now = Date.now();
      if (CMP.healAt && now - CMP.healAt < 15000) return;
      CMP.healAt = now;
      CMP.scene = null;
      const n = $('#cmpNote');
      if (n) n.textContent = '高清图不见了，正在重新生成…';
      setTimeout(() => { if (!$('#cmpMask').hidden) cmpOpen(); }, 300);
    };
    const lab = sc.scale || 2;
    put('#cmpTopA', sc.back.src, sc.stand.src, bg => {
      const el = $('#cmpTagL');
      if (el && bg.naturalWidth) el.textContent = '原图 ' + bg.naturalWidth + '×' + bg.naturalHeight;
      cmpFit();
    });
    put('#cmpLayerB', sc.back.x2, sc.stand.x2, bg => {
      if (bg.naturalWidth && bg.naturalHeight) CMP.ratio = bg.naturalWidth / bg.naturalHeight;
      const el = $('#cmpTagR');
      if (el && bg.naturalWidth) {
        el.textContent = '超分 ' + lab + ' 倍 ' + bg.naturalWidth + '×' + bg.naturalHeight;
      }
      cmpFit();
    }, heal);
    // 每次打开都从"看全图"开始，别把上次放大过的状态带过来
    const stage = $('#cmpStage');
    if (stage) stage.classList.remove('zoom');
    const zb = $('#cmpZoom');
    if (zb) zb.textContent = '看细节（放大 2.4 倍）';
    $('#cmpWait').hidden = true;
    $('#cmpSub').textContent = '左边原图，右边超分 ' + lab + ' 倍';
    cmpSetSplit(CMP.split);
    const note = $('#cmpNote');
    if (note) {
      note.textContent = '取自「' + unitName(sc.unit) + '」第 ' + sc.part + ' 段。'
        + '两张是同一个画面，只有分辨率不同；播放时程序会优先用高清图，'
        + '没做过高清的自动用原图，不会黑图。';
    }
    cmpFit();
    cmpShowHint();
  }

  /** 竖线：点哪跳哪，按住拖动跟随（鼠标 / 触屏都走 pointer 事件） */
  function cmpBindSplit() {
    const stage = $('#cmpStage');
    if (!stage || stage.dataset.dragBound) return;
    stage.dataset.dragBound = '1';
    let on = false;
    const jump = e => {
      const r = stage.getBoundingClientRect();
      if (!r.width) return;
      cmpSetSplit(100 * (e.clientX - r.left) / r.width);
    };
    stage.addEventListener('pointerdown', e => {
      on = true;
      try { stage.setPointerCapture(e.pointerId); } catch (err) { /* 老浏览器 */ }
      jump(e);
      e.preventDefault();
    });
    stage.addEventListener('pointermove', e => { if (on) jump(e); });
    const stop = e => {
      on = false;
      try { stage.releasePointerCapture(e.pointerId); } catch (err) { /* 同上 */ }
    };
    stage.addEventListener('pointerup', stop);
    stage.addEventListener('pointercancel', stop);
  }

  /** pct < 0 = 这轮不是进度（出错/没装剧情），进度条整个藏起来。
   *  goText 给了才亮出那个"去剧情安装"的按钮（文案由服务端按原因给）。 */
  function cmpWait(title, pct, msg, goText) {
    const box = $('#cmpWait');
    if (!box) return;
    box.hidden = false;
    $('#cmpWaitTitle').textContent = title;
    const bar = $('.cmpBar');
    if (bar) bar.hidden = pct < 0;
    // 0% 时也给一点，否则进度条看着像坏了、像卡住
    const w = pct > 0 ? Math.min(100, pct) : 8;
    $('#cmpBarI').style.width = w + '%';
    $('#cmpWaitMsg').textContent = msg || '';
    const go = $('#cmpGoPacks');
    if (go) {
      go.hidden = !goText;
      if (goText) go.textContent = goText;
    }
  }

  /** 超分进度的人话版本：只报"第几张/共几张"，不把引擎名和参数抖给用户 */
  function cmpJobLine(job) {
    const t = +job.total || 0;
    if (!t) return '';
    const cur = Math.min(t, (+job.done || 0) + 1);
    return '正在做第 ' + cur + ' / ' + t + ' 张。';
  }

  function cmpClose() {
    const mask = $('#cmpMask');
    if (mask) mask.hidden = true;
    // 超分任务留在后台继续跑（关浮层 ≠ 取消），下次点开接着看进度
  }

  async function cmpPoll() {
    if (!CMP.jobId) return;
    clearTimeout(CMP.timer);
    let d;
    try { d = await api('jobs'); } catch (e) { CMP.timer = setTimeout(cmpPoll, 2500); return; }
    const job = (d.jobs || []).find(j => j.id === CMP.jobId);
    if (!job) { CMP.jobId = null; cmpOpen(); return; }        // 任务被清掉了，重来
    if (job.state === 'failed') {
      CMP.jobId = null;
      cmpWait('这次没能做出来', -1, '超分失败：' + (job.error || '原因不明')
        + '。到「帮助 / 教程」里可以看看是不是缺了超分引擎。');
      return;
    }
    if (job.state === 'done' || job.state === 'cancelled') {
      CMP.jobId = null;
      const sc = await api('upscale/demo').catch(() => null);
      if (sc && sc.ok && sc.ready) { CMP.scene = sc; cmpPaint(sc); return; }
      cmpWait('图片没找到', -1,
        '超分跑完了，但没找到生成的高清图 —— 可能刚在「清理空间」里被删掉了。再点一次按钮试试。');
      return;
    }
    cmpWait('正在对原图进行超分…', job.percent,
      '只做对比用的这两张（背景 + 立绘），做完就存下来了。'
      + '第一次要等十几秒，之后点按钮直接显示。' + cmpJobLine(job));
    CMP.timer = setTimeout(cmpPoll, 1000);
  }

  async function cmpOpen() {
    const mask = $('#cmpMask');
    if (!mask) return;
    mask.hidden = false;
    CMP.split = 50;               // 每次打开都从"左原图 / 右超分"对半分开始
    cmpFit();                     // 先按当前窗口定量一次，等待层底下也有个像样的空舞台
    clearTimeout(CMP.timer);
    cmpWait('正在准备对比图…', 0, '第一次要先做这两张的超分，稍等一下。');
    try {
      /* **每次都问一次后端**（本地接口，几十毫秒）：用户可能刚在
         「设置 / 清理空间」里删了原图或高清图，靠内存里"上次是好的"直接秒开，
         会拿着一张已经不存在的图去显示（浏览器还可能吃内存缓存，连错误都不报）。
         贵的从来不是这个查询，是超分本身。 */
      const sc = await api('upscale/demo');
      if (!sc.ok) {
        // 没装剧情 / 原图被清理 / 没有可用的段落 —— 三种原因服务端分好了，
        // 连同"下一步该点哪个按钮"一起给过来，别只丢一行字
        cmpWait('现在还不能对比', -1, sc.error || '还没有可以对比的图片。',
          sc.needInstall ? (sc.goText || '去「剧情安装」挑一部') : '');
        return;
      }
      CMP.scene = sc;
      if (sc.ready) { cmpPaint(sc); return; }
      const d = await api('upscale/demo', { method: 'POST', body: '{}' });
      if (!d.job) {
        // 服务端说"已经没有要做的了"，那就再问一次状态
        const again = await api('upscale/demo').catch(() => null);
        if (again && again.ok && again.ready) { CMP.scene = again; cmpPaint(again); return; }
        cmpWait('还没能生成对比图', -1, '再点一次按钮试试。');
        return;
      }
      CMP.jobId = d.job.id;
      cmpWait('正在对原图进行超分…', d.job.percent || 0,
        '只做对比用的这两张（背景 + 立绘），做完就存下来了。第一次要等十几秒。'
        + cmpJobLine(d.job));
      cmpPoll();
      connectJobs(false);        // 底部任务条也显示这件事，关掉浮层也看得见进度
    } catch (e) {
      const m = String((e && e.message) || '');
      // 最常见的两种：程序刚更新过、但本地服务还是旧进程 —— 新接口它根本不认识，
      // 报的是 HTTP 404 / "unknown api"。这不是功能坏了，重启一次就好。
      const stale = /unknown api|404|not found|failed to fetch|networkerror/i.test(m);
      cmpWait(stale ? '本地服务还是旧版本' : '没能打开对比图', -1,
        stale
          ? '这个功能要新版本的服务端才认识。请关掉启动窗口（或任务栏里的黑窗），'
            + '重新双击程序目录里的「启动.bat」，然后再点这个按钮 —— 只需重启这一次。'
          : m + '　如果是刚更新过程序，请关掉启动窗口后重新双击「启动.bat」。');
    }
  }

  if ($('#btnCmpDemo')) $('#btnCmpDemo').onclick = cmpOpen;
  if ($('#cmpClose')) $('#cmpClose').onclick = cmpClose;
  if ($('#cmpMask')) {
    document.addEventListener('keydown', e => {
      const mask = $('#cmpMask');
      if (mask.hidden) return;
      if (e.key === 'Escape') { cmpClose(); return; }
      // 左右方向键微调竖线：鼠标拖累了还有退路
      if (e.key === 'ArrowLeft') { cmpSetSplit(CMP.split - 2); e.preventDefault(); }
      if (e.key === 'ArrowRight') { cmpSetSplit(CMP.split + 2); e.preventDefault(); }
    });
    cmpBindSplit();
    window.addEventListener('resize', cmpFit);
  }
  if ($('#cmpGoPacks')) {
    $('#cmpGoPacks').onclick = () => {
      cmpClose();
      goto('packs');
      toastMsg('勾一个想看的角色装上，装完回「提高CG画质」点「查看效果」就能对比了');
    };
  }
  if ($('#cmpZoom')) {
    $('#cmpZoom').onclick = () => {
      const stage = $('#cmpStage');
      const on = !stage.classList.contains('zoom');
      stage.classList.toggle('zoom', on);
      $('#cmpZoom').textContent = on ? '看全图' : '看细节（放大 2.4 倍）';
    };
  }
  $('#btnDelOrig').onclick = async () => {
    if (!confirm('删除「已经有超分图」的原图？\n\n只会删有对应超分文件的那部分，删完照样能播放。')) return;
    try {
      await api('delete-originals', { method: 'POST', body: '{}' });
      toastMsg('正在清理'); connectJobs(true);
    } catch (e) { toastMsg(e.message, true); }
  };
  const btnStats = $('#btnStats');
  if (btnStats) btnStats.onclick = () => loadStats(true);
  const btnOpenLogs = $('#btnOpenLogs');
  if (btnOpenLogs) {
    btnOpenLogs.onclick = async () => {
      try {
        await api('open', { method: 'POST', body: JSON.stringify({ which: 'logs' }) });
        toastMsg('已打开日志目录（app.log）');
      } catch (e) { toastMsg(e.message, true); }
    };
  }
  const btnPrune = $('#btnPrune');
  if (btnPrune) {
    btnPrune.onclick = () => startJob('cdn/prune', { dry: true },
      '开始扫描未引用的缓存（只统计，不删）');
  }
  const btnPruneGo = $('#btnPruneGo');
  if (btnPruneGo) {
    btnPruneGo.onclick = () => {
      if (!confirm('确认删掉这些没人用的旧压缩包？\n\n' +
        '删掉后如果以后重新装回那部剧情，会重新下载一遍。')) return;
      startJob('cdn/prune', { dry: false }, '开始清理没用的旧缓存');
    };
  }

  /* ---------------------------------------------------------- 清理空间
   * 每一项的 confirm 文案都要写清"删了会怎样" —— 这些操作不可撤销，
   * 而用户点之前唯一的信息来源就是这几行字。
   */
  const CLEAN_CONFIRM = {
    bundles: () => '删除全部资源包缓存（原始压缩包）？\n\n'
      + '这些是下载回来的原始压缩包，解包成图片 / 语音 / 剧本之后就再也用不到了。\n'
      + '删掉不影响已完成解包的剧情播放，只是以后重新安装、或「补全缺失」'
      + '某部剧情时会重新下载。\n\n确定删除？',
    x2: () => '删除全部超分高清图？\n\n'
      + '删完立刻用回原图播放，不会黑图、不影响剧情，但画质会变回原样。\n'
      + '想再要高清得重新做一次（比较慢）。\n\n确定删除？',
    export: () => '清空导出成品目录？\n\n'
      + '里面的视频、CG 压缩包、导出的翻译 txt 都会被删掉，删了要重新导一次。\n'
      + '想要的东西先拷走。\n\n确定清空？',
    logs: () => '清理日志文件？\n\n只用于排错，删了不影响任何功能。'
      + '正在写的那个文件会被系统占用，会自动跳过。\n\n确定清理？',
    downloads: () => '清理下载残留？\n\n'
      + '没下完的下载留下的临时文件，以及导出视频时传了一半就断掉的录制文件。'
      + '删了不影响已装的剧情，也不影响已经导出好的视频。\n\n确定清理？',
    backup: () => '清理旧备份目录？\n\n确定清理？',
  };

  async function runClean(what, btn) {
    const f = CLEAN_CONFIRM[what];
    if (f && !confirm(f())) return;
    if (btn) btn.disabled = true;
    try {
      await api('space/clean', {
        method: 'POST', body: JSON.stringify({ what, dry: false }),
      });
      toastMsg('已开始清理，进度见底部任务条');
      connectJobs(true);
      // 任务跑完才准再点（避免重复提交同一件事）
      setTimeout(() => { if (btn) btn.disabled = false; }, 4000);
    } catch (e) {
      toastMsg('清理失败：' + e.message, true);
      if (btn) btn.disabled = false;
    }
  }
  $$('[data-clean]').forEach(b => {
    b.onclick = () => runClean(b.dataset.clean, b);
  });
  // 「去清理空间」那块 panel 已删（说明与页面 lead / 左侧导航重复），入口靠左侧导航
  const goPacks = $('#btnGoPacks');
  if (goPacks) goPacks.onclick = () => goto('packs');
  const openExp2 = $('#btnOpenExport2');
  if (openExp2) openExp2.onclick = () => openDir('export', '导出目录');

  // ---------------------------------------------------------------- 导出
  /* 视频导出：**每个角色一条视频**，所选段落连续录进同一条
     （MediaRecorder 不停，切段时继续录，所以不需要 ffmpeg 拼接）。 */
  let _vidDep = null;
  async function checkVidDeps(force) {
    if (_vidDep && !force) return _vidDep;
    try { _vidDep = await api('export/video/info'); }
    catch (e) { _vidDep = { ready: true, missing: [] }; }
    return _vidDep;
  }

  /* ④ 的剧情选择列表：与 ②③⑤ 共用 storylist.js。
     只列「已安装」的剧情 —— 没装就没有剧本可渲染，列出来也点不动，
     所以状态筛选整个不显示（noStatus）。 */
  function ensureVidList() {
    if (vidList) return vidList;
    vidList = SL.create({
      bar: '#vidBar', list: '#vidList', sel: ST.vidSel, src: 'char',
      units: src => (src === 'char' ? ST.units.char : ST.units.adv)
        .filter(k => stateOf(k) !== 'missing'),
      status: stateOf,
      noStatus: true,
      banners: bannersReady,
      // 切分类就清空勾选：段落数按分类定（角色 6 段、章节最多 9 段），
      // 混选会让"第 7 段"这种勾选落到只有 6 段的角色身上，导出时那个角色直接失败。
      onSrc: () => {
        ST.vidSel.clear();
        renderParts();
      },
      // 勾选变了要重排「录哪几段」：不同剧情段数差别很大（角色 6 段、
      // 其他剧情有 2 段的也有 28 段的），段数必须跟着所选剧情走
      onChange: () => renderParts(),
      onRender: () => updateVidInfo(),
      empty: () => '还没有已安装的剧情 —— 先到「② 剧情安装」装一部',
    });
    return vidList;
  }

  function renderExport() {
    const warn = $('#vidWarn');
    const box = $('#vidBox');
    if (!warn || !box) return;

    /* 导出视频要 playwright + av + numpy（离线渲染 mp4），缺了先提示。
       提示放在列表**上方**并且把列表收起来：以前是把提示写进列表容器里，
       用户会以为"列表就是空的"。 */
    if (_vidDep === null) {
      warn.hidden = false;
      warn.className = 'dim';
      warn.textContent = '检查导出环境…';
      box.hidden = true;
      checkVidDeps().then(() => renderExport());
      return;
    }
    if (!_vidDep.ready) {
      warn.hidden = false;
      warn.className = 'warn';
      warn.innerHTML =
        '缺少导出视频所需的组件：<b>' + esc((_vidDep.missing || []).join('、')) + '</b><br>' +
        '它们用来驱动无头浏览器截帧、以及编码 mp4。<br>' +
        '<b>用便携包的：</b>便携版自带，出现这个多半是解压不完整或被杀软拦了，' +
        '重新解压并加白名单。<br>' +
        '<b>从源码克隆的：</b>双击 <code>启动.bat</code>，或手动 ' +
        '<code>pip install playwright av</code>。';
      box.hidden = true;
      return;
    }
    // 列表数据可能还没加载（直接进本页时）——加载完自己重画
    if (!ST.units.loaded) {
      warn.hidden = false;
      warn.className = 'dim';
      warn.textContent = '正在读取剧情列表…';
      box.hidden = true;
      loadUnits().then(() => { renderExport(); renderBasePack(); }).catch(() => { });
      return;
    }
    warn.hidden = true;
    box.hidden = false;
    ensureVidList().refresh();
    renderParts();
  }

  /** 段落数：按**勾选的剧情**取真实段数（其他剧情常常只有 3 段，
   *  一律给 9 段等于让人勾到不存在的段，导出时那部会直接失败）。
   *  没勾选时退回该类别的常见段数（角色 6 段、章节 9 段）。 */
  function partCount() {
    let n = 0;
    ST.vidSel.forEach(function (k) {
      const p = SL.info(k).parts;
      if (p > n) n = p;
    });
    if (n) return n;
    return (vidList ? vidList.getSrc() : 'char') === 'char' ? 6 : 9;
  }

  function renderParts() {
    // 段落对所有选中角色通用；勾相邻几段 = 连续录制，合成一条
    const count = partCount();
    // 重画时保留已勾的段（搜索、勾选都会触发重画，以前会把用户选的段冲掉）
    let keep = vidParts().filter(v => v >= 1 && v <= count);
    if (!keep.length) keep = [1];
    // value 用 **1 基**（第 1 段 = 1）：后端 story.Timeline 是 `_build_part(pi - 1)`，
    // 导出文件名 seg_label 也是 p<该值>。以前这里给 0 基，选第 2 段会被当成第 1 段，
    // 选第 1 段更会因为 -1 变成"最后一段"。
    $('#vidParts').innerHTML = Array.from({ length: count }, function (_, i) {
      const on = keep.indexOf(i + 1) >= 0;
      return '<label class="' + (on ? 'on' : '') + '">' +
        '<input type="checkbox" value="' + (i + 1) + '"' + (on ? ' checked' : '') + '>' +
        '第 ' + (i + 1) + ' 段</label>';
    }).join('');
    $$('#vidParts label').forEach(function (lb) {
      const cb = lb.querySelector('input');
      cb.onchange = function () {
        lb.classList.toggle('on', cb.checked);
        updateVidInfo();
      };
    });
    updateVidInfo();
  }

  function vidParts() {
    return $$('#vidParts input:checked').map(c => +c.value).sort((a, b) => a - b);
  }

  function updateVidInfo() {
    const parts = vidParts();
    const n = ST.vidSel.size;
    const info = $('#vidSelInfo');
    if (info) {
      info.textContent = n
        ? '已选 ' + n + ' 部剧情 · 第 ' + parts.join('、') + ' 段'
        : '未选择剧情';
    }
    const h = $('#vidHint');
    if (h) {
      h.innerHTML = n
        ? '将导出 <b>' + n + '</b> 条 mp4（每部剧情一条，各含所选 ' + parts.length +
          ' 段，连续段落合并成同一条）' + mixedPartsNote()
        : '选剧情 → 选段落 → 开始导出';
    }
  }

  /** 所选剧情的段数不一致时提醒一句：段号是"每部剧情各自数"的，
   *  勾了第 5 段而某部只有 3 段，那部在导出时会失败（其余照常导出）。 */
  function mixedPartsNote() {
    const ns = [];
    ST.vidSel.forEach(function (k) {
      const p = SL.info(k).parts;
      if (p && ns.indexOf(p) < 0) ns.push(p);
    });
    if (ns.length < 2) return '';
    ns.sort((a, b) => a - b);
    return '<br><span style="color:#e5c98a">注意：所选剧情段数不同（最少 '
      + ns[0] + ' 段 / 最多 ' + ns[ns.length - 1] + ' 段），勾到超出某部段数的段号时，'
      + '那一部会导出失败，其余照常。</span>';
  }

  $('#vidAll').onclick = () => ensureVidList().selectAll();
  $('#vidNone').onclick = () => ensureVidList().clear();

  $('#btnVidQueue').onclick = async () => {
    if (!ST.vidSel.size) { toastMsg('先选剧情', true); return; }
    const parts = vidParts();
    if (!parts.length) { toastMsg('至少选一段', true); return; }
    const units = Array.from(ST.vidSel);
    if (!confirm('将导出 ' + units.length + ' 条 mp4（每部剧情一条，各含所选 ' +
      parts.length + ' 段，连续段落合并成同一条）。\n\n' +
      '· 离线渲染：逐句定格截图后编码，画面稳定、无掉帧\n' +
      '· 实测每句约 ' + FACT.t('secExportLine') + ' → 单段约 '
      + FACT.t('secExportPart') + '，6 段全导约 ' + FACT.t('secExportAll') + '\n' +
      '· 期间可以关掉这个页面，任务在后台继续\n\n开始？')) return;
    try {
      await api('export/video', {
        method: 'POST',
        body: JSON.stringify({ units: units, parts: parts }),
      });
      toastMsg('已开始导出，进度见底部');
      connectJobs(true);
    } catch (e) {
      toastMsg('启动失败：' + e.message, true);
    }
  };

  async function loadCg() {
    if (ST.cg) return ST.cg;
    ST.cg = await api('cglist');
    return ST.cg;
  }

  const cgOf = k => ((ST.cg && ST.cg.chars) || [])
    .find(c => c.id === +String(k).slice(5)) || null;

  /* ④ 的 CG 选择列表：同一套 storylist.js。CG 只有角色侧（主线/活动没有
     「角色」这种分组），所以只给一个分类页签；组数/张数放在副标题里。 */
  let cgList = null;
  function ensureCgList() {
    if (cgList) return cgList;
    cgList = SL.create({
      bar: '#cgBar', list: '#cgList', sel: ST.cgSel, src: 'char',
      srcs: ['char'],
      units: () => ((ST.cg && ST.cg.chars) || []).map(c => 'char_' + c.id),
      status: () => 'ready',
      noStatus: true,
      banners: bannersReady,
      // cgmap 里有三个负数分组装「不属于某个角色」的 CG（NPC / 路人），
      // 服务端兜底名是「角色-2」，直接显示等于没说；这里讲明白它是什么。
      name: k => {
        const c = cgOf(k);
        if (!c) return '';
        return c.id < 0 ? ('非角色 CG #' + (-c.id)) : (c.name || '');
      },
      extra: k => {
        const c = cgOf(k);
        return c ? [(c.groups || []).length + ' 组 · ' + c.total + ' 张'] : [];
      },
      onChange: () => updateCgInfo(),
      onRender: () => updateCgInfo(),
      empty: () => '没有可导出的 CG —— 先到「② 剧情安装」装一部剧情',
    });
    return cgList;
  }

  function renderCgList() {
    const warn = $('#cgWarn');
    if (!warn) return;
    if (!ST.cg) {
      warn.hidden = false;
      warn.className = 'dim';
      warn.textContent = '正在读取 CG 清单…';
      return;
    }
    warn.hidden = true;
    ensureCgList().refresh();
  }

  function updateCgInfo() {
    let n = 0;
    // 选中集合里存的是 char_<id>（和另外几个列表同一套 key），导出时再转回数字
    ST.cgSel.forEach(function (k) {
      const c = cgOf(k);
      if (c) n += c.total;
    });
    const el = $('#cgInfo');
    if (el) el.textContent = '已选 ' + ST.cgSel.size + ' 个角色 · ' + n + ' 张 CG';
  }

  $('#cgAll').onclick = () => ensureCgList().selectAll();
  $('#cgNone').onclick = () => ensureCgList().clear();
  $('#btnCgExport').onclick = async () => {
    if (!ST.cgSel.size) { toastMsg('先选角色', true); return; }
    try {
      await api('export/cg', {
        method: 'POST',
        body: JSON.stringify({ chars: [...ST.cgSel].map(k => +String(k).slice(5)) }),
      });
      toastMsg('开始打包 CG'); connectJobs(true);
    } catch (e) { toastMsg(e.message, true); }
  };
  /** 打开本地目录，并把路径回显到页面（资源管理器有时开在后台，用户以为没成功） */
  async function openDir(which, label) {
    try {
      const d = await api('open', { method: 'POST', body: JSON.stringify({ which }) });
      toastMsg('已打开' + (label || '') + '：' + (d.path || which));
    } catch (e) { toastMsg(e.message, true); }
  }
  $('#btnOpenExport').onclick = () => openDir('export', '导出目录');
  const btnOpenExpTr = $('#btnOpenExpTr');
  if (btnOpenExpTr) btnOpenExpTr.onclick = () => openDir('export', '导出目录');

  /* 「导出完成后自动打开文件夹」：④ 导出页和 ⑤ 翻译页各有一个勾选框，
     共用同一个设置（localStorage），勾哪个都同步，不会两边不一致。 */
  const OPEN_EXP_KEY = 'escah_open_exp';
  const openExpBoxes = $$('#setOpenExp, #setOpenExpTr');
  function readAutoOpen() {
    try { return localStorage.getItem(OPEN_EXP_KEY) === '1'; } catch (e) { return false; }
  }
  function syncOpenExp() {
    const on = readAutoOpen();
    openExpBoxes.forEach(cb => { cb.checked = on; });
  }
  openExpBoxes.forEach(cb => {
    cb.onchange = () => {
      try { localStorage.setItem(OPEN_EXP_KEY, cb.checked ? '1' : '0'); } catch (e) { }
      syncOpenExp();
      toastMsg(cb.checked
        ? '导出完成后会自动打开文件夹'
        : '已关闭自动打开文件夹（导出位置仍会写在页面上）');
    };
  });
  if (openExpBoxes.length) syncOpenExp();
  $('#btnRefreshExp').onclick = loadExports;

  /** 导出一结束就把结论写死在页面上。
   *  底部任务条十几秒就收走、操作记录也会自动折叠 —— 用户回头想找
   *  "刚才那个文件导到哪去了"必须有个不会消失的地方。
   *  另外：**勾不勾「自动打开文件夹」都要写**，以前不勾就完全查不到位置。 */
  async function afterExportDone(j) {
    let d = null;
    try { d = await api('exports'); } catch (e) { }
    const items = (d && d.items) || [];
    // 文件名前缀各不相同（译文 / 视频 / CG），按修改时间取最新的那个
    const newest = items.slice().sort(function (a, b) {
      return String(b.at).localeCompare(String(a.at));
    })[0];
    const dir = (d && d.dir) || '导出目录';
    const file = newest
      ? newest.name + '（' + human(newest.bytes) + (newest.at ? '，' + newest.at : '') + '）'
      : (j.message || '');
    const isTr = j.kind === 'transzip';
    const box = $(isTr ? '#trExpLast' : '#expLast');
    if (box) {
      box.dataset.set = '1';        // 标记已写过，别被 loadExports 的"最近文件"覆盖
      box.innerHTML = '上次导出：<b>' + esc(file) + '</b><br>保存在 <b>' + esc(dir) + '</b>';
    }
    const r = $('#trExpResult');
    if (isTr && r) r.textContent = '';
    notify('导出完成：' + file + ' → 保存在 ' + dir, 'ok');
    loadExports();
    if (readAutoOpen()) openDir('export', '导出目录');
  }

  async function loadExports() {
    try {
      const d = await api('exports');
      const box = $('#expList');
      box.innerHTML = (d.items || []).map(f => `
        <div><span class="nm">${esc(f.name)}</span>
          <span class="sz">${human(f.bytes)}</span>
          <span class="sz">${esc(f.at)}</span></div>`).join('')
        || '<div class="dim">还没有导出过文件</div>';
      // 刚打开页面时，把"上一次导出的文件 + 保存位置"也摆出来（不留空提示）；
      // 已经由 afterExportDone 写过的不覆盖，避免越写越旧。
      const last = $('#expLast');
      const first = (d.items || [])[0];
      if (last && first && !last.dataset.set) {
        last.dataset.set = '1';
        last.innerHTML = '最近导出的文件：<b>' + esc(first.name) + '</b>（'
          + human(first.bytes) + '，' + esc(first.at) + '）'
          + (d.dir ? '<br>保存在 <b>' + esc(d.dir) + '</b>' : '');
      }
    } catch (e) { }
  }

  // ---------------------------------------------------------------- 译文
  function renderTrans() {
    const t = (ST.state && ST.state.trans) || {};
    $('#transStats').innerHTML = [
      ['已有中文的剧情', `${t.units || 0}<small>部</small>`, ''],
      ['已译句数', `${(t.messages || 0).toLocaleString()}<small>句</small>`, ''],
      ['怎么用', '播放器里看', '这些剧情会自动显示中文'],
    ].map(([k, v, s]) => `<div class="stat"><div class="k">${esc(k)}</div>
      <div class="v">${v}</div><div class="k">${esc(s)}</div></div>`).join('');
  }

  /* 「自己动手翻」教程：正文、浮窗、开合都在 assets/js/trans_tut.js 里，
     播放器的「翻译」面板用同一份 —— 这里只负责把按钮绑上去，
     别再往本文件写教程文案（以前两处各一份，改一次要改两处）。 */
  if (window.TRANS_TUT) TRANS_TUT.bind('#btnTutTrans');

  // ---------------------------------------------------------------- 批量导入（txt / 文件夹 / zip）
  let trSrc = 'multi';
  let trMode = 'merge';    // merge 合并 / replace 覆盖
  let trPending = [];      // [{name, unit, text}]
  let trZipBlob = null;

  /* 三步式界面里，「选择文件」按钮的文案跟着 ① 的类型走，
     免得用户点了「整个文件夹」还去猜接下来要选什么（用户 2026-11-03）。 */
  const TR_PICK_TX = {
    multi: '选择 txt 文件…',
    dir: '选择文件夹…',
    zip: '选择 zip 压缩包…',
  };

  function trCfgInput() {
    const inp = $('#trInput');
    inp.value = '';
    inp.webkitdirectory = (trSrc === 'dir');
    inp.multiple = (trSrc === 'multi');
    inp.accept = (trSrc === 'zip') ? '.zip' : '.txt';
    const pick = $('#trPick');
    if (pick) pick.textContent = TR_PICK_TX[trSrc] || '选择文件…';
  }

  /* 没选到东西时「导入」按不动 —— 以前点了只会弹一句 toast，白点一次 */
  function syncTrImportBtn() {
    const btn = $('#btnTrImportBulk');
    if (!btn) return;
    btn.disabled = trSrc === 'zip' ? !trZipBlob : !trPending.length;
  }

  function trPick() { trCfgInput(); $('#trInput').click(); }

  /* 认剧情标识：**只认本页导出时写的那一行** `# 剧情标识 : char_415`。
   * 认不出来就明确告诉用户"重新导出原文再翻"，不再退化成拿整个文件名当标识 ——
   * 那样 `char_415_トキサダ.txt` 会变成 unit「char_415_トキサダ」，导入必然失败，
   * 报的还是"没有剧情数据：data/xxx.js"这种用户看不懂的错（用户 2026-11-02）。 */
  function detectUnit(text) {
    const m = String(text || '').match(/#\s*剧情标识\s*:\s*((?:char|adv)_\d+)/i);
    return m ? m[1] : '';
  }
  /** 文件名里带合法编号（导出时就是 char_415_角色名.txt）也可以认 */
  function unitFromName(fname) {
    const m = String(fname || '').match(/(?:^|[^\w])((?:char|adv)_\d+)/i);
    return m ? m[1] : '';
  }
  function setTrSel(s) { $('#trBulkInfo').textContent = s; }

  async function trOnFiles(files) {
    trZipBlob = null;
    if (trSrc === 'zip') {
      const f = files[0];
      if (!f) { setTrSel('还没选文件'); return; }
      if (!/\.zip$/i.test(f.name)) { toastMsg('请选择 .zip 压缩包', true); return; }
      trZipBlob = f;
      setTrSel('已选压缩包：' + f.name + '（' + human(f.size) + '）—— 点「导入」解包并导入');
      syncTrImportBtn();
      return;
    }
    const arr = [...files].filter(f => f.name.toLowerCase().endsWith('.txt'));
    const skip = files.length - arr.length;
    trPending = [];
    const bad = [];
    for (const f of arr) {
      const text = await f.text();
      const unit = detectUnit(text) || unitFromName(f.name);
      if (!unit) { bad.push(f.name); continue; }      // 认不出就不导，别硬塞
      trPending.push({ name: f.name, unit, text });
    }
    const res = $('#trResult');
    if (bad.length) {
      // 提示必须说清"怎么办"：这不是"格式错误"，是"这不是本页导出的原文"
      if (res) {
        res.innerHTML = '<div class="warn" style="margin:0 0 8px;line-height:1.9">'
          + '<b>有 ' + bad.length + ' 个 txt 认不出是哪部剧情，已跳过</b>：'
          + esc(bad.slice(0, 3).join('、')) + (bad.length > 3 ? ' 等' : '') + '<br>'
          + '它们开头缺少「<code># 剧情标识 : char_xxx</code>」这一行，或者被改成了别的样子。<br>'
          + '<b>请重新点「导出所选剧情」拿到原文，在导出的文件里填译文</b>，再导回来。'
          + '</div>';
      }
      toastMsg('有 ' + bad.length + ' 个文件认不出是哪部剧情，已跳过', true);
    } else if (res) {
      res.innerHTML = '';                             // 上次的提示别留在页面上
    }
    let s = '已选 ' + arr.length + ' 个 txt';
    if (skip) s += '（跳过 ' + skip + ' 个非 txt）';
    if (bad.length) s += '；其中 ' + bad.length + ' 个认不出是哪部剧情，会被跳过';
    s += trPending.length ? ' —— 点「导入」' : ' —— 没有可导入的文件';
    setTrSel(s);
    syncTrImportBtn();
  }

  function trRenderBulk(d) {
    const units = d.units || [];
    const okU = units.filter(u => u.ok).length;
    const failU = units.filter(u => !u.ok).length;
    let html = '✅ 成功 ' + okU + ' 部 · 失败 ' + failU + ' 部 · ' +
      '新增 ' + d.added + ' · 更新 ' + d.updated + ' · 跳过 ' + d.skipped + ' · 格式错误 ' + d.bad;
    const errs = units.filter(u => !u.ok);
    if (errs.length) {
      html += '<div style="margin-top:6px;color:#ffb3ba">' +
        errs.map(u => esc(u.unit) + '：' + esc(u.error || '')).join('<br>') + '</div>';
    }
    $('#trResult').innerHTML = html;
  }

  const trSrcBox = $('#trSrc');
  if (trSrcBox) {
    trSrcBox.addEventListener('click', ev => {
      const btn = ev.target.closest('button[data-src]');
      if (!btn) return;
      trSrc = btn.dataset.src;
      [...trSrcBox.children].forEach(b => b.classList.toggle('on', b === btn));
      trPending = []; trZipBlob = null;
      setTrSel('还没选文件');
      trCfgInput();          // 顺手把「选择文件」按钮的文案换成对应的说法
      syncTrImportBtn();     // 换了类型，之前选的文件就不算数了
      const res = $('#trResult');
      if (res) res.innerHTML = '';
    });

    /* ③ 导入方式：合并 / 覆盖。原来是个裸下拉框，用户不知道在问什么，
       现在点选即生效，每个选项自带一句后果说明。 */
    const trModeBox = $('#trModeBox');
    if (trModeBox) {
      trModeBox.addEventListener('click', ev => {
        const btn = ev.target.closest('button[data-mode]');
        if (!btn) return;
        trMode = btn.dataset.mode === 'replace' ? 'replace' : 'merge';
        [...trModeBox.children].forEach(b => b.classList.toggle('on', b === btn));
      });
    }

    $('#trPick').onclick = trPick;
    $('#trInput').onchange = ev => trOnFiles(ev.target.files);
    $('#btnTrImportBulk').onclick = async () => {
      const mode = trMode;
      try {
        if (trSrc === 'zip') {
          if (!trZipBlob) { toastMsg('先选一个 zip 压缩包', true); return; }
          $('#trResult').textContent = '正在解压并导入…（进度见底部）';
          const r = await fetch('api/import/trans/zip?mode=' + encodeURIComponent(mode),
            { method: 'POST', body: trZipBlob });
          const d = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
          connectJobs(true);            // 进度条走底部任务条
          toastMsg('已开始导入，进度见底部');
        } else {
          if (!trPending.length) {
            toastMsg('没有可导入的文件 —— 请先「导出所选剧情」拿到原文，填好译文再导回来', true);
            return;
          }
          $('#trResult').textContent = '正在导入…（进度见底部）';
          const items = trPending.map(p => ({ unit: p.unit, text: p.text }));
          await startJob('import/trans/multi', { items, mode },
            '已开始导入，进度见底部');
        }
      } catch (e) {
        $('#trResult').textContent = '失败：' + e.message;
      }
    };
  }

  /* 译文批量导出：剧情选择用 storylist.js（角色 / 主线 / 活动 / 其他 四个分类），
     只有装好的剧情能勾 —— 没装就没有剧本可导。 */
  ST.trSel = ST.trSel || new Set();

  function updateTrInfo() {
    const el = $('#trSelInfo');
    if (!el) return;
    el.textContent = ST.trSel.size
      ? '已选 ' + ST.trSel.size + ' 部剧情' : '未选择剧情';
  }

  /** 译文状态徽标：有译文 / 未翻译 / 未安装 */
  function transBadge(k) {
    if (stateOf(k) === 'missing') return { cls: 'no', text: '未安装' };
    const have = (ST.state && ST.state.trans && ST.state.trans.list) || [];
    return have.indexOf(k) >= 0
      ? { cls: 'have-tr', text: '有译文' } : { cls: 'must', text: '未翻译' };
  }

  function ensureTrList() {
    if (trListEl) return trListEl;
    trListEl = SL.create({
      bar: '#trBar', list: '#trList', sel: ST.trSel, src: 'char',
      units: packUnits,
      status: stateOf,
      banners: bannersReady,
      disabled: k => stateOf(k) === 'missing',
      badge: transBadge,
      /* 这里**不给每行放「填入」按钮**（用户 2026-11-02 反馈：用不上、白占位置）。
         正常情况下导出的文件里就带着「剧情标识」，导入时自动认，不需要人工指定。 */
      onChange: () => updateTrInfo(),
      empty: () => '还没有已安装的剧情 —— 先到「剧情安装」装一部',
    });
    return trListEl;
  }

  function renderTrList() {
    const box = $('#trList');
    if (!box) return;
    if (!ST.units.loaded) {
      box.innerHTML = '<div class="sl-empty">正在读取剧情列表…</div>';
      loadUnits().then(() => { renderTrList(); renderPacks(); }).catch(() => { });
      return;
    }
    ensureTrList().refresh();
    updateTrInfo();
  }

  if ($('#trAll')) {
    $('#trAll').onclick = () => { ensureTrList().selectAll(); updateTrInfo(); };
    $('#trNone').onclick = () => {
      ST.trSel.clear();
      ensureTrList().refresh();
      updateTrInfo();
    };
    /* 「全选已翻译 / 全选未翻译」都**跟着列表当前的筛选走**
       （分类 / 安装状态 / 搜索词都算），不是无脑全库：
       用户先把列表filter到"主线 + 未安装以外的"，再点按钮才符合直觉。
       筛选结果从 ensureTrList().items() 拿 —— 那就是列表现在画着的那批。 */
    $('#trAllTrans').onclick = () => {
      const have = (ST.state && ST.state.trans && ST.state.trans.list) || [];
      let hit = 0;
      ensureTrList().items().forEach(function (k) {
        if (stateOf(k) === 'missing') return;       // 没装的勾了也导不出
        if (have.indexOf(k) < 0) return;
        ST.trSel.add(k);
        hit++;
      });
      ensureTrList().refresh();
      updateTrInfo();
      if (!hit) toastMsg('当前筛选里没有已翻译的剧情', true);
      else toastMsg('已勾选 ' + hit + ' 部有译文的剧情');
    };
    /* 全选未翻译：同样只看当前筛选；不能只遍历译文清单（那里没有"未翻译"的），
       得遍历列表当前的 items()。 */
    const btnNotTrans = $('#trNotTrans');
    if (btnNotTrans) {
      btnNotTrans.onclick = () => {
        const have = (ST.state && ST.state.trans && ST.state.trans.list) || [];
        let hit = 0;
        ensureTrList().items().forEach(function (k) {
          if (stateOf(k) === 'missing') return;
          if (have.indexOf(k) >= 0) return;
          ST.trSel.add(k);
          hit++;
        });
        ensureTrList().refresh();
        updateTrInfo();
        if (!hit) toastMsg('当前筛选里没有未翻译的剧情', true);
        else toastMsg('已勾选 ' + hit + ' 部未翻译的剧情');
      };
    }
  }

  const btnTrExport = $('#btnTrExport');
  if (btnTrExport) {
    btnTrExport.onclick = async () => {
      const units = Array.from(ST.trSel);
      const res = $('#trExpResult');
      if (!units.length) { toastMsg('先选要导出的剧情', true); return; }
      if (res) res.textContent = '正在打包…';
      try {
        await api('export/trans/batch', {
          method: 'POST', body: JSON.stringify({ units }),
        });
        if (res) res.textContent = '';
        toastMsg('开始导出 ' + units.length + ' 部，完成后见底部任务条');
        connectJobs(true);
      } catch (e) {
        if (res) res.textContent = '失败：' + e.message;
      }
    };
  }

  // ---------------------------------------------------------------- 更新
  // 「更新」只有一个入口：后端 _refresh_manifest_and_wiki 顺序跑
  // ① 刷新官方清单 ② 更新角色信息。第二步失败不影响第一步。
  $('#btnRefreshManifest').onclick = () => {
    const box = $('#manResult');
    if (box) box.textContent = '已开始，进度见底部';
    const w = $('#wikiResult');
    if (w) w.textContent = '';
    const tip = $('#wikiProxyTip');
    if (tip) tip.style.display = 'none';
    startJob('manifest/refresh', {}, '更新剧情目录');
  };
  /** 只补角色资料：线路全不通（第二步失败）时的重试入口。
   *  数据仍优先走 GitHub（多线路，与「检查程序更新」共用线路表）。 */
  function wikiSync() {
    const res = $('#wikiResult');
    if (res) res.textContent = '正在获取角色资料…';
    const tip = $('#wikiProxyTip');
    if (tip) tip.style.display = 'none';
    startJob('wiki/sync', {}, '更新角色资料');
  }
  const btnWikiRetry = $('#btnWikiRetry');
  if (btnWikiRetry) btnWikiRetry.onclick = () => wikiSync();

  $('#btnCheckUpdate').onclick = async () => {
    $('#updResult').textContent = '检查中…';
    try {
      const d = await api('update/check');
      if (d.error) { $('#updResult').textContent = '⚠ ' + d.error; return; }
      ST.updUrl = d.url || '';
      const ready = d.staged || '';
      if (ready) {
        $('#updResult').innerHTML =
          `新版本 <b>v${esc(ready)}</b> 已经下载好，点右侧按钮重启即生效（不用再等下载）`;
      } else if (d.hasUpdate) {
        $('#updResult').innerHTML =
          `发现新版本 <b>${esc(d.latest)}</b>（当前 ${esc(d.current)}）`;
      } else {
        $('#updResult').innerHTML = `已是最新版（当前 v${esc(d.current)}）`;
      }
      const applyBtn = $('#btnApplyUpdate');
      applyBtn.textContent = ready ? '重启并应用' : '下载并应用';
      applyBtn.disabled = !ready && !d.hasUpdate;
      // 「检查更新」发现新版时，把 Release 说明（更新修复了哪些功能）也显示出来。
      // 只显示"这一版改了什么"（服务端已裁掉历史版本与下载说明），
      // 想看全文给一条链接，别把几千字塞进这个小框。
      const notesBox = $('#updNotes');
      if (notesBox) {
        if ((ready || d.hasUpdate) && d.notes) {
          const more = d.html
            ? `<p style="margin:10px 0 0"><a href="${esc(d.html)}" target="_blank" rel="noopener">在浏览器里打开完整发布说明</a></p>`
            : '';
          notesBox.innerHTML = renderMd(d.notes) + more;
          notesBox.style.display = 'block';
        } else {
          notesBox.innerHTML = '';
          notesBox.style.display = 'none';
        }
      }
    } catch (e) {
      $('#updResult').textContent = '⚠ ' + e.message;
    }
  };
  $('#btnApplyUpdate').onclick = async () => {
    if (!confirm('会先下载新版本，然后程序自动重启一次。\n\n' +
      '你的素材、译文、设置都不会丢。继续？')) return;
    try {
      await api('update/apply', {
        method: 'POST', body: JSON.stringify({ url: ST.updUrl }),
      });
      toastMsg('开始更新，程序会自动重启，请稍候…');
      connectJobs(true);
    } catch (e) { toastMsg(e.message, true); }
  };
  // 「每次启动程序后检查更新」：存到 userdata/config.json（autoCheckUpdate）
  const setAutoCheck = $('#setAutoCheck');
  if (setAutoCheck) {
    setAutoCheck.onchange = async () => {
      setAutoCheck.dataset.touched = '1';    // 用户的选择不要再被设置值覆盖
      const on = setAutoCheck.checked;
      try {
        await api('config', {
          method: 'POST', body: JSON.stringify({ autoCheckUpdate: on }),
        });
        if (ST.state) {
          ST.state.config = Object.assign({}, ST.state.config || {},
            { autoCheckUpdate: on });
        }
        toastMsg(on ? '已开启：每次启动会检查一次更新' : '已关闭：启动时不再检查更新');
      } catch (e) { toastMsg(e.message, true); }
    };
  }

  // ---------------------------------------------------------------- 设置
  function renderSettings() {
    const L = ST.links || {};
    // 默认地址以密文形式内置（web/data/packs.links.js），这里填的是"用户覆盖值"
    $('#setBase').value = L.custom ? (L.cdn || L.base || '') : '';
    $('#setBase').placeholder = L.effective
      ? '默认地址：' + L.effective
      : 'https://你的镜像地址';
    $('#srcNow').innerHTML = L.effective
      ? `当前用的地址：<code>${esc(L.effective)}</code>`
        + (L.custom ? '（你自己填的）' : '（程序自带）')
      : '⚠ 没有可用的下载地址，安装会失败。请填一个，或点「刷新」重试。';
    const cfg = (ST.state && ST.state.config) || {};
    // 「保留压缩包」复选框已随 zip 模式一起移除，别再读它（会报 null.checked）
    if ($('#setThreads2')) $('#setThreads2').value = cfg.threads || 32;
    // 每次启动检查更新：默认勾选（后端 DEFAULTS.autoCheckUpdate 也是 True）
    const ac = $('#setAutoCheck');
    if (ac && !ac.dataset.touched) ac.checked = cfg.autoCheckUpdate !== false;
    renderUpdateCheck();
  }

  /** 把"启动时那一次自动检查"的结果回显到更新页（只显示一次，别覆盖用户手点的结果） */
  function renderUpdateCheck() {
    const box = $('#updResult');
    if (!box || ST.updShown) return;
    const uc = (ST.state && ST.state.updateCheck) || {};
    if (!uc.done || uc.staged) return;      // 已下好的走顶部"重启即生效"的提示条
    ST.updShown = true;
    if (uc.hasUpdate && uc.latest) {
      box.innerHTML = `启动时检查过：有新版本 <b>v${esc(uc.latest)}</b>`
        + `（当前 v${esc(uc.current)}），点左边的按钮就能下载`;
    } else if (!uc.error) {
      box.innerHTML = `启动时检查过：已是最新版（v${esc(uc.current)}）`;
    }
  }

  $('#btnSaveBase').onclick = async () => {
    const cdn = ($('#setBase').value || '').trim();
    try {
      await api('links', { method: 'POST', body: JSON.stringify({ cdn }) });
      toastMsg(cdn ? '下载地址已保存（只影响这台电脑）' : '已恢复默认下载地址');
      ST.links = await api('links');
      renderSettings(); renderPacks();
    } catch (e) { toastMsg(e.message, true); }
  };
  if ($('#btnSaveCfg')) $('#btnSaveCfg').onclick = async () => {
    try {
      await api('config', {
        method: 'POST',
        body: JSON.stringify({
          threads: +$('#setThreads2').value,
        }),
      });
      toastMsg('设置已保存');
    } catch (e) { toastMsg(e.message, true); }
  };
  $('#btnOpenUserdata').onclick = () => openDir('userdata', '数据目录');
  $('#btnOpenMedia').onclick = () => api('open', {
    method: 'POST', body: JSON.stringify({ which: 'media' }),
  }).catch(e => toastMsg(e.message, true));
  $('#btnClearJobs').onclick = () => api('jobs/clear', { method: 'POST', body: '{}' })
    .then(() => toastMsg('已清空')).catch(e => toastMsg(e.message, true));
  $('#btnShutdown').onclick = async () => {
    if (!confirm('停止本地服务？页面会失效，需要重新双击「启动.bat」。')) return;
    try { await api('shutdown', { method: 'POST', body: '{}' }); } catch (e) { }
    document.body.innerHTML =
      '<div style="padding:60px;text-align:center;color:#7b90a8">' +
      '<h2 style="color:#dbe8f6">服务已停止</h2>' +
      '<p>重新双击项目目录里的 <code>启动.bat</code> 即可再次使用。</p></div>';
  };

  // ---------------------------------------------------------------- 导航
  function goto(sec) {
    // 切页面就结束新手引导：用户点"去下载资源"是立刻想干活，
    // 引导浮层继续挂在那儿会挡住他要点的东西。
    if (window.TOUR && TOUR.active) {
      try { TOUR.done(); } catch (e) { }
    }
    $$('#side button').forEach(b => b.classList.toggle('on', b.dataset.sec === sec));
    $$('main section').forEach(s => s.classList.toggle('on', s.id === 'sec-' + sec));
    if (sec === 'export') loadCg().then(renderCgList).catch(() => { });
    if (sec === 'export') loadExports();
    // 磁盘占用搬到「设置 / 清理空间」了（2026-11），这里跟着改
    if (sec === 'settings') loadStats();
    if (sec === 'trans') renderTrList();
    // 画质页的剧情列表要等 /api/cdn/units 回来才有人名，切过去时补一次
    if (sec === 'quality') renderQuality();
  }
  $$('#side button').forEach(b => { b.onclick = () => goto(b.dataset.sec); });

  // ---------------------------------------------------------------- 启动
  (async () => {
    try {
      await loadAll();
      connectJobs();
      loadExports();
      const q = new URLSearchParams(location.search);
      if (q.get('unit')) {
        const u = q.get('unit');
        ST.sel.add(u);
        // 定位到哪一类就切到哪一类（以前一律停在「角色」，从播放器跳过来
        // 想装主线/活动剧情的人会看到"没勾上任何东西"）
        ST.cat = SL.srcOf(u);
        goto('packs');
        renderPacks();
        if (packList) packList.setSrc(ST.cat);
        toastMsg(stateOf(u) === 'ready'
          ? '「' + unitName(u) + '」已经装好了'
          : '已定位到「' + unitName(u) + '」，确认后点「开始安装」');
      } else if (location.hash) {
        const sec = location.hash.replace('#', '');
        if (document.querySelector('#sec-' + sec)) goto(sec);
      }
    } catch (e) {
      document.querySelector('main').innerHTML =
        `<div class="warn" style="margin:30px">
          连不上本地服务：${esc(e.message)}<br><br>
          请确认是用 <code>启动.bat</code>（或 <code>python -m server</code>）打开的，
          不是双击 html 文件。
         </div>`;
    }
  })();

  document.addEventListener('keydown', e => {
    if (e.key === 'h' || e.key === 'H') {
      if (/input|textarea|select/i.test((e.target || {}).tagName || '')) return;
      helpTour();
    }
  });

  const SETUP_STEPS = [
    { el: '#basePack', title: '① 第一步：先下「基础数据」',
      text: '这里两个都要下。一个告诉程序<b>现在有哪些剧情能装</b>；'
        + '另一个是所有剧情共用的<b>背景和音乐</b>。'
        + '两个都下完，后面才不会没图、没声音。' },
    { el: '#packList', title: '② 第二步：挑想看的剧情',
      text: '在上面搜名字、或者按分类翻，找到就把那一行<b>勾上</b>，再点「开始安装」。'
        + '它的画面、声音、立绘会一起帮你下好，<b>你不需要准备任何文件</b>。' },
    { el: '#jobbar', title: '③ 底下那条是进度',
      text: '能看到下了几成、还剩多久，也能暂停或取消。下完之后还要花点时间「拆开装好」，'
        + '进度会继续往前走，<b>不是卡住了</b>。关掉网页也没关系，下次打开还能接着来。' },
    { el: '#packList', title: '④ 不想看了可以卸载',
      text: '装好的剧情右边有「卸载」，只删它自己的东西。'
        + '<b>大家共用的背景和音乐会留着</b>，别的剧情照常能看。' },
    { el: '#side', title: '⑤ 左边还有别的功能',
      text: '<b>提高CG画质</b>：把图放大变清楚，动手前可以先点「查看效果」对比一眼；'
        + '<b>导出</b>：把剧情录成视频、把图片打包下载；'
        + '<b>翻译</b>：把自己翻好的中文导进来，也能一键挑出还没翻的；'
        + '<b>更新</b>：官方出了新剧情，点一下就能看到；'
        + '<b>设置 / 清理空间</b>：看空间被什么占了、把不要的清掉。' },
    { el: '#btnHelp', title: '⑥ 忘了就点这个「?」',
      text: '任何时候点右上角<b>「?」</b>都能再看一遍这个引导。'
        + '左边「帮助」页里还写了常见问题和几个不容易找到的按钮。' },
  ];

  function helpTour() {
    if (!window.TOUR) return;
    TOUR.start(SETUP_STEPS, { key: 'escah_tour_setup' });
  }
  const hb = document.querySelector('#btnHelp');
  if (hb) hb.onclick = helpTour;
  function startWelcome() {
    if (!window.TOUR) return;
    TOUR.auto({
      key: 'escah_tour_setup',
      steps: SETUP_STEPS,
      title: '资源管理 · 第一次来？',
      intro: '这里可以下载剧情、把画面变高清、导出视频 / CG、导入中文翻译，以及更新程序。花 30 秒认一下界面会省很多事。',
    });
  }
  // 免责声明优先：先让用户看到边界，再弹引导（否则两层遮罩会打架）
  if (window.DISCLAIMER) {
    let agreed = '';
    try { agreed = localStorage.getItem('escah_disclaimer_v1') || ''; } catch (e) { }
    if (agreed) startWelcome();
    else document.addEventListener('disclaimer:accepted', startWelcome);
  } else {
    startWelcome();
  }
})();
