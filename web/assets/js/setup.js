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
    cat: 'char', dlFilter: '',        // '' | 'miss' | 'have'
    cgSel: new Set(), vidSel: new Set(), jobs: {}, updUrl: '', links: null,
    // 云端可安装的单元与它们的本地状态（由 /api/cdn/units 提供）
    units: { char: [], adv: [], state: {}, loaded: false, at: 0, needManifest: false },
    pub: null,          // 公共素材（背景/BGM/音效）已下载数量
  };

  // ---------------------------------------------------------------- 工具
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

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
        'z-index:9800;padding:9px 18px;border-radius:10px;font-size:13px;' +
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
  }

  // ---------------------------------------------------------------- 数据
  async function loadAll() {
    const [state, manifest, links, pub] = await Promise.all([
      api('state'), api('manifest'), api('links').catch(() => null),
      api('cdn/public').catch(() => null),   // 公共素材进度（背景/BGM/音效各下了多少）
    ]);
    ST.state = state;
    ST.manifest = manifest;
    ST.links = links;
    ST.pub = pub;
    renderAll();
  }

  const depIndex = () => {
    const m = {};
    ((ST.manifest && ST.manifest.depots) || []).forEach(d => { m[d.id] = d; });
    return m;
  };
  const installed = () => (ST.state && ST.state.installed) || {};

  function renderAll() {
    $('#hdrSub').textContent = 'v' + ((ST.state && ST.state.version) || '?');
    const cov = (ST.state && ST.state.coverage) || {};
    $('#hdrStat').textContent =
      `已装 ${cov.units || 0}/${cov.unitsTotal || 0} 部剧情`
      + (cov.bundleCache && cov.bundleCache.bytes
        ? ` · bundle 缓存 ${human(cov.bundleCache.bytes)}` : '');
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

  $('#ovMin').textContent = '30–60 MB / 部';
  $('#ovCore').textContent = cdn.manifestReady ? '已就绪' : '需联网刷新';
  $('#ovAll').textContent = total ? '约 ' + Math.round(total * 45 / 1024) + ' GB' : '—';

  const cards = [
    ['已安装剧情', `${done}`, `/ ${total || '?'} 部`],
    ['bundle 缓存', human((cov.bundleCache || {}).bytes),
      `${(cov.bundleCache || {}).files || 0} 个包`],
    ['剩余磁盘', human((ST.state && ST.state.free) || 0), ''],
    ['译文', `${(ST.state.trans && ST.state.trans.messages) || 0}<small>句</small>`,
      `${(ST.state.trans && ST.state.trans.units) || 0} 部剧情`],
    ['下载源', (cdn.cdn || '').replace(/^https?:\/\//, '').split('/')[0] || '未配置',
      cdn.manifestReady ? '官方 CDN' : '点「更新 → 刷新清单」'],
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
      Math.round(keys.length * 45 / 1024) + ' GB。\n\n建议先装一两个试试再全量。确定吗？')) return;
    ST.sel = new Set(keys);
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
     所以界面上不再有 core / char_1.zip 这类概念，只有"哪部剧情装了没装"。 */

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
    try {
      ST.units.loaded = false;
      await loadAll();
      renderPacks();
    } catch (e) { /* 忽略 */ }
  }

  const ADV_LABEL = { main: '主线', event: '活动', other: '其他', tutorial: '新手教程', op: '开场动画' };

  function unitName(key) {
    if (key.indexOf('char_') === 0) {
      const id = key.slice(5);
      const nm = (window.__NAMES || {})[id] || '';
      const m = (window.__CHARMETA || {})[id] || {};
      return '角色 ' + id + (nm ? ' · ' + nm : '') + (m.r ? '（' + m.r + '）' : '');
    }
    const adv = (window.__ADV || []).find(a => a.key === key);
    if (adv) {
      const label = ADV_LABEL[adv.kind] || adv.kind;
      return label + (adv.no ? ' 第 ' + adv.no + ' 话' : '');
    }
    return key;
  }

  const stateOf = key => ST.units.state[key] || 'missing';

  function filteredUnits() {
    const q = ($('#packSearch').value || '').trim().toLowerCase();
    let list = ST.cat === 'adv' ? ST.units.adv : ST.units.char;
    if (q) {
      list = list.filter(k => k.toLowerCase().indexOf(q) >= 0 ||
        unitName(k).toLowerCase().indexOf(q) >= 0);
    }
    // 状态筛选：'' 全部 / 'miss' 只看没装的 / 'have' 只看已装的
    const dl = ST.dlFilter || '';
    if (dl === 'miss') list = list.filter(k => stateOf(k) === 'missing');
    if (dl === 'have') list = list.filter(k => stateOf(k) !== 'missing');

    // 排序走共用函数（common.js）：播放器 / CG 页 / 安装列表口径完全一致
    const by = ($('#packSort') || {}).value || 'date-desc';
    return ESC.sortItems(list, by, k => ({
      id: k,
      name: unitName(k),
      date: ESC.dateOf(k),
    }));
  }

  /* ---------------------------------------------------------------- 基础数据
   * 就是"基础包"：浏览剧情**必须先有**的东西。现在它只有一个成员 ——
   * 官方资源清单（5 MB）。索引/目录树/图片映射随程序自带，不需要下载。
   * 单独成块而不是混在剧情列表里，是因为它和"哪个角色"无关，属于全局前提。
   */
  function renderBasePack() {
    const box = $('#basePack');
    if (!box) return;
    const cdn = (ST.state && ST.state.cdn) || {};
    const ready = !!cdn.manifestReady;

    const pub = ST.pub || {};
    // 背景允许 2% 缺口：官方源本身有 11 个 bgimage 包里没有图
    const near = (a, b) => (b ? (a || 0) >= b * 0.98 : true);
    const pubAll = ready && near(pub.back, pub.backTotal)
      && near(pub.music, pub.musicTotal) && near(pub.sfx, pub.sfxTotal);
    const pubAny = (pub.back || 0) + (pub.music || 0) + (pub.sfx || 0) > 0;

    // 清单描述去掉"已就绪"前缀 —— 状态统一由右侧徽标表达，别在一行里说两遍
    const mfTxt = (cdn.manifest || '').replace(/^已就绪\s*·?\s*/, '');

    const pubLine = pubAny
      ? '背景 ' + (pub.back || 0) + '/' + (pub.backTotal || 0)
        + ' · BGM ' + (pub.music || 0) + '/' + (pub.musicTotal || 0)
        + ' · 音效 ' + (pub.sfx || 0) + '/' + (pub.sfxTotal || 0)
      : '约 <b>775 MB</b>（背景 205 + BGM 539 + 音效 31）';

    box.innerHTML =
      '<div class="bp-head"><b>基础数据（必需）</b>' +
      '</div>' +

      // ① 资源清单
      '<div class="bp-row"><span class="nm">资源清单</span>' +
      '<span class="dim">' +
      (ready ? esc(mfTxt || '已下载') : '约 5 MB · 决定"现在有哪些剧情可装"') +
      '</span>' +
      (ready ? '<span class="st ok">已就绪</span>'
        : '<button class="primary" id="bpGet">下载</button>') +
      '</div>' +

      // ② 公共素材
      '<div class="bp-row"><span class="nm">公共素材（背景 / BGM / 音效）</span>' +
      '<span class="dim" title="官方更新后点「补全」只会下新增的，已有的跳过">' +
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

      // ③ 素材本身
      '<div class="bp-row"><span class="nm">素材本身（立绘 / 语音 / 剧本）</span>' +
      '<span class="dim">按剧情单独装，见下方列表</span>' +
      '<span class="st none">按剧情</span></div>';

    const bp = $('#bpPub');
    if (bp) {
      bp.onclick = () => {
        if (!confirm('下载公共素材（背景 / BGM / 音效，约 775 MB）？\n\n'
          + '· 所有剧情共用，下过之后装任何剧情都不会缺背景、也不会没音乐\n'
          + '· 已有的会跳过，重复点不会重下；官方更新后再点也只补新增的\n'
          + '· 下载 + 解包合计约 10–20 分钟（取决于网速），期间可关掉页面\n\n开始？')) return;
        startJob('install/base', {}, '开始下载公共素材');
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
        '<div class="warn" style="margin:0 0 14px">还没有资源清单。<br>' +
        '清单决定"现在有哪些剧情可装"，会随官方更新变化，所以不随程序附带，' +
        '需要先下载一次（约 5 MB）。</div>' +
        '<button class="primary" id="btnGetManifest">下载资源清单</button>' +
        '<div class="dim" style="margin-top:10px">下载后会解密 25,000+ 个资源包的索引，约 10–20 秒。</div></div>';
      $('#btnGetManifest').onclick = () => startJob('manifest/refresh', {},
        '开始下载资源清单');
      updateSelBar();
      return;
    }

    if (!ST.units.loaded) {
      box.innerHTML = '<div style="padding:22px;color:#7b90a8">正在读取官方资源清单…</div>';
      loadUnits().then(() => { renderPacks(); renderOverview(); })
        .catch(e => {
          box.innerHTML = '<div style="padding:22px;color:#ff9aa5">清单读取失败：' +
            esc(e.message) + '<br>点「更新 → 刷新官方清单」重试。</div>';
        });
      updateSelBar();
      return;
    }

    $('#nChar').textContent = '(' + ST.units.char.length + ')';
    $('#nAdv').textContent = '(' + ST.units.adv.length + ')';

    const list = filteredUnits();
    const BADGE = {
      ready: ['ok', '已安装'],
      'soft-missing': ['must', '缺语音'],
      partial: ['no', '不完整'],
      missing: ['no', '未安装'],
    };
    if (!list.length) {
      box.innerHTML = '<div style="padding:22px;color:#7b90a8;text-align:center">' +
        (ST.dlFilter === 'miss' ? '这一类都装好了 —— 把筛选切回「全部剧情」查看'
        : ST.dlFilter === 'have' ? '这一类还没装 —— 把筛选切回「全部剧情」查看'
          : '没有匹配的剧情') +
        '</div>';
    } else {
      box.innerHTML = list.map(function (k) {
        const st = stateOf(k);
        const b = BADGE[st] || BADGE.missing;
        const checked = ST.sel.has(k);
        return '<label class="packrow" data-id="' + esc(k) + '">' +
          '<input type="checkbox" data-id="' + esc(k) + '"' +
          (checked ? ' checked' : '') + (st !== 'missing' ? ' disabled' : '') + '>' +
          '<span class="ttl"><b>' + esc(unitName(k)) + '</b>' +
          '<span class="sub">' + esc(k) + '</span></span>' +
          '<span class="st ' + b[0] + '">' + b[1] + '</span>' +
          (st !== 'missing'
            ? '<button class="rowrm" data-rm="' + esc(k) +
              '" title="删除已解包的资源，释放空间">卸载</button>'
            : '') +
          '</label>';
      }).join('');
    }

    box.querySelectorAll('input[type=checkbox]').forEach(function (cb) {
      cb.onchange = function () {
        if (cb.checked) ST.sel.add(cb.dataset.id); else ST.sel.delete(cb.dataset.id);
        updateSelBar();
      };
    });

    // 卸载：只删"这部剧独占"的东西，背景/CG 与 bundle 缓存默认保留
    box.querySelectorAll('[data-rm]').forEach(function (btn) {
      btn.onclick = function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        const k = btn.dataset.rm;
        if (!confirm('卸载「' + unitName(k) + '」？\n\n'
          + '会删除：剧本、语音、头像、该角色独占的立绘（通常几十 MB）。\n'
          + '背景 / CG 可能被别的剧情共用，会保留。\n\n'
          + '资源包缓存默认保留 —— 以后重装不用重新下载。')) return;
        startJob('uninstall', { units: [k] }, '正在卸载…');
      };
    });
    updateSelBar();
  }

  function selSummary() {
    const keys = [];
    ST.sel.forEach(function (k) { if (stateOf(k) === 'missing') keys.push(k); });
    return { todo: keys.length, keys: keys };
  }

  function updateSelBar() {
    const s = selSummary();
    const bar = $('#selInfo');
    if (!bar) return;
    bar.innerHTML = s.todo
      ? '将安装 <b>' + s.todo + '</b> 部剧情 · 约 <b>' +
        Math.round(s.todo * 45) + ' MB</b>（按每部 45 MB 估算）'
      : (ST.sel.size ? '选中的都已安装' : '未选择');
    const btn = $('#btnInstall');
    if (btn) btn.disabled = !s.todo;
  }

  $('#packSearch').oninput = () => renderPacks();
  if ($('#packSort')) {
    $('#packSort').onchange = () => renderPacks();
  }
  if ($('#packDl')) {
    $('#packDl').onchange = () => {
      ST.dlFilter = $('#packDl').value;
      renderPacks();
    };
  }
  $$('#packTabs button').forEach(b => {
    b.onclick = () => {
      $$('#packTabs button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      ST.cat = b.dataset.k;
      renderPacks();
    };
  });
  if ($('#pickAll')) {
    $('#pickAll').onclick = () => {
      filteredUnits().forEach(function (k) {
        if (stateOf(k) === 'missing') ST.sel.add(k);
      });
      renderPacks();
    };
  }
  if ($('#pickNone')) {
    $('#pickNone').onclick = () => { ST.sel.clear(); renderPacks(); };
  }
  if ($('#pickByChar')) $('#pickByChar').hidden = true;
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
      8: '保守：约 0.5–1 MB/s，几乎不会失败',
      16: '稳妥：约 1–3 MB/s',
      32: '推荐：约 3–9 MB/s（实测 32 并发能到 9 MB/s）',
      64: '极限：能吃满带宽，但个别包失败率会上升',
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
  $('#btnInstall').onclick = async () => {
    const s = selSummary();
    if (!s.todo) return;
    const threads = +$('#optThreads').value;
    if (!confirm('将从官方 CDN 安装 ' + s.todo + ' 部剧情。\n\n' +
      '· 每部角色剧情约 30–60 MB（语音 + 背景 + 立绘 + CG）\n' +
      '· 下载并发 ' + threads + '（官方 CDN 单连接只有 60–100 KB/s，并发越高越快）\n' +
      '· 装完会在本机解包并合成立绘，单部约 30–90 秒\n\n开始？')) return;
    try {
      await api('download', {
        method: 'POST',
        body: JSON.stringify({
          selected: s.keys,
          threads: threads,
          upscale: $('#optUpscale') ? $('#optUpscale').checked : false,
          scale: 2,
        }),
      });
      toastMsg('已开始安装，进度见底部');
      connectJobs(true);
    } catch (e) {
      toastMsg('启动失败：' + e.message, true);
    }
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
        renderJobs(d.jobs || []);
      } catch (e) { /* 服务停了就别刷了 */ }
    };
    tick();
    jobTimer = setInterval(tick, 1200);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  }

  function renderJobs(list) {
    const bar = $('#jobbar');
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
    }

    // 导出任务跑完 → 按用户设置决定要不要自动弹出文件夹
    const expDone = (list || []).find(
      j => (j.kind === 'video' || j.kind === 'cg') && j.state === 'done');
    if (expDone && ST._expShown !== expDone.id) {
      ST._expShown = expDone.id;
      let auto = false;
      try { auto = localStorage.getItem('escah_open_exp') === '1'; } catch (e) { }
      if (auto) openDir('export', '导出目录');
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
          '（更新包 3–5 MB；也可下全量包）</li>' +
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

    if (!list || !list.length) { bar.innerHTML = ''; return; }
    const now = Date.now() / 1000;
    // 只有"还在跑"的常驻；结束的（done / 取消 / 失败）留 6 秒就收起来。
    // 以前只过滤 done，导致**取消的任务永远挂在底部条上**不走。
    list = list.filter(j => {
      if (j.state === 'running' || j.state === 'queued') return true;
      const endAt = (j.ended || (j.started || j.created) + (j.elapsed || 0));
      return now - endAt < 6;
    }).slice(0, 3);
    if (!list.length) { bar.innerHTML = ''; return; }
    bar.innerHTML = list.map(j => {
      const pct = j.state === 'done' ? 100 : (j.percent || 0);
      // 速度单位跟着任务走：下载是字节，截帧是"帧"、安装是"包"、超分是"张"。
    // 以前一律按字节显示，截帧阶段就会出现 "0 B/s" 这种莫名其妙的信息。
    const u = j.unit || 'B';
    const sp = j.speed
      ? (u === 'B'
        ? ` · ${human(j.speed)}/s`
        : ` · ${j.speed.toFixed(1)} ${u}/秒`)
      : '';
      const eta = (j.eta && j.state === 'running') ? `剩 ${secs(j.eta)}` : '';
      const cls = [j.state === 'failed' ? 'failed' : '',
        j.state === 'done' ? 'done' : '',
        j.paused ? 'paused' : ''].filter(Boolean).join(' ');
      // 速度只在右侧数值区显示一次（之前这里也拼了一次，导致两处重复、把布局挤乱）
      const msg = (j.error ? '⚠ ' + j.error : (j.message || ''));
      // 进度条要说清四件事：百分比、已下/总量（字节与个数）、速度、剩余
      const bd = j.bytesDone || 0;
      const bt = j.bytesTotal || 0;
      const bsp = j.bytesSpeed || 0;
      const line1 = [pct.toFixed(1) + '%',
        (bt ? `${human(bd)} / ${human(bt)}` : '')].filter(Boolean).join(' · ');
      const line2 = [
        (j.total ? `${Math.round(j.done)}/${Math.round(j.total)} ${j.unit || '个'}` : ''),
        (bsp ? `${human(bsp)}/s` : ''),
        eta,
      ].filter(Boolean).join(' · ');
      return `<div class="job${cls}">
        <span class="jt">${esc(j.title)}</span>
        <span class="jm" title="${esc(msg)}">${esc(msg)}</span>
        <span class="jp"><i style="width:${pct}%"></i></span>
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
    // 有任务刚结束 → 重新拉状态（清单/依赖/安装都会改变列表）
    const done = list.some(j => j.state === 'done' || j.state === 'failed');
    if (done && !ST._refreshT) {
      ST._refreshT = setTimeout(() => {
        ST._refreshT = null;
        reloadAfterJobs();
      }, 1200);
    }
  }

  // ---------------------------------------------------------------- 画质
  function renderQuality() {
    const eng = (ST.state && ST.state.engine) || {};
    $('#engineInfo').textContent = '当前引擎：' + (eng.note || '未知')
      + (ST.state && ST.state.pillow ? '（Pillow 已就绪）' : '（缺少 Pillow，超分与转码不可用）');
    refreshPlan();
  }

  /* 超分预估：多少张 / 原图多大 / 放大到多少 / 估计要多久
     超分是最容易让人后悔的操作（几分钟、CPU 满载），光写"很慢"没说服力。 */
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
    try {
      const d = await api('upscale/plan?kinds='
        + encodeURIComponent(kinds.join(','))
        + '&scale=' + encodeURIComponent($('#upScale').value));
      ST._plan = d;
      if (!d.total) {
        box.innerHTML = '<span class="dim">'
          + esc(d.hint || '没有需要超分的图（已做过的会自动跳过）') + '</span>';
        return;
      }
      const LAB = { stand: '立绘', back: '背景', cg: 'CG', standfull: '全身立绘' };
      const by = Object.keys(d.byKind || {})
        .map(k => (LAB[k] || k) + ' ' + d.byKind[k]).join(' · ');
      const s = d.sample;
      box.innerHTML =
        '<b>本次待超分 ' + d.total + ' 张</b>（' + esc(by) + '）<br>'
        + (s ? '原图 ' + s.w + '×' + s.h + ' → <b>' + s.outW + '×' + s.outH
          + '</b>（' + d.scale + ' 倍）<br>' : '')
        + '单张约 <b>' + d.perImageSec + ' 秒</b>，预计共 <b>' + esc(d.etaText) + '</b>'
        + '<div class="dim" style="margin-top:6px">按当前引擎（'
        + esc(d.engineNote || d.engine) + '）估算，实际有 ±30% 波动。'
        + '超分会占满 CPU，期间电脑变卡、风扇会响；随时可取消，已完成的不白做。</div>';
    } catch (e) {
      box.innerHTML = '<span class="dim">预估失败：' + esc(e.message) + '</span>';
      ST._plan = null;
    }
  }

  $$('.upk').forEach(c => { c.onchange = () => refreshPlan(); });
  if ($('#upScale')) $('#upScale').onchange = () => refreshPlan();

  /* 磁盘统计要遍历 24 万个文件（服务端约 1–2 秒），所以只在真的打开这一页时算，
     并且服务端本身还带 30 秒缓存。 */
  let _statsAt = 0;
  async function loadStats(force) {
    if (!force && Date.now() - _statsAt < 20000) return;
    _statsAt = Date.now();
    const disk = $('#diskStats');
    if (!disk) return;
    disk.innerHTML = '<div class="dim">统计中…（首次约 1–2 秒）</div>';
    try {
      const d = await api('stats' + (force ? '?force=1' : ''));
      const img = d.img || {};
      const rows = [
        ['原图 img/', human((img.img || {}).bytes), `${(img.img || {}).files || 0} 个文件`],
        ['超分 img_x2/', human((img.img_x2 || {}).bytes), `${(img.img_x2 || {}).files || 0} 个文件`],
        ['语音 audio/', human((img.audio || {}).bytes), `${(img.audio || {}).files || 0} 个文件`],
        ['剩余磁盘', human(img.free), '统计于 ' + (img.at || '-')],
      ];
      disk.innerHTML = rows.map(([k, v, s]) => `
        <div class="stat"><div class="k">${esc(k)}</div>
          <div class="v">${esc(v)}</div><div class="k">${esc(s)}</div></div>`).join('');
    } catch (e) {
      disk.innerHTML = '<div class="dim">统计失败：' + esc(e.message) + '</div>';
    }
  }

  $('#btnUpscale').onclick = async () => {
    const kinds = $$('.upk').filter(c => c.checked).map(c => c.value);
    if (!kinds.length) { toastMsg('至少选一类图', true); return; }
    const p = ST._plan;
    const eta = (p && p.total)
      ? `\n本次 ${p.total} 张，单张约 ${p.perImageSec} 秒，预计 ${p.etaText}` : '';
    if (!confirm(`将对已下载的【${kinds.join(' / ')}】做超分（${$('#upScale').value} 倍）。${eta}\n\n` +
      `这会占用大量 CPU，期间电脑会变卡；可以随时取消，已完成的不会重做。继续？`)) return;
    try {
      await api('upscale', {
        method: 'POST',
        body: JSON.stringify({ kinds, scale: +$('#upScale').value }),
      });
      toastMsg('超分已开始');
      connectJobs(true);
    } catch (e) { toastMsg(e.message, true); }
  };
  $('#btnRebuildX2').onclick = async () => {
    try {
      await api('rebuild-x2', { method: 'POST', body: '{}' });
      toastMsg('正在重建超分覆盖层');
      connectJobs(true);
    } catch (e) { toastMsg(e.message, true); }
  };
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
      if (!confirm('确认删除这些"没有被任何已装剧情引用"的资源包？\n\n' +
        '删掉后如果以后重新装回那部剧情，会重新下载一遍。')) return;
      startJob('cdn/prune', { dry: false }, '开始清理未引用的缓存');
    };
  }

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

  function renderExport() {
    const kind = $('#vidKind').value;
    const q = ($('#vidSearch').value || '').trim().toLowerCase();
    let list = (kind === 'char' ? ST.units.char : ST.units.adv)
      .filter(k => (ST.units.state[k] || 'missing') !== 'missing');   // 只列已装的
    if (q) {
      list = list.filter(k => k.toLowerCase().indexOf(q) >= 0 ||
        unitName(k).toLowerCase().indexOf(q) >= 0);
    }
    // 排序走共用函数（common.js），与播放器、CG 页、安装列表完全一致
    list = ESC.sortItems(list, ($('#vidSort') || {}).value || 'date-desc',
      k => ({ id: k, name: unitName(k), date: ESC.dateOf(k) }));
    const box = $('#vidList');
    if (!box) return;

    // 导出视频要 playwright + av + numpy（离线渲染 mp4），缺了先提示
    if (_vidDep === null) {
      box.innerHTML = '<div class="dim" style="padding:14px">检查导出环境…</div>';
      checkVidDeps().then(() => renderExport());
      return;
    }
    if (!_vidDep.ready) {
      box.innerHTML = '<div class="warn" style="margin:0 0 12px;line-height:2">' +
        '缺少导出视频所需的组件：<b>' +
        esc((_vidDep.missing || []).join('、')) + '</b><br>' +
        '它们用来驱动无头浏览器截帧、以及编码 mp4。<br>' +
        '<b>用便携包的：</b>便携版自带，出现这个多半是解压不完整或被杀软拦了，' +
        '重新解压并加白名单。<br>' +
        '<b>从源码克隆的：</b>双击 <code>启动.bat</code>，或手动 ' +
        '<code>pip install playwright av</code>。</div>';
      return;
    }
    // 列表数据可能还没加载（直接进本页时）——加载完自己重画
    if (!ST.units.loaded) {
      box.innerHTML = '<div class="dim" style="padding:14px">正在读取剧情列表…</div>';
      loadUnits().then(() => { renderExport(); renderBasePack(); }).catch(() => { });
      return;
    }
    if (!list.length) {
      box.innerHTML = '<div class="dim" style="padding:14px">' +
        '还没有已安装的剧情 —— 先到「剧情安装」装一部' +
        '</div>';
    } else {
      box.innerHTML = list.map(function (k) {
        return '<label class="charrow" data-id="' + esc(k) + '">' +
          '<input type="checkbox" data-id="' + esc(k) + '"' +
          (ST.vidSel.has(k) ? ' checked' : '') + '>' +
          '<span class="nm">' + esc(unitName(k)) +
          ' <span class="n">' + esc(k) + '</span></span>' +
          '<span class="n">' + (ST.units.state[k] === 'ready' ? '完整' : '缺语音') + '</span>' +
          '</label>';
      }).join('');
      box.querySelectorAll('input[type=checkbox]').forEach(function (cb) {
        cb.onchange = function () {
          if (cb.checked) ST.vidSel.add(cb.dataset.id);
          else ST.vidSel.delete(cb.dataset.id);
          updateVidInfo();
        };
      });
    }
    renderParts();
  }

  function renderParts() {
    // 段落对所有选中角色通用；勾相邻几段 = 连续录制，合成一条
    const count = $('#vidKind').value === 'adv' ? 9 : 6;
    $('#vidParts').innerHTML = Array.from({ length: count }, function (_, i) {
      return '<label class="' + (i < 1 ? 'on' : '') + '">' +
        '<input type="checkbox" value="' + i + '"' + (i < 1 ? ' checked' : '') + '>' +
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
        ? '已选 ' + n + ' 个 · 第 ' + parts.map(i => i + 1).join('、') + ' 段'
        : '未选择角色';
    }
    const h = $('#vidHint');
    if (h) {
      h.innerHTML = n
        ? '将导出 <b>' + n + '</b> 条 mp4（每个角色一条，各含所选 ' + parts.length +
          ' 段，连续段落合并成同一条）'
        : '选角色 → 选段落 → 开始导出';
    }
  }

  $('#vidKind').onchange = () => { ST.vidSel.clear(); renderExport(); };
  if ($('#vidSort')) $('#vidSort').onchange = () => renderExport();
  $('#vidSearch').oninput = () => renderExport();
  $('#vidAll').onclick = () => {
    const kind = $('#vidKind').value;
    (kind === 'char' ? ST.units.char : ST.units.adv).forEach(function (k) {
      if ((ST.units.state[k] || 'missing') !== 'missing') ST.vidSel.add(k);
    });
    renderExport();
  };
  $('#vidNone').onclick = () => { ST.vidSel.clear(); renderExport(); };

  $('#btnVidQueue').onclick = async () => {
    if (!ST.vidSel.size) { toastMsg('先选角色', true); return; }
    const parts = vidParts();
    if (!parts.length) { toastMsg('至少选一段', true); return; }
    const units = Array.from(ST.vidSel);
    if (!confirm('将导出 ' + units.length + ' 条 mp4（每个角色一条，各含所选 ' +
      parts.length + ' 段，连续段落合并成同一条）。\n\n' +
      '· 离线渲染：逐句定格截图后编码，画面稳定、无掉帧\n' +
      '· 实测每句约 2 秒 → 单段约 3–5 分钟，6 段全导约 15–25 分钟\n' +
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

  function renderCgList() {
    const q = ($('#cgSearch').value || '').trim().toLowerCase();
    const chars = ((ST.cg && ST.cg.chars) || []).filter(c =>
      !q || String(c.id).includes(q) || (c.name || '').toLowerCase().includes(q));
    const box = $('#cgList');
    box.innerHTML = chars.slice(0, 400).map(c => `
      <label class="charrow" data-id="${c.id}">
        <input type="checkbox" data-id="${c.id}" ${ST.cgSel.has(c.id) ? 'checked' : ''}>
        <span class="nm">${esc(c.name)} <span class="n">ID ${c.id}</span></span>
        <span class="n">${c.groups.length} 组 · ${c.total} 张</span>
      </label>`).join('') || '<div style="padding:16px;color:#7b90a8">没有匹配</div>';
    box.querySelectorAll('input').forEach(cb => {
      cb.onchange = () => {
        const id = +cb.dataset.id;
        if (cb.checked) ST.cgSel.add(id); else ST.cgSel.delete(id);
        updateCgInfo();
      };
    });
    updateCgInfo();
  }

  function updateCgInfo() {
    const chars = (ST.cg && ST.cg.chars) || [];
    let n = 0;
    ST.cgSel.forEach(id => {
      const c = chars.find(x => x.id === id);
      if (c) n += c.total;
    });
    $('#cgInfo').textContent = `已选 ${ST.cgSel.size} 个角色 · ${n} 张 CG`;
  }

  $('#cgSearch').oninput = () => renderCgList();
  $('#cgAll').onclick = () => {
    ((ST.cg && ST.cg.chars) || []).forEach(c => ST.cgSel.add(c.id));
    renderCgList();
  };
  $('#cgNone').onclick = () => { ST.cgSel.clear(); renderCgList(); };
  $('#btnCgExport').onclick = async () => {
    if (!ST.cgSel.size) { toastMsg('先选角色', true); return; }
    try {
      await api('export/cg', {
        method: 'POST', body: JSON.stringify({ chars: [...ST.cgSel] }),
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

  // 「导出完成后自动打开文件夹」开关（存 localStorage，三页共用）
  const setOpenExp = $('#setOpenExp');
  if (setOpenExp) {
    try { setOpenExp.checked = localStorage.getItem('escah_open_exp') === '1'; } catch (e) { }
    setOpenExp.onchange = () => {
      try {
        localStorage.setItem('escah_open_exp', setOpenExp.checked ? '1' : '0');
      } catch (e) { }
      toastMsg(setOpenExp.checked ? '导出完成后会自动打开文件夹' : '已关闭自动打开文件夹');
    };
  }
  $('#btnRefreshExp').onclick = loadExports;

  async function loadExports() {
    try {
      const d = await api('exports');
      const box = $('#expList');
      box.innerHTML = (d.items || []).map(f => `
        <div><span class="nm">${esc(f.name)}</span>
          <span class="sz">${human(f.bytes)}</span>
          <span class="sz">${esc(f.at)}</span></div>`).join('')
        || '<div class="dim">还没有导出过文件</div>';
    } catch (e) { }
  }

  // ---------------------------------------------------------------- 译文
  function renderTrans() {
    const t = (ST.state && ST.state.trans) || {};
    $('#transStats').innerHTML = [
      ['已译剧情', `${t.units || 0}<small>部</small>`, ''],
      ['已译句数', `${(t.messages || 0).toLocaleString()}<small>句</small>`, ''],
      ['译文目录', 'assets/i18n/zh-Hans', 'key 用日文原文'],
    ].map(([k, v, s]) => `<div class="stat"><div class="k">${esc(k)}</div>
      <div class="v">${v}</div><div class="k">${esc(s)}</div></div>`).join('');
  }
  $('#trFile').onchange = async ev => {
    const f = ev.target.files[0];
    if (f) $('#trFileN').textContent = f.name;
  };
  $('#btnTrImport').onclick = async () => {
    const unit = ($('#trUnit').value || '').trim();
    const f = $('#trFile').files[0];
    if (!unit) { toastMsg('填一下剧情标识，如 char_1', true); return; }
    if (!f) { toastMsg('选一个 txt 文件', true); return; }
    const text = await f.text();
    try {
      const d = await api('import/trans', {
        method: 'POST',
        body: JSON.stringify({ unit, text, mode: $('#trMode').value }),
      });
      $('#trResult').innerHTML =
        `✅ 新增 ${d.added} · 更新 ${d.updated} · 跳过 ${d.skipped} · 格式错误 ${d.bad}；本部共 ${d.total} 句`;
      toastMsg('导入完成，刷新播放器即可看到');
    } catch (e) {
      $('#trResult').textContent = '失败：' + e.message;
    }
  };

  // ---------------------------------------------------------------- 更新
  $('#btnRefreshManifest').onclick = () => {
    const box = $('#manResult');
    if (box) box.textContent = '已开始，进度见底部（首次约 10–20 秒）';
    startJob('manifest/refresh', {}, '开始刷新资源清单');
  };

  $('#btnCheckUpdate').onclick = async () => {
    $('#updResult').textContent = '检查中…';
    try {
      const d = await api('update/check');
      if (d.error) { $('#updResult').textContent = '⚠ ' + d.error; return; }
      ST.updUrl = d.url || '';
      $('#updResult').innerHTML = d.hasUpdate
        ? `发现新版本 <b>${esc(d.latest)}</b>（当前 ${esc(d.current)}）`
        : `已是最新（当前 ${esc(d.current)}${d.latest ? '，远端 ' + esc(d.latest) : ''}）`;
      $('#btnApplyUpdate').disabled = !d.hasUpdate || !d.url;
      if (d.notes) { $('#updNotes').hidden = false; $('#updNotes').textContent = d.notes; }
    } catch (e) {
      $('#updResult').textContent = '⚠ ' + e.message;
    }
  };
  $('#btnApplyUpdate').onclick = async () => {
    if (!confirm('将下载新版本 → 退出当前程序 → 覆盖程序文件 → 自动重启。\n\n' +
      '你的资源、译文、设置都不会丢。继续？')) return;
    try {
      await api('update/apply', {
        method: 'POST', body: JSON.stringify({ url: ST.updUrl }),
      });
      toastMsg('开始更新，程序会自动重启，请稍候…');
      connectJobs(true);
    } catch (e) { toastMsg(e.message, true); }
  };

  // ---------------------------------------------------------------- 设置
  function renderSettings() {
    const L = ST.links || {};
    // 默认地址以密文形式内置（web/data/packs.links.js），这里填的是"用户覆盖值"
    $('#setBase').value = L.custom ? (L.cdn || L.base || '') : '';
    $('#setBase').placeholder = L.effective
      ? '默认（内置加密）：' + L.effective
      : 'https://你的镜像/AssetBundles';
    $('#srcNow').innerHTML = L.effective
      ? `当前生效：<code>${esc(L.effective)}</code>`
        + (L.custom ? '（来自你的自定义配置）' : '（内置加密配置，源码里看不到明文）')
      : '⚠ 没有可用的资源源，安装会失败。请填写或检查 <code>web/data/packs.links.js</code>。';
    const cfg = (ST.state && ST.state.config) || {};
    // 「保留压缩包」复选框已随 zip 模式一起移除，别再读它（会报 null.checked）
    if ($('#setThreads2')) $('#setThreads2').value = cfg.threads || 32;
  }

  $('#btnSaveBase').onclick = async () => {
    const cdn = ($('#setBase').value || '').trim();
    try {
      await api('links', { method: 'POST', body: JSON.stringify({ cdn }) });
      toastMsg(cdn ? '资源源已保存（仅本机生效）' : '已清除自定义源，回到内置加密配置');
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
    if (sec === 'quality') loadStats();
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
        ST.sel.add(q.get('unit'));
        goto('packs');
        renderPacks();
        toastMsg('已定位到 ' + q.get('unit') + '，确认后点「开始安装」');
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
    { el: '#basePack', title: '① 先下基础数据',
      text: '这是所有剧情的前提。<b>资源清单</b>告诉程序「现在有哪些剧情可装」，'
        + '<b>公共素材</b>是所有剧情共用的背景和音乐。'
        + '两个都下完，后面才不会缺图、没声音。' },
    { el: '#packList', title: '② 挑想看的剧情',
      text: '用上面的搜索框找角色，或用右上角的两个下拉换排序和筛选。'
        + '<b>勾选</b>一部就会自动带上它需要的全部资源，然后点「开始安装」。' },
    { el: '#jobbar', title: '③ 看进度',
      text: '底部会显示<b>已下 / 总共、速度、剩余时间</b>，可以暂停或取消。'
        + '关掉网页也没关系，任务会在后台继续，重新打开还能看到。' },
    { el: '#packList', title: '④ 空间不够就卸载',
      text: '装好的剧情右边有「卸载」，只删它自己的剧本、语音和立绘。'
        + '<b>共用素材不会动</b>，别的剧情照常能看。' },
    { el: '#side', title: '⑤ 左边还有别的功能',
      text: '<b>画质</b>：把图片放大变清晰（很吃 CPU，可以先看预估再决定）；'
        + '<b>导出</b>：把剧情存成视频或导出 CG；'
        + '<b>译文</b>：导入你自己翻译的中文；'
        + '<b>更新</b>：官方上新后点一下刷新清单。' },
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
      intro: '这里负责从官方 CDN 安装剧情、超分、导出视频/CG、导入译文和更新程序。花 30 秒认一下界面会省很多事。',
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
