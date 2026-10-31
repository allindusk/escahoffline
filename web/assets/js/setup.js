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
  const ACT = { rows: [], max: 150, hideTimer: null, auto: true };
  const apPanel = $('#actPanel');
  const apBody = $('#apBody');
  const apToggle = $('#apToggle');

  function actExpand() {
    if (!apPanel) return;
    apPanel.classList.remove('collapsed');
    if (apToggle) apToggle.textContent = '收起';
    clearTimeout(ACT.hideTimer);
    // 只有开了「自动收起」才计时；关掉就一直留着，等用户自己收
    if (ACT.auto) ACT.hideTimer = setTimeout(actCollapse, 5000);
  }

  function actCollapse() {
    if (!apPanel) return;
    apPanel.classList.add('collapsed');
    if (apToggle) apToggle.textContent = '展开';
    clearTimeout(ACT.hideTimer);
  }

  function renderAct() {
    if (!apBody) return;
    apBody.innerHTML = ACT.rows.length
      ? ACT.rows.map(function (r) {
        return '<div class="apRow ' + r.lv + '"><b>' + r.at + '</b><span>' +
          esc(r.text) + '</span></div>';
      }).join('')
      : '<div class="apRow"><span>暂无记录</span></div>';
    apBody.scrollTop = apBody.scrollHeight;
  }

  /** 记一条操作/提示。level: info | ok | warn | err */
  function notify(text, level) {
    if (!text) return;
    const t = new Date();
    const at = [t.getHours(), t.getMinutes(), t.getSeconds()]
      .map(function (n) { return String(n).padStart(2, '0'); }).join(':');
    ACT.rows.push({ at: at, text: String(text), lv: level || 'info' });
    if (ACT.rows.length > ACT.max) ACT.rows = ACT.rows.slice(-ACT.max);
    renderAct();
    actExpand();
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
        ? ` · 下载缓存 ${human(cov.bundleCache.bytes)}` : '');
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
    ['下载缓存', human((cov.bundleCache || {}).bytes),
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
      Math.round(keys.length * 45 / 1024) + ' GB。\n\n建议先装一两个试试再全量。确定吗？')) return;
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
    try {
      ST.units.loaded = false;
      await loadAll();
      renderPacks();
    } catch (e) { /* 忽略 */ }
  }

  /* 名字一律用人话（角色名 / 章节标题）。**不再拼「角色 415」「char_415」**：
     角色一共 384 个、ID 却到 415，序号化的写法会让人以为"角色缺了 31 个"。
     取名 / 稀有度 / 实装日期 / 横幅路径统一在 storylist.js 里（②③⑤ 三页共用），
     剧情标题的推算（主线第N部第X章）统一在 common.js 的 ESC.storyTitle。 */
  function unitName(key) { return SL.info(key).name; }

  const stateOf = key => ST.units.state[key] || 'missing';

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
    const pubAny = (pub.back || 0) + (pub.music || 0) + (pub.sfx || 0)
      + (pub.avatar || 0) + (pub.banner || 0) > 0;
    // 后端结构版本：v<2 说明这个服务进程还是加 banner 之前的旧代码
    // （本地服务是常驻进程，改完 Python 必须重启程序；否则前端会看到缺字段，
    //   以前被 `|| 0` 兜成「横幅 0/0」，看起来像"没有横幅"）
    const pubVer = +(pub.v || 0);
    const pubStale = pubAny && pubVer < 2;
    const cnt = (have, total) => (total == null ? '—'
      : (have || 0) + '/' + (total || 0));
    const pubAll = ready && !pubStale && near(pub.back, pub.backTotal)
      && near(pub.music, pub.musicTotal) && near(pub.sfx, pub.sfxTotal)
      && near(pub.avatar, pub.avatarTotal) && near(pub.banner, pub.bannerTotal);

    // 清单描述去掉"已就绪"前缀 —— 状态统一由右侧徽标表达，别在一行里说两遍
    const mfTxt = (cdn.manifest || '').replace(/^已就绪\s*·?\s*/, '');

    const pubLine = pubAny
      ? '背景 ' + cnt(pub.back, pub.backTotal)
      + ' · 音乐 ' + cnt(pub.music, pub.musicTotal)
      + ' · 音效 ' + cnt(pub.sfx, pub.sfxTotal)
      + ' · 头像 ' + cnt(pub.avatar, pub.avatarTotal)
      + ' · 横幅 ' + cnt(pub.banner, pub.bannerTotal)
      + (pubStale ? ' · <b style="color:#f0c05a">程序该重启了</b>' : '')
      : '约 <b>835 MB</b>（背景 205 + 音乐 539 + 音效 31 + 头像 20 + 横幅 40）';

    box.innerHTML =
      '<div class="bp-head"><b>基础数据（必需）</b>' +
      '</div>' +

      // ① 剧情目录
      '<div class="bp-row"><span class="nm">剧情目录</span>' +
      '<span class="dim">' +
      (ready ? esc(mfTxt || '已下载') : '约 5 MB · 决定"现在有哪些剧情能装"') +
      '</span>' +
      (ready ? '<span class="st ok">已就绪</span>'
        : '<button class="primary" id="bpGet">下载</button>') +
      '</div>' +

      // ② 公共素材
      '<div class="bp-row"><span class="nm">公共素材（背景图 / 背景音乐 / 音效 / 头像 / 剧情横幅）</span>' +
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

      // ③ 剧情本体
      '<div class="bp-row"><span class="nm">剧情本体（立绘 / 语音 / 剧本）</span>' +
      '<span class="dim">按剧情单独装，见下方列表</span>' +
      '<span class="st none">按剧情</span></div>';

    const bp = $('#bpPub');
    if (bp) {
      bp.onclick = () => {
        if (!confirm('下载公共素材（背景 / 音乐 / 音效 / 头像 / 剧情横幅，约 835 MB）？\n\n'
          + '· 所有剧情共用，下过之后装任何剧情都不会缺背景、也不会没音乐\n'
          + '· 头像与剧情横幅也在这里：下完侧栏/CG 页才有头像，剧情列表才有标题横幅\n'
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
        '<div class="warn" style="margin:0 0 14px">还没有剧情目录。<br>' +
        '目录决定"现在有哪些剧情能装"，官方出新剧情它会变，所以不随程序附带，' +
        '需要先下载一次（约 5 MB）。</div>' +
        '<button class="primary" id="btnGetManifest">下载剧情目录</button>' +
        '<div class="dim" style="margin-top:10px">约 10–20 秒。</div></div>';
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

  function ensurePackList() {
    if (packList) return packList;
    packList = SL.create({
      bar: '#packBar', list: '#packList', sel: ST.sel, src: ST.cat || 'char',
      units: packUnits,
      status: stateOf,
      banners: bannersReady,
      /* 只有「已装好」的不给再勾。缺语音（soft-missing）/ 不完整（partial）
         必须能勾 —— 勾上再点「开始安装」就是**补齐**：后端按磁盘上已有文件跳过，
         只下缺的那些包再重新导出语音，不用整个卸载重装（以前这几行被禁用，
         用户只能卸载后重下几十 MB）。 */
      disabled: k => stateOf(k) === 'ready',
      badge: k => {
        const b = UNIT_BADGE[stateOf(k)] || UNIT_BADGE.missing;
        return { cls: b[0], text: b[1] };
      },
      rowTitle: k => {
        const st = stateOf(k);
        if (st === 'soft-missing') {
          return '剧本已装、语音没装（能播但没声音）—— 勾上后点「开始安装」只补语音';
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
        if (st !== 'ready') {
          out.unshift({ act: 'fill', label: '补齐', cls: 'ok',
            title: '只下载缺的语音 / 图片并重新导出，不用整个重装' });
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
          + '下载缓存会保留 —— 以后重装不用重新下载。')) return;
        startJob('uninstall', { units: [k] }, '正在卸载…');
      },
      onChange: () => updateSelBar(),
    });
    return packList;
  }

  /** 勾选统计：fresh = 全新安装，fill = 已装但缺东西（缺语音 / 不完整） */
  function selSummary() {
    const fresh = [];
    const fill = [];
    ST.sel.forEach(function (k) {
      const st = stateOf(k);
      if (st === 'missing') fresh.push(k);
      else if (st !== 'ready') fill.push(k);
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
        '</b> 部 · 约 <b>' + Math.round(s.fresh * 45) +
        ' MB</b>（补齐只下缺的包，通常几 MB）';
    } else if (s.fresh) {
      bar.innerHTML = '将安装 <b>' + s.fresh + '</b> 部剧情 · 约 <b>' +
        Math.round(s.fresh * 45) + ' MB</b>（按每部 45 MB 估算）';
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
    if (s.fresh) what.push('安装 ' + s.fresh + ' 部（约 ' + Math.round(s.fresh * 45) + ' MB）');
    if (s.fill) what.push('补齐 ' + s.fill + ' 部（缺语音/不完整，只下缺的包）');
    if (!confirm('即将' + what.join(' + ') + '。\n\n' +
      '· 每部角色剧情约 30–60 MB（语音 + 背景 + 立绘 + CG）\n' +
      '· 下载速度：同时下 ' + threads + ' 个\n' +
      '· 单部约 30–90 秒装好\n' +
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
        ST._fail = (ST._fail || 0) + 1;
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
        // 已完成的任务不再回顾，否则一进页面就刷一屏）
        if (j.state === 'running' || j.state === 'queued') {
          notify('开始：' + (j.title || j.kind || '任务'), 'info');
        }
        continue;
      }
      if (prev === j.state) continue;                        // 状态没变 → 不动
      const ttl = j.title || j.kind || '任务';
      if (j.state === 'running') notify('开始：' + ttl, 'info');
      else if (j.state === 'done') notify('完成：' + ttl, 'ok');
      else if (j.state === 'failed') {
        notify('失败：' + ttl + (j.error ? '：' + j.error : ''), 'err');
      }
      // 批量导出译文完成 → 刷新「已导出文件」列表（zip 就在那儿）
      if (j.state === 'done' && j.kind === 'transzip') loadExports();
      // 导出任务刚跑完 → 按设置自动弹目录（译文 zip 也算导出）
      if (j.state === 'done'
          && (j.kind === 'video' || j.kind === 'cg' || j.kind === 'transzip')) {
        let auto = false;
        try { auto = localStorage.getItem('escah_open_exp') === '1'; } catch (e) { }
        if (auto) openDir('export', '导出目录');
      }
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
      return `<div class="job${cls ? ' ' + cls : ''}">
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
        const b = UNIT_BADGE[stateOf(k)] || UNIT_BADGE.missing;
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
      }).catch(() => { });
    }
    updateUpInfo();
    refreshPlan();
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
    disk.innerHTML = '<div class="dim">统计中…（首次约 1–3 秒）</div>';
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
    // 具体目录只放在鼠标悬停里 —— 小白看路径没意义，排错时悬停就能看到。
    const rows = (d.groups || []).map(function (g) {
      return '<div class="kv" title="' + esc(g.path || '') + '">'
        + '<span class="nm">' + esc(g.label) + '</span>'
        + '<span class="sz">' + human(g.bytes) + '</span></div>';
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
    bundles: () => '删除全部下载缓存（原始压缩包）？\n\n'
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
      + '中断下载留下的临时文件与视频导出的临时上传文件，删了不影响已装剧情。'
      + '\n\n确定清理？',
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
  const goClean = $('#btnGoClean');
  if (goClean) goClean.onclick = () => goto('settings');
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
  let vidList = null;
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
      ['已有中文的剧情', `${t.units || 0}<small>部</small>`, ''],
      ['已译句数', `${(t.messages || 0).toLocaleString()}<small>句</small>`, ''],
      ['怎么用', '播放器里看', '这些剧情会自动显示中文'],
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
    if (!unit) { toastMsg('先填剧情标识 —— 可以用下方列表每行的「填入」', true); return; }
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
      // 上面「批量导入」要填剧情标识，从列表点一下比手打可靠
      actions: () => [{ act: 'fill', label: '填入', title: '把这个剧情标识填到上面的导入框' }],
      onAction: (k, act) => {
        if (act !== 'fill') return;
        const inp = $('#trUnit');
        if (inp) inp.value = k;
        toastMsg('已填入：' + unitName(k));
      },
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
    $('#trAllTrans').onclick = () => {
      const have = (ST.state && ST.state.trans && ST.state.trans.list) || [];
      have.forEach(function (k) {
        if (stateOf(k) !== 'missing') ST.trSel.add(k);
      });
      ensureTrList().refresh();
      updateTrInfo();
    };
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
    { el: '#basePack', title: '① 先下基础数据',
      text: '这是所有剧情的前提。<b>剧情目录</b>告诉程序「现在有哪些剧情能装」，'
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
      text: '<b>提高CG画质</b>：把图片放大变清晰（比较吃电脑，可以先看预估再决定）；'
        + '<b>导出</b>：把剧情存成视频或导出 CG；'
        + '<b>翻译</b>：导入你自己翻译的中文；'
        + '<b>更新</b>：官方出新剧情后点一下更新目录；'
        + '<b>设置 / 清理空间</b>：看空间都被什么占了、一键清掉能删的缓存。' },
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
