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

  /** 剧情横幅：`char_32` / `adv_main_1` → `media/img/story/<stem>.webp`。 */
  function banner(stem) {
    return stem && has('story', stem) ? 'media/img/story/' + stem + '.webp' : '';
  }
  /** 已有路径（advindex 里带 cover 的）也要先过一遍清单，避免必然 404。 */
  function bannerKeep(path) {
    var m = /^media\/img\/story\/([^./]+)\./.exec(path || '');
    if (!m) return path || '';
    return has('story', m[1]) ? path : '';
  }
  /** 角色头像：385 张 `media/img/avatar/<id>.png`。 */
  function avatar(id) {
    var stem = String(id);
    return has('avatar', stem) ? 'media/img/avatar/' + stem + '.png' : '';
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
      box += '<img alt="" loading="lazy" src="' + escAttr(src) + '">';
    } else {
      box += '<span class="mu-ph">' + escAttr(fallbackText || '—') + '</span>';
    }
    return box + '</span>';
  }

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

  window.MediaUI = {
    BANNER_H: BANNER_H,
    has: has, banner: banner, bannerKeep: bannerKeep,
    avatar: avatar, bannerHTML: bannerHTML,
  };
})();
