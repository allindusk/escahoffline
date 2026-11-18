/* 全站共用的「角色图片」口径（2026-11-15 从三处重复代码抽出来）
 * ----------------------------------------------------------------
 * 之前 storylist.js / player.js / music.js 各写了一遍：
 *   · `media/img/story/<stem>.webp` 拼路径 + 查 mediaindex 防 404
 *   · 横幅画框 81px 高的尺寸常量（storylist.css / player.css 里写死）
 *   · 头像 `media/img/avatar/<id>.png`
 * 结果就是原声集页的浮窗把横幅拉成 150px 高又 `object-fit:cover` 裁掉一半，
 * 而剧情页是 81px 按高对齐不裁切 —— 同一个资源两种画法。
 *
 * 这里统一：路径函数 + 尺寸变量 `--mu-bnr-h`。CSS 里凡是要画横幅的地方
 * 一律 `var(--mu-bnr-h, 81px)`，改一处就全站跟着变。
 *
 * 依赖：data/mediaindex.js（window.__MEDIA）可选；没加载时退回"拼路径"，
 * 由 <img onerror> 兜底，不让整页报错。
 */
(function () {
  'use strict';

  /** 跟剧情播放页 / 资源管理页的角色横幅画框一致（81 高，图片按高对齐不裁）。 */
  var BANNER_H = 81;

  // 缓存成 Set：storylist 一屏 600+ 行，每行都问一次，indexOf 会白扫 2 万条
  var _sets = {};
  function has(kind, stem) {
    var M = window.__MEDIA;
    if (M && Array.isArray(M[kind])) {
      if (!_sets[kind]) _sets[kind] = new Set(M[kind].map(String));
      return _sets[kind].has(String(stem));
    }
    // 清单不可用（纯静态托管 / 旧包）→ 照旧拼路径，让 onerror 兜底
    return true;
  }

  /** 素材指纹：服务端开机时算好写进 mediaindex.js（`window.__MEDIA_VER`）。
      拼在图片地址后面，浏览器就能长期缓存；素材一增删改指纹就变，地址跟着变，
      自动取新图 —— 不需要谁手工去改版本常量。 */
  function ver() {
    var v = window.__MEDIA_VER;
    return v ? '?v=' + v : '';
  }

  /** 剧情横幅：`char_32` / `adv_main_1` → `media/img/story/<stem>.webp`。 */
  function banner(stem) {
    return stem && has('story', stem) ? 'media/img/story/' + stem + '.webp' + ver() : '';
  }
  /** 已有路径（advindex 里带 cover 的）也要先过一遍清单，避免必然 404。 */
  function bannerKeep(path) {
    var m = /^media\/img\/story\/([^./]+)\./.exec(path || '');
    if (!m) return path || '';
    return has('story', m[1]) ? path + ver() : '';
  }
  /** 角色头像：385 张 `media/img/avatar/<id>.png`。 */
  function avatar(id) {
    var stem = String(id);
    return has('avatar', stem) ? 'media/img/avatar/' + stem + '.png' + ver() : '';
  }
  /** 2026-11-16：NPC / 敌人头像已改成立绘（由 music.js 自己按编号映射取图），
      mediaui.sd() 不再需要
  function sd(id) {
    return 'media/img/sd/' + id + 'stand.webp';
  }
  */

  function escAttr(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /** 81px 画框的 HTML（图片按高对齐、左对齐，右侧留空，跟剧情页完全一致）。 */
  function bannerHTML(src, fallbackText) {
    var box = '<span class="mu-bnr">';
    if (src) {
      box += '<img alt="" data-lsrc="' + escAttr(src) + '">';
    } else {
      box += '<span class="mu-ph">' + escAttr(fallbackText || '—') + '</span>';
    }
    return box + '</span>';
  }

  /* ---------------------------------------------------------------- 懒加载
   * 为什么不用浏览器自带的 `loading="lazy"`：
   *   侧栏/抽屉是 `position:fixed` 的整屏容器，Chrome 判定里面的图"都在视野内"，
   *   385 张头像 + 横幅会在页面一打开就全部排队请求（实测单次 6.7 MB）。弱机/老硬盘上
   *   要等好几秒，这期间列表只有首字色块 —— 用户看到的就是"先显示字母，过好多秒才出图"。
   *   这里改成 `data-lsrc`，真正赋 src 推迟到**快进入视野**那一刻：列表随滚动一片片出图；
   *   抽屉在屏幕外（窄屏默认收起）时一张都不请求，展开才取。
   *   判定有两条路：滚动/尺寸变化时按矩形算（可靠的那层），
   *   IntersectionObserver 只用来兜"没滚动但元素自己出现了"（抽屉展开、面板显示）。
   *   任何地方新渲染出来的 `img[data-lsrc]` 由 MutationObserver 自动纳入，
   *   各页的列表构建代码不用逐个调用。
   */
  var _io = null;        // 观察器：元素"变得可见"时触发一次统一清扫（抽屉展开靠它）
  var _qImg = [];        // 待取图的 data-lsrc 图片
  var _qRun = [];        // 待执行的"整行进入视野再取图"回调
  var EXTRA = 300;       // 提前 300px 就开始取，滚到时不至于白一下再出图

  function _near(el) {
    if (!el || !el.isConnected) return false;
    var r = el.getBoundingClientRect();
    // 全 0 的矩形 = 还在 display:none 的容器里（窄屏侧栏默认收起就是这种）。
    // 这种一律不取：等抽屉被打开、矩形有了真实坐标，再按下面那条判断。
    if (!r.width && !r.height && !r.top && !r.left) return false;
    var vh = document.documentElement.clientHeight || 800;
    var vw = document.documentElement.clientWidth || 400;
    return r.top < vh + EXTRA && r.bottom > -EXTRA && r.left < vw + EXTRA && r.right > -EXTRA;
  }

  /* 核心清扫：把"已经快进入视野"的项一次性处理掉。
     为什么不只靠 IntersectionObserver：无头浏览器和部分 WebView 在程序化滚动后
     不一定补发观察回调（实测滚到 1400px 处一张都没加）。滚动事件 + 矩形判断是
     100% 可靠的那层网，观察器只当加速。 */
  function _sweep() {
    var i;
    for (i = _qRun.length - 1; i >= 0; i--) {
      var it = _qRun[i];
      if (!_near(it.el)) continue;
      _qRun.splice(i, 1);
      it.el.__lazyRun = null;
      if (_io) _io.unobserve(it.el);
      try { it.fn(); } catch (err) {
        if (window.console && console.warn) console.warn(err);
      }
    }
    for (i = _qImg.length - 1; i >= 0; i--) {
      var im = _qImg[i];
      if (!_near(im)) continue;
      _qImg.splice(i, 1);
      if (_io) _io.unobserve(im);
      var src = im.getAttribute('data-lsrc');
      if (src) {
        im.src = src;
        im.removeAttribute('data-lsrc');
      }
    }
  }

  /* 合并同一帧里的多次触发。
     用 setTimeout 不用 requestAnimationFrame：rAF 在"页面被判定不可见 / 无头浏览器
     虚拟时间"下可能整帧不发，一旦被卡住，后面所有滚动都再也不会清扫（实测滚不动）。
     定时器是被保证会执行的，代价只是最多晚一帧。 */
  var _timer = 0;
  var _poll = 0;
  var _idle = 0;

  /* 兜底轮询：只要队列里还有没取的图，就每 700ms 按矩形扫一次。
     为什么必须有这一层：滚动事件、IntersectionObserver、transitionend 在不同环境
     都可能不送达（实测：同样的代码在页内列表里滚动 3000px，一次回调都没有，
     而手动清扫能立刻取出 29 张）。队列取空后轮询自动停；
     连续一分钟什么都没取到也停，免得常年空转，用户真的动起来时再被唤醒。
     单次只做"待取项"的矩形判断，几百项也就零点几毫秒。 */
  function _pollTick() {
    if (!_qImg.length && !_qRun.length) { clearInterval(_poll); _poll = 0; return; }
    if (document.visibilityState === 'hidden') return;   // 后台标签页不做无用功
    var before = _qImg.length + _qRun.length;
    _sweep();
    if (_qImg.length + _qRun.length === before) {
      if (++_idle > 85) { clearInterval(_poll); _poll = 0; _idle = 0; }
    } else {
      _idle = 0;
    }
  }

  function _schedule() {
    _idle = 0;
    if (!_poll) _poll = setInterval(_pollTick, 700);
    if (_timer) return;
    _timer = setTimeout(function () { _timer = 0; _sweep(); }, 0);
  }

  function _track(el) {
    if (!_io) {
      try {
        _io = new IntersectionObserver(function () { _schedule(); },
          { rootMargin: EXTRA + 'px 0px' });
      } catch (e) { _io = null; }
    }
    if (_io) _io.observe(el);
  }

  /* 注册一张 data-lsrc 图片：真正赋 src 推迟到"快进入视野"那一刻 */
  function _watch(im) {
    if (!im || im.__lz || im.getAttribute('data-lsrc') == null) return;
    im.__lz = 1;
    _qImg.push(im);
    _track(im);
  }

  function _scan(node) {
    if (!node || node.nodeType !== 1) return;
    if (node.tagName === 'IMG') { _watch(node); return; }
    var ims = node.querySelectorAll ? node.querySelectorAll('img[data-lsrc]') : [];
    for (var i = 0; i < ims.length; i++) _watch(ims[i]);
  }

  /* 新渲染出来的图自动纳入：列表是 innerHTML 一次铺出来的，靠各页自己调用太容易漏 */
  var _mo = new MutationObserver(function (muts) {
    for (var m = 0; m < muts.length; m++) {
      var added = muts[m].addedNodes;
      for (var i = 0; i < added.length; i++) _scan(added[i]);
    }
    _schedule();
  });

  function lazyImgs(root) {
    var scope = root || document.body;
    if (!scope || !scope.querySelectorAll) return;
    var ims = scope.querySelectorAll('img[data-lsrc]');
    for (var i = 0; i < ims.length; i++) _watch(ims[i]);
    _schedule();
  }

  /* 行级"进入视野再干活"：有些行的图不是写死的 src，而是"按顺序试几张候选"
     （applyChain：本地超分 → 转码覆盖层 → 出厂映射），没法用 data-lsrc 表达。
     这类就整行进入视野再开始取第一张，避免几百行同时排队。 */
  function lazyRun(el, fn) {
    if (!el || typeof fn !== 'function') return;
    el.__lazyRun = fn;
    _qRun.push({ el: el, fn: fn });
    _track(el);
    _schedule();
  }

  // 滚动事件不冒泡，必须 capture 才收得到内层列表的滚动；抽屉展开时会触发过渡结束
  window.addEventListener('scroll', _schedule, { passive: true, capture: true });
  window.addEventListener('resize', _schedule, { passive: true });
  window.addEventListener('transitionend', _schedule, { passive: true, capture: true });

  /* 抽屉/侧栏是靠"点一下改 class"展开的，既不滚动也不一定有过渡：
     这类"元素自己挪进视野"的情况靠这三条补齐 —— 点完延迟再清扫一次（等开合动画走完），
     同时监听 class/style 变化。 */
  function _lateSchedule() {
    _schedule();
    setTimeout(_schedule, 260);
    setTimeout(_schedule, 700);
  }
  document.addEventListener('click', _lateSchedule, { passive: true, capture: true });
  document.addEventListener('keydown', _lateSchedule, { passive: true, capture: true });

  function _boot() {
    if (!document.body) { document.addEventListener('DOMContentLoaded', _boot); return; }
    _mo.observe(document.body, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ['class', 'style', 'hidden'],
    });
    lazyImgs();
  }
  _boot();

  // 共享尺寸变量 + 画框样式只在这里定义一次
  var st = document.createElement('style');
  st.textContent =
    ':root{--mu-bnr-h:' + BANNER_H + 'px}' +
    '.mu-bnr{position:relative;display:block;width:100%;height:var(--mu-bnr-h,81px);' +
    'border-radius:6px;overflow:hidden;border:1px solid #2d3446;' +
    'background:#0d1620;line-height:0}' +
    '.mu-bnr img{position:absolute;left:0;top:0;height:var(--mu-bnr-h,81px);' +
    'width:auto;max-width:none;display:block;border:0;border-radius:0}' +
    '.mu-bnr .mu-ph{display:flex;height:100%;align-items:center;' +
    'justify-content:center;font-size:14px;color:#6b7488;background:#141a26}';
  document.head.appendChild(st);

  /** 重新拉一次 data/mediaindex.js（新内容会整体覆盖 window.__MEDIA）。
   *
   *  为什么需要：各页只在**文档加载时**读一次清单，"这张头像 / 横幅有没有"
   *  全靠它。装完公共素材后服务端会重写 mediaindex.js（stages.regen_mediaindex），
   *  但页面内存里还是旧的那份 —— banner() / avatar() 查不到新图，列表就一直画
   *  色块占位，用户必须手动刷新整个页面才看得到（2026-11-17 用户反馈：
   *  「下载完公共素材要硬刷新页面，列表才显示头像和横幅」）。
   *
   *  地址用时间戳而不是 ?v=<指纹>：服务端只对"v 与当前文件指纹一致"的地址回
   *  immutable，其余一律 no-cache，所以时间戳地址一定能拿到最新内容，
   *  前端也不必先知道指纹。重复调用会复用同一次加载。
   */
  var _reloading = null;
  function reload() {
    if (_reloading) return _reloading;
    _reloading = new Promise(function (resolve) {
      var s = document.createElement('script');
      s.src = 'data/mediaindex.js?t=' + Date.now();
      s.onload = function () { _sets = {}; _reloading = null; resolve(true); };
      s.onerror = function () { _reloading = null; resolve(false); };
      document.head.appendChild(s);
    });
    return _reloading;
  }

  /** 页面重新可见时比对一次清单指纹，变了就重载并通知各页重画。
   *
   *  为什么需要：mediaindex.js 是按"内容指纹"immutable 缓存的静态文件，**页面
   *  不重新加载就永远用内存里那份旧的**。实测场景：装完公共素材 →（哪怕重启
   *  过程序）停在原来的标签页、或从后台切回来，立绘页 / 播放器列表还是没头像，
   *  必须硬刷新才出来（2026-11-17 用户反馈）。
   *
   *  服务端只 stat 一个文件，20 秒内重复触发会跳过，所以代价可以忽略。
   */
  var _chkAt = 0;
  function checkFresh() {
    if (document.hidden) return;
    var now = Date.now();
    if (now - _chkAt < 20000) return;
    _chkAt = now;
    fetch('api/mediaver', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.ver || d.ver === window.__MEDIA_VER) return;
        reload().then(function (ok) {
          if (!ok) return;
          try {
            window.dispatchEvent(new CustomEvent('media:reloaded'));
          } catch (e) { /* 极老浏览器：没有 CustomEvent 就不通知 */ }
        });
      })
      .catch(function () { /* 服务没起来 / 纯静态托管：静默跳过 */ });
  }
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) checkFresh();
  });
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) checkFresh();          // 从"后退缓存"恢复的页面
  });

  window.MediaUI = {
    BANNER_H: BANNER_H,
    has: has, banner: banner, bannerKeep: bannerKeep,
    avatar: avatar, bannerHTML: bannerHTML,
    lazyImgs: lazyImgs, lazyRun: lazyRun,
    reload: reload, checkFresh: checkFresh,
  };
  // 和后续 DOM 变化都接上，各页不需要自己调用。
  if (document.body) lazyImgs();
  else document.addEventListener('DOMContentLoaded', function () { lazyImgs(); });
})();
