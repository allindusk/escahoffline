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
  document.body.classList.add(
    page === 'setup.html' ? 'mb-setup'
      : page === 'cg.html' ? 'mb-cg' : 'mb-player');

  /* ---------------------------------------------------------------- CG 抽屉
   * 播放器页有 #sTab 把手，CG 页当初没做收起，窄屏下 252px 的侧栏会一直压着
   * 卡片墙。这里补一个浮动按钮；选完角色 / 点空白处自动收起。
   */
  function setupCgDrawer() {
    if (page !== 'cg.html') return;
    document.body.classList.add('mb-cg');
    const side = document.getElementById('sidebar');
    if (!side || document.getElementById('mbSideBtn')) return;

    const btn = document.createElement('button');
    btn.id = 'mbSideBtn';
    btn.type = 'button';
    btn.textContent = '☰ 角色列表';
    btn.onclick = function (e) {
      e.stopPropagation();                    // 别触发下面的"点外部收起"
      side.classList.toggle('mbOpen');
    };
    document.body.appendChild(btn);

    // 选完角色就收起，别一直盖着卡片墙
    side.addEventListener('click', function (e) {
      if (e.target.closest('.char-btn')) side.classList.remove('mbOpen');
    });
    // 点抽屉外也收起
    document.addEventListener('click', function (e) {
      if (!side.classList.contains('mbOpen')) return;
      if (side.contains(e.target) || e.target === btn) return;
      side.classList.remove('mbOpen');
    });
  }

  setupCgDrawer();

  if (!IS_MOBILE) return;                     // 桌面到此为止

  /* 手机上没有 Esc 键，CG 灯箱的「关闭 Esc」会让人以为是两段操作；
     换成「✕ 关闭」，顺便省 20px 宽度（顶栏要单行才不亏画面高度）。 */
  if (page === 'cg.html') {
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
      sel: '#btnCgExport, #btnExportCg',
      name: '导出 / 打包 CG',
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
})();
