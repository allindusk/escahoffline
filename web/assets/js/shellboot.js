/* 自动进入「全站外壳」shell.html
 * --------------------------------
 * 播放组件必须常驻在一个不会被卸载的 document 里，换页面才能不中断。
 * 本站是多页应用，直接访问 setup.html / index.html 时没有那个常驻层，
 * 所以这里在最早期把顶层导航换成 shell.html，并把原本要去的页面用 ?p= 带过去。
 *
 * 已经在 iframe 里（= 外壳内部）→ 什么都不做。
 * ?noshell=1 → 本次会话退回原来的多页模式（排查问题用），并记住这个选择。
 */
(function () {
  'use strict';
  try {
    if (window.parent !== window) return;
    if (/shell\.html$/.test(location.pathname)) return;
    var q = new URLSearchParams(location.search);
    if (q.get('noshell') === '1') {
      sessionStorage.setItem('esc_no_shell', '1');
      return;
    }
    if (sessionStorage.getItem('esc_no_shell') === '1') return;
    var rel = location.pathname.replace(/^\//, '') + location.search + location.hash;
    location.replace('shell.html?p=' + encodeURIComponent(rel));
  } catch (e) { /* 出错就按原来的多页模式继续，不阻断页面 */ }
})();
