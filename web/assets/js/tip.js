/* 全站悬停说明气泡（14px）
 * ---------------------------------------------------------------------------
 * 原生 title 的气泡字号由浏览器决定（Edge/Chrome 约 12px），低于全站 14px
 * 字号下限 —— 用户反复反馈"悬浮说明字太小"。这里在 document 上做一次事件
 * 委托，把所有 `title` / `data-tip` 都改成自绘气泡：
 *
 *   · 鼠标移入 → 把 title 暂存、移除（不移除的话原生小气泡会和新气泡一起弹）；
 *     移开还原。data-tip 优先级更高，且不碰 title（i18n 的 applyDom 只认 title）。
 *   · 延迟 220ms 才显示：扫过列表时不会一路刷屏。
 *   · `?` 角标的可见标记在 tip.css（.qhint），说明文字直接写在它的 data-tip 上。
 *
 * 与播放器的关系：index.html 的 #settings 面板由 player.js 的 initSettingTips
 * 自己处理（它有更复杂的在面板内滚动/定位逻辑），这里跳过 #settings 内的元素，
 * 避免同一个控件弹出两个气泡。
 */
(function () {
  'use strict';
  if (window.__ESC_TIP) return;
  window.__ESC_TIP = 1;

  const DELAY = 220;
  let box = null;
  let timer = 0;
  let cur = null;

  function ensureBox() {
    if (box && box.isConnected) return box;
    // 播放器（playwright 页）自己也会建一个 #tipBox 给设置面板用：
    // 复用同一个元素，别造出两个同 id 的浮层（样式/查询都会打架）
    let el = document.getElementById('tipBox');
    if (!el) {
      el = document.createElement('div');
      el.id = 'tipBox';
      document.body.appendChild(el);
    }
    box = el;
    return box;
  }

  function hide() {
    clearTimeout(timer);
    timer = 0;
    if (box) box.classList.remove('on');
    // 还原被临时摘掉的 title（只还原我们自己摘的）。
    // 期间若已有别的代码写了新 title（播放器 #pinfo 每段都会刷新），以新的为准，
    // 别用旧文案覆盖回去 —— 那是"悬停过一次，提示就永久变旧"的来源。
    if (cur && cur.dataset.tipStash != null) {
      if (!cur.getAttribute('title')) cur.setAttribute('title', cur.dataset.tipStash);
      delete cur.dataset.tipStash;
    }
    cur = null;
  }

  function show(el) {
    const own = el.getAttribute('data-tip') || '';
    const ttl = el.getAttribute('title') || '';
    const txt = own || ttl;
    if (!txt) return;
    if (!own && ttl) {
      el.dataset.tipStash = ttl;      // 交给 hide() 还原
      el.removeAttribute('title');    // 关掉原生小气泡
    }
    cur = el;
    const b = ensureBox();
    b.textContent = txt;
    b.classList.add('on');
    // 先归零量尺寸，再按控件矩形摆位（fixed 与 getBoundingClientRect 同一坐标系）
    b.style.left = '0px';
    b.style.top = '0px';
    const r = el.getBoundingClientRect();
    const bw = b.offsetWidth;
    const bh = b.offsetHeight;
    let x = Math.min(Math.max(8, r.left), window.innerWidth - bw - 8);
    let y = r.top - bh - 8;
    if (y < 8) y = r.bottom + 8;      // 上面放不下就翻到下面
    b.style.left = Math.round(x) + 'px';
    b.style.top = Math.round(y) + 'px';
  }

  function pick(target) {
    if (!target || !target.closest) return null;
    const el = target.closest('[title],[data-tip]');
    if (!el) return null;
    // 播放器设置面板有自己的气泡实现，别抢
    if (el.closest('#settings')) return null;
    // 已经自带信息浮窗的区域（播放器侧栏 #bnrTip、播放列表 #hov）也不再叠一层：
    // 那些行/图标本身就有 14px 详情卡，再弹一个就是同一份信息显示两遍。
    if (el.closest('[data-tip-off]')) return null;
    return el;
  }

  document.addEventListener('mouseover', function (e) {
    const el = pick(e.target);
    if (!el || el === cur) return;
    hide();
    clearTimeout(timer);
    timer = setTimeout(function () { show(el); }, DELAY);
  });

  document.addEventListener('mouseout', function (e) {
    if (!cur) {
      clearTimeout(timer);
      timer = 0;
      return;
    }
    // 在同一控件内部换焦（span → 里面子元素）不算离开
    if (e.relatedTarget && cur.contains(e.relatedTarget)) return;
    hide();
  });

  // 键盘用户也要能看到（.qhint 有 tabindex 时）
  document.addEventListener('focusin', function (e) {
    const el = pick(e.target);
    if (el) { hide(); show(el); }
  });
  document.addEventListener('focusout', hide);

  document.addEventListener('scroll', hide, true);   // 捕获阶段：任何容器滚动都算
  // 点击别处收起；点「?」角标本身 = 切换显示（触屏没有 hover，只能点）。
  // 捕获阶段就要拦，否则同一次点击会先 show（focusin）再被这里 hide 掉。
  document.addEventListener('click', function (e) {
    const el = e.target && e.target.closest ? e.target.closest('.qhint') : null;
    if (el) {
      if (cur === el) hide();
      else { hide(); show(el); }
      return;
    }
    hide();
  }, true);
  window.addEventListener('blur', hide);
})();
