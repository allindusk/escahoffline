/* 移动端适配（三页共用）
 *
 * 只做两件事，桌面 UA 下整段直接 return，电脑页面行为与以前完全一致：
 *   1) 窄屏时给 CG 页补一个「角色列表」抽屉开关（播放器页自带 #sTab 把手）
 *   2) 识别手机 / 平板访问，把"必须在电脑上完成"的功能拦下来并弹窗说明
 *
 * 为什么用 UA 而不是只看窗口宽度：电脑把窗口拖窄也能命中媒体查询，
 * 但那时超分、导出视频其实完全能用，不该弹"请在电脑上使用"。反之 iPad
 * 横屏有 1024px 宽，光看宽度会漏判成桌面。所以判定用 UA（iPadOS 13+
 * 会把自己伪装成 Mac，靠 maxTouchPoints 补一刀）。
 */
(function () {
  'use strict';

  const UA = navigator.userAgent || '';
  const IS_MOBILE =
    /Android|iPhone|iPad|iPod|Mobile|HarmonyOS|Windows Phone/i.test(UA) ||
    (/Macintosh/.test(UA) && navigator.maxTouchPoints > 1);

  const page = (location.pathname.split('/').pop() || 'index.html').toLowerCase();

  // 页面标识：给 body 打类，窄屏样式里 .mb-setup / .mb-cg 才能只命中对应页面。
  // 否则 main { padding } 这类规则会把播放器的舞台也一起缩掉（实测少了 90px）。
  // 2026-11-17 加了原声集页的 mb-music：它的窄屏布局要独占一套（抽屉 + 网格控制条）。
  // 立绘页再单独多一个 mb-stand：它和 CG 页共用 mb-cg 的侧栏骨架，
  // 但卡片尺寸档位要自己算（立绘是竖图，CG 是 16:9）。
  document.body.classList.add(
    page === 'setup.html' ? 'mb-setup'
      : page === 'cg.html' || page === 'stand.html' ? 'mb-cg'
        : page === 'music.html' ? 'mb-music' : 'mb-player');
  if (page === 'stand.html') document.body.classList.add('mb-stand');

  /* ---------------------------------------------------------------- CG 抽屉
   * 播放器页有 #sTab 把手，CG / 立绘页当初没做收起，窄屏下 252px 的侧栏会一直压着
   * 卡片墙。这里补一个浮动按钮 + 一层遮罩；选完角色 / 点空白处自动收起。
   * 2026-11-17：原来只有按钮没有遮罩，抽屉一开就盖掉大半屏幕、只能再点同一个按钮关，
   * 点在卡片墙上的那次点击还会被抽屉"吃掉"；补上遮罩和「✕ 收起」文案。
   */
  function setupCgDrawer() {
    // 立绘/SD 页复用同一套侧栏骨架，抽屉逻辑也跟着复用
    if (page !== 'cg.html' && page !== 'stand.html') return;
    document.body.classList.add('mb-cg');
    const side = document.getElementById('sidebar');
    if (!side || document.getElementById('mbSideBtn')) return;

    const mask = document.createElement('div');
    mask.id = 'mbMask';
    document.body.appendChild(mask);

    const btn = document.createElement('button');
    btn.id = 'mbSideBtn';
    btn.type = 'button';
    document.body.appendChild(btn);

    function setOpen(on) {
      side.classList.toggle('mbOpen', on);
      document.body.classList.toggle('mb-open', on);
      btn.textContent = on ? '✕ 收起' : '☰ 角色列表';
    }
    setOpen(false);

    btn.onclick = function (e) {
      e.stopPropagation();                    // 别触发下面的"点外部收起"
      setOpen(!side.classList.contains('mbOpen'));
    };
    mask.onclick = function () { setOpen(false); };

    // 选完角色就收起，别一直盖着卡片墙
    side.addEventListener('click', function (e) {
      if (e.target.closest('.char-btn')) setOpen(false);
    });
    // 点抽屉外也收起（遮罩之外的边角）
    document.addEventListener('click', function (e) {
      if (!side.classList.contains('mbOpen')) return;
      if (side.contains(e.target) || e.target === btn) return;
      setOpen(false);
    });
  }

  setupCgDrawer();

  if (!IS_MOBILE) return;                     // 桌面到此为止

  /* 手机上没有 Esc 键，CG 灯箱的「关闭 Esc」会让人以为是两段操作；
     换成「✕ 关闭」，顺便省 20px 宽度（顶栏要单行才不亏画面高度）。 */
  if (page === 'cg.html' || page === 'stand.html') {
    const lbc = document.getElementById('lbClose');
    if (lbc) lbc.textContent = '✕ 关闭';
  }

  /* ------------------------------------------------ 播放器首次进入默认收起
   * 手机上左右侧栏各 250–300px，两个都展开会把舞台挤成一条缝。
   * 只处理"从没设置过"的情况（localStorage 无值）；用户自己开合过的偏好不覆盖。
   */
  function autoCollapsePlayerPanels() {
    if (page !== 'index.html') return;
    try {
      const s = document.getElementById('sidebar');
      const st = document.getElementById('sTab');
      if (s && st && localStorage.getItem('escah_sidebar') === null) {
        s.classList.add('hide');
        st.classList.add('show');
        document.body.classList.remove('sideOpen');
      }
      const h = document.getElementById('history');
      const ht = document.getElementById('hTab');
      if (h && ht && localStorage.getItem('escah_hist') === null) {
        h.classList.add('hide');
        ht.classList.add('show');
        document.body.classList.remove('histOpen');
      }
      // 舞台尺寸变了要重算一遍缩放
      window.dispatchEvent(new Event('resize'));
    } catch (e) { /* 无痕模式禁用 localStorage 时忽略 */ }
  }
  autoCollapsePlayerPanels();

  /* ------------------------------------------------ 选完剧情自动收起侧栏
   * 手机上侧栏是盖在画面上的抽屉，点完角色还挡着就得手动点「收起」。
   * 点到真正的剧情条目（.char-btn）后自动收起；点搜索框、筛选、复制按钮不算。
   */
  function autoCloseSidebarOnPick() {
    if (page !== 'index.html') return;
    const side = document.getElementById('sidebar');
    const tab = document.getElementById('sTab');
    if (!side) return;
    side.addEventListener('click', function (e) {
      if (side.classList.contains('hide')) return;
      if (!e.target.closest('.char-btn')) return;   // 不是剧情条目
      if (e.target.closest('.cpy')) return;         // 复制按钮，别收起
      side.classList.add('hide');
      if (tab) tab.classList.add('show');
      document.body.classList.remove('sideOpen');
      try { localStorage.setItem('escah_sidebar', '0'); } catch (err) { }
      window.dispatchEvent(new Event('resize'));
    });
  }
  autoCloseSidebarOnPick();

  /* ------------------------------------------------ 竖屏转屏提示
   * 播放器是 16:9 的横版舞台，竖屏时上下必然留大片黑边。给一条一次性提示，
   * 点掉后记进 localStorage 不再打扰；横屏时 CSS 自动不显示它。
   */
  function setupRotateHint() {
    if (page !== 'index.html') return;
    try {
      if (localStorage.getItem('escah_rotate_hint') === '0') return;
    } catch (e) { /* 无痕模式读不到，照常显示 */ }
    const b = document.createElement('button');
    b.id = 'mbRotate';
    b.type = 'button';
    b.textContent = '↻ 横屏观看画面更大，点此关闭';
    b.onclick = function () {
      b.remove();
      try { localStorage.setItem('escah_rotate_hint', '0'); } catch (e) { }
    };
    document.body.appendChild(b);
  }
  setupRotateHint();

  /* ------------------------------------------------ 必须在电脑上用的功能
   * 这些操作要么重到手机等不起、要么产物只落在电脑上、要么会直接断掉
   * 手机自己正在用的连接，所以在移动端点击时拦下来讲清楚原因。
   * 只是"不适合"，不是"不能用"：所以保留按钮本身，点开给说明，不隐藏。
   */
  const BLOCKED = [
    {
      sel: '#btnUpscale',
      name: '超分（放大高清化）',
      why: '超分要在电脑上跑很久（一部剧情通常 1–3 分钟）、很吃 CPU，' +
        '跑的时候电脑会变卡。手机上既看不到进度细节，也没法随时中断。',
    },
    {
      sel: '#btnRebuildX2',
      name: '重新登记高清图',
      why: '它要扫一遍电脑上的高清图目录重新登记，属于电脑端的维护操作，' +
        '手机上点了也不会让手机看到变化。',
    },
    {
      sel: '#btnVidQueue',
      name: '导出剧情视频',
      why: '导出视频要用电脑上的无头浏览器一句一句截帧、再编码成 mp4，' +
        '成品文件也只会落在电脑的导出目录里，手机拿不到。',
    },
    {
      sel: '#btnCgExport, #btnExportCg, #btnExport, #btnExportStand',
      name: '导出 / 打包（CG、立绘、原声集）',
      why: '打包好的压缩包会存在电脑的导出目录里，手机上下载不到。',
    },
    {
      sel: '#btnOpenExport, #btnOpenUserdata, #btnOpenMedia, #btnOpenLogs, #tpOpenExp',
      name: '打开本地目录',
      why: '这个按钮是在电脑上弹出资源管理器窗口，手机上点了你看不到任何变化。',
    },
    {
      sel: '#btnDelOrig, [data-clean]',
      name: '删除文件',
      why: '删除后不可恢复。请在电脑上核对清楚占用与内容再动手。',
    },
    {
      sel: '#btnPrune, #btnPruneGo',
      name: '扫描 / 清理磁盘',
      why: '清理会真的删掉电脑上的文件，建议在电脑上看着清单确认。',
    },
    {
      sel: '#btnShutdown',
      name: '停止本地服务',
      why: '服务跑在你连接的这台电脑上，手机上点停止会立刻断开，页面也就打不开了。',
    },
    {
      sel: '#btnCheckUpdate, #btnApplyUpdate',
      name: '程序更新',
      why: '更新会覆盖电脑上的程序文件并重启服务，过程中手机会断开。请在电脑上操作。',
    },
  ];

  function showBlock(rule) {
    if (document.getElementById('mbMask')) return;
    const m = document.createElement('div');
    m.id = 'mbMask';
    m.className = 'mbMask';
    m.innerHTML =
      '<div class="mbBox">' +
      '<h3>这个功能请在电脑上使用</h3>' +
      '<p><b>' + rule.name + '</b> 需要在电脑上操作。</p>' +
      '<p class="mbWhy">' + rule.why + '</p>' +
      '<p class="mbHint">你现在是用手机 / 平板访问电脑上的播放器。' +
      '浏览剧情、播放、切换语言都不受影响；这个功能请回到电脑的浏览器里操作。</p>' +
      '<button type="button" class="mbPrimary">知道了</button>' +
      '</div>';
    document.body.appendChild(m);
    m.querySelector('.mbPrimary').onclick = function () { m.remove(); };
    m.addEventListener('click', function (e) { if (e.target === m) m.remove(); });
  }

  /* 捕获阶段拦截：按钮自己的 onclick / addEventListener 都是目标阶段或冒泡阶段，
     在这里 stopImmediatePropagation 就能让它们一个都不执行。 */
  document.addEventListener('click', function (e) {
    const t = e.target;
    if (!t || typeof t.closest !== 'function') return;
    for (let i = 0; i < BLOCKED.length; i++) {
      const r = BLOCKED[i];
      if (t.closest(r.sel)) {
        e.preventDefault();
        e.stopImmediatePropagation();
        showBlock(r);
        return;
      }
    }
  }, true);

  /* ------------------------------------------------ 点浮层之外就收起（移动端交互）
   * 抽屉/面板展开后，手机上除了点「收起」按钮，点面板之外的任何地方也该收起来。
   * 桌面侧栏是**常驻**的，点画面把侧栏收掉会破坏桌面体验 —— 所以整段必须在
   * `if (!IS_MOBILE) return` 之后（本文件上面那行），只对手机/平板生效。
   *
   * 收起一律走各模块**现成的开合函数**（点它的按钮 / 改它的 class），
   * 这样按钮文案、localStorage、fitStage 等副作用都不会漏。这里只判断"这一下
   * 点击是不是发生在浮层之外"。CG / 立绘的角色抽屉在 setupCgDrawer() 里已有
   * 遮罩点外部收起，不在此重复处理。
   */
  (function bindOutsideDismiss() {
    // 这些是"打开浮层的开关"。点它们时即使某个浮层已展开，也不该顺手收掉——
    // 典型场景：点 ⚙ 打开设置时，正好把前面的剧情列表收没了，很突兀。
    const TRIGGERS =
      '#sTab, #hTab, #sToggle, #hToggle, #btnSet, #setClose, ' +
      '#btnTrans, #btnKeys, #btnLan, #btnShare, #mbSideBtn, ' +
      '#apToggle, #apHead, #btnList, #plClose';
    // 更高一层的模态弹窗开着时，点它的遮罩只该关它自己，不能穿透把底层浮层也收了。
    // 注意 CG / 立绘页的 #mbMask 不在这里：那两页的抽屉在自己那段逻辑里已经
    // 「点遮罩收起」，而这段规则表只覆盖剧情页 / 资源管理页（#mbMask 根本不存在）。
    const MODAL =
      '#tutMask, #dlMask, #dlgMask, #dlgPop, #shareMask, ' +
      '#tourTip, #tourWelcome, #apDetailMask:not([hidden])';

    // 每项：isOpen 判断展开 / panel 浮层自身选择器 / close 收起动作
    const rules = [
      {
        page: 'index.html',
        isOpen: () => document.body.classList.contains('sideOpen'),
        panel: '#sidebar',
        close: () => { const b = document.getElementById('sToggle'); if (b) b.click(); },
      },
      {
        page: 'index.html',
        isOpen: () => document.body.classList.contains('histOpen'),
        panel: '#history',
        close: () => { const b = document.getElementById('hToggle'); if (b) b.click(); },
      },
      {
        page: 'index.html',
        isOpen: () => { const s = document.getElementById('settings'); return !!s && !s.hidden; },
        panel: '#settings',
        close: () => { const s = document.getElementById('settings'); if (s) s.hidden = true; },
      },
      {
        page: 'index.html',
        isOpen: () => { const p = document.getElementById('transPanel'); return !!p && p.classList.contains('on'); },
        panel: '#transPanel',
        close: () => { const p = document.getElementById('transPanel'); if (p) p.classList.remove('on'); },
      },
      {
        page: 'setup.html',
        isOpen: () => { const a = document.getElementById('actPanel'); return !!a && !a.classList.contains('collapsed'); },
        panel: '#actPanel',
        close: () => { const b = document.getElementById('apToggle'); if (b) b.click(); },
      },
    ].filter(r => !r.page || r.page === page);
    if (!rules.length) return;

    // 冒泡阶段（BLOCKED 是捕获阶段 stopImmediatePropagation：被它拦下的按钮
    // 事件根本到不了这里，不会误收起浮层，两者不冲突）
    document.addEventListener('click', function (e) {
      const t = e.target;
      if (!t || typeof t.closest !== 'function') return;
      if (t.closest(TRIGGERS)) return;
      try { if (document.querySelector(MODAL)) return; } catch (err) { /* 忽略 */ }
      for (const r of rules) {
        if (!r.isOpen()) continue;
        if (t.closest(r.panel)) continue;         // 点在浮层内部，不算外部
        r.close();
      }
    });
  })();
})();
